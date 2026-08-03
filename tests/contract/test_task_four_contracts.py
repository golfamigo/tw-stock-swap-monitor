"""Executable Task 4 contracts for scoped repository adapters and JSON evidence."""

from __future__ import annotations

import importlib
import importlib.util
import warnings
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from app.application.configuration import (
    CANONICAL_FORMAT_VERSION,
    ConfigurationLayer,
    ConfigurationResolver,
    ResolvedConfigurationSnapshot,
    canonical_content_hash,
    canonical_json,
)
from app.domain.access import AccessContext
from app.domain.entities import (
    Instrument,
    Portfolio,
    Position,
    RotationPlan,
    StrategyRun,
)
from app.domain.enums import PositionRole, PositionStatus, Scope
from app.domain.errors import (
    AuthorizationDenied,
    NotFoundForActor,
    PlanReferenceUnauthorizedError,
    PositionAlreadyOpenError,
)
from app.domain.values import (
    ConfigurationSnapshotRef,
    IdempotencyKey,
    InstrumentRef,
    Ownership,
    Quantity,
    ScanIdentity,
)
from app.repositories.configuration_layers import (
    ConfigurationLayerSelection,
    ConfigurationLayerSelectionEntry,
)
from app.repositories.locks import LockLease
from app.schemas.common import ConfigurationLayerScope, ParentVersion
from app.schemas.configuration import LayerPatchSchema
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError, SAWarning
from sqlalchemy.orm import Session

NOW = datetime(2026, 8, 1, 9, tzinfo=UTC)
EXPECTED_FOUNDATION_TABLES = frozenset(
    {
        "users",
        "portfolios",
        "instruments",
        "positions",
        "candidate_groups",
        "candidate_group_instruments",
        "rotation_plans",
        "rotation_plan_candidate_groups",
        "rotation_plan_source_positions",
        "rotation_plan_protected_positions",
        "configuration_layers",
        "configuration_snapshots",
        "logical_scan_runs",
        "scan_attempts",
        "strategy_runs",
        "recommendation_states",
        "child_intents",
    }
)


def _scan_identity(plan: RotationPlan) -> ScanIdentity:
    return ScanIdentity(
        rotation_plan_id=plan.rotation_plan_id,
        market_session_date=date(2026, 8, 1),
        scan_window_start=NOW,
        scan_interval="PT3M",
        configuration_snapshot_hash="a" * 64,
        market_timezone="Asia/Taipei",
    )


class EvidenceStringEnum(str, Enum):
    """Enum which would otherwise silently pass the ``str`` primitive check."""

    VALUE = "evidence"


class EvidenceIntegerEnum(IntEnum):
    """Enum which would otherwise silently pass the ``int`` primitive check."""

    VALUE = 1


class ConfigurationStringEnum(str, Enum):
    """String enum accepted by configuration canonicalization."""

    VALUE = "configuration"


class ConfigurationIntegerEnum(IntEnum):
    """IntEnum that configuration persistence must reject."""

    VALUE = 1


class ConfigurationNonStringEnum(Enum):
    """Non-string Enum that configuration persistence must reject."""

    VALUE = 1


def _load(module_name: str) -> Any:
    spec = importlib.util.find_spec(module_name)
    assert spec is not None, f"{module_name} must be implemented for Task 4"
    return importlib.import_module(module_name)


def _api() -> dict[str, Any]:
    memory = _load("app.persistence.in_memory")
    repositories = _load("app.repositories")
    evidence = _load("app.persistence.evidence")
    models = _load("app.persistence.models")
    return {
        "InMemoryConfigurationLayerRepository": memory.InMemoryConfigurationLayerRepository,
        "InMemoryInstrumentRepository": memory.InMemoryInstrumentRepository,
        "InMemoryLockProvider": memory.InMemoryLockProvider,
        "InMemoryPositionRepository": memory.InMemoryPositionRepository,
        "InMemoryRotationPlanRepository": memory.InMemoryRotationPlanRepository,
        "InMemoryStrategyRunRepository": memory.InMemoryStrategyRunRepository,
        "ScanLockRequest": repositories.ScanLockRequest,
        "decode_evidence": evidence.decode_evidence,
        "encode_evidence": evidence.encode_evidence,
        "Base": models.Base,
    }


def _context(user_id: UUID, *, administrator: bool = False) -> AccessContext:
    return AccessContext(
        actor_user_id=user_id,
        is_administrator=administrator,
        request_id=uuid4(),
        authentication_method="contract-test",
    )


def _portfolio(user_id: UUID) -> Portfolio:
    return Portfolio(portfolio_id=uuid4(), user_id=user_id, created_at=NOW)


def _position(
    portfolio: Portfolio,
    instrument_id: UUID,
    *,
    closed: bool = False,
    position_id: UUID | None = None,
) -> Position:
    return Position(
        position_id=position_id or uuid4(),
        portfolio_id=portfolio.portfolio_id,
        instrument=InstrumentRef(instrument_id),
        quantity=Quantity(Decimal("10.00")),
        role=PositionRole.NORMAL,
        status=PositionStatus.CLOSED if closed else PositionStatus.OPEN,
        opened_at=NOW,
        closed_at=NOW + timedelta(minutes=1) if closed else None,
    )


def _plan(
    portfolio: Portfolio,
    *,
    candidate_group_ids: tuple[UUID, ...] | None = None,
    source_position_ids: tuple[UUID, ...] = (),
    protected_position_ids: tuple[UUID, ...] = (),
) -> RotationPlan:
    return RotationPlan(
        rotation_plan_id=uuid4(),
        portfolio_id=portfolio.portfolio_id,
        candidate_group_ids=candidate_group_ids or (uuid4(),),
        source_position_ids=source_position_ids,
        protected_position_ids=protected_position_ids,
        created_at=NOW,
    )


def _run(
    plan: RotationPlan,
    *,
    configuration_snapshot: ConfigurationSnapshotRef | None = None,
    outputs: dict[str, object] | None = None,
) -> StrategyRun:
    return StrategyRun(
        strategy_run_id=uuid4(),
        rotation_plan_id=plan.rotation_plan_id,
        portfolio_id=plan.portfolio_id,
        configuration_snapshot=configuration_snapshot
        or ConfigurationSnapshotRef(uuid4(), "a" * 64, NOW),
        market_data_snapshot_id="reproducible-market-snapshot",
        state_transition="SCANNING->ACTION_PENDING",
        outputs=outputs if outputs is not None else {"score": Decimal("1.2300"), "at": NOW},
        occurred_at=NOW,
    )


def _portfolio_layer(
    portfolio: Portfolio, owner_id: UUID, *, reference_id: UUID | None = None, version: int = 1
) -> ConfigurationLayer:
    patch = LayerPatchSchema.for_scope(ConfigurationLayerScope.PORTFOLIO, {"settings": {}})
    return ConfigurationLayer(
        scope=ConfigurationLayerScope.PORTFOLIO,
        patch=patch,
        reference_id=reference_id or uuid4(),
        version=version,
        content_hash=canonical_content_hash(patch.model_dump(mode="python", exclude_unset=True)),
        ownership=Ownership(Scope.PORTFOLIO, portfolio.portfolio_id),
        portfolio_owner_id=owner_id,
    )


def _plan_bound_layer(
    plan: RotationPlan,
    owner_id: UUID,
    *,
    scope: ConfigurationLayerScope,
) -> ConfigurationLayer:
    patch = LayerPatchSchema.for_scope(scope, {"settings": {}})
    return ConfigurationLayer(
        scope=scope,
        patch=patch,
        reference_id=plan.rotation_plan_id,
        version=1,
        content_hash=canonical_content_hash(
            patch.model_dump(mode="python", by_alias=True, exclude_unset=True)
        ),
        ownership=Ownership(Scope.PORTFOLIO, plan.portfolio_id),
        portfolio_owner_id=owner_id,
        runtime_expires_at=(NOW + timedelta(minutes=15))
        if scope is ConfigurationLayerScope.RUNTIME_OVERRIDE
        else None,
    )


def _layer_selection(*layers: ConfigurationLayer) -> ConfigurationLayerSelection:
    """Build exact layer identity evidence for repository-load contracts."""

    return ConfigurationLayerSelection(
        entries=tuple(
            ConfigurationLayerSelectionEntry(
                scope=layer.scope,
                reference_id=layer.reference_id,
                version=layer.version,
                content_hash=layer.content_hash,
            )
            for layer in layers
        )
    )


def _snapshot(
    *, parent_versions: tuple[ParentVersion, ...], runtime_expires_at: datetime | None = None
) -> ResolvedConfigurationSnapshot:
    payload: dict[str, object] = {
        "configuration_name": "contract",
        "settings": {"stable": "value"},
        "rules": [],
        "keyed_items": [],
        "extensions": {},
        "nullable_note": None,
    }
    return ResolvedConfigurationSnapshot(
        payload=payload,
        parent_versions=parent_versions,
        created_by=uuid4(),
        created_at=NOW,
        runtime_expires_at=runtime_expires_at,
        canonical_format_version=CANONICAL_FORMAT_VERSION,
        content_hash=canonical_content_hash(payload),
        canonical_json=canonical_json(payload),
    )


