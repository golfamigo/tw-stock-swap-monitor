"""Deterministic in-memory adapters that implement Task 4 repository contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from threading import Lock
from uuid import UUID, uuid4

from app.application.child_intents import ChildIntent, same_logical_child_intent
from app.application.configuration import (
    ConfigurationLayer,
    restore_resolved_configuration_snapshot,
)
from app.application.configuration_snapshots import PersistedConfigurationSnapshot
from app.domain.access import AccessContext
from app.domain.entities import (
    Instrument,
    LogicalScanRun,
    Position,
    RotationPlan,
    ScanAttempt,
    StrategyRun,
)
from app.domain.enums import LogicalScanStatus, Scope
from app.domain.errors import (
    IdempotencyConflictError,
    LogicalScanAlreadyCompletedError,
    NotFoundForActor,
    PlanReferenceUnauthorizedError,
    PositionAlreadyOpenError,
)
from app.domain.values import IdempotencyKey, Ownership
from app.repositories.configuration_layers import (
    ConfigurationLayerSelection,
    ConfigurationLayerSelectionEntry,
)
from app.repositories.locks import LockLease, ScanLockRequest
from app.repositories.logical_scans import LogicalScanRepository, validate_scan_attempt_append
from app.repositories.strategy_runs import require_exact_strategy_run_retry
from app.schemas.common import ConfigurationLayerScope
from app.state_machine.states import RecommendationState, RecommendationStateRecord


class _PortfolioScopedAdapter:
    """Centralize portfolio owner lookup and access checks for memory adapters."""

    def __init__(self, portfolio_owners: Mapping[UUID, UUID]) -> None:
        self._portfolio_owners = dict(portfolio_owners)

    def _portfolio_owner(self, portfolio_id: UUID) -> UUID:
        try:
            return self._portfolio_owners[portfolio_id]
        except KeyError as error:
            raise NotFoundForActor("portfolio was not found for actor") from error

    def _require_portfolio_read(self, portfolio_id: UUID, access_context: AccessContext) -> UUID:
        owner_id = self._portfolio_owner(portfolio_id)
        access_context.require_read(
            Ownership(Scope.PORTFOLIO, portfolio_id), portfolio_owner_id=owner_id
        )
        return owner_id

    def _require_portfolio_mutation(
        self, portfolio_id: UUID, access_context: AccessContext
    ) -> UUID:
        owner_id = self._portfolio_owner(portfolio_id)
        access_context.require_mutation(
            Ownership(Scope.PORTFOLIO, portfolio_id), portfolio_owner_id=owner_id
        )
        return owner_id


class InMemoryInstrumentRepository:
    """System instrument adapter with explicit read/mutation authorization."""

    def __init__(self) -> None:
        self._instruments: dict[UUID, Instrument] = {}

    def add(self, instrument: Instrument, *, access_context: AccessContext) -> None:
        access_context.require_mutation(instrument.ownership)
        self._instruments[instrument.instrument_id] = instrument

    def get(self, instrument_id: UUID, *, access_context: AccessContext) -> Instrument:
        try:
            instrument = self._instruments[instrument_id]
        except KeyError as error:
            raise NotFoundForActor("instrument was not found for actor") from error
        access_context.require_read(instrument.ownership)
        return instrument


class InMemoryPositionRepository(_PortfolioScopedAdapter):
    """Position history adapter retaining CLOSED rows and denying duplicate OPEN rows."""

    def __init__(self, portfolio_owners: Mapping[UUID, UUID]) -> None:
        super().__init__(portfolio_owners)
        self._positions: dict[UUID, Position] = {}

    def add(self, position: Position, *, access_context: AccessContext) -> None:
        self._require_portfolio_mutation(position.portfolio_id, access_context)
        if position.status.value == "OPEN":
            for stored in self._positions.values():
                if (
                    stored.status.value == "OPEN"
                    and stored.portfolio_id == position.portfolio_id
                    and stored.instrument == position.instrument
                    and stored.position_id != position.position_id
                ):
                    raise PositionAlreadyOpenError(
                        "only one OPEN position is allowed for a portfolio/instrument pair"
                    )
        self._positions[position.position_id] = position

    def get(self, position_id: UUID, *, access_context: AccessContext) -> Position:
        try:
            position = self._positions[position_id]
        except KeyError as error:
            raise NotFoundForActor("position was not found for actor") from error
        self._require_portfolio_read(position.portfolio_id, access_context)
        return position

    def list_for_portfolio(
        self, portfolio_id: UUID, *, access_context: AccessContext
    ) -> Sequence[Position]:
        self._require_portfolio_read(portfolio_id, access_context)
        return tuple(item for item in self._positions.values() if item.portfolio_id == portfolio_id)


class InMemoryRecommendationStateRepository(_PortfolioScopedAdapter):
    """Atomic in-memory durable-state adapter for one recommendation lifecycle per plan."""

    def __init__(self, portfolio_owners: Mapping[UUID, UUID]) -> None:
        super().__init__(portfolio_owners)
        self._records: dict[UUID, RecommendationStateRecord] = {}
        self._state_lock = Lock()

    def get_or_create(
        self,
        *,
        plan: RotationPlan,
        created_at: datetime,
        access_context: AccessContext,
    ) -> RecommendationStateRecord:
        self._require_portfolio_mutation(plan.portfolio_id, access_context)
        with self._state_lock:
            existing = self._records.get(plan.rotation_plan_id)
            if existing is not None:
                if existing.portfolio_id != plan.portfolio_id:
                    raise ValueError("recommendation state plan portfolio does not match")
                return existing
            record = RecommendationStateRecord(
                rotation_plan_id=plan.rotation_plan_id,
                portfolio_id=plan.portfolio_id,
                state=RecommendationState.IDLE,
                remaining_stages_halted=False,
                revision=0,
                updated_at=created_at,
            )
            self._records[plan.rotation_plan_id] = record
            return record

    def get(
        self, rotation_plan_id: UUID, *, access_context: AccessContext
    ) -> RecommendationStateRecord:
        with self._state_lock:
            try:
                record = self._records[rotation_plan_id]
            except KeyError as error:
                raise NotFoundForActor("recommendation state was not found for actor") from error
        self._require_portfolio_read(record.portfolio_id, access_context)
        return record

    def record_transition(
        self,
        *,
        plan: RotationPlan,
        previous: RecommendationStateRecord,
        next_state: RecommendationState,
        remaining_stages_halted: bool,
        changed_at: datetime,
        access_context: AccessContext,
        finalization_strategy_run_id: UUID | None = None,
        finalization_strategy_key: str | None = None,
    ) -> RecommendationStateRecord:
        self._require_portfolio_mutation(plan.portfolio_id, access_context)
        with self._state_lock:
            try:
                stored = self._records[plan.rotation_plan_id]
            except KeyError as error:
                raise NotFoundForActor("recommendation state was not found for actor") from error
            if previous != stored:
                raise IdempotencyConflictError(
                    "recommendation state changed before transition could persist"
                )
            if previous.portfolio_id != plan.portfolio_id:
                raise ValueError("recommendation state plan portfolio does not match")
            updated = RecommendationStateRecord(
                rotation_plan_id=plan.rotation_plan_id,
                portfolio_id=plan.portfolio_id,
                state=next_state,
                remaining_stages_halted=remaining_stages_halted,
                revision=previous.revision + 1,
                updated_at=changed_at,
                finalization_strategy_run_id=finalization_strategy_run_id,
                finalization_strategy_key=finalization_strategy_key,
                legacy_finalization_claim_status=stored.legacy_finalization_claim_status,
                legacy_finalization_strategy_run_id=(stored.legacy_finalization_strategy_run_id),
                legacy_finalization_strategy_key=stored.legacy_finalization_strategy_key,
            )
            self._records[plan.rotation_plan_id] = updated
            return updated


class InMemoryRotationPlanRepository(_PortfolioScopedAdapter):
    """Rotation-plan adapter that resolves portfolio ownership before every access."""

    def __init__(
        self,
        portfolio_owners: Mapping[UUID, UUID],
        *,
        candidate_group_owners: Mapping[UUID, UUID],
        position_portfolios: Mapping[UUID, UUID],
    ) -> None:
        super().__init__(portfolio_owners)
        self._plans: dict[UUID, RotationPlan] = {}
        self._candidate_group_owners = dict(candidate_group_owners)
        self._position_portfolios = dict(position_portfolios)

    def add(self, plan: RotationPlan, *, access_context: AccessContext) -> None:
        plan_owner_id = self._require_portfolio_mutation(plan.portfolio_id, access_context)
        for candidate_group_id in plan.candidate_group_ids:
            if self._candidate_group_owners.get(candidate_group_id) != plan_owner_id:
                raise PlanReferenceUnauthorizedError(
                    "candidate group belongs to another user or is unavailable"
                )
        for position_id in (*plan.source_position_ids, *plan.protected_position_ids):
            if self._position_portfolios.get(position_id) != plan.portfolio_id:
                raise PlanReferenceUnauthorizedError(
                    "position belongs to another portfolio or is unavailable"
                )
        self._plans[plan.rotation_plan_id] = plan

    def get(self, rotation_plan_id: UUID, *, access_context: AccessContext) -> RotationPlan:
        try:
            plan = self._plans[rotation_plan_id]
        except KeyError as error:
            raise NotFoundForActor("rotation plan was not found for actor") from error
        self._require_portfolio_read(plan.portfolio_id, access_context)
        return plan


class InMemoryLogicalScanRepository(_PortfolioScopedAdapter):
    """Recoverable scan and attempt store keyed by the preliminary scan lock identity."""

    def __init__(self, portfolio_owners: Mapping[UUID, UUID]) -> None:
        super().__init__(portfolio_owners)
        self._scans_by_id: dict[UUID, LogicalScanRun] = {}
        self._scans_by_key: dict[str, LogicalScanRun] = {}
        self._attempts_by_scan: dict[UUID, list[ScanAttempt]] = {}
        self._strategy_runs_by_logical_scan: Mapping[UUID, StrategyRun] = {}

    def create_or_recover(
        self,
        *,
        request: ScanLockRequest,
        created_at: datetime,
        access_context: AccessContext,
    ) -> LogicalScanRun:
        self._require_portfolio_mutation(request.plan.portfolio_id, access_context)
        existing = self._scans_by_key.get(request.key)
        if existing is not None:
            if not self._matches_request(existing, request):
                raise ValueError("scan lock key resolves to different scan identity components")
            return existing
        scan = LogicalScanRun(
            logical_scan_run_id=uuid4(),
            rotation_plan_id=request.plan.rotation_plan_id,
            portfolio_id=request.plan.portfolio_id,
            market_session_date=request.market_session_date,
            scan_window_start=request.scan_window_start,
            scan_interval=request.scan_interval,
            market_timezone=request.identity.market_timezone,
            configuration_snapshot_hash=request.identity.configuration_snapshot_hash,
            scan_identity_format_version=request.identity.format_version,
            scan_lock_key=request.key,
            status=LogicalScanStatus.RUNNING,
            created_at=created_at,
        )
        self._scans_by_id[scan.logical_scan_run_id] = scan
        self._scans_by_key[scan.scan_lock_key] = scan
        self._attempts_by_scan[scan.logical_scan_run_id] = []
        return scan

    def get_by_lock_key(
        self, *, request: ScanLockRequest, access_context: AccessContext
    ) -> LogicalScanRun | None:
        self._require_portfolio_read(request.plan.portfolio_id, access_context)
        scan = self._scans_by_key.get(request.key)
        if scan is None:
            return None
        if not self._matches_request(scan, request):
            raise ValueError("scan lock key resolves to different scan identity components")
        return scan

    @staticmethod
    def _matches_request(scan: LogicalScanRun, request: ScanLockRequest) -> bool:
        return (
            scan.rotation_plan_id == request.plan.rotation_plan_id
            and scan.portfolio_id == request.plan.portfolio_id
            and scan.identity == request.identity
            and scan.scan_lock_key == request.key
        )

    def get(self, logical_scan_run_id: UUID, *, access_context: AccessContext) -> LogicalScanRun:
        try:
            scan = self._scans_by_id[logical_scan_run_id]
        except KeyError as error:
            raise NotFoundForActor("logical scan was not found for actor") from error
        self._require_portfolio_read(scan.portfolio_id, access_context)
        return scan

    def record_attempt(self, *, attempt: ScanAttempt, access_context: AccessContext) -> None:
        scan = self.get(attempt.logical_scan_run_id, access_context=access_context)
        self._require_portfolio_mutation(scan.portfolio_id, access_context)
        attempts = self._attempts_by_scan[scan.logical_scan_run_id]
        if any(stored.attempt_number == attempt.attempt_number for stored in attempts):
            raise ValueError("logical scan attempt_number must be unique")
        self._validate_attempt_references(scan=scan, attempt=attempt, existing_attempts=attempts)
        if scan.status is LogicalScanStatus.COMPLETED and attempt.final_strategy_run_id is None:
            raise LogicalScanAlreadyCompletedError(
                "completed logical scans can record only final strategy run evidence"
            )
        validate_scan_attempt_append(existing_attempts=attempts, candidate=attempt)
        attempts.append(attempt)

    def bind_strategy_run_store(self, *, runs_by_logical_scan: Mapping[UUID, StrategyRun]) -> None:
        """Read final-run authority from the actual in-memory strategy run relation."""

        self._strategy_runs_by_logical_scan = runs_by_logical_scan

    def list_attempts(
        self, *, logical_scan_run_id: UUID, access_context: AccessContext
    ) -> Sequence[ScanAttempt]:
        self.get(logical_scan_run_id, access_context=access_context)
        return tuple(
            sorted(
                self._attempts_by_scan[logical_scan_run_id],
                key=lambda attempt: attempt.attempt_number,
            )
        )

    def attach_final_strategy_run(
        self,
        *,
        logical_scan_run_id: UUID,
        plan: RotationPlan,
        strategy_run_id: UUID,
        completed_at: datetime,
        access_context: AccessContext,
    ) -> LogicalScanRun:
        scan = self.get(logical_scan_run_id, access_context=access_context)
        self._require_portfolio_mutation(scan.portfolio_id, access_context)
        if scan.rotation_plan_id != plan.rotation_plan_id or scan.portfolio_id != plan.portfolio_id:
            raise ValueError("logical scan must belong to the supplied rotation plan")
        if scan.status is LogicalScanStatus.COMPLETED:
            raise LogicalScanAlreadyCompletedError("logical scan already has a final strategy run")
        stored_run = self._strategy_runs_by_logical_scan.get(logical_scan_run_id)
        if stored_run is None or stored_run.strategy_run_id != strategy_run_id:
            raise ValueError("logical scan final strategy run was not persisted")
        if not any(
            attempt.status.value == "SUCCEEDED" and attempt.final_strategy_run_id == strategy_run_id
            for attempt in self._attempts_by_scan[logical_scan_run_id]
        ):
            raise ValueError(
                "logical scan requires durable final success evidence before completion"
            )
        completed = replace(
            scan,
            status=LogicalScanStatus.COMPLETED,
            completed_at=completed_at,
            final_strategy_run_id=strategy_run_id,
        )
        self._scans_by_id[logical_scan_run_id] = completed
        self._scans_by_key[completed.scan_lock_key] = completed
        return completed

    def _validate_attempt_references(
        self,
        *,
        scan: LogicalScanRun,
        attempt: ScanAttempt,
        existing_attempts: Sequence[ScanAttempt],
    ) -> None:
        attempt_ids = {stored.scan_attempt_id for stored in existing_attempts}
        if (
            attempt.recovery_of_attempt_id is not None
            and attempt.recovery_of_attempt_id not in attempt_ids
        ):
            raise ValueError("scan attempt recovery reference must exist in the same logical scan")
        if attempt.duplicate_of_attempt_id is not None:
            original = next(
                (
                    stored
                    for stored in existing_attempts
                    if stored.scan_attempt_id == attempt.duplicate_of_attempt_id
                ),
                None,
            )
            if original is None:
                raise ValueError(
                    "scan attempt duplicate reference must exist in the same logical scan"
                )
            if original.status.value != "SUCCEEDED":
                raise ValueError("scan attempt duplicate reference must target a succeeded attempt")
        if attempt.final_strategy_run_id is not None:
            stored_run = self._strategy_runs_by_logical_scan.get(scan.logical_scan_run_id)
            if stored_run is None or stored_run.strategy_run_id != attempt.final_strategy_run_id:
                raise ValueError("final strategy run reference must be the scan's actual final run")
            if (
                scan.status is LogicalScanStatus.COMPLETED
                and scan.final_strategy_run_id != attempt.final_strategy_run_id
            ):
                raise ValueError("final strategy run reference must be the scan's actual final run")


class InMemoryStrategyRunRepository(_PortfolioScopedAdapter):
    """Final run store with one outcome per plan-local idempotency key."""

    def __init__(
        self,
        portfolio_owners: Mapping[UUID, UUID],
        *,
        logical_scan_repository: LogicalScanRepository | None = None,
        child_intent_repository: InMemoryChildIntentRepository | None = None,
    ) -> None:
        super().__init__(portfolio_owners)
        self._runs_by_key: dict[tuple[UUID, str], StrategyRun] = {}
        self._runs_by_id: dict[UUID, StrategyRun] = {}
        self._logical_scan_by_key: dict[tuple[UUID, str], UUID | None] = {}
        self._runs_by_logical_scan: dict[UUID, StrategyRun] = {}
        self._logical_scan_repository = logical_scan_repository or InMemoryLogicalScanRepository(
            portfolio_owners
        )
        if isinstance(self._logical_scan_repository, InMemoryLogicalScanRepository):
            self._logical_scan_repository.bind_strategy_run_store(
                runs_by_logical_scan=self._runs_by_logical_scan
            )
        if child_intent_repository is not None:
            child_intent_repository.bind_strategy_run_store(runs_by_id=self._runs_by_id)

    def record_or_get(
        self,
        *,
        plan: RotationPlan,
        run: StrategyRun,
        idempotency_key: IdempotencyKey,
        logical_scan_run_id: UUID | None = None,
        access_context: AccessContext,
    ) -> StrategyRun:
        self._require_portfolio_mutation(plan.portfolio_id, access_context)
        if run.rotation_plan_id != plan.rotation_plan_id or run.portfolio_id != plan.portfolio_id:
            raise ValueError("strategy run must belong to the supplied rotation plan")
        if not isinstance(idempotency_key, IdempotencyKey):
            raise TypeError("idempotency_key must be an IdempotencyKey")
        if logical_scan_run_id is not None and not isinstance(logical_scan_run_id, UUID):
            raise TypeError("logical_scan_run_id must be a UUID or None")
        key = (plan.rotation_plan_id, idempotency_key.value)
        existing = self._runs_by_key.get(key)
        if existing is not None:
            require_exact_strategy_run_retry(
                existing=existing,
                candidate=run,
                stored_logical_scan_run_id=self._logical_scan_by_key[key],
                requested_logical_scan_run_id=logical_scan_run_id,
            )
            return existing
        if logical_scan_run_id is not None:
            scan = self._logical_scan_repository.get(
                logical_scan_run_id, access_context=access_context
            )
            if (
                scan.rotation_plan_id != plan.rotation_plan_id
                or scan.portfolio_id != plan.portfolio_id
            ):
                raise ValueError("logical scan must belong to the supplied rotation plan")
            if logical_scan_run_id in self._runs_by_logical_scan:
                raise LogicalScanAlreadyCompletedError(
                    "logical scan already has a final strategy run"
                )
        self._runs_by_key[key] = run
        self._runs_by_id[run.strategy_run_id] = run
        self._logical_scan_by_key[key] = logical_scan_run_id
        if logical_scan_run_id is not None:
            self._runs_by_logical_scan[logical_scan_run_id] = run
        return run

    def get_for_logical_scan(
        self, *, logical_scan_run_id: UUID, access_context: AccessContext
    ) -> StrategyRun | None:
        """Read a scan's immutable final result after enforcing its portfolio scope."""

        scan = self._logical_scan_repository.get(logical_scan_run_id, access_context=access_context)
        self._require_portfolio_read(scan.portfolio_id, access_context)
        return self._runs_by_logical_scan.get(logical_scan_run_id)


