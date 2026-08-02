"""Explicit conversions between pure domain values and SQLAlchemy projections."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.application.configuration import (
    ConfigurationLayer,
    restore_resolved_configuration_snapshot,
)
from app.domain.entities import (
    Instrument,
    LogicalScanRun,
    Position,
    RotationPlan,
    ScanAttempt,
    StrategyRun,
)
from app.domain.enums import (
    LogicalScanStatus,
    PositionRole,
    PositionStatus,
    ScanAttemptStatus,
    Scope,
)
from app.domain.values import ConfigurationSnapshotRef, InstrumentRef, Ownership, Quantity
from app.persistence.configuration_payload import (
    decode_configuration_payload,
    encode_configuration_payload,
)
from app.persistence.evidence import decode_evidence, encode_evidence
from app.persistence.models import (
    ConfigurationLayerModel,
    ConfigurationSnapshotModel,
    InstrumentModel,
    LogicalScanRunModel,
    PositionModel,
    RotationPlanModel,
    ScanAttemptModel,
    StrategyRunModel,
)
from app.persistence.records import PersistedConfigurationSnapshot
from app.schemas.common import ConfigurationLayerScope, ParentVersion
from app.schemas.configuration import LayerPatchSchema


def _persist_datetime(value: datetime) -> datetime:
    """Normalize aggregate timestamps before storage independent of database session zone."""

    return value.astimezone(UTC)


def _restore_datetime(value: datetime) -> datetime:
    """Restore UTC awareness for dialects such as SQLite that discard tzinfo."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def instrument_to_model(instrument: Instrument) -> InstrumentModel:
    """Project a global, market-qualified domain instrument into storage."""

    return InstrumentModel(
        instrument_id=instrument.instrument_id,
        market=instrument.market,
        symbol=instrument.symbol,
        created_at=_persist_datetime(instrument.created_at),
    )


def instrument_from_model(model: InstrumentModel) -> Instrument:
    """Restore a validated global instrument from its persistence projection."""

    return Instrument(
        instrument_id=model.instrument_id,
        market=model.market,
        symbol=model.symbol,
        created_at=_restore_datetime(model.created_at),
    )


def position_to_model(position: Position) -> PositionModel:
    """Project a validated domain position into its persistence representation."""

    return PositionModel(
        position_id=position.position_id,
        portfolio_id=position.portfolio_id,
        instrument_id=position.instrument.instrument_id,
        quantity=position.quantity.value,
        role=position.role.value,
        status=position.status.value,
        opened_at=_persist_datetime(position.opened_at),
        closed_at=_persist_datetime(position.closed_at) if position.closed_at is not None else None,
    )


def position_from_model(model: PositionModel) -> Position:
    """Reconstruct a domain position, treating corrupted enum data as invalid storage."""

    return Position(
        position_id=model.position_id,
        portfolio_id=model.portfolio_id,
        instrument=InstrumentRef(model.instrument_id),
        quantity=Quantity(model.quantity),
        role=PositionRole(model.role),
        status=PositionStatus(model.status),
        opened_at=_restore_datetime(model.opened_at),
        closed_at=_restore_datetime(model.closed_at) if model.closed_at is not None else None,
    )


def rotation_plan_to_model(plan: RotationPlan) -> RotationPlanModel:
    """Project the plan root; its explicit references use association tables."""

    return RotationPlanModel(
        rotation_plan_id=plan.rotation_plan_id,
        portfolio_id=plan.portfolio_id,
        created_at=_persist_datetime(plan.created_at),
    )


def rotation_plan_from_model(
    model: RotationPlanModel,
    *,
    candidate_group_ids: tuple[object, ...],
    source_position_ids: tuple[object, ...],
    protected_position_ids: tuple[object, ...],
) -> RotationPlan:
    """Restore a plan only from its explicitly persisted association identifiers."""

    def identifiers(values: tuple[object, ...], field_name: str) -> tuple[UUID, ...]:
        identifiers: list[UUID] = []
        for value in values:
            if not isinstance(value, UUID):
                raise ValueError(f"stored {field_name} contains an invalid identifier")
            identifiers.append(value)
        return tuple(identifiers)

    return RotationPlan(
        rotation_plan_id=model.rotation_plan_id,
        portfolio_id=model.portfolio_id,
        candidate_group_ids=identifiers(candidate_group_ids, "candidate group association"),
        source_position_ids=identifiers(source_position_ids, "source position association"),
        protected_position_ids=identifiers(
            protected_position_ids, "protected position association"
        ),
        created_at=_restore_datetime(model.created_at),
    )