def _persisted_snapshot(
    *,
    parent_versions: tuple[ParentVersion, ...] = (),
    snapshot_id: UUID | None = None,
    config_version: int = 1,
    target_ownership: Ownership | None = None,
    target_reference_id: UUID | None = None,
    runtime_expires_at: datetime | None = None,
) -> Any:
    records = _load("app.persistence.records")
    return records.PersistedConfigurationSnapshot(
        snapshot_id=snapshot_id or uuid4(),
        resolved_snapshot=_snapshot(
            parent_versions=parent_versions, runtime_expires_at=runtime_expires_at
        ),
        config_version=config_version,
        target_ownership=target_ownership or Ownership(Scope.SYSTEM, None),
        target_reference_id=target_reference_id or uuid4(),
    )


def test_task_four_modules_and_ports_exist() -> None:
    for name in (
        "app.repositories.base",
        "app.repositories.positions",
        "app.repositories.rotation_plans",
        "app.repositories.strategy_runs",
        "app.repositories.locks",
        "app.persistence.models",
        "app.persistence.mappers",
        "app.persistence.repositories",
        "app.persistence.in_memory",
        "app.persistence.evidence",
    ):
        _load(name)


def test_position_repository_requires_access_context_and_hides_other_users() -> None:
    api = _api()
    owner_id, other_id = uuid4(), uuid4()
    portfolio = _portfolio(owner_id)
    repository = api["InMemoryPositionRepository"]({portfolio.portfolio_id: owner_id})
    position = _position(portfolio, uuid4())
    repository.add(position, access_context=_context(owner_id))

    with pytest.raises(TypeError):
        repository.get(position.position_id)
    with pytest.raises(NotFoundForActor):
        repository.get(position.position_id, access_context=_context(other_id))
    assert repository.get(position.position_id, access_context=_context(owner_id)) == position


def test_system_instrument_is_readable_but_not_mutable_by_an_ordinary_user() -> None:
    api = _api()
    user_id = uuid4()
    instrument = Instrument(instrument_id=uuid4(), market="TWSE", symbol="TEST", created_at=NOW)
    repository = api["InMemoryInstrumentRepository"]()
    repository.add(instrument, access_context=_context(user_id, administrator=True))

    assert repository.get(instrument.instrument_id, access_context=_context(user_id)) == instrument
    with pytest.raises(AuthorizationDenied):
        repository.add(instrument, access_context=_context(user_id))


def test_strategy_run_repository_returns_the_existing_run_for_one_idempotency_key() -> None:
    api = _api()
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    repository = api["InMemoryStrategyRunRepository"]({portfolio.portfolio_id: owner_id})

    candidate = _run(plan)
    first = repository.record_or_get(
        plan=plan,
        run=candidate,
        idempotency_key=IdempotencyKey("one-logical-scan"),
        access_context=_context(owner_id),
    )
    second = repository.record_or_get(
        plan=plan,
        run=candidate,
        idempotency_key=IdempotencyKey("one-logical-scan"),
        access_context=_context(owner_id),
    )

    assert second == first
    with pytest.raises(ValueError, match="idempotency key"):
        repository.record_or_get(
            plan=plan,
            run=replace(first, outputs={"score": Decimal("9.99"), "at": NOW}),
            idempotency_key=IdempotencyKey("one-logical-scan"),
            access_context=_context(owner_id),
        )
    with pytest.raises(TypeError, match="IdempotencyKey"):
        repository.record_or_get(
            plan=plan,
            run=_run(plan),
            idempotency_key="raw-idempotency-key",
            access_context=_context(owner_id),
        )


def test_in_memory_strategy_run_rejects_changed_output_or_logical_scan_for_one_key() -> None:
    """Exact retries retain every immutable run field and the original logical-scan link."""

    api = _api()
    memory = _load("app.persistence.in_memory")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    scans = memory.InMemoryLogicalScanRepository({portfolio.portfolio_id: owner_id})
    strategy_runs = api["InMemoryStrategyRunRepository"](
        {portfolio.portfolio_id: owner_id}, logical_scan_repository=scans
    )
    first_request = api["ScanLockRequest"](
        plan=plan,
        market_session_date="2026-08-01",
        scan_window_start=NOW,
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        configuration_snapshot_hash="a" * 64,
    )
    second_request = api["ScanLockRequest"](
        plan=plan,
        market_session_date="2026-08-01",
        scan_window_start=NOW + timedelta(minutes=3),
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        configuration_snapshot_hash="a" * 64,
    )
    first_scan = scans.create_or_recover(
        request=first_request, created_at=NOW, access_context=_context(owner_id)
    )
    second_scan = scans.create_or_recover(
        request=second_request, created_at=NOW, access_context=_context(owner_id)
    )
    candidate = _run(plan)
    stored = strategy_runs.record_or_get(
        plan=plan,
        run=candidate,
        idempotency_key=IdempotencyKey("in-memory-exact-retry"),
        logical_scan_run_id=first_scan.logical_scan_run_id,
        access_context=_context(owner_id),
    )

    assert (
        strategy_runs.record_or_get(
            plan=plan,
            run=stored,
            idempotency_key=IdempotencyKey("in-memory-exact-retry"),
            logical_scan_run_id=first_scan.logical_scan_run_id,
            access_context=_context(owner_id),
        )
        == stored
    )
    with pytest.raises(ValueError, match="idempotency key"):
        strategy_runs.record_or_get(
            plan=plan,
            run=replace(stored, outputs={"score": Decimal("8.88"), "at": NOW}),
            idempotency_key=IdempotencyKey("in-memory-exact-retry"),
            logical_scan_run_id=first_scan.logical_scan_run_id,
            access_context=_context(owner_id),
        )
    with pytest.raises(ValueError, match="idempotency key"):
        strategy_runs.record_or_get(
            plan=plan,
            run=stored,
            idempotency_key=IdempotencyKey("in-memory-exact-retry"),
            logical_scan_run_id=second_scan.logical_scan_run_id,
            access_context=_context(owner_id),
        )


def test_scan_lock_allows_exactly_one_concurrent_winner() -> None:
    api = _api()
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    provider = api["InMemoryLockProvider"]({portfolio.portfolio_id: owner_id})
    request = api["ScanLockRequest"](
        plan=plan,
        market_session_date="2026-08-01",
        scan_window_start=NOW,
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        configuration_snapshot_hash="a" * 64,
    )

    leases = [
        provider.acquire_scan_lock(request, access_context=_context(owner_id)) for _ in range(2)
    ]

    assert sum(lease is not None for lease in leases) == 1


def test_in_memory_lock_rejects_a_forged_key_without_releasing_the_real_lease() -> None:
    api = _api()
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    provider = api["InMemoryLockProvider"]({portfolio.portfolio_id: owner_id})
    request = api["ScanLockRequest"](
        plan=plan,
        market_session_date="2026-08-01",
        scan_window_start=NOW,
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        configuration_snapshot_hash="a" * 64,
    )
    lease = provider.acquire_scan_lock(request, access_context=_context(owner_id))
    assert lease is not None

    with pytest.raises(ValueError, match="lease key"):
        provider.release(
            LockLease(lease_id=lease.lease_id, key="forged-key"), access_context=_context(owner_id)
        )
    assert provider.acquire_scan_lock(request, access_context=_context(owner_id)) is None
    provider.release(lease, access_context=_context(owner_id))
    assert provider.acquire_scan_lock(request, access_context=_context(owner_id)) is not None


def test_open_position_uniqueness_keeps_closed_history_insertable() -> None:
    api = _api()
    owner_id, instrument_id = uuid4(), uuid4()
    portfolio = _portfolio(owner_id)
    repository = api["InMemoryPositionRepository"]({portfolio.portfolio_id: owner_id})
    repository.add(_position(portfolio, instrument_id), access_context=_context(owner_id))
    repository.add(
        _position(portfolio, instrument_id, closed=True), access_context=_context(owner_id)
    )
    repository.add(
        _position(portfolio, instrument_id, closed=True), access_context=_context(owner_id)
    )

    with pytest.raises(PositionAlreadyOpenError):
        repository.add(_position(portfolio, instrument_id), access_context=_context(owner_id))


def test_configuration_loading_rejects_a_second_portfolio_owned_by_the_same_user() -> None:
    api = _api()
    owner_id = uuid4()
    plan_portfolio = _portfolio(owner_id)
    other_portfolio = _portfolio(owner_id)
    plan = _plan(plan_portfolio)
    repository = api["InMemoryConfigurationLayerRepository"](
        {plan_portfolio.portfolio_id: owner_id, other_portfolio.portfolio_id: owner_id}
    )
    matching = _portfolio_layer(plan_portfolio, owner_id)
    unrelated = _portfolio_layer(other_portfolio, owner_id)
    repository.add(matching, access_context=_context(owner_id))
    repository.add(unrelated, access_context=_context(owner_id))

    assert repository.load_for_plan(
        plan=plan,
        selection=_layer_selection(matching),
        access_context=_context(owner_id),
    ) == (matching,)