class InMemoryChildIntentRepository(_PortfolioScopedAdapter):
    """Store deterministic child fingerprints without a notification transport or destination."""

    def __init__(self, portfolio_owners: Mapping[UUID, UUID]) -> None:
        super().__init__(portfolio_owners)
        self._intents_by_key: dict[str, ChildIntent] = {}
        self._intent_keys_by_strategy_run: dict[UUID, list[str]] = {}
        self._strategy_runs_by_id: Mapping[UUID, StrategyRun] = {}
        self._intent_lock = Lock()

    def bind_strategy_run_store(self, *, runs_by_id: Mapping[UUID, StrategyRun]) -> None:
        """Bind intent validation to the immutable strategy-run persistence relation."""

        self._strategy_runs_by_id = runs_by_id

    def record_or_get(self, *, intent: ChildIntent, access_context: AccessContext) -> ChildIntent:
        self._require_portfolio_mutation(intent.portfolio_id, access_context)
        with self._intent_lock:
            strategy_run = self._strategy_runs_by_id.get(intent.strategy_run_id)
            if strategy_run is None:
                raise ValueError("child intent references a missing strategy run")
            if (
                strategy_run.rotation_plan_id != intent.rotation_plan_id
                or strategy_run.portfolio_id != intent.portfolio_id
            ):
                raise ValueError("child intent strategy run does not match its plan scope")
            if _final_strategy_key_from_run(strategy_run) != intent.final_strategy_key:
                raise ValueError("child intent final strategy key does not match strategy run")
            existing = self._intents_by_key.get(intent.intent_key)
            if existing is not None:
                self._require_portfolio_read(existing.portfolio_id, access_context)
                if not same_logical_child_intent(existing=existing, candidate=intent):
                    raise IdempotencyConflictError(
                        "child intent key already records different evidence"
                    )
                return existing
            self._intents_by_key[intent.intent_key] = intent
            self._intent_keys_by_strategy_run.setdefault(intent.strategy_run_id, []).append(
                intent.intent_key
            )
            return intent

    def get(self, *, intent_key: str, access_context: AccessContext) -> ChildIntent:
        try:
            intent = self._intents_by_key[intent_key]
        except KeyError as error:
            raise NotFoundForActor("child intent was not found for actor") from error
        self._require_portfolio_read(intent.portfolio_id, access_context)
        return intent

    def list_for_strategy_run(
        self, *, strategy_run_id: UUID, access_context: AccessContext
    ) -> Sequence[ChildIntent]:
        intents = tuple(
            self._intents_by_key[key]
            for key in self._intent_keys_by_strategy_run.get(strategy_run_id, ())
        )
        for intent in intents:
            self._require_portfolio_read(intent.portfolio_id, access_context)
        return intents


