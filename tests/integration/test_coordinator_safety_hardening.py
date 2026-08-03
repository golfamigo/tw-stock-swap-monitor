"""Safety regressions for durable coordinator state and authoritative evidence."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Event, Thread
from typing import Never
from uuid import UUID

import pytest
from app.application.child_intents import ChildIntentPurpose, build_child_intent
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
    RunCoordinatorResult,
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
from app.domain.enums import LogicalScanStatus, PositionRole, PositionStatus, Scope
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
    RecommendationStateRecord,
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


def _configuration(
    plan: RotationPlan,
    *,
    snapshot_id: UUID = CONFIGURATION_ID,
    created_at: datetime = NOW,
    config_version: int = 1,
) -> PersistedConfigurationSnapshot:
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
        created_at=created_at,
        runtime_expires_at=None,
        canonical_format_version=CANONICAL_FORMAT_VERSION,
        content_hash=canonical_content_hash(payload),
        canonical_json=canonical_json(payload),
    )
    return PersistedConfigurationSnapshot(
        snapshot_id=snapshot_id,
        resolved_snapshot=resolved,
        config_version=config_version,
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


class _FailOnceChildIntents(InMemoryChildIntentRepository):
    def __init__(self) -> None:
        super().__init__({PORTFOLIO_ID: OWNER_ID})
        self._remaining_failures = 1

    def record_or_get(self, *, intent: object, access_context: AccessContext) -> object:
        if self._remaining_failures:
            self._remaining_failures -= 1
            raise RuntimeError("injected child intent persistence failure")
        return super().record_or_get(intent=intent, access_context=access_context)


class _FailOnceAttachScans(InMemoryLogicalScanRepository):
    def __init__(self) -> None:
        super().__init__({PORTFOLIO_ID: OWNER_ID})
        self._remaining_failures = 1

    def attach_final_strategy_run(self, **kwargs: object) -> object:
        if self._remaining_failures:
            self._remaining_failures -= 1
            raise RuntimeError("injected logical scan finalization failure")
        return super().attach_final_strategy_run(**kwargs)  # type: ignore[arg-type]


class _PauseFirstChildIntentWrite(InMemoryChildIntentRepository):
    def __init__(self) -> None:
        super().__init__({PORTFOLIO_ID: OWNER_ID})
        self.first_write_started = Event()
        self.release_first_write = Event()
        self._writes = 0

    def record_or_get(self, *, intent: object, access_context: AccessContext) -> object:
        self._writes += 1
        if self._writes == 1:
            self.first_write_started.set()
            assert self.release_first_write.wait(timeout=1)
        return super().record_or_get(intent=intent, access_context=access_context)


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
    configuration_snapshots: InMemoryConfigurationSnapshotRepository
    logical_scans: InMemoryLogicalScanRepository
    recommendation_states: InMemoryRecommendationStateRepository
    strategy_runs: InMemoryStrategyRunRepository
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
    child_intents: InMemoryChildIntentRepository | None = None,
    locks: _RecordingLock | None = None,
    provider: _Provider | None = None,
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
    intents = child_intents or InMemoryChildIntentRepository({PORTFOLIO_ID: OWNER_ID})
    used_locks = locks or _RecordingLock([])
    used_provider = provider or _Provider([selected_snapshot])
    strategy_runs = InMemoryStrategyRunRepository(
        {PORTFOLIO_ID: OWNER_ID},
        logical_scan_repository=scans,
        child_intent_repository=intents,
    )
    coordinator = RunCoordinator(
        rotation_plans=plans,
        configuration_snapshots=configurations,
        positions=position_repository,
        recommendation_states=states,
        child_intents=intents,
        logical_scans=scans,
        strategy_runs=strategy_runs,
        locks=used_locks,
        market_data=used_provider,
        evaluator=lambda *_: evaluation,
        now=lambda: NOW,
        new_uuid=_uuid_factory(),
    )
    return _Parts(
        coordinator,
        intents,
        stored_configuration,
        configurations,
        scans,
        states,
        strategy_runs,
        used_locks,
        used_provider,
    )


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


@pytest.mark.parametrize(
    ("initial_state", "expected_state", "expected_halted"),
    [
        (RecommendationState.ACTION_PENDING, RecommendationState.DATA_DEGRADED, False),
        (RecommendationState.ACTION_NOTIFIED, RecommendationState.DATA_DEGRADED, False),
        (
            RecommendationState.PARTIALLY_EXECUTED,
            RecommendationState.WAITING_CONFIRMATION,
            True,
        ),
    ],
)
def test_non_actionable_data_persists_the_required_degraded_state_transition(
    initial_state: RecommendationState,
    expected_state: RecommendationState,
    expected_halted: bool,
) -> None:
    plan = _plan()
    parts = _parts(
        evaluation=RotationEvaluation(
            event=RecommendationEvent.PLAN_ACTIVATED,
            guards=TransitionGuards(plan_is_valid=True),
            outputs={"attempt": "not-used-for-stale-data"},
        ),
        plan=plan,
        snapshot=_snapshot(stale=True),
    )
    _seed_state(parts.recommendation_states, plan, initial_state)

    outcome = parts.coordinator.run(_request(parts.configuration, plan=plan))

    assert outcome.disposition is RunDisposition.DEGRADED
    persisted = parts.recommendation_states.get(plan.rotation_plan_id, access_context=_context())
    assert persisted.state is expected_state
    assert persisted.remaining_stages_halted is expected_halted


def test_degraded_recommendation_requires_fresh_recovery_evaluation() -> None:
    plan = _plan()
    parts = _parts(
        evaluation=RotationEvaluation(
            event=RecommendationEvent.PLAN_ACTIVATED,
            guards=TransitionGuards(plan_is_valid=True),
            outputs={"attempt": "not-used-for-stale-data"},
        ),
        plan=plan,
        snapshot=_snapshot(stale=True),
    )
    _seed_state(parts.recommendation_states, plan, RecommendationState.ACTION_PENDING)
    parts.coordinator.run(_request(parts.configuration, plan=plan))

    recovered = _parts(
        evaluation=RotationEvaluation(
            event=RecommendationEvent.DATA_RECOVERED,
            guards=TransitionGuards(data_outcome=DataOutcome.FRESH_FULL_EVALUATION),
            outputs={"attempt": "fresh-recovery"},
        ),
        plan=plan,
        recommendation_states=parts.recommendation_states,
    )

    outcome = recovered.coordinator.run(_request(recovered.configuration, plan=plan))

    assert outcome.disposition is RunDisposition.COMPLETED
    assert (
        parts.recommendation_states.get(plan.rotation_plan_id, access_context=_context()).state
        is RecommendationState.WATCHING
    )


def test_finalization_failure_keeps_scan_recoverable_until_state_and_intent_are_persisted() -> None:
    position = _position()
    plan = _plan()
    intents = _FailOnceChildIntents()
    parts = _parts(
        evaluation=RotationEvaluation(
            event=RecommendationEvent.DECISION_VALIDATED,
            guards=TransitionGuards(sizing_outcome=SizingOutcome.PASSED),
            outputs={"attempt": "recover-child-intent"},
            sale_source_position_id=position.position_id,
        ),
        plan=plan,
        positions=(position,),
        child_intents=intents,
    )
    _seed_state(parts.recommendation_states, plan, RecommendationState.ACTION_PENDING)
    request = _request(parts.configuration, plan=plan)

    failed = parts.coordinator.run(request)

    assert failed.disposition is RunDisposition.FAILED
    assert failed.logical_scan_run_id is not None
    assert (
        parts.logical_scans.get(failed.logical_scan_run_id, access_context=_context()).status
        is not LogicalScanStatus.COMPLETED
    )
    assert (
        parts.recommendation_states.get(plan.rotation_plan_id, access_context=_context()).state
        is RecommendationState.ACTION_NOTIFIED
    )
    assert (
        parts.child_intents.list_for_strategy_run(
            strategy_run_id=parts.strategy_runs.get_for_logical_scan(
                logical_scan_run_id=failed.logical_scan_run_id,
                access_context=_context(),
            ).strategy_run_id,
            access_context=_context(),
        )
        == ()
    )

    recovered = parts.coordinator.run(request)

    assert recovered.disposition is RunDisposition.COMPLETED
    assert parts.provider.calls == 1
    assert recovered.strategy_run is not None
    assert parts.child_intents.list_for_strategy_run(
        strategy_run_id=recovered.strategy_run.strategy_run_id, access_context=_context()
    )
    assert (
        parts.logical_scans.get(recovered.logical_scan_run_id, access_context=_context()).status
        is LogicalScanStatus.COMPLETED
    )


def test_success_evidence_is_durable_before_scan_completion_and_attach_recovery() -> None:
    position = _position()
    plan = _plan()
    scans = _FailOnceAttachScans()
    parts = _parts(
        evaluation=RotationEvaluation(
            event=RecommendationEvent.DECISION_VALIDATED,
            guards=TransitionGuards(sizing_outcome=SizingOutcome.PASSED),
            outputs={"attempt": "recover-attach"},
            sale_source_position_id=position.position_id,
        ),
        plan=plan,
        positions=(position,),
        logical_scans=scans,
    )
    _seed_state(parts.recommendation_states, plan, RecommendationState.ACTION_PENDING)
    request = _request(parts.configuration, plan=plan)

    pending = parts.coordinator.run(request)

    assert pending.disposition is RunDisposition.FAILED
    assert pending.logical_scan_run_id is not None
    candidate = parts.strategy_runs.get_for_logical_scan(
        logical_scan_run_id=pending.logical_scan_run_id, access_context=_context()
    )
    assert candidate is not None
    pending_attempts = parts.logical_scans.list_attempts(
        logical_scan_run_id=pending.logical_scan_run_id, access_context=_context()
    )
    assert any(
        attempt.status.value == "SUCCEEDED"
        and attempt.final_strategy_run_id == candidate.strategy_run_id
        for attempt in pending_attempts
    )
    assert (
        parts.logical_scans.get(pending.logical_scan_run_id, access_context=_context()).status
        is LogicalScanStatus.RUNNING
    )

    recovered = parts.coordinator.run(request)

    assert recovered.disposition is RunDisposition.COMPLETED
    completed_scan = parts.logical_scans.get(pending.logical_scan_run_id, access_context=_context())
    assert completed_scan.status is LogicalScanStatus.COMPLETED
    completed_attempts = parts.logical_scans.list_attempts(
        logical_scan_run_id=pending.logical_scan_run_id, access_context=_context()
    )
    final_successes = [
        attempt
        for attempt in completed_attempts
        if (
            attempt.status.value == "SUCCEEDED"
            and attempt.final_strategy_run_id == candidate.strategy_run_id
        )
    ]
    assert len(final_successes) == 1


def test_pending_finalization_uses_the_stored_run_configuration_not_a_new_request_reference() -> (
    None
):
    position = _position()
    plan = _plan()
    first_configuration = _configuration(plan)
    scans = _FailOnceAttachScans()
    parts = _parts(
        evaluation=RotationEvaluation(
            event=RecommendationEvent.DECISION_VALIDATED,
            guards=TransitionGuards(sizing_outcome=SizingOutcome.PASSED),
            outputs={"attempt": "recover-config-reference"},
            sale_source_position_id=position.position_id,
        ),
        plan=plan,
        positions=(position,),
        configuration=first_configuration,
        logical_scans=scans,
    )
    _seed_state(parts.recommendation_states, plan, RecommendationState.ACTION_PENDING)
    first_request = _request(first_configuration, plan=plan)

    pending = parts.coordinator.run(first_request)

    assert pending.disposition is RunDisposition.FAILED
    assert pending.logical_scan_run_id is not None
    stored_run = parts.strategy_runs.get_for_logical_scan(
        logical_scan_run_id=pending.logical_scan_run_id, access_context=_context()
    )
    assert stored_run is not None
    second_configuration = _configuration(
        plan,
        snapshot_id=UUID("00000000-0000-0000-0000-000000001099"),
        created_at=NOW + timedelta(days=1),
        config_version=2,
    )
    assert (
        second_configuration.resolved_snapshot.content_hash
        == first_configuration.resolved_snapshot.content_hash
    )
    parts.configuration_snapshots.record_or_get(
        snapshot=second_configuration, access_context=_context()
    )

    recovered = parts.coordinator.run(
        replace(
            first_request,
            configuration_snapshot=_configuration_ref(second_configuration),
        )
    )

    assert recovered.disposition is RunDisposition.COMPLETED
    terminal = parts.logical_scans.list_attempts(
        logical_scan_run_id=pending.logical_scan_run_id, access_context=_context()
    )[-1]
    assert terminal.status.value == "SUCCEEDED"
    assert terminal.configuration_snapshot == stored_run.configuration_snapshot
    assert terminal.configuration_snapshot != _configuration_ref(second_configuration)


def test_concurrent_scan_candidates_finalize_the_fenced_loser_without_another_intent() -> None:
    position = _position()
    plan = _plan()
    intents = _PauseFirstChildIntentWrite()
    parts = _parts(
        evaluation=RotationEvaluation(
            event=RecommendationEvent.DECISION_VALIDATED,
            guards=TransitionGuards(sizing_outcome=SizingOutcome.PASSED),
            outputs={"attempt": "state-fence"},
            sale_source_position_id=position.position_id,
        ),
        plan=plan,
        positions=(position,),
        child_intents=intents,
        provider=_Provider([_snapshot(), _snapshot()]),
    )
    _seed_state(parts.recommendation_states, plan, RecommendationState.ACTION_PENDING)
    first_request = _request(parts.configuration, plan=plan)
    second_request = replace(
        first_request,
        scan_window_start=first_request.scan_window_start + timedelta(minutes=3),
    )
    results: list[tuple[RunCoordinatorRequest, RunCoordinatorResult]] = []
    errors: list[Exception] = []

    def run(request: RunCoordinatorRequest) -> None:
        try:
            results.append((request, parts.coordinator.run(request)))
        except Exception as error:
            errors.append(error)

    first = Thread(target=lambda: run(first_request))
    second = Thread(target=lambda: run(second_request))
    first.start()
    assert intents.first_write_started.wait(timeout=1)
    second.start()
    second.join(timeout=1)
    intents.release_first_write.set()
    first.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert sum(result.disposition is RunDisposition.COMPLETED for _, result in results) == 1
    assert sum(result.disposition is RunDisposition.SUPERSEDED for _, result in results) == 1
    completed_scan_ids = [
        result.logical_scan_run_id
        for _, result in results
        if result.disposition in {RunDisposition.COMPLETED, RunDisposition.SUPERSEDED}
    ]
    assert len(completed_scan_ids) == 2
    assert all(
        parts.logical_scans.get(scan_id, access_context=_context()).status
        is LogicalScanStatus.COMPLETED
        for scan_id in completed_scan_ids
    )
    all_runs = [
        parts.strategy_runs.get_for_logical_scan(
            logical_scan_run_id=result.logical_scan_run_id, access_context=_context()
        )
        for _, result in results
        if result.logical_scan_run_id is not None
    ]
    assert (
        sum(
            len(
                parts.child_intents.list_for_strategy_run(
                    strategy_run_id=run.strategy_run_id, access_context=_context()
                )
            )
            for run in all_runs
            if run is not None
        )
        == 1
    )
    loser_request, loser = next(
        (request, result)
        for request, result in results
        if result.disposition is RunDisposition.SUPERSEDED
    )
    assert loser.logical_scan_run_id is not None
    assert loser.audit["failure_code"] == "FINALIZATION_SUPERSEDED"
    assert (
        parts.logical_scans.get(loser.logical_scan_run_id, access_context=_context()).status
        is LogicalScanStatus.COMPLETED
    )

    retried_loser = parts.coordinator.run(loser_request)

    assert retried_loser.disposition is RunDisposition.SUPERSEDED
    assert retried_loser.audit["failure_code"] == "FINALIZATION_SUPERSEDED"
    assert (
        sum(
            len(
                parts.child_intents.list_for_strategy_run(
                    strategy_run_id=run.strategy_run_id, access_context=_context()
                )
            )
            for run in all_runs
            if run is not None
        )
        == 1
    )


def test_legacy_pending_finalization_backfills_its_missing_fence_before_completion() -> None:
    position = _position()
    plan = _plan()
    scans = _FailOnceAttachScans()
    parts = _parts(
        evaluation=RotationEvaluation(
            event=RecommendationEvent.DECISION_VALIDATED,
            guards=TransitionGuards(sizing_outcome=SizingOutcome.PASSED),
            outputs={"attempt": "legacy-fence-backfill"},
            sale_source_position_id=position.position_id,
        ),
        plan=plan,
        positions=(position,),
        logical_scans=scans,
    )
    _seed_state(parts.recommendation_states, plan, RecommendationState.ACTION_PENDING)
    request = _request(parts.configuration, plan=plan)

    pending = parts.coordinator.run(request)

    assert pending.disposition is RunDisposition.FAILED
    assert pending.logical_scan_run_id is not None
    candidate = parts.strategy_runs.get_for_logical_scan(
        logical_scan_run_id=pending.logical_scan_run_id, access_context=_context()
    )
    assert candidate is not None
    legacy_state = parts.recommendation_states.get(plan.rotation_plan_id, access_context=_context())
    parts.recommendation_states._records[plan.rotation_plan_id] = replace(  # type: ignore[attr-defined]
        legacy_state,
        finalization_strategy_run_id=None,
        finalization_strategy_key=None,
    )

    recovered = parts.coordinator.run(request)

    assert recovered.disposition is RunDisposition.COMPLETED
    backfilled = parts.recommendation_states.get(plan.rotation_plan_id, access_context=_context())
    assert backfilled.finalization_strategy_run_id == candidate.strategy_run_id
    assert backfilled.finalization_strategy_key == recovered.final_strategy_key
    assert parts.provider.calls == 1


def test_unfenced_matching_state_does_not_let_a_new_candidate_claim_legacy_recovery() -> None:
    position = _position()
    plan = _plan()
    parts = _parts(
        evaluation=RotationEvaluation(
            event=RecommendationEvent.DECISION_VALIDATED,
            guards=TransitionGuards(sizing_outcome=SizingOutcome.PASSED),
            outputs={"attempt": "unfenced-noop"},
            sale_source_position_id=position.position_id,
        ),
        plan=plan,
        positions=(position,),
    )
    _seed_state(parts.recommendation_states, plan, RecommendationState.ACTION_NOTIFIED)

    result = parts.coordinator.run(_request(parts.configuration, plan=plan))

    assert result.disposition is RunDisposition.SUPERSEDED
    assert result.logical_scan_run_id is not None
    state = parts.recommendation_states.get(plan.rotation_plan_id, access_context=_context())
    assert state.finalization_strategy_run_id is None
    assert state.finalization_strategy_key is None
    strategy_run = parts.strategy_runs.get_for_logical_scan(
        logical_scan_run_id=result.logical_scan_run_id, access_context=_context()
    )
    assert strategy_run is not None
    assert (
        parts.child_intents.list_for_strategy_run(
            strategy_run_id=strategy_run.strategy_run_id, access_context=_context()
        )
        == ()
    )


def test_child_intent_repository_deduplicates_a_logical_retry_with_new_id_and_timestamp() -> None:
    plan = _plan()
    parts = _parts(
        evaluation=RotationEvaluation(
            event=RecommendationEvent.PLAN_ACTIVATED,
            guards=TransitionGuards(plan_is_valid=True),
            outputs={"attempt": "intent-retry-link"},
        ),
        plan=plan,
    )
    completed = parts.coordinator.run(_request(parts.configuration, plan=plan))
    assert completed.strategy_run is not None
    repository = parts.child_intents
    stable = {
        "rotation_plan_id": plan.rotation_plan_id,
        "portfolio_id": PORTFOLIO_ID,
        "strategy_run_id": completed.strategy_run.strategy_run_id,
        "final_strategy_key": completed.final_strategy_key,
        "purpose": ChildIntentPurpose.NOTIFICATION,
    }
    assert isinstance(stable["final_strategy_key"], str)
    first = build_child_intent(
        child_intent_id=UUID("00000000-0000-0000-0000-000000001011"),
        created_at=NOW,
        **stable,
    )
    retried = build_child_intent(
        child_intent_id=UUID("00000000-0000-0000-0000-000000001012"),
        created_at=NOW + timedelta(minutes=1),
        **stable,
    )

    stored = repository.record_or_get(intent=first, access_context=_context())
    recovered = repository.record_or_get(intent=retried, access_context=_context())

    assert recovered == stored
    assert repository.list_for_strategy_run(
        strategy_run_id=stable["strategy_run_id"], access_context=_context()
    ) == (stored,)


def test_child_intents_require_an_existing_strategy_run_and_matching_final_key() -> None:
    plan = _plan()
    parts = _parts(
        evaluation=RotationEvaluation(
            event=RecommendationEvent.PLAN_ACTIVATED,
            guards=TransitionGuards(plan_is_valid=True),
            outputs={"attempt": "intent-link-validation"},
        ),
        plan=plan,
    )
    completed = parts.coordinator.run(_request(parts.configuration, plan=plan))
    assert completed.strategy_run is not None
    assert isinstance(completed.final_strategy_key, str)
    missing = build_child_intent(
        child_intent_id=UUID("00000000-0000-0000-0000-000000001020"),
        rotation_plan_id=plan.rotation_plan_id,
        portfolio_id=plan.portfolio_id,
        strategy_run_id=UUID("00000000-0000-0000-0000-000000001021"),
        final_strategy_key=completed.final_strategy_key,
        purpose=ChildIntentPurpose.NOTIFICATION,
        created_at=NOW,
    )
    mismatched = build_child_intent(
        child_intent_id=UUID("00000000-0000-0000-0000-000000001022"),
        rotation_plan_id=plan.rotation_plan_id,
        portfolio_id=plan.portfolio_id,
        strategy_run_id=completed.strategy_run.strategy_run_id,
        final_strategy_key="f" * 64,
        purpose=ChildIntentPurpose.NOTIFICATION,
        created_at=NOW,
    )

    with pytest.raises(ValueError, match="missing strategy run"):
        parts.child_intents.record_or_get(intent=missing, access_context=_context())
    with pytest.raises(ValueError, match="final strategy key"):
        parts.child_intents.record_or_get(intent=mismatched, access_context=_context())


def test_recommendation_state_compare_and_set_has_one_concurrent_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    states = InMemoryRecommendationStateRepository({PORTFOLIO_ID: OWNER_ID})
    initial = states.get_or_create(plan=plan, created_at=NOW, access_context=_context())
    barrier = Barrier(2)
    original_get = states.get

    def synchronized_get(
        rotation_plan_id: UUID, *, access_context: AccessContext
    ) -> RecommendationStateRecord:
        record = original_get(rotation_plan_id, access_context=access_context)
        barrier.wait(timeout=1)
        return record

    monkeypatch.setattr(states, "get", synchronized_get)
    winners: list[RecommendationStateRecord] = []
    errors: list[Exception] = []

    def advance(next_state: RecommendationState) -> None:
        try:
            winners.append(
                states.record_transition(
                    plan=plan,
                    previous=initial,
                    next_state=next_state,
                    remaining_stages_halted=False,
                    changed_at=NOW,
                    access_context=_context(),
                )
            )
        except Exception as error:
            errors.append(error)

    first = Thread(target=lambda: advance(RecommendationState.WATCHING))
    second = Thread(target=lambda: advance(RecommendationState.DATA_DEGRADED))
    first.start()
    second.start()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(winners) == 1
    assert len(errors) == 1


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