def test_in_memory_configuration_loading_excludes_other_plan_bound_layers() -> None:
    """Plan-scoped layers never leak between two plans in one portfolio."""

    api = _api()
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    first_plan = _plan(portfolio)
    second_plan = _plan(portfolio)
    repository = api["InMemoryConfigurationLayerRepository"]({portfolio.portfolio_id: owner_id})
    first_rotation = _plan_bound_layer(
        first_plan, owner_id, scope=ConfigurationLayerScope.ROTATION_PLAN
    )
    first_runtime = _plan_bound_layer(
        first_plan, owner_id, scope=ConfigurationLayerScope.RUNTIME_OVERRIDE
    )
    second_rotation = _plan_bound_layer(
        second_plan, owner_id, scope=ConfigurationLayerScope.ROTATION_PLAN
    )
    second_runtime = _plan_bound_layer(
        second_plan, owner_id, scope=ConfigurationLayerScope.RUNTIME_OVERRIDE
    )
    for layer in (first_rotation, first_runtime, second_rotation, second_runtime):
        repository.add(layer, access_context=_context(owner_id))

    assert repository.load_for_plan(
        plan=first_plan,
        selection=_layer_selection(first_rotation, first_runtime),
        access_context=_context(owner_id),
    ) == (
        first_rotation,
        first_runtime,
    )


def test_sql_configuration_loading_excludes_a_sibling_portfolio_layer() -> None:
    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    persistence_repositories = _load("app.persistence.repositories")
    owner_id = uuid4()
    plan_portfolio = _portfolio(owner_id)
    sibling_portfolio = _portfolio(owner_id)
    plan = _plan(plan_portfolio)
    matching = _portfolio_layer(plan_portfolio, owner_id)
    sibling = _portfolio_layer(sibling_portfolio, owner_id)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as seed_session:
        seed_session.add_all(
            (
                persistence_models.UserModel(user_id=owner_id, created_at=NOW),
                persistence_models.PortfolioModel(
                    portfolio_id=plan_portfolio.portfolio_id,
                    user_id=owner_id,
                    created_at=NOW,
                ),
                persistence_models.PortfolioModel(
                    portfolio_id=sibling_portfolio.portfolio_id,
                    user_id=owner_id,
                    created_at=NOW,
                ),
                persistence_mappers.configuration_layer_to_model(matching),
                persistence_mappers.configuration_layer_to_model(sibling),
            )
        )
        seed_session.commit()

        layers = persistence_repositories.SqlAlchemyConfigurationLayerRepository(
            seed_session
        ).load_for_plan(
            plan=plan,
            selection=_layer_selection(matching),
            access_context=_context(owner_id),
        )

    assert tuple(layers) == (matching,)


def test_sql_configuration_loading_excludes_other_plan_bound_layers() -> None:
    """The SQL loader enforces reference_id == plan_id for plan-bound layers."""

    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    persistence_repositories = _load("app.persistence.repositories")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    first_plan = _plan(portfolio)
    second_plan = _plan(portfolio)
    first_rotation = _plan_bound_layer(
        first_plan, owner_id, scope=ConfigurationLayerScope.ROTATION_PLAN
    )
    first_runtime = _plan_bound_layer(
        first_plan, owner_id, scope=ConfigurationLayerScope.RUNTIME_OVERRIDE
    )
    second_rotation = _plan_bound_layer(
        second_plan, owner_id, scope=ConfigurationLayerScope.ROTATION_PLAN
    )
    second_runtime = _plan_bound_layer(
        second_plan, owner_id, scope=ConfigurationLayerScope.RUNTIME_OVERRIDE
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                persistence_models.UserModel(user_id=owner_id, created_at=NOW),
                persistence_models.PortfolioModel(
                    portfolio_id=portfolio.portfolio_id, user_id=owner_id, created_at=NOW
                ),
                persistence_models.RotationPlanModel(
                    rotation_plan_id=first_plan.rotation_plan_id,
                    portfolio_id=portfolio.portfolio_id,
                    created_at=NOW,
                ),
                persistence_models.RotationPlanModel(
                    rotation_plan_id=second_plan.rotation_plan_id,
                    portfolio_id=portfolio.portfolio_id,
                    created_at=NOW,
                ),
                persistence_mappers.configuration_layer_to_model(first_rotation),
                persistence_mappers.configuration_layer_to_model(first_runtime),
                persistence_mappers.configuration_layer_to_model(second_rotation),
                persistence_mappers.configuration_layer_to_model(second_runtime),
            )
        )
        session.commit()
        layers = persistence_repositories.SqlAlchemyConfigurationLayerRepository(
            session
        ).load_for_plan(
            plan=first_plan,
            selection=_layer_selection(first_rotation, first_runtime),
            access_context=_context(owner_id),
        )

    assert tuple(layers) == (first_rotation, first_runtime)


def test_in_memory_rotation_plan_write_rejects_cross_user_groups_and_positions() -> None:
    api = _api()
    owner_id, other_user_id = uuid4(), uuid4()
    plan_portfolio = _portfolio(owner_id)
    other_portfolio = _portfolio(other_user_id)
    permitted_group_id, foreign_group_id = uuid4(), uuid4()
    foreign_position_id = uuid4()
    repository = api["InMemoryRotationPlanRepository"](
        {
            plan_portfolio.portfolio_id: owner_id,
            other_portfolio.portfolio_id: other_user_id,
        },
        candidate_group_owners={permitted_group_id: owner_id, foreign_group_id: other_user_id},
        position_portfolios={foreign_position_id: other_portfolio.portfolio_id},
    )
    foreign_group_plan = _plan(plan_portfolio, candidate_group_ids=(foreign_group_id,))
    foreign_position_plan = _plan(
        plan_portfolio,
        candidate_group_ids=(permitted_group_id,),
        source_position_ids=(foreign_position_id,),
    )

    with pytest.raises(PlanReferenceUnauthorizedError, match="candidate group"):
        repository.add(foreign_group_plan, access_context=_context(owner_id))
    with pytest.raises(PlanReferenceUnauthorizedError, match="position"):
        repository.add(foreign_position_plan, access_context=_context(owner_id))


def test_sql_rotation_plan_write_rejects_cross_user_groups_and_positions() -> None:
    persistence_models = _load("app.persistence.models")
    persistence_repositories = _load("app.persistence.repositories")
    owner_id, other_user_id = uuid4(), uuid4()
    plan_portfolio = _portfolio(owner_id)
    other_portfolio = _portfolio(other_user_id)
    permitted_group_id, foreign_group_id = uuid4(), uuid4()
    instrument_id, foreign_position_id = uuid4(), uuid4()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                persistence_models.UserModel(user_id=owner_id, created_at=NOW),
                persistence_models.UserModel(user_id=other_user_id, created_at=NOW),
                persistence_models.PortfolioModel(
                    portfolio_id=plan_portfolio.portfolio_id,
                    user_id=owner_id,
                    created_at=NOW,
                ),
                persistence_models.PortfolioModel(
                    portfolio_id=other_portfolio.portfolio_id,
                    user_id=other_user_id,
                    created_at=NOW,
                ),
                persistence_models.CandidateGroupModel(
                    candidate_group_id=permitted_group_id,
                    user_id=owner_id,
                    created_at=NOW,
                ),
                persistence_models.CandidateGroupModel(
                    candidate_group_id=foreign_group_id,
                    user_id=other_user_id,
                    created_at=NOW,
                ),
                persistence_models.InstrumentModel(
                    instrument_id=instrument_id,
                    market="TWSE",
                    symbol="CONTRACT",
                    created_at=NOW,
                ),
                persistence_models.PositionModel(
                    position_id=foreign_position_id,
                    portfolio_id=other_portfolio.portfolio_id,
                    instrument_id=instrument_id,
                    quantity=Decimal("1"),
                    role=PositionRole.NORMAL.value,
                    status=PositionStatus.OPEN.value,
                    opened_at=NOW,
                    closed_at=None,
                ),
            )
        )
        session.commit()
        repository = persistence_repositories.SqlAlchemyRotationPlanRepository(session)
        foreign_group_plan = _plan(plan_portfolio, candidate_group_ids=(foreign_group_id,))
        foreign_position_plan = _plan(
            plan_portfolio,
            candidate_group_ids=(permitted_group_id,),
            source_position_ids=(foreign_position_id,),
        )

        with pytest.raises(PlanReferenceUnauthorizedError, match="candidate group"):
            repository.add(foreign_group_plan, access_context=_context(owner_id))
        with pytest.raises(PlanReferenceUnauthorizedError, match="position"):
            repository.add(foreign_position_plan, access_context=_context(owner_id))