def _final_strategy_key_from_run(strategy_run: StrategyRun) -> str | None:
    """Read the immutable final key directly from one persisted strategy run."""

    idempotency = strategy_run.outputs.get("idempotency")
    if not isinstance(idempotency, Mapping):
        return None
    final_strategy_key = idempotency.get("final_strategy_key")
    return final_strategy_key if isinstance(final_strategy_key, str) else None


class InMemoryConfigurationLayerRepository(_PortfolioScopedAdapter):
    """Layer store that filters portfolio-owned layers by the exact plan portfolio."""

    def __init__(self, portfolio_owners: Mapping[UUID, UUID]) -> None:
        super().__init__(portfolio_owners)
        self._layers: list[ConfigurationLayer] = []

    def add(self, layer: ConfigurationLayer, *, access_context: AccessContext) -> None:
        if layer.ownership.scope is Scope.PORTFOLIO:
            portfolio_id = layer.ownership.owner_id
            if portfolio_id is None:
                raise ValueError("portfolio configuration layer owner is required")
            owner_id = self._require_portfolio_mutation(portfolio_id, access_context)
            if layer.portfolio_owner_id != owner_id:
                raise ValueError("configuration layer portfolio owner does not match portfolio")
        else:
            access_context.require_mutation(
                layer.ownership, portfolio_owner_id=layer.portfolio_owner_id
            )
        self._layers.append(layer)

    def load_for_plan(
        self,
        *,
        plan: RotationPlan,
        selection: ConfigurationLayerSelection,
        access_context: AccessContext,
    ) -> Sequence[ConfigurationLayer]:
        plan_owner_id = self._require_portfolio_read(plan.portfolio_id, access_context)
        if not isinstance(selection, ConfigurationLayerSelection):
            raise TypeError("selection must be a ConfigurationLayerSelection")
        selection.validate_for_plan(plan)
        selected: list[ConfigurationLayer] = []
        for entry in selection.ordered_entries:
            matches = [
                layer
                for layer in self._layers
                if (
                    layer.scope is entry.scope
                    and layer.reference_id == entry.reference_id
                    and layer.version == entry.version
                    and layer.content_hash == entry.content_hash
                )
            ]
            if not matches:
                raise ValueError("selected configuration layer is missing")
            if len(matches) > 1:
                raise ValueError("duplicate selected configuration layer identity")
            layer = matches[0]
            self._validate_selected_layer(
                layer=layer,
                entry=entry,
                plan=plan,
                plan_owner_id=plan_owner_id,
            )
            selected.append(layer)
        return tuple(selected)

    @staticmethod
    def _validate_selected_layer(
        *,
        layer: ConfigurationLayer,
        entry: ConfigurationLayerSelectionEntry,
        plan: RotationPlan,
        plan_owner_id: UUID,
    ) -> None:
        if entry.scope in {
            ConfigurationLayerScope.SYSTEM,
            ConfigurationLayerScope.MARKET,
            ConfigurationLayerScope.STRATEGY,
        }:
            if layer.ownership != Ownership(Scope.SYSTEM, None):
                raise ValueError("system configuration selection has invalid ownership")
            return
        if entry.scope is ConfigurationLayerScope.USER:
            if layer.ownership != Ownership(Scope.USER, plan_owner_id):
                raise ValueError("user configuration selection does not match plan owner")
            return
        if layer.ownership != Ownership(Scope.PORTFOLIO, plan.portfolio_id):
            raise ValueError(
                "portfolio configuration selection does not match exact plan portfolio"
            )
        if layer.portfolio_owner_id != plan_owner_id:
            raise ValueError("configuration layer portfolio owner does not match plan owner")