def configuration_layer_to_model(layer: ConfigurationLayer) -> ConfigurationLayerModel:
    """Project an already hash-verified sparse layer for persistence."""

    patch_payload = layer.patch.model_dump(mode="python", by_alias=True, exclude_unset=True)
    patch = encode_configuration_payload(patch_payload)
    if not isinstance(patch, dict):
        raise ValueError("encoded configuration layer patch must be a JSON object")
    return ConfigurationLayerModel(
        layer_id=uuid4(),
        scope=layer.scope.value,
        reference_id=layer.reference_id,
        version=layer.version,
        patch=patch,
        content_hash=layer.content_hash,
        content_hash_format_version=layer.content_hash_format_version,
        ownership_scope=layer.ownership.scope.value,
        owner_id=layer.ownership.owner_id,
        portfolio_owner_id=layer.portfolio_owner_id,
        runtime_expires_at=(
            _persist_datetime(layer.runtime_expires_at)
            if layer.runtime_expires_at is not None
            else None
        ),
    )


def configuration_layer_from_model(model: ConfigurationLayerModel) -> ConfigurationLayer:
    """Reconstruct a layer and repeat the canonical-hash validation at the boundary."""

    scope = ConfigurationLayerScope(model.scope)
    decoded_patch = decode_configuration_payload(model.patch)
    if not isinstance(decoded_patch, Mapping):
        raise ValueError("stored configuration layer patch must decode to a mapping")
    patch = LayerPatchSchema.for_scope(scope, decoded_patch)
    return ConfigurationLayer(
        scope=scope,
        patch=patch,
        reference_id=model.reference_id,
        version=model.version,
        content_hash=model.content_hash,
        ownership=Ownership(Scope(model.ownership_scope), model.owner_id),
        content_hash_format_version=model.content_hash_format_version,
        portfolio_owner_id=model.portfolio_owner_id,
        runtime_expires_at=(
            _restore_datetime(model.runtime_expires_at)
            if model.runtime_expires_at is not None
            else None
        ),
    )


def configuration_snapshot_to_model(
    snapshot: PersistedConfigurationSnapshot,
) -> ConfigurationSnapshotModel:
    """Validate then persist all merged configuration evidence needed to reproduce a run."""

    declared_snapshot = snapshot.resolved_snapshot
    resolved_snapshot = restore_resolved_configuration_snapshot(
        payload=declared_snapshot.payload,
        parent_versions=declared_snapshot.parent_versions,
        created_by=declared_snapshot.created_by,
        created_at=declared_snapshot.created_at,
        runtime_expires_at=declared_snapshot.runtime_expires_at,
        canonical_format_version=declared_snapshot.canonical_format_version,
        content_hash=declared_snapshot.content_hash,
        canonical_payload_json=declared_snapshot.canonical_json,
    )
    parent_versions = [
        {
            "scope": parent.scope.value,
            "reference_id": str(parent.reference_id),
            "version": parent.version,
            "content_hash": parent.content_hash,
            "content_hash_format_version": parent.content_hash_format_version,
        }
        for parent in resolved_snapshot.parent_versions
    ]
    return ConfigurationSnapshotModel(
        snapshot_id=snapshot.snapshot_id,
        content_hash=resolved_snapshot.content_hash,
        canonical_format_version=resolved_snapshot.canonical_format_version,
        parent_versions=parent_versions,
        merged_payload=_encoded_configuration_mapping(resolved_snapshot.payload),
        canonical_json=resolved_snapshot.canonical_json,
        config_version=snapshot.config_version,
        target_scope=snapshot.target_ownership.scope.value,
        target_owner_id=snapshot.target_ownership.owner_id,
        target_reference_id=snapshot.target_reference_id,
        runtime_expires_at=(
            _persist_datetime(resolved_snapshot.runtime_expires_at)
            if resolved_snapshot.runtime_expires_at is not None
            else None
        ),
        created_at=_persist_datetime(resolved_snapshot.created_at),
        created_by=resolved_snapshot.created_by,
    )