def test_evidence_codec_round_trips_supported_values_without_mapping_key_collisions() -> None:
    api = _api()
    offset = timezone(timedelta(hours=8))
    value = {
        "$evidence": "ordinary-user-key",
        "decimal": Decimal("1.2300"),
        "identifier": UUID("00000000-0000-0000-0000-000000000123"),
        "timestamp": datetime(2026, 8, 1, 9, 30, 15, 123456, tzinfo=offset),
        "nested": ({"items": [Decimal("2.00")]},),
    }

    encoded = api["encode_evidence"](value)
    decoded = api["decode_evidence"](encoded)

    assert decoded == value
    assert str(decoded["decimal"]) == "1.2300"


def test_evidence_codec_rejects_malformed_stored_values() -> None:
    api = _api()

    with pytest.raises(ValueError):
        api["decode_evidence"]({"$evidence": "decimal", "value": "not-a-number"})


@pytest.mark.parametrize("value", [EvidenceStringEnum.VALUE, EvidenceIntegerEnum.VALUE])
def test_evidence_enums_are_rejected_before_domain_or_codec_persistence(value: object) -> None:
    api = _api()
    plan = _plan(_portfolio(uuid4()))

    with pytest.raises(ValueError, match="Enum"):
        StrategyRun(
            strategy_run_id=uuid4(),
            rotation_plan_id=plan.rotation_plan_id,
            portfolio_id=plan.portfolio_id,
            configuration_snapshot=ConfigurationSnapshotRef(uuid4(), "a" * 64, NOW),
            market_data_snapshot_id="enum-evidence",
            state_transition="SCANNING->ACTION_PENDING",
            outputs={"enum": value},
            occurred_at=NOW,
        )
    with pytest.raises(ValueError, match="Enum"):
        api["encode_evidence"](value)