class InMemoryConfigurationSnapshotRepository(_PortfolioScopedAdapter):
    """Scoped immutable snapshot adapter with canonical validation on every boundary."""

    def __init__(self, portfolio_owners: Mapping[UUID, UUID]) -> None:
        super().__init__(portfolio_owners)
        self._snapshots: dict[UUID, PersistedConfigurationSnapshot] = {}
        self._snapshot_ids_by_target_version: dict[tuple[Scope, UUID | None, UUID, int], UUID] = {}

    def record_or_get(
        self,
        *,
        snapshot: PersistedConfigurationSnapshot,
        access_context: AccessContext,
    ) -> PersistedConfigurationSnapshot:
        canonical_snapshot = _canonical_snapshot(snapshot)
        self._require_snapshot_mutation(canonical_snapshot, access_context)
        target_key = _snapshot_target_version_key(canonical_snapshot)
        target_snapshot_id = self._snapshot_ids_by_target_version.get(target_key)
        if target_snapshot_id is not None:
            existing_for_target = self._snapshots[target_snapshot_id]
            self._require_snapshot_read(existing_for_target, access_context)
            if existing_for_target != canonical_snapshot:
                raise IdempotencyConflictError(
                    "configuration snapshot target and version already record different evidence"
                )
            return existing_for_target
        existing = self._snapshots.get(canonical_snapshot.snapshot_id)
        if existing is None:
            self._snapshots[canonical_snapshot.snapshot_id] = canonical_snapshot
            self._snapshot_ids_by_target_version[target_key] = canonical_snapshot.snapshot_id
            return canonical_snapshot
        self._require_snapshot_read(existing, access_context)
        if existing != canonical_snapshot:
            raise IdempotencyConflictError(
                "configuration snapshot id already records different evidence"
            )
        return existing

    def get(
        self,
        snapshot_id: UUID,
        *,
        access_context: AccessContext,
    ) -> PersistedConfigurationSnapshot:
        try:
            stored = self._snapshots[snapshot_id]
        except KeyError as error:
            raise NotFoundForActor("configuration snapshot was not found for actor") from error
        self._require_snapshot_read(stored, access_context)
        return _canonical_snapshot(stored)

    def _require_snapshot_read(
        self, snapshot: PersistedConfigurationSnapshot, access_context: AccessContext
    ) -> None:
        ownership = snapshot.target_ownership
        if ownership.scope is Scope.PORTFOLIO:
            assert ownership.owner_id is not None
            self._require_portfolio_read(ownership.owner_id, access_context)
            return
        access_context.require_read(ownership)

    def _require_snapshot_mutation(
        self, snapshot: PersistedConfigurationSnapshot, access_context: AccessContext
    ) -> None:
        ownership = snapshot.target_ownership
        if ownership.scope is Scope.PORTFOLIO:
            assert ownership.owner_id is not None
            self._require_portfolio_mutation(ownership.owner_id, access_context)
            return
        access_context.require_mutation(ownership)


