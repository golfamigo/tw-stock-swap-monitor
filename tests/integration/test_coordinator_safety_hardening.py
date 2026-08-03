"""Safety regressions for durable coordinator state and authoritative evidence."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Never
from uuid import UUID

import pytest
from app.application.child_intents import ChildIntentPurpose
from app.application.configuration import (
    CANONICAL_FORMAT_VERSION,
    ResolvedConfigurationSnapshot,
    canonical_content_hash,
    canonical_json,
)
from app.application.configuration_snapshots import PersistedConfigurationSnapshot
from app.application.run_coordinator import (
    ConfigurationSnapshotValidationError,
    RunCoordinator,
    RunCoordinatorRequest,
    RunDisposition,
    TriggerSource,
)
from app.data_sources.models import (
    Bar,
    DataQuality,
    MarketDataRequest,
    MarketDataSnapshot,
    Quote,
)
from app.domain.access import AccessContext
from app.domain.entities import Instrument, Position, RotationPlan
from app.domain.enums import PositionRole, PositionStatus, Scope
from app.domain.values import ConfigurationSnapshotRef, InstrumentRef, Ownership, Quantity
from app.persistence.in_memory import (
    InMemoryChildIntentRepository,
    InMemoryConfigurationSnapshotRepository,
    InMemoryLockProvider,
    InMemoryLogicalScanRepository,
    InMemoryPositionRepository,
    InMemoryRecommendationStateRepository,
    InMemoryRotationPlanRepository,
    InMemoryStrategyRunRepository,
)
from app.persistence.mappers import scan_attempt_from_model, scan_attempt_to_model
from app.repositories.locks import LockLease, ScanLockRequest
from app.services.rotation_run import RotationEvaluation
from app.state_machine.machine import TransitionGuards
from app.state_machine.states import (
    DataOutcome,
    RecommendationEvent,
    RecommendationState,
    RuleOutcome,
    SizingOutcome,
)

NOW = datetime(2026, 8, 2, 1, tzinfo=UTC)
OWNER_ID = UUID("00000000-0000-0000-0000-000000001001")
PORTFOLIO_ID = UUID("00000000-0000-0000-0000-000000001002")
PLAN_ID = UUID("00000000-0000-0000-0000-000000001003")
GROUP_ID = UUID("00000000-0000-0000-0000-000000001004")
INSTRUMENT_ID = UUID("00000000-0000-0000-0000-000000001005")
CONFIGURATION_ID = UUID("00000000-0000-0000-0000-000000001006")
POSITION_ID = UUID("00000000-0000-0000-0000-000000001007")


def _context() -> AccessContext:
    return AccessContext(
        actor_user_id=OWNER_ID,
        request_id=UUID("00000000-0000-0000-0000-000000001008"),
        authentication_method="task-nine-hardening-test",
    )


def _plan(*, protected_position_ids: tuple[UUID, ...] = ()) -> RotationPlan:
    return RotationPlan(
        rotation_plan_id=PLAN_ID,
        portfolio_id=PORTFOLIO_ID,
        candidate_group_ids=(GROUP_ID,),
        source_position_ids=(),
        protected_position_ids=protected_position_ids,
        created_at=NOW,
    )


def _position(*, role: PositionRole = PositionRole.NORMAL) -> Position:
    return Position(
        position_id=POSITION_ID,
        portfolio_id=PORTFOLIO_ID,
        instrument=InstrumentRef(INSTRUMENT_ID),
        quantity=Quantity(Decimal("1")),
        role=role,
        status=PositionStatus.OPEN,
        opened_at=NOW,
    )


def _configuration(plan: RotationPlan) -> PersistedConfigurationSnapshot:
    payload = {
        "configuration_name": "task-nine-hardening",
        "settings": {},
        "rules": [],
        "keyed_items": [],
        "extensions": {},
        "nullable_note": None,
    }
    resolved = ResolvedConfigurationSnapshot(
        payload=payload,
        parent_versions=(),
        created_by=OWNER_ID,
        created_at=NOW,
        runtime_expires_at=None,
        canonical_format_version=CANONICAL_FORMAT_VERSION,
        content_hash=canonical_content_hash(payload),
        canonical_json=canonical_json(payload),
    )
    return PersistedConfigurationSnapshot(
        snapshot_id=CONFIGURATION_ID,
        resolved_snapshot=resolved,
        config_version=1,
        target_ownership=Ownership(Scope.PORTFOLIO, plan.portfolio_id),
        target_reference_id=plan.rotation_plan_id,
    )


def _configuration_ref(snapshot: PersistedConfigurationSnapshot) -> ConfigurationSnapshotRef:
    resolved = snapshot.resolved_snapshot
    return ConfigurationSnapshotRef(
        snapshot.snapshot_id, resolved.content_hash, resolved.created_at
    )


def _market_request() -> MarketDataRequest:
    instrument = Instrument(
        instrument_id=INSTRUMENT_ID,
        market="test-market",
        symbol="TEST",
        created_at=NOW,
    )
    return MarketDataRequest(
        request_id=UUID("00000000-0000-0000-0000-000000001009"),
        instruments=(instrument,),
        requested_at=NOW,
        intraday_start=NOW,
        intraday_end=NOW + timedelta(minutes=15),
        intraday_interval=timedelta(minutes=15),
        daily_start=date(2026, 8, 1),
        daily_end=date(2026, 8, 2),
        maximum_data_delay=timedelta(minutes=5),
    )


def _snapshot(*, stale: bool = False) -> MarketDataSnapshot:
    price = Decimal("10")
    request = _market_request()
    bar = Bar(
        instrument_id=INSTRUMENT_ID,
        starts_at=NOW,
        ends_at=NOW + timedelta(minutes=15),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("100"),
    )
    fetched_at = NOW + timedelta(minutes=1)
    return MarketDataSnapshot(
        provider="offline-hardening-provider",
        request=request,
        quotes=(Quote(instrument_id=INSTRUMENT_ID, as_of=NOW, price=price),),
        intraday_bars=(bar,),
        daily_bars=(bar,),
        quality=DataQuality(
            provider="offline-hardening-provider",
            fetched_at=fetched_at,
            source_timestamp=NOW,
            delay=fetched_at - NOW,
            stale=stale,
            missing_fields=(),
            bars_complete=True,
            anomalies=(),
            confidence=Decimal("1"),
        ),
    )


class _Provider:
    provider_name = "offline-hardening-provider"

    def __init__(self, responses: list[MarketDataSnapshot]) -> None:
        self._responses = deque(responses)
        self.calls = 0

    def get_market_snapshot(self, request: MarketDataRequest) -> MarketDataSnapshot:
        del request
        self.calls += 1
        return self._responses.popleft()

    def get_quotes(self, request: MarketDataRequest) -> Never:
        del request
        raise AssertionError("coordinator must use a complete snapshot")

    def get_intraday_bars(self, request: MarketDataRequest) -> Never:
        del request
        raise AssertionError("coordinator must use a complete snapshot")

    def get_daily_bars(self, request: MarketDataRequest) -> Never:
        del request
        raise AssertionError("coordinator must use a complete snapshot")


class _RecordingLock(InMemoryLockProvider):
    def __init__(self, events: list[str]) -> None:
        super().__init__({PORTFOLIO_ID: OWNER_ID})
        self._events = events
        self.calls = 0

    def acquire_scan_lock(
        self, request: ScanLockRequest, *, access_context: AccessContext
    ) -> LockLease | None:
        self.calls += 1
        self._events.append("lock")
        return super().acquire_scan_lock(request, access_context=access_context)


class _RecordingScans(InMemoryLogicalScanRepository):
    def __init__(self, events: list[str]) -> None:
        super().__init__({PORTFOLIO_ID: OWNER_ID})
        self._events = events

    def get_by_lock_key(self, *, request: ScanLockRequest, access_context: AccessContext) -> object:
        self._events.append("scan")
        return super().get_by_lock_key(request=request, access_context=access_context)


def _uuid_factory() -> Callable[[], UUID]:
    value = 1010

    def next_uuid() -> UUID:
        nonlocal value
        result = UUID(f"00000000-0000-0000-0000-{value:012d}")
        value += 1
        return result

    return next_uuid


@dataclass(slots=True)
class _Parts:
    coordinator: RunCoordinator
    child_intents: InMemoryChildIntentRepository
    configuration: PersistedConfigurationSnapshot
    logical_scans: InMemoryLogicalScanRepository
    recommendation_states: InMemoryRecommendationStateRepository
    locks: _RecordingLock
    provider: _Provider


def _parts(
    *,
    evaluation: RotationEvaluation,
    plan: RotationPlan | None = None,
    positions: tuple[Position, ...] = (),
    snapshot: MarketDataSnapshot | None = None,
    configuration: PersistedConfigurationSnapshot | None = None,
    logical_scans: InMemoryLogicalScanRepository | None = None,
    recommendation_states: InMemoryRecommendationStateRepository | None = None,
    locks: _RecordingLock | None = None,
) -> _Parts:
    selected_plan = plan or _plan()
    selected_snapshot = snapshot or _snapshot()
    position_portfolios = {position.position_id: position.portfolio_id for position in positions}
    plans = InMemoryRotationPlanRepository(
        {PORTFOLIO_ID: OWNER_ID},
        candidate_group_owners={GROUP_ID: OWNER_ID},
        position_portfolios=position_portfolios,
    )
    plans.add(selected_plan, access_context=_context())
    position_repository = InMemoryPositionRepository({PORTFOLIO_ID: OWNER_ID})
    for position in positions:
        position_repository.add(position, access_context=_context())
    stored_configuration = configuration or _configuration(selected_plan)
    configurations = InMemoryConfigurationSnapshotRepository({PORTFOLIO_ID: OWNER_ID})
    configurations.record_or_get(snapshot=stored_configuration, access_context=_context())
    scans = logical_scans or InMemoryLogicalScanRepository({PORTFOLIO_ID: OWNER_ID})
    states = recommendation_states or InMemoryRecommendationStateRepository(
        {PORTFOLIO_ID: OWNER_ID}
    )
    intents = InMemoryChildIntentRepository({PORTFOLIO_ID: OWNER_ID})
    used_locks = locks or _RecordingLock([])
    provider = _Provider([selected_snapshot])
    coordinator = RunCoordinator(
        rotation_plans=plans,
        configuration_snapshots=configurations,
        positions=position_repository,
        recommendation_states=states,
        child_intents=intents,
        logical_scans=scans,
        strategy_runs=InMemoryStrategyRunRepository(
            {PORTFOLIO_ID: OWNER_ID}, logical_scan_repository=scans
        ),
        locks=used_locks,
        market_data=provider,
        evaluator=lambda *_: evaluation,
        now=lambda: NOW,
        new_uuid=_uuid_factory(),
    )
    return _Parts(coordinator, intents, stored_configuration, scans, states, used_locks, provider)


def _request(
    configuration: PersistedConfigurationSnapshot,
    *,
    plan: RotationPlan | None = None,
) -> RunCoordinatorRequest:
    return RunCoordinatorRequest(
        plan=plan or _plan(),
        configuration_snapshot=_configuration_ref(configuration),
        market_session_date="2026-08-02",
        scan_window_start=NOW + timedelta(hours=8),
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        market_data_request=_market_request(),
        access_context=_context(),
        trigger_source=TriggerSource.API,
        correlation_id="hardening-correlation",
    )


def _seed_state(
    states: InMemoryRecommendationStateRepository,
    plan: RotationPlan,
    state: RecommendationState,
    *,
    halted: bool = False,
) -> None:
    initial = states.get_or_create(plan=plan, created_at=NOW, access_context=_context())
    states.record_transition(
        plan=plan,
        previous=initial,
        next_state=state,
        remaining_stages_halted=halted,
        changed_at=NOW,
        access_context=_context(),
    )


@pytest.mark.parametrize(
    ("role", "plan_lists_position"),
    [
        (PositionRole.PROTECTED_CORE, False),
        (PositionRole.NORMAL, True),
    ],
)
def test_coordinator_rejects_authoritative_protected_sale_sources(
    role: PositionRole, plan_lists_position: bool
) -> None:
    position = _position(role=role)
    plan = _plan(protected_position_ids=(position.position_id,) if plan_lists_position else ())
    evaluation = RotationEvaluation(
        event=RecommendationEvent.ACTION_SIGNAL,
        guards=TransitionGuards(rule_outcome=RuleOutcome.PASSED),
        outputs={"attempt": "protected"},
        sale_source_position_id=position.position_id,
    )
    parts = _parts(evaluation=evaluation, plan=plan, positions=(position,))
    _seed_state(parts.recommendation_states, plan, RecommendationState.NEAR_TRIGGER)

    outcome = parts.coordinator.run(_request(parts.configuration, plan=plan))

    assert outcome.disposition is RunDisposition.FAILED
    assert outcome.strategy_run is None
    assert parts.recommendation_states.get(
        plan.rotation_plan_id, access_context=_context()
    ).state is (RecommendationState.NEAR_TRIGGER)


def test_recreated_coordinator_uses_durable_state_not_evaluator_controlled_state() -> None:
    position = _position()
    plan = _plan()
    confirmation = RotationEvaluation(
        event=RecommendationEvent.EXECUTION_CONFIRMED,
        guards=TransitionGuards(confirmation_is_valid=True),
        outputs={"attempt": "late-confirmation"},
    )
    parts = _parts(evaluation=confirmation, plan=plan, positions=(position,))
    _seed_state(parts.recommendation_states, plan, RecommendationState.INVALIDATED)

    confirmed = parts.coordinator.run(_request(parts.configuration, plan=plan))

    assert confirmed.disposition is RunDisposition.COMPLETED
    persisted = parts.recommendation_states.get(plan.rotation_plan_id, access_context=_context())
    assert persisted.state is RecommendationState.WAITING_CONFIRMATION
    assert persisted.remaining_stages_halted

    bypass = RotationEvaluation(
        event=RecommendationEvent.ACTION_SIGNAL,
        guards=TransitionGuards(rule_outcome=RuleOutcome.PASSED),
        outputs={"attempt": "must-not-bypass"},
        sale_source_position_id=position.position_id,
    )
    recreated = _parts(
        evaluation=bypass,
        plan=plan,
        positions=(position,),
        recommendation_states=parts.recommendation_states,
    )

    rejected = recreated.coordinator.run(_request(recreated.configuration, plan=plan))

    assert rejected.disposition is RunDisposition.FAILED
    assert (
        parts.recommendation_states.get(plan.rotation_plan_id, access_context=_context()).state
        is RecommendationState.WAITING_CONFIRMATION
    )


def test_partial_halt_persists_across_recreation_and_requires_authorized_fresh_resume() -> None:
    plan = _plan()
    degraded = RotationEvaluation(
        event=RecommendationEvent.DATA_DEGRADED,
        guards=TransitionGuards(data_outcome=DataOutcome.DEGRADED),
        outputs={"attempt": "degraded"},
    )
    parts = _parts(evaluation=degraded, plan=plan)
    _seed_state(parts.recommendation_states, plan, RecommendationState.PARTIALLY_EXECUTED)

    degraded_outcome = parts.coordinator.run(_request(parts.configuration, plan=plan))

    assert degraded_outcome.disposition is RunDisposition.COMPLETED
    halted = parts.recommendation_states.get(plan.rotation_plan_id, access_context=_context())
    assert halted.state is RecommendationState.WAITING_CONFIRMATION
    assert halted.remaining_stages_halted

    denied_resume = RotationEvaluation(
        event=RecommendationEvent.RESUME_REMAINING_STAGES,
        guards=TransitionGuards(rule_outcome=RuleOutcome.FRESH_FULL_EVALUATION),
        outputs={"attempt": "denied-resume"},
    )
    recreated = _parts(
        evaluation=denied_resume,
        plan=plan,
        recommendation_states=parts.recommendation_states,
    )

    denied = recreated.coordinator.run(_request(recreated.configuration, plan=plan))

    assert denied.disposition is RunDisposition.FAILED
    assert parts.recommendation_states.get(
        plan.rotation_plan_id, access_context=_context()
    ).remaining_stages_halted


def test_scan_repository_and_provider_are_not_touched_before_lock_acquisition() -> None:
    events: list[str] = []
    scans = _RecordingScans(events)
    locks = _RecordingLock(events)
    activation = RotationEvaluation(
        event=RecommendationEvent.PLAN_ACTIVATED,
        guards=TransitionGuards(plan_is_valid=True),
        outputs={"attempt": "ordering"},
    )
    parts = _parts(evaluation=activation, logical_scans=scans, locks=locks)

    parts.coordinator.run(_request(parts.configuration))

    assert events.index("lock") < events.index("scan")
    assert parts.provider.calls == 1


def test_attempt_evidence_retains_configuration_reference_and_final_identity() -> None:
    activation = RotationEvaluation(
        event=RecommendationEvent.PLAN_ACTIVATED,
        guards=TransitionGuards(plan_is_valid=True),
        outputs={"attempt": "audit"},
    )
    parts = _parts(evaluation=activation, snapshot=_snapshot(stale=True))

    outcome = parts.coordinator.run(_request(parts.configuration))
    assert outcome.disposition is RunDisposition.DEGRADED
    assert outcome.logical_scan_run_id is not None
    terminal = parts.logical_scans.list_attempts(
        logical_scan_run_id=outcome.logical_scan_run_id, access_context=_context()
    )[-1]

    assert terminal.configuration_snapshot == _configuration_ref(parts.configuration)
    assert terminal.final_strategy_key == outcome.final_strategy_key
    assert terminal.final_strategy_identity_format_version == "strategy-run:v1"
    assert scan_attempt_from_model(scan_attempt_to_model(terminal)) == terminal


def test_action_notified_records_one_deterministic_child_intent_without_delivery() -> None:
    position = _position()
    plan = _plan()
    notification = RotationEvaluation(
        event=RecommendationEvent.DECISION_VALIDATED,
        guards=TransitionGuards(sizing_outcome=SizingOutcome.PASSED),
        outputs={"attempt": "notification"},
        sale_source_position_id=position.position_id,
    )
    parts = _parts(evaluation=notification, plan=plan, positions=(position,))
    _seed_state(parts.recommendation_states, plan, RecommendationState.ACTION_PENDING)

    first = parts.coordinator.run(_request(parts.configuration, plan=plan))
    second = parts.coordinator.run(_request(parts.configuration, plan=plan))

    assert first.disposition is RunDisposition.COMPLETED
    assert second.strategy_run == first.strategy_run
    intent_key = first.audit["notification_intent_key"]
    assert isinstance(intent_key, str)
    intent = parts.child_intents.get(intent_key=intent_key, access_context=_context())
    assert intent.purpose is ChildIntentPurpose.NOTIFICATION
    assert parts.child_intents.list_for_strategy_run(
        strategy_run_id=first.strategy_run.strategy_run_id, access_context=_context()
    ) == (intent,)
    assert "destination" not in first.audit


@pytest.mark.parametrize("unknown", [True, False])
def test_invalid_configuration_reference_fails_before_lock_or_provider(unknown: bool) -> None:
    activation = RotationEvaluation(
        event=RecommendationEvent.PLAN_ACTIVATED,
        guards=TransitionGuards(plan_is_valid=True),
        outputs={"attempt": "configuration"},
    )
    parts = _parts(evaluation=activation)
    reference = _configuration_ref(parts.configuration)
    invalid_reference = ConfigurationSnapshotRef(
        UUID("00000000-0000-0000-0000-000000001099") if unknown else reference.snapshot_id,
        reference.content_hash if unknown else "0" * 64,
        reference.created_at,
    )
    request = RunCoordinatorRequest(
        plan=_plan(),
        configuration_snapshot=invalid_reference,
        market_session_date="2026-08-02",
        scan_window_start=NOW + timedelta(hours=8),
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        market_data_request=_market_request(),
        access_context=_context(),
        trigger_source=TriggerSource.API,
        correlation_id="invalid-configuration",
    )

    with pytest.raises(ConfigurationSnapshotValidationError):
        parts.coordinator.run(request)

    assert parts.locks.calls == 0
    assert parts.provider.calls == 0


def test_configuration_bound_to_a_different_plan_fails_before_lock_or_provider() -> None:
    activation = RotationEvaluation(
        event=RecommendationEvent.PLAN_ACTIVATED,
        guards=TransitionGuards(plan_is_valid=True),
        outputs={"attempt": "wrong-configuration-plan"},
    )
    wrong_plan_configuration = replace(
        _configuration(_plan()),
        target_reference_id=UUID("00000000-0000-0000-0000-000000001098"),
    )
    parts = _parts(evaluation=activation, configuration=wrong_plan_configuration)

    with pytest.raises(ConfigurationSnapshotValidationError):
        parts.coordinator.run(_request(parts.configuration))

    assert parts.locks.calls == 0
    assert parts.provider.calls == 0