def test_strategy_run_persistence_round_trips_supported_evidence() -> None:
    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    evidence_offset = timezone(timedelta(hours=8))
    run = StrategyRun(
        strategy_run_id=uuid4(),
        rotation_plan_id=plan.rotation_plan_id,
        portfolio_id=plan.portfolio_id,
        configuration_snapshot=ConfigurationSnapshotRef(uuid4(), "a" * 64, NOW),
        market_data_snapshot_id="reproducible-market-snapshot",
        state_transition="SCANNING->ACTION_PENDING",
        outputs={
            "decimal": Decimal("1.2300"),
            "identifier": UUID("00000000-0000-0000-0000-000000000123"),
            "timestamp": datetime(2026, 8, 1, 9, 30, 15, 123456, tzinfo=evidence_offset),
            "nested": {"$evidence": "ordinary-user-key", "tuple": ([Decimal("2.00")],)},
        },
        occurred_at=NOW,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(persistence_mappers.strategy_run_to_model(run, idempotency_key="round-trip"))
        session.commit()
        stored = session.get(persistence_models.StrategyRunModel, run.strategy_run_id)
        assert stored is not None
        restored = persistence_mappers.strategy_run_from_model(stored)

    assert restored.outputs == run.outputs
    assert restored.configuration_snapshot == run.configuration_snapshot
    assert restored.outputs["decimal"] == Decimal("1.2300")
    assert isinstance(restored.outputs["identifier"], UUID)
    assert restored.outputs["timestamp"].utcoffset() == timedelta(hours=8)
    assert restored.outputs["nested"] == run.outputs["nested"]

    malformed = persistence_mappers.strategy_run_to_model(
        _run(plan), idempotency_key="malformed-stored-evidence"
    )
    malformed.outputs = {"$evidence": "decimal", "value": "not-a-number"}
    with Session(engine) as session:
        session.add(malformed)
        session.commit()
        stored_malformed = session.get(
            persistence_models.StrategyRunModel, malformed.strategy_run_id
        )
        assert stored_malformed is not None
        with pytest.raises(ValueError, match="stored decimal evidence"):
            persistence_mappers.strategy_run_from_model(stored_malformed)


def test_configuration_layer_versions_use_distinct_primary_keys() -> None:
    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    reference_id = uuid4()
    first = _portfolio_layer(portfolio, owner_id, reference_id=reference_id, version=1)
    second = _portfolio_layer(portfolio, owner_id, reference_id=reference_id, version=2)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        first_model = persistence_mappers.configuration_layer_to_model(first)
        second_model = persistence_mappers.configuration_layer_to_model(second)
        session.add_all((first_model, second_model))
        session.commit()
        first_layer_id = first_model.layer_id
        second_layer_id = second_model.layer_id

    assert first_layer_id != second_layer_id


def test_configuration_layer_persistence_round_trips_typed_sparse_patch_and_hash() -> None:
    """A sparse patch must retain canonical types and the stable delete representation."""

    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    offset = timezone(timedelta(hours=8))
    patch = LayerPatchSchema.for_scope(
        ConfigurationLayerScope.PORTFOLIO,
        {
            "settings": {
                "threshold": Decimal("1.2300"),
                "observed_at": datetime(2026, 8, 1, 9, 30, tzinfo=offset),
            },
            "extensions": {
                "mode": ConfigurationStringEnum.VALUE,
                "obsolete": {"$delete": True},
            },
        },
    )
    layer = ConfigurationLayer(
        scope=ConfigurationLayerScope.PORTFOLIO,
        patch=patch,
        reference_id=uuid4(),
        version=1,
        content_hash=canonical_content_hash(
            patch.model_dump(mode="python", by_alias=True, exclude_unset=True)
        ),
        ownership=Ownership(Scope.PORTFOLIO, portfolio.portfolio_id),
        portfolio_owner_id=owner_id,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        model = persistence_mappers.configuration_layer_to_model(layer)
        session.add(model)
        session.commit()
        stored = session.get(persistence_models.ConfigurationLayerModel, model.layer_id)
        assert stored is not None
        restored = persistence_mappers.configuration_layer_from_model(stored)

    assert restored.content_hash == layer.content_hash
    restored_patch = restored.patch.model_dump(mode="python", by_alias=True, exclude_unset=True)
    assert restored_patch == layer.patch.model_dump(
        mode="python", by_alias=True, exclude_unset=True
    )
    assert restored_patch["extensions"] == {
        "mode": "configuration",
        "obsolete": {"$delete": True},
    }
    assert canonical_content_hash(restored_patch) == layer.content_hash


@pytest.mark.parametrize(
    "value", [ConfigurationIntegerEnum.VALUE, ConfigurationNonStringEnum.VALUE]
)
def test_configuration_payload_rejects_non_string_enums(value: object) -> None:
    """Only Enum values canonicalized as strings may enter persisted configuration."""

    codec = _load("app.persistence.configuration_payload")

    with pytest.raises(ValueError, match="Enum"):
        codec.encode_configuration_payload(value)


def _resolved_snapshot_with_nested_typed_payload() -> ResolvedConfigurationSnapshot:
    """Resolve a real snapshot so its payload contains nested MappingProxyType values."""

    offset = timezone(timedelta(hours=8))
    patch = LayerPatchSchema.for_scope(
        ConfigurationLayerScope.SYSTEM,
        {
            "configuration_name": "typed-contract",
            "settings": {
                "threshold": Decimal("1.2300"),
                "observed_at": datetime(2026, 8, 1, 9, 30, tzinfo=offset),
            },
            "rules": [],
            "keyed_items": [],
            "extensions": {
                "identifier": UUID("00000000-0000-0000-0000-000000000123"),
                "mode": ConfigurationStringEnum.VALUE,
            },
            "nullable_note": None,
        },
    )
    layer = ConfigurationLayer(
        scope=ConfigurationLayerScope.SYSTEM,
        patch=patch,
        reference_id=uuid4(),
        version=1,
        content_hash=canonical_content_hash(
            patch.model_dump(mode="python", by_alias=True, exclude_unset=True)
        ),
        ownership=Ownership(Scope.SYSTEM, None),
    )
    return ConfigurationResolver(maximum_runtime_ttl=timedelta(hours=1)).resolve(
        [layer],
        access_context=_context(uuid4()),
        created_by=uuid4(),
        created_at=NOW,
        now=NOW,
        run_deadline=NOW + timedelta(minutes=30),
    )


def test_configuration_snapshot_persistence_round_trips_resolver_payload_and_hash() -> None:
    """Snapshot JSON must reversibly preserve a resolved, deeply frozen payload."""

    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    records = _load("app.persistence.records")
    resolved_snapshot = _resolved_snapshot_with_nested_typed_payload()
    snapshot = records.PersistedConfigurationSnapshot(
        snapshot_id=uuid4(),
        resolved_snapshot=resolved_snapshot,
        config_version=1,
        target_ownership=Ownership(Scope.SYSTEM, None),
        target_reference_id=uuid4(),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        model = persistence_mappers.configuration_snapshot_to_model(snapshot)
        session.add(model)
        session.commit()
        stored = session.get(persistence_models.ConfigurationSnapshotModel, snapshot.snapshot_id)
        assert stored is not None
        restored = persistence_mappers.configuration_snapshot_from_model(stored)

    assert restored.resolved_snapshot.content_hash == resolved_snapshot.content_hash
    assert restored.resolved_snapshot.canonical_json == resolved_snapshot.canonical_json
    assert restored.resolved_snapshot.payload == resolved_snapshot.payload
    assert restored.resolved_snapshot.payload["extensions"]["mode"] == "configuration"
    assert (
        canonical_content_hash(restored.resolved_snapshot.payload) == resolved_snapshot.content_hash
    )


def test_configuration_snapshot_rejects_self_consistent_schema_invalid_payload() -> None:
    """Canonical hash agreement cannot bypass final resolved-schema validation."""

    persistence_codec = _load("app.persistence.configuration_payload")
    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    records = _load("app.persistence.records")
    snapshot = records.PersistedConfigurationSnapshot(
        snapshot_id=uuid4(),
        resolved_snapshot=_resolved_snapshot_with_nested_typed_payload(),
        config_version=1,
        target_ownership=Ownership(Scope.SYSTEM, None),
        target_reference_id=uuid4(),
    )
    invalid_payload = {"configuration_name": "missing-required-fields"}
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        model = persistence_mappers.configuration_snapshot_to_model(snapshot)
        model.merged_payload = persistence_codec.encode_configuration_payload(invalid_payload)
        model.canonical_json = canonical_json(invalid_payload)
        model.content_hash = canonical_content_hash(invalid_payload)
        session.add(model)
        session.commit()
        stored = session.get(persistence_models.ConfigurationSnapshotModel, snapshot.snapshot_id)
        assert stored is not None
        with pytest.raises(ValueError, match="resolved configuration payload"):
            persistence_mappers.configuration_snapshot_from_model(stored)


def test_configuration_snapshot_mapper_rejects_forged_snapshot_on_write() -> None:
    """Snapshot persistence must not trust a manually constructed immutable dataclass."""

    persistence_mappers = _load("app.persistence.mappers")
    records = _load("app.persistence.records")
    valid = _snapshot(parent_versions=())
    forged = ResolvedConfigurationSnapshot(
        payload=valid.payload,
        parent_versions=valid.parent_versions,
        created_by=valid.created_by,
        created_at=valid.created_at,
        runtime_expires_at=valid.runtime_expires_at,
        canonical_format_version=valid.canonical_format_version,
        content_hash="b" * 64,
        canonical_json=valid.canonical_json,
    )

    with pytest.raises(ValueError, match="content_hash"):
        persistence_mappers.configuration_snapshot_to_model(
            records.PersistedConfigurationSnapshot(
                snapshot_id=uuid4(),
                resolved_snapshot=forged,
                config_version=1,
                target_ownership=Ownership(Scope.SYSTEM, None),
                target_reference_id=uuid4(),
            )
        )


def test_sql_strategy_run_requires_matching_valid_configuration_snapshot() -> None:
    """A final run may reference only the exact, canonical snapshot stored for it."""

    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    persistence_repositories = _load("app.persistence.repositories")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    valid_snapshot = _persisted_snapshot(snapshot_id=uuid4())
    corrupt_snapshot = _persisted_snapshot(snapshot_id=uuid4())
    corrupt_model = persistence_mappers.configuration_snapshot_to_model(corrupt_snapshot)
    corrupt_model.canonical_json = "{}"
    valid_ref = ConfigurationSnapshotRef(
        valid_snapshot.snapshot_id,
        valid_snapshot.resolved_snapshot.content_hash,
        valid_snapshot.resolved_snapshot.created_at,
    )
    corrupt_ref = ConfigurationSnapshotRef(
        corrupt_snapshot.snapshot_id,
        corrupt_snapshot.resolved_snapshot.content_hash,
        corrupt_snapshot.resolved_snapshot.created_at,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                persistence_models.UserModel(user_id=owner_id, created_at=NOW),
                persistence_models.PortfolioModel(
                    portfolio_id=portfolio.portfolio_id, user_id=owner_id, created_at=NOW
                ),
                persistence_models.RotationPlanModel(
                    rotation_plan_id=plan.rotation_plan_id,
                    portfolio_id=portfolio.portfolio_id,
                    created_at=NOW,
                ),
                persistence_mappers.configuration_snapshot_to_model(valid_snapshot),
                corrupt_model,
            )
        )
        session.commit()
        repository = persistence_repositories.SqlAlchemyStrategyRunRepository(session)

        assert (
            repository.record_or_get(
                plan=plan,
                run=_run(plan, configuration_snapshot=valid_ref),
                idempotency_key=IdempotencyKey("valid-snapshot-reference"),
                access_context=_context(owner_id),
            ).configuration_snapshot
            == valid_ref
        )
        with pytest.raises(ValueError, match="configuration snapshot"):
            repository.record_or_get(
                plan=plan,
                run=_run(
                    plan,
                    configuration_snapshot=ConfigurationSnapshotRef(
                        valid_ref.snapshot_id,
                        "b" * 64,
                        valid_ref.created_at,
                    ),
                ),
                idempotency_key=IdempotencyKey("mismatched-snapshot-reference"),
                access_context=_context(owner_id),
            )
        with pytest.raises(ValueError, match="configuration snapshot"):
            repository.record_or_get(
                plan=plan,
                run=_run(
                    plan,
                    configuration_snapshot=ConfigurationSnapshotRef(uuid4(), "a" * 64, NOW),
                ),
                idempotency_key=IdempotencyKey("missing-snapshot-reference"),
                access_context=_context(owner_id),
            )
        with pytest.raises(ValueError, match="canonical_json"):
            repository.record_or_get(
                plan=plan,
                run=_run(plan, configuration_snapshot=corrupt_ref),
                idempotency_key=IdempotencyKey("corrupt-snapshot-reference"),
                access_context=_context(owner_id),
            )


def test_sql_strategy_run_snapshot_target_ownership_must_match_plan_portfolio() -> None:
    """Snapshot evidence cannot be reused across sibling portfolios of one user."""

    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    persistence_repositories = _load("app.persistence.repositories")
    owner_id = uuid4()
    first_portfolio = _portfolio(owner_id)
    second_portfolio = _portfolio(owner_id)
    plan = _plan(first_portfolio)
    system_snapshot = _persisted_snapshot(
        snapshot_id=uuid4(), target_ownership=Ownership(Scope.SYSTEM, None)
    )
    user_snapshot = _persisted_snapshot(
        snapshot_id=uuid4(), target_ownership=Ownership(Scope.USER, owner_id)
    )
    first_portfolio_snapshot = _persisted_snapshot(
        snapshot_id=uuid4(),
        target_ownership=Ownership(Scope.PORTFOLIO, first_portfolio.portfolio_id),
    )
    second_portfolio_snapshot = _persisted_snapshot(
        snapshot_id=uuid4(),
        target_ownership=Ownership(Scope.PORTFOLIO, second_portfolio.portfolio_id),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    def reference(snapshot: Any) -> ConfigurationSnapshotRef:
        return ConfigurationSnapshotRef(
            snapshot.snapshot_id,
            snapshot.resolved_snapshot.content_hash,
            snapshot.resolved_snapshot.created_at,
        )

    with Session(engine) as session:
        session.add_all(
            (
                persistence_models.UserModel(user_id=owner_id, created_at=NOW),
                persistence_models.PortfolioModel(
                    portfolio_id=first_portfolio.portfolio_id,
                    user_id=owner_id,
                    created_at=NOW,
                ),
                persistence_models.PortfolioModel(
                    portfolio_id=second_portfolio.portfolio_id,
                    user_id=owner_id,
                    created_at=NOW,
                ),
                persistence_models.RotationPlanModel(
                    rotation_plan_id=plan.rotation_plan_id,
                    portfolio_id=first_portfolio.portfolio_id,
                    created_at=NOW,
                ),
                persistence_mappers.configuration_snapshot_to_model(system_snapshot),
                persistence_mappers.configuration_snapshot_to_model(user_snapshot),
                persistence_mappers.configuration_snapshot_to_model(first_portfolio_snapshot),
                persistence_mappers.configuration_snapshot_to_model(second_portfolio_snapshot),
            )
        )
        session.commit()
        repository = persistence_repositories.SqlAlchemyStrategyRunRepository(session)

        for label, snapshot in (
            ("system", system_snapshot),
            ("user", user_snapshot),
            ("portfolio", first_portfolio_snapshot),
        ):
            snapshot_ref = reference(snapshot)
            assert (
                repository.record_or_get(
                    plan=plan,
                    run=_run(plan, configuration_snapshot=snapshot_ref),
                    idempotency_key=IdempotencyKey(f"snapshot-target-{label}"),
                    access_context=_context(owner_id),
                ).configuration_snapshot
                == snapshot_ref
            )

        with pytest.raises(ValueError, match="target ownership"):
            repository.record_or_get(
                plan=plan,
                run=_run(plan, configuration_snapshot=reference(second_portfolio_snapshot)),
                idempotency_key=IdempotencyKey("snapshot-target-sibling-portfolio"),
                access_context=_context(owner_id),
            )


def test_sql_rotation_plan_round_trip_restores_timezone_aware_created_at() -> None:
    """SQLite drops tzinfo, so rotation-plan mapping must restore it on reads."""

    persistence_models = _load("app.persistence.models")
    persistence_repositories = _load("app.persistence.repositories")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    candidate_group_id = uuid4()
    offset = timezone(timedelta(hours=8))
    plan = RotationPlan(
        rotation_plan_id=uuid4(),
        portfolio_id=portfolio.portfolio_id,
        candidate_group_ids=(candidate_group_id,),
        source_position_ids=(),
        protected_position_ids=(),
        created_at=datetime(2026, 8, 1, 9, tzinfo=offset),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                persistence_models.UserModel(user_id=owner_id, created_at=NOW),
                persistence_models.PortfolioModel(
                    portfolio_id=portfolio.portfolio_id, user_id=owner_id, created_at=NOW
                ),
                persistence_models.CandidateGroupModel(
                    candidate_group_id=candidate_group_id, user_id=owner_id, created_at=NOW
                ),
            )
        )
        repository = persistence_repositories.SqlAlchemyRotationPlanRepository(session)
        repository.add(plan, access_context=_context(owner_id))
        session.commit()
        restored = repository.get(plan.rotation_plan_id, access_context=_context(owner_id))

    assert restored == plan
    assert restored.created_at.tzinfo is not None


def test_in_memory_logical_scan_recovers_attempts_and_rejects_a_second_final_run() -> None:
    """Retries share one scan record, retain attempt evidence, and complete only once."""

    api = _api()
    logical_scans = _load("app.repositories.logical_scans")
    memory = _load("app.persistence.in_memory")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    repository = memory.InMemoryLogicalScanRepository({portfolio.portfolio_id: owner_id})
    strategy_runs = api["InMemoryStrategyRunRepository"](
        {portfolio.portfolio_id: owner_id}, logical_scan_repository=repository
    )
    request = api["ScanLockRequest"](
        plan=plan,
        market_session_date="2026-08-01",
        scan_window_start=NOW,
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        configuration_snapshot_hash="a" * 64,
    )

    scan = repository.create_or_recover(
        request=request, created_at=NOW, access_context=_context(owner_id)
    )
    assert (
        repository.create_or_recover(
            request=request, created_at=NOW, access_context=_context(owner_id)
        )
        == scan
    )
    first_attempt = logical_scans.ScanAttempt(
        scan_attempt_id=uuid4(),
        logical_scan_run_id=scan.logical_scan_run_id,
        attempt_number=1,
        status=logical_scans.ScanAttemptStatus.FAILED,
        configuration_snapshot_hash="a" * 64,
        market_data_snapshot_id=None,
        market_data_content_hash=None,
        trigger_correlation_id="contract:first",
        actor_correlation_id="worker:contract",
        started_at=NOW,
        completed_at=NOW + timedelta(minutes=1),
        failure_code="MARKET_TIMEOUT",
        failure_detail="provider did not produce a market snapshot",
    )
    retry_attempt = logical_scans.ScanAttempt(
        scan_attempt_id=uuid4(),
        logical_scan_run_id=scan.logical_scan_run_id,
        attempt_number=2,
        status=logical_scans.ScanAttemptStatus.SUCCEEDED,
        configuration_snapshot_hash="a" * 64,
        market_data_snapshot_id="retry-market-snapshot",
        market_data_content_hash="b" * 64,
        trigger_correlation_id="contract:retry",
        actor_correlation_id="worker:contract",
        started_at=NOW + timedelta(minutes=2),
        completed_at=NOW + timedelta(minutes=3),
        recovery_of_attempt_id=first_attempt.scan_attempt_id,
    )
    repository.record_attempt(attempt=first_attempt, access_context=_context(owner_id))
    repository.record_attempt(attempt=retry_attempt, access_context=_context(owner_id))
    assert repository.list_attempts(
        logical_scan_run_id=scan.logical_scan_run_id, access_context=_context(owner_id)
    ) == (first_attempt, retry_attempt)

    completed_run = strategy_runs.record_or_get(
        plan=plan,
        run=_run(plan),
        idempotency_key=IdempotencyKey("logical-scan-completion"),
        logical_scan_run_id=scan.logical_scan_run_id,
        access_context=_context(owner_id),
    )
    repository.attach_final_strategy_run(
        logical_scan_run_id=scan.logical_scan_run_id,
        plan=plan,
        strategy_run_id=completed_run.strategy_run_id,
        completed_at=NOW + timedelta(minutes=3),
        access_context=_context(owner_id),
    )
    recovered = repository.get_by_lock_key(request=request, access_context=_context(owner_id))
    assert recovered.status is logical_scans.LogicalScanStatus.COMPLETED
    assert recovered.final_strategy_run_id == completed_run.strategy_run_id
    with pytest.raises(ValueError, match="final strategy run"):
        strategy_runs.record_or_get(
            plan=plan,
            run=_run(plan),
            idempotency_key=IdempotencyKey("logical-scan-second-completion"),
            logical_scan_run_id=scan.logical_scan_run_id,
            access_context=_context(owner_id),
        )


def test_sql_logical_scan_repository_recovers_one_key_and_retains_attempts() -> None:
    """The SQL adapter supplies the same recoverable scan/attempt foundation contract."""

    api = _api()
    logical_scans = _load("app.repositories.logical_scans")
    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    persistence_repositories = _load("app.persistence.repositories")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    request = api["ScanLockRequest"](
        plan=plan,
        market_session_date="2026-08-01",
        scan_window_start=NOW,
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        configuration_snapshot_hash="a" * 64,
    )
    later_request = api["ScanLockRequest"](
        plan=plan,
        market_session_date="2026-08-01",
        scan_window_start=NOW + timedelta(minutes=3),
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        configuration_snapshot_hash="a" * 64,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                persistence_models.UserModel(user_id=owner_id, created_at=NOW),
                persistence_models.PortfolioModel(
                    portfolio_id=portfolio.portfolio_id, user_id=owner_id, created_at=NOW
                ),
                persistence_models.RotationPlanModel(
                    rotation_plan_id=plan.rotation_plan_id,
                    portfolio_id=portfolio.portfolio_id,
                    created_at=NOW,
                ),
            )
        )
        session.commit()
        repository = persistence_repositories.SqlAlchemyLogicalScanRepository(session)
        scan = repository.create_or_recover(
            request=request, created_at=NOW, access_context=_context(owner_id)
        )
        assert (
            repository.create_or_recover(
                request=request, created_at=NOW, access_context=_context(owner_id)
            )
            == scan
        )
        later_scan = repository.create_or_recover(
            request=later_request, created_at=NOW, access_context=_context(owner_id)
        )
        attempt = logical_scans.ScanAttempt(
            scan_attempt_id=uuid4(),
            logical_scan_run_id=scan.logical_scan_run_id,
            attempt_number=1,
            status=logical_scans.ScanAttemptStatus.SUCCEEDED,
            configuration_snapshot_hash="a" * 64,
            market_data_snapshot_id="sql-market-snapshot",
            market_data_content_hash="b" * 64,
            trigger_correlation_id="contract:sql",
            actor_correlation_id="worker:contract",
            started_at=NOW,
            completed_at=NOW + timedelta(minutes=1),
        )
        repository.record_attempt(attempt=attempt, access_context=_context(owner_id))
        session.commit()

        assert repository.list_attempts(
            logical_scan_run_id=scan.logical_scan_run_id, access_context=_context(owner_id)
        ) == (attempt,)

        snapshot = _persisted_snapshot(snapshot_id=uuid4())
        session.add(persistence_mappers.configuration_snapshot_to_model(snapshot))
        session.flush()
        snapshot_ref = ConfigurationSnapshotRef(
            snapshot.snapshot_id,
            snapshot.resolved_snapshot.content_hash,
            snapshot.resolved_snapshot.created_at,
        )
        strategy_runs = persistence_repositories.SqlAlchemyStrategyRunRepository(session)
        completed = strategy_runs.record_or_get(
            plan=plan,
            run=_run(plan, configuration_snapshot=snapshot_ref),
            idempotency_key=IdempotencyKey("recover-completed-logical-scan"),
            logical_scan_run_id=scan.logical_scan_run_id,
            access_context=_context(owner_id),
        )
        recovered_final = strategy_runs.record_or_get(
            plan=plan,
            run=completed,
            idempotency_key=IdempotencyKey("recover-completed-logical-scan"),
            logical_scan_run_id=scan.logical_scan_run_id,
            access_context=_context(owner_id),
        )
        with pytest.raises(ValueError, match="idempotency key"):
            strategy_runs.record_or_get(
                plan=plan,
                run=replace(completed, outputs={"score": Decimal("8.88"), "at": NOW}),
                idempotency_key=IdempotencyKey("recover-completed-logical-scan"),
                logical_scan_run_id=scan.logical_scan_run_id,
                access_context=_context(owner_id),
            )
        with pytest.raises(ValueError, match="idempotency key"):
            strategy_runs.record_or_get(
                plan=plan,
                run=completed,
                idempotency_key=IdempotencyKey("recover-completed-logical-scan"),
                logical_scan_run_id=later_scan.logical_scan_run_id,
                access_context=_context(owner_id),
            )

    assert recovered_final == completed


def test_sql_strategy_run_rejects_linking_a_logical_scan_from_another_plan() -> None:
    """The final-result relation cannot cross rotation-plan boundaries."""

    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    persistence_repositories = _load("app.persistence.repositories")
    owner_id = uuid4()
    first_portfolio = _portfolio(owner_id)
    second_portfolio = _portfolio(owner_id)
    first_plan = _plan(first_portfolio)
    second_plan = _plan(second_portfolio)
    snapshot = _persisted_snapshot(snapshot_id=uuid4())
    snapshot_ref = ConfigurationSnapshotRef(
        snapshot.snapshot_id,
        snapshot.resolved_snapshot.content_hash,
        snapshot.resolved_snapshot.created_at,
    )
    foreign_scan_id = uuid4()
    foreign_identity = _scan_identity(first_plan)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                persistence_models.UserModel(user_id=owner_id, created_at=NOW),
                persistence_models.PortfolioModel(
                    portfolio_id=first_portfolio.portfolio_id,
                    user_id=owner_id,
                    created_at=NOW,
                ),
                persistence_models.PortfolioModel(
                    portfolio_id=second_portfolio.portfolio_id,
                    user_id=owner_id,
                    created_at=NOW,
                ),
                persistence_models.RotationPlanModel(
                    rotation_plan_id=first_plan.rotation_plan_id,
                    portfolio_id=first_portfolio.portfolio_id,
                    created_at=NOW,
                ),
                persistence_models.RotationPlanModel(
                    rotation_plan_id=second_plan.rotation_plan_id,
                    portfolio_id=second_portfolio.portfolio_id,
                    created_at=NOW,
                ),
                persistence_mappers.configuration_snapshot_to_model(snapshot),
                persistence_models.LogicalScanRunModel(
                    logical_scan_run_id=foreign_scan_id,
                    rotation_plan_id=first_plan.rotation_plan_id,
                    market_session_date=foreign_identity.market_session_date.isoformat(),
                    scan_window_start=NOW,
                    scan_interval="PT3M",
                    market_timezone=foreign_identity.market_timezone,
                    configuration_snapshot_hash=foreign_identity.configuration_snapshot_hash,
                    scan_identity_format_version=foreign_identity.format_version,
                    scan_lock_key=foreign_identity.key,
                    status="RUNNING",
                    completed_at=None,
                    created_at=NOW,
                ),
            )
        )
        session.commit()

        with pytest.raises(ValueError, match="logical scan"):
            persistence_repositories.SqlAlchemyStrategyRunRepository(session).record_or_get(
                plan=second_plan,
                run=_run(second_plan, configuration_snapshot=snapshot_ref),
                idempotency_key=IdempotencyKey("foreign-logical-scan"),
                logical_scan_run_id=foreign_scan_id,
                access_context=_context(owner_id),
            )