def _canonical_snapshot(
    snapshot: PersistedConfigurationSnapshot,
) -> PersistedConfigurationSnapshot:
    """Repeat full resolved-snapshot canonical validation without ORM dependencies."""

    resolved = snapshot.resolved_snapshot
    validated = restore_resolved_configuration_snapshot(
        payload=resolved.payload,
        parent_versions=resolved.parent_versions,
        created_by=resolved.created_by,
        created_at=resolved.created_at,
        runtime_expires_at=resolved.runtime_expires_at,
        canonical_format_version=resolved.canonical_format_version,
        content_hash=resolved.content_hash,
        canonical_payload_json=resolved.canonical_json,
    )
    return PersistedConfigurationSnapshot(
        snapshot_id=snapshot.snapshot_id,
        resolved_snapshot=validated,
        config_version=snapshot.config_version,
        target_ownership=snapshot.target_ownership,
        target_reference_id=snapshot.target_reference_id,
    )


def _snapshot_target_version_key(
    snapshot: PersistedConfigurationSnapshot,
) -> tuple[Scope, UUID | None, UUID, int]:
    """Return the null-safe target identity used by every snapshot adapter."""

    ownership = snapshot.target_ownership
    return (
        ownership.scope,
        ownership.owner_id,
        snapshot.target_reference_id,
        snapshot.config_version,
    )