def configuration_snapshot_from_model(
    model: ConfigurationSnapshotModel,
) -> PersistedConfigurationSnapshot:
    """Restore immutable snapshot evidence and reject malformed parent records."""

    payload = decode_configuration_payload(model.merged_payload)
    if not isinstance(payload, Mapping):
        raise ValueError("stored configuration snapshot payload must decode to a mapping")

    parents: list[ParentVersion] = []
    for raw_parent in model.parent_versions:
        try:
            raw_scope = raw_parent["scope"]
            raw_reference_id = raw_parent["reference_id"]
            raw_version = raw_parent["version"]
            raw_content_hash = raw_parent["content_hash"]
            raw_hash_format = raw_parent["content_hash_format_version"]
            if (
                not isinstance(raw_scope, str)
                or not isinstance(raw_reference_id, str)
                or isinstance(raw_version, bool)
                or not isinstance(raw_version, int)
                or not isinstance(raw_content_hash, str)
                or not isinstance(raw_hash_format, str)
            ):
                raise ValueError("stored configuration snapshot parent version has invalid types")
            parents.append(
                ParentVersion(
                    scope=ConfigurationLayerScope(raw_scope),
                    reference_id=UUID(raw_reference_id),
                    version=raw_version,
                    content_hash=raw_content_hash,
                    content_hash_format_version=raw_hash_format,
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("stored configuration snapshot parent version is malformed") from error
    resolved_snapshot = restore_resolved_configuration_snapshot(
        payload=payload,
        parent_versions=tuple(parents),
        created_by=model.created_by,
        created_at=_restore_datetime(model.created_at),
        runtime_expires_at=(
            _restore_datetime(model.runtime_expires_at)
            if model.runtime_expires_at is not None
            else None
        ),
        canonical_format_version=model.canonical_format_version,
        content_hash=model.content_hash,
        canonical_payload_json=model.canonical_json,
    )
    try:
        target_ownership = Ownership(Scope(model.target_scope), model.target_owner_id)
    except ValueError as error:
        raise ValueError("stored configuration snapshot target ownership is malformed") from error
    return PersistedConfigurationSnapshot(
        snapshot_id=model.snapshot_id,
        resolved_snapshot=resolved_snapshot,
        config_version=model.config_version,
        target_ownership=target_ownership,
        target_reference_id=model.target_reference_id,
    )


def _encoded_configuration_mapping(payload: Mapping[str, object]) -> dict[str, object]:
    """Encode a configuration mapping and guard the JSON-column boundary shape."""

    encoded = encode_configuration_payload(payload)
    if not isinstance(encoded, dict):
        raise ValueError("encoded configuration snapshot payload must be a JSON object")
    return encoded


def logical_scan_to_model(scan: LogicalScanRun) -> LogicalScanRunModel:
    """Project a validated logical scan without duplicating final-run ownership."""

    return LogicalScanRunModel(
        logical_scan_run_id=scan.logical_scan_run_id,
        rotation_plan_id=scan.rotation_plan_id,
        market_session_date=scan.market_session_date,
        scan_window_start=_persist_datetime(scan.scan_window_start),
        scan_interval=scan.scan_interval,
        market_timezone=scan.market_timezone,
        configuration_snapshot_hash=scan.configuration_snapshot_hash,
        scan_identity_format_version=scan.scan_identity_format_version,
        scan_lock_key=scan.scan_lock_key,
        status=scan.status.value,
        completed_at=(
            _persist_datetime(scan.completed_at) if scan.completed_at is not None else None
        ),
        created_at=_persist_datetime(scan.created_at),
    )


def logical_scan_from_model(
    model: LogicalScanRunModel,
    *,
    portfolio_id: UUID,
    final_strategy_run_id: UUID | None,
) -> LogicalScanRun:
    """Restore scan state with the final result derived from its one-to-one relation."""

    return LogicalScanRun(
        logical_scan_run_id=model.logical_scan_run_id,
        rotation_plan_id=model.rotation_plan_id,
        portfolio_id=portfolio_id,
        market_session_date=model.market_session_date,
        scan_window_start=_restore_datetime(model.scan_window_start),
        scan_interval=model.scan_interval,
        market_timezone=model.market_timezone,
        configuration_snapshot_hash=model.configuration_snapshot_hash,
        scan_identity_format_version=model.scan_identity_format_version,
        scan_lock_key=model.scan_lock_key,
        status=LogicalScanStatus(model.status),
        created_at=_restore_datetime(model.created_at),
        completed_at=(
            _restore_datetime(model.completed_at) if model.completed_at is not None else None
        ),
        final_strategy_run_id=final_strategy_run_id,
    )


def scan_attempt_to_model(attempt: ScanAttempt) -> ScanAttemptModel:
    """Project immutable provider-attempt evidence into its SQL representation."""

    return ScanAttemptModel(
        scan_attempt_id=attempt.scan_attempt_id,
        logical_scan_run_id=attempt.logical_scan_run_id,
        attempt_number=attempt.attempt_number,
        status=attempt.status.value,
        configuration_snapshot_hash=attempt.configuration_snapshot_hash,
        market_data_snapshot_id=attempt.market_data_snapshot_id,
        market_data_content_hash=attempt.market_data_content_hash,
        trigger_correlation_id=attempt.trigger_correlation_id,
        actor_correlation_id=attempt.actor_correlation_id,
        started_at=_persist_datetime(attempt.started_at),
        completed_at=(
            _persist_datetime(attempt.completed_at) if attempt.completed_at is not None else None
        ),
        failure_code=attempt.failure_code,
        failure_detail=attempt.failure_detail,
        recovery_of_attempt_id=attempt.recovery_of_attempt_id,
        duplicate_of_attempt_id=attempt.duplicate_of_attempt_id,
        final_strategy_run_id=attempt.final_strategy_run_id,
    )


def scan_attempt_from_model(model: ScanAttemptModel) -> ScanAttempt:
    """Restore one provider attempt and normalize SQLite's naive datetime result."""

    return ScanAttempt(
        scan_attempt_id=model.scan_attempt_id,
        logical_scan_run_id=model.logical_scan_run_id,
        attempt_number=model.attempt_number,
        status=ScanAttemptStatus(model.status),
        configuration_snapshot_hash=model.configuration_snapshot_hash,
        market_data_snapshot_id=model.market_data_snapshot_id,
        market_data_content_hash=model.market_data_content_hash,
        trigger_correlation_id=model.trigger_correlation_id,
        actor_correlation_id=model.actor_correlation_id,
        started_at=_restore_datetime(model.started_at),
        completed_at=(
            _restore_datetime(model.completed_at) if model.completed_at is not None else None
        ),
        failure_code=model.failure_code,
        failure_detail=model.failure_detail,
        recovery_of_attempt_id=model.recovery_of_attempt_id,
        duplicate_of_attempt_id=model.duplicate_of_attempt_id,
        final_strategy_run_id=model.final_strategy_run_id,
    )


def strategy_run_to_model(
    run: StrategyRun, *, idempotency_key: str, logical_scan_run_id: UUID | None = None
) -> StrategyRunModel:
    """Project immutable run evidence through the reversible JSON codec."""

    if not idempotency_key.strip():
        raise ValueError("idempotency_key must not be blank")
    encoded_outputs = encode_evidence(run.outputs)
    if not isinstance(encoded_outputs, dict):
        raise ValueError("encoded strategy run outputs must be a JSON object")
    return StrategyRunModel(
        strategy_run_id=run.strategy_run_id,
        rotation_plan_id=run.rotation_plan_id,
        portfolio_id=run.portfolio_id,
        logical_scan_run_id=logical_scan_run_id,
        configuration_snapshot_id=run.configuration_snapshot.snapshot_id,
        configuration_content_hash=run.configuration_snapshot.content_hash,
        configuration_snapshot_created_at=_persist_datetime(run.configuration_snapshot.created_at),
        market_data_snapshot_id=run.market_data_snapshot_id,
        state_transition=run.state_transition,
        outputs=encoded_outputs,
        occurred_at=_persist_datetime(run.occurred_at),
        idempotency_key=idempotency_key,
    )


def strategy_run_from_model(model: StrategyRunModel) -> StrategyRun:
    """Restore a run only when persisted evidence retains its declared shape."""

    decoded_outputs = decode_evidence(model.outputs)
    if not isinstance(decoded_outputs, Mapping):
        raise ValueError("stored strategy run outputs must decode to a mapping")
    return StrategyRun(
        strategy_run_id=model.strategy_run_id,
        rotation_plan_id=model.rotation_plan_id,
        portfolio_id=model.portfolio_id,
        configuration_snapshot=ConfigurationSnapshotRef(
            snapshot_id=model.configuration_snapshot_id,
            content_hash=model.configuration_content_hash,
            created_at=_restore_datetime(model.configuration_snapshot_created_at),
        ),
        market_data_snapshot_id=model.market_data_snapshot_id,
        state_transition=model.state_transition,
        outputs=decoded_outputs,
        occurred_at=_restore_datetime(model.occurred_at),
    )