def test_configuration_snapshots_allow_equal_payload_hashes_with_distinct_provenance() -> None:
    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    first_parent = ParentVersion(
        scope=ConfigurationLayerScope.SYSTEM,
        reference_id=uuid4(),
        version=1,
        content_hash="a" * 64,
        content_hash_format_version=CANONICAL_FORMAT_VERSION,
    )
    second_parent = ParentVersion(
        scope=ConfigurationLayerScope.SYSTEM,
        reference_id=uuid4(),
        version=2,
        content_hash="b" * 64,
        content_hash_format_version=CANONICAL_FORMAT_VERSION,
    )
    first = _persisted_snapshot(parent_versions=(first_parent,))
    second = _persisted_snapshot(parent_versions=(second_parent,))
    assert first.resolved_snapshot.content_hash == second.resolved_snapshot.content_hash
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                persistence_mappers.configuration_snapshot_to_model(first),
                persistence_mappers.configuration_snapshot_to_model(second),
            )
        )
        session.commit()


@pytest.mark.parametrize(
    ("field_name", "corrupt_value", "message"),
    [
        pytest.param("canonical_json", "{}", "canonical_json", id="canonical-json"),
        pytest.param("content_hash", "b" * 64, "content_hash", id="content-hash"),
    ],
)
def test_configuration_snapshot_mapper_rejects_corrupt_canonical_evidence(
    field_name: str, corrupt_value: str, message: str
) -> None:
    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    snapshot = _persisted_snapshot()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        model = persistence_mappers.configuration_snapshot_to_model(snapshot)
        setattr(model, field_name, corrupt_value)
        session.add(model)
        session.commit()
        stored = session.get(persistence_models.ConfigurationSnapshotModel, model.snapshot_id)
        assert stored is not None
        with pytest.raises(ValueError, match=message):
            persistence_mappers.configuration_snapshot_from_model(stored)