class InMemoryLockProvider(_PortfolioScopedAdapter):
    """Thread-safe preliminary scan lock for M0/M1's single-process adapters."""

    def __init__(self, portfolio_owners: Mapping[UUID, UUID]) -> None:
        super().__init__(portfolio_owners)
        self._guard = Lock()
        self._leases: dict[str, LockLease] = {}
        self._lease_portfolios: dict[UUID, UUID] = {}
        self._lease_keys: dict[UUID, str] = {}

    def acquire_scan_lock(
        self, request: ScanLockRequest, *, access_context: AccessContext
    ) -> LockLease | None:
        self._require_portfolio_mutation(request.plan.portfolio_id, access_context)
        with self._guard:
            if request.key in self._leases:
                return None
            lease = LockLease(lease_id=uuid4(), key=request.key)
            self._leases[request.key] = lease
            self._lease_portfolios[lease.lease_id] = request.plan.portfolio_id
            self._lease_keys[lease.lease_id] = request.key
            return lease

    def release(self, lease: LockLease, *, access_context: AccessContext) -> None:
        with self._guard:
            portfolio_id = self._lease_portfolios.get(lease.lease_id)
            if portfolio_id is None:
                return
            self._require_portfolio_mutation(portfolio_id, access_context)
            stored_key = self._lease_keys[lease.lease_id]
            if lease.key != stored_key:
                raise ValueError("lease key does not match the acquired lock")
            self._leases.pop(stored_key, None)
            self._lease_portfolios.pop(lease.lease_id, None)
            self._lease_keys.pop(lease.lease_id, None)