def test_configuration_snapshot_provenance_round_trips_portfolio_target_and_expiry() -> None:
    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    portfolio_id = uuid4()
    expiry = NOW + timedelta(minutes=15)
    snapshot = _persisted_snapshot(
        config_version=7,
        target_ownership=Ownership(Scope.PORTFOLIO, portfolio_id),
        runtime_expires_at=expiry,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(persistence_mappers.configuration_snapshot_to_model(snapshot))
        session.commit()
        stored = session.get(persistence_models.ConfigurationSnapshotModel, snapshot.snapshot_id)
        assert stored is not None
        restored = persistence_mappers.configuration_snapshot_from_model(stored)

    assert restored.config_version == 7
    assert restored.target_ownership == Ownership(Scope.PORTFOLIO, portfolio_id)
    assert restored.resolved_snapshot.runtime_expires_at == expiry


def test_configuration_snapshot_provenance_rejects_nonpositive_version() -> None:
    records = _load("app.persistence.records")

    with pytest.raises(ValueError, match="config_version"):
        records.PersistedConfigurationSnapshot(
            snapshot_id=uuid4(),
            resolved_snapshot=_snapshot(parent_versions=()),
            config_version=0,
            target_ownership=Ownership(Scope.SYSTEM, None),
            target_reference_id=uuid4(),
        )


@pytest.mark.parametrize(
    ("scope", "ownership_scope", "owner_id", "portfolio_owner_id"),
    [
        pytest.param("SYSTEM", "SYSTEM", uuid4(), None, id="system-has-owner"),
        pytest.param("USER", "SYSTEM", None, None, id="scope-mapping-invalid"),
        pytest.param("PORTFOLIO", "PORTFOLIO", uuid4(), None, id="portfolio-missing-owner"),
    ],
)
def test_configuration_layer_database_scope_constraints_reject_invalid_rows(
    scope: str, ownership_scope: str, owner_id: UUID | None, portfolio_owner_id: UUID | None
) -> None:
    persistence_models = _load("app.persistence.models")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            persistence_models.ConfigurationLayerModel(
                layer_id=uuid4(),
                scope=scope,
                reference_id=uuid4(),
                version=1,
                patch={},
                content_hash="a" * 64,
                content_hash_format_version=CANONICAL_FORMAT_VERSION,
                ownership_scope=ownership_scope,
                owner_id=owner_id,
                portfolio_owner_id=portfolio_owner_id,
                runtime_expires_at=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_logical_scan_allows_many_attempts_but_only_one_linked_final_strategy_run() -> None:
    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    snapshot = _persisted_snapshot(snapshot_id=uuid4())
    snapshot_ref = ConfigurationSnapshotRef(
        snapshot.snapshot_id,
        snapshot.resolved_snapshot.content_hash,
        snapshot.resolved_snapshot.created_at,
    )
    first = _run(plan, configuration_snapshot=snapshot_ref)
    second = _run(plan, configuration_snapshot=snapshot_ref)
    scan_id = uuid4()
    scan_identity = _scan_identity(plan)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                persistence_models.UserModel(user_id=owner_id, created_at=NOW),
                persistence_models.PortfolioModel(
                    portfolio_id=portfolio.portfolio_id, user_id=owner_id, created_at=NOW
                ),
                persistence_models.RotationPlanModel(
                    rotation_plan_id=plan.rotation_plan_id,
                    portfolio_id=portfolio.portfolio_id,
                    created_at=NOW,
                ),
                persistence_mappers.configuration_snapshot_to_model(snapshot),
                persistence_models.LogicalScanRunModel(
                    logical_scan_run_id=scan_id,
                    rotation_plan_id=plan.rotation_plan_id,
                    market_session_date=scan_identity.market_session_date.isoformat(),
                    scan_window_start=NOW,
                    scan_interval="PT3M",
                    market_timezone=scan_identity.market_timezone,
                    configuration_snapshot_hash=scan_identity.configuration_snapshot_hash,
                    scan_identity_format_version=scan_identity.format_version,
                    scan_lock_key=scan_identity.key,
                    status="COMPLETED",
                    completed_at=NOW,
                    created_at=NOW,
                ),
                persistence_models.ScanAttemptModel(
                    scan_attempt_id=uuid4(),
                    logical_scan_run_id=scan_id,
                    attempt_number=1,
                    status="FAILED",
                    configuration_snapshot_hash="a" * 64,
                    market_data_snapshot_id=None,
                    market_data_content_hash=None,
                    trigger_correlation_id="contract:first",
                    actor_correlation_id="worker:contract",
                    started_at=NOW,
                    completed_at=NOW,
                    failure_code="MARKET_TIMEOUT",
                    failure_detail="provider did not produce a market snapshot",
                    recovery_of_attempt_id=None,
                    duplicate_of_attempt_id=None,
                    final_strategy_run_id=None,
                ),
                persistence_models.ScanAttemptModel(
                    scan_attempt_id=uuid4(),
                    logical_scan_run_id=scan_id,
                    attempt_number=2,
                    status="DEGRADED",
                    configuration_snapshot_hash="a" * 64,
                    market_data_snapshot_id="retry-snapshot",
                    market_data_content_hash="b" * 64,
                    trigger_correlation_id="contract:retry",
                    actor_correlation_id="worker:contract",
                    started_at=NOW,
                    completed_at=NOW,
                    failure_code="MISSING_REQUIRED_FIELD",
                    failure_detail="provider omitted required volume evidence",
                    recovery_of_attempt_id=None,
                    duplicate_of_attempt_id=None,
                    final_strategy_run_id=None,
                ),
                persistence_mappers.strategy_run_to_model(
                    first, idempotency_key="logical-first", logical_scan_run_id=scan_id
                ),
            )
        )
        session.commit()
        session.add(
            persistence_mappers.strategy_run_to_model(
                second, idempotency_key="logical-second", logical_scan_run_id=scan_id
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_sql_position_integrity_errors_are_not_all_reclassified_as_open_duplicates() -> None:
    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    persistence_repositories = _load("app.persistence.repositories")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    instrument_id = uuid4()
    existing = _position(portfolio, instrument_id, closed=True)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                persistence_models.UserModel(user_id=owner_id, created_at=NOW),
                persistence_models.PortfolioModel(
                    portfolio_id=portfolio.portfolio_id, user_id=owner_id, created_at=NOW
                ),
                persistence_models.InstrumentModel(
                    instrument_id=instrument_id,
                    market="TWSE",
                    symbol="INTEGRITY",
                    created_at=NOW,
                ),
                persistence_mappers.position_to_model(existing),
            )
        )
        session.commit()
        duplicate_primary_key = _position(
            portfolio, instrument_id, position_id=existing.position_id
        )

        with pytest.raises(IntegrityError, match="UNIQUE constraint"):
            persistence_repositories.SqlAlchemyPositionRepository(session).add(
                duplicate_primary_key, access_context=_context(owner_id)
            )


def test_sql_position_open_conflict_preserves_unrelated_outer_pending_work() -> None:
    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    persistence_repositories = _load("app.persistence.repositories")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    instrument_id = uuid4()
    existing = _position(portfolio, instrument_id)
    outer_instrument_id = uuid4()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                persistence_models.UserModel(user_id=owner_id, created_at=NOW),
                persistence_models.PortfolioModel(
                    portfolio_id=portfolio.portfolio_id, user_id=owner_id, created_at=NOW
                ),
                persistence_models.InstrumentModel(
                    instrument_id=instrument_id,
                    market="TWSE",
                    symbol="EXISTING",
                    created_at=NOW,
                ),
                persistence_mappers.position_to_model(existing),
            )
        )
        session.commit()
        session.add(
            persistence_models.InstrumentModel(
                instrument_id=outer_instrument_id,
                market="TWSE",
                symbol="OUTER",
                created_at=NOW,
            )
        )

        with pytest.raises(PositionAlreadyOpenError):
            persistence_repositories.SqlAlchemyPositionRepository(session).add(
                _position(portfolio, instrument_id), access_context=_context(owner_id)
            )
        session.commit()
        assert session.get(persistence_models.InstrumentModel, outer_instrument_id) is not None


def test_sql_strategy_run_idempotency_conflict_preserves_outer_pending_work() -> None:
    persistence_mappers = _load("app.persistence.mappers")
    persistence_models = _load("app.persistence.models")
    persistence_repositories = _load("app.persistence.repositories")
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    persisted_snapshot = _persisted_snapshot(snapshot_id=uuid4())
    snapshot = ConfigurationSnapshotRef(
        persisted_snapshot.snapshot_id,
        persisted_snapshot.resolved_snapshot.content_hash,
        persisted_snapshot.resolved_snapshot.created_at,
    )
    first_run = StrategyRun(
        strategy_run_id=uuid4(),
        rotation_plan_id=plan.rotation_plan_id,
        portfolio_id=portfolio.portfolio_id,
        configuration_snapshot=snapshot,
        market_data_snapshot_id="market",
        state_transition="SCANNING->ACTION_PENDING",
        outputs={"result": "first"},
        occurred_at=NOW,
    )
    contender = replace(first_run, outputs={"result": "contender"})
    engine = create_engine("sqlite+pysqlite:///:memory:")
    persistence_models.Base.metadata.create_all(engine)

    with Session(engine) as seed_session:
        seed_session.add_all(
            (
                persistence_models.UserModel(user_id=owner_id, created_at=NOW),
                persistence_models.PortfolioModel(
                    portfolio_id=portfolio.portfolio_id, user_id=owner_id, created_at=NOW
                ),
                persistence_models.RotationPlanModel(
                    rotation_plan_id=plan.rotation_plan_id,
                    portfolio_id=portfolio.portfolio_id,
                    created_at=NOW,
                ),
                persistence_mappers.configuration_snapshot_to_model(persisted_snapshot),
                persistence_mappers.strategy_run_to_model(first_run, idempotency_key="race"),
            )
        )
        seed_session.commit()

    with (
        Session(engine) as normal_mismatch_session,
        pytest.raises(ValueError, match="idempotency key"),
    ):
        persistence_repositories.SqlAlchemyStrategyRunRepository(
            normal_mismatch_session
        ).record_or_get(
            plan=plan,
            run=contender,
            idempotency_key=IdempotencyKey("race"),
            access_context=_context(owner_id),
        )

    with Session(engine) as stale_race_session:
        outer_instrument_id = uuid4()
        stale_race_session.add(
            persistence_models.InstrumentModel(
                instrument_id=outer_instrument_id,
                market="TWSE",
                symbol="STRATEGY_OUTER",
                created_at=NOW,
            )
        )
        strategy_run_identity_key = inspect(
            persistence_models.StrategyRunModel
        ).identity_key_from_primary_key((first_run.strategy_run_id,))
        assert strategy_run_identity_key not in stale_race_session.identity_map

        original_scalar = stale_race_session.scalar
        hidden_first_lookup = True

        def stale_first_strategy_lookup(*args: object, **kwargs: object) -> object:
            nonlocal hidden_first_lookup
            if hidden_first_lookup and "strategy_runs" in str(args[0]):
                hidden_first_lookup = False
                return None
            return original_scalar(*args, **kwargs)  # type: ignore[call-overload]

        with warnings.catch_warnings(record=True) as captured_warnings:
            warnings.simplefilter("always", SAWarning)
            with patch.object(
                stale_race_session, "scalar", side_effect=stale_first_strategy_lookup
            ):
                result = persistence_repositories.SqlAlchemyStrategyRunRepository(
                    stale_race_session
                ).record_or_get(
                    plan=plan,
                    run=first_run,
                    idempotency_key=IdempotencyKey("race"),
                    access_context=_context(owner_id),
                )
        assert result == first_run
        assert hidden_first_lookup is False
        assert not [
            warning for warning in captured_warnings if issubclass(warning.category, SAWarning)
        ]
        stale_race_session.commit()
        assert (
            stale_race_session.get(persistence_models.InstrumentModel, outer_instrument_id)
            is not None
        )


def test_migration_metadata_covers_foundation_tables_and_open_position_partial_index() -> None:
    api = _api()
    metadata = api["Base"].metadata
    assert set(metadata.tables) == EXPECTED_FOUNDATION_TABLES
    positions = metadata.tables["positions"]
    assert any(
        index.unique and "OPEN" in str(index.dialect_options["postgresql"].get("where"))
        for index in positions.indexes
    )


def test_frozen_alembic_upgrade_creates_every_foundation_table(tmp_path: Path) -> None:
    database_path = tmp_path / "foundation.sqlite3"
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("script_location", str(Path("migrations").resolve()))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    with engine.connect() as connection:
        table_names = set(connection.dialect.get_table_names(connection))
    assert table_names == set(EXPECTED_FOUNDATION_TABLES) | {"alembic_version"}


def test_foundation_migration_is_revision_frozen_not_live_metadata() -> None:
    revision_source = Path("migrations/versions/0001_foundation.py").read_text(encoding="utf-8")

    assert "app.persistence.models" not in revision_source
    assert "metadata.create_all" not in revision_source
    assert "metadata.drop_all" not in revision_source
