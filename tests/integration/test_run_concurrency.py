"""Offline contracts for lock-first, idempotent rotation-run coordination."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Event, Thread
from typing import Never
from uuid import UUID

from app.application.configuration import (
    CANONICAL_FORMAT_VERSION,
    ResolvedConfigurationSnapshot,
    canonical_content_hash,
    canonical_json,
)
from app.application.configuration_snapshots import PersistedConfigurationSnapshot
from app.application.idempotency import (
    FINAL_STRATEGY_IDENTITY_FORMAT_VERSION,
    FinalStrategyIdentity,
    build_scan_lock_key,
)
from app.application.run_coordinator import (
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
from app.domain.entities import Instrument, RotationPlan
from app.domain.enums import Scope
from app.domain.values import ConfigurationSnapshotRef, Ownership
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
from app.repositories.locks import LockLease, LockProvider, ScanLockRequest
from app.services.rotation_run import RotationEvaluation
from app.state_machine.machine import TransitionGuards
from app.state_machine.states import RecommendationEvent

NOW = datetime(2026, 8, 1, 1, tzinfo=UTC)
OWNER_ID = UUID("00000000-0000-0000-0000-000000000801")
PORTFOLIO_ID = UUID("00000000-0000-0000-0000-000000000802")
PLAN_ID = UUID("00000000-0000-0000-0000-000000000803")
GROUP_ID = UUID("00000000-0000-0000-0000-000000000804")
INSTRUMENT_ID = UUID("00000000-0000-0000-0000-000000000805")
CONFIGURATION_ID = UUID("00000000-0000-0000-0000-000000000806")


def _context() -> AccessContext:
    return AccessContext(
        actor_user_id=OWNER_ID,
        request_id=UUID("00000000-0000-0000-0000-000000000807"),
        authentication_method="task-nine-test",
    )


def _plan() -> RotationPlan:
    return RotationPlan(
        rotation_plan_id=PLAN_ID,
        portfolio_id=PORTFOLIO_ID,
        candidate_group_ids=(GROUP_ID,),
        source_position_ids=(),
        protected_position_ids=(),
        created_at=NOW,
    )


def _configuration(plan: RotationPlan) -> PersistedConfigurationSnapshot:
    payload = {
        "configuration_name": "task-nine-concurrency",
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


def _market_request() -> MarketDataRequest:
    instrument = Instrument(
        instrument_id=INSTRUMENT_ID,
        market="test-market",
        symbol="TEST",
        created_at=NOW,
    )
    return MarketDataRequest(
        request_id=UUID("00000000-0000-0000-0000-000000000808"),
        instruments=(instrument,),
        requested_at=NOW,
        intraday_start=NOW,
        intraday_end=NOW + timedelta(minutes=15),
        intraday_interval=timedelta(minutes=15),
        daily_start=date(2026, 7, 31),
        daily_end=date(2026, 8, 1),
        maximum_data_delay=timedelta(minutes=5),
    )


def _snapshot(*, price: Decimal = Decimal("10"), stale: bool = False) -> MarketDataSnapshot:
    request = _market_request()
    source_timestamp = NOW
    fetched_at = NOW + timedelta(minutes=1)
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
    return MarketDataSnapshot(
        provider="offline-test-provider",
        request=request,
        quotes=(Quote(instrument_id=INSTRUMENT_ID, as_of=NOW, price=price),),
        intraday_bars=(bar,),
        daily_bars=(bar,),
        quality=DataQuality(
            provider="offline-test-provider",
            fetched_at=fetched_at,
            source_timestamp=source_timestamp,
            delay=fetched_at - source_timestamp,
            stale=stale,
            missing_fields=(),
            bars_complete=True,
            anomalies=(),
            confidence=Decimal("1"),
        ),
    )


class _OfflineProvider:
    provider_name = "offline-test-provider"

    def __init__(self, responses: list[MarketDataSnapshot | Exception]) -> None:
        self._responses = deque(responses)
        self.calls = 0

    def get_market_snapshot(self, request: MarketDataRequest) -> MarketDataSnapshot:
        del request
        self.calls += 1
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    def get_quotes(self, request: MarketDataRequest) -> Never:
        del request
        raise AssertionError("coordinator must use atomic market snapshots")

    def get_intraday_bars(self, request: MarketDataRequest) -> Never:
        del request
        raise AssertionError("coordinator must use atomic market snapshots")

    def get_daily_bars(self, request: MarketDataRequest) -> Never:
        del request
        raise AssertionError("coordinator must use atomic market snapshots")


class _BlockingOfflineProvider(_OfflineProvider):
    def __init__(self, snapshot: MarketDataSnapshot) -> None:
        super().__init__([snapshot])
        self.started = Event()
        self.release = Event()

    def get_market_snapshot(self, request: MarketDataRequest) -> MarketDataSnapshot:
        self.started.set()
        assert self.release.wait(timeout=1), "test did not release provider"
        return super().get_market_snapshot(request)


class _DenyingLock:
    def acquire_scan_lock(
        self, request: ScanLockRequest, *, access_context: AccessContext
    ) -> LockLease | None:
        del request, access_context
        return None

    def release(self, lease: LockLease, *, access_context: AccessContext) -> None:
        del lease, access_context


def _evaluation(*_: object) -> RotationEvaluation:
    return RotationEvaluation(
        event=RecommendationEvent.PLAN_ACTIVATED,
        guards=TransitionGuards(plan_is_valid=True),
        outputs={"evaluation": "deterministic"},
    )


def _uuid_factory() -> Callable[[], UUID]:
    next_value = 809

    def build_uuid() -> UUID:
        nonlocal next_value
        value = UUID(f"00000000-0000-0000-0000-{next_value:012d}")
        next_value += 1
        return value

    return build_uuid


def _coordinator(
    provider: _OfflineProvider,
    *,
    lock: LockProvider | None = None,
    max_provider_attempts: int = 2,
) -> RunCoordinator:
    plan = _plan()
    plans = InMemoryRotationPlanRepository(
        {PORTFOLIO_ID: OWNER_ID},
        candidate_group_owners={GROUP_ID: OWNER_ID},
        position_portfolios={},
    )
    plans.add(plan, access_context=_context())
    scans = InMemoryLogicalScanRepository({PORTFOLIO_ID: OWNER_ID})
    configurations = InMemoryConfigurationSnapshotRepository({PORTFOLIO_ID: OWNER_ID})
    configurations.record_or_get(snapshot=_configuration(plan), access_context=_context())
    return RunCoordinator(
        rotation_plans=plans,
        configuration_snapshots=configurations,
        positions=InMemoryPositionRepository({PORTFOLIO_ID: OWNER_ID}),
        recommendation_states=InMemoryRecommendationStateRepository({PORTFOLIO_ID: OWNER_ID}),
        child_intents=InMemoryChildIntentRepository({PORTFOLIO_ID: OWNER_ID}),
        logical_scans=scans,
        strategy_runs=InMemoryStrategyRunRepository(
            {PORTFOLIO_ID: OWNER_ID}, logical_scan_repository=scans
        ),
        locks=lock if lock is not None else InMemoryLockProvider({PORTFOLIO_ID: OWNER_ID}),
        market_data=provider,
        evaluator=_evaluation,
        now=lambda: NOW,
        new_uuid=_uuid_factory(),
        max_provider_attempts=max_provider_attempts,
    )


def _request(
    *,
    trigger_source: TriggerSource = TriggerSource.API,
    configuration_hash: str = "a" * 64,
) -> RunCoordinatorRequest:
    configuration = _configuration(_plan())
    return RunCoordinatorRequest(
        plan=_plan(),
        configuration_snapshot=ConfigurationSnapshotRef(
            CONFIGURATION_ID,
            configuration_hash
            if configuration_hash != "a" * 64
            else configuration.resolved_snapshot.content_hash,
            NOW,
        ),
        market_session_date="2026-08-01",
        scan_window_start=NOW + timedelta(hours=8),
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        market_data_request=_market_request(),
        access_context=_context(),
        trigger_source=trigger_source,
        correlation_id="test-correlation-1",
    )


def test_final_strategy_key_has_a_versioned_canonical_golden_vector() -> None:
    identity = FinalStrategyIdentity(
        scan_lock_key="0" * 64,
        market_data_snapshot_id="market-data:v1:" + "a" * 64,
        market_data_content_hash="a" * 64,
    )

    assert identity.format_version == FINAL_STRATEGY_IDENTITY_FORMAT_VERSION
    assert identity.key == "71a54b4afb46a784a22d3c1764a57739c1aed27a2f2112144230c3a7583415f9"


def test_configuration_change_changes_the_pre_provider_scan_lock_key() -> None:
    first = ScanLockRequest(
        plan=_plan(),
        market_session_date="2026-08-01",
        scan_window_start=NOW + timedelta(hours=8),
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        configuration_snapshot_hash="a" * 64,
    )
    changed = replace(first, configuration_snapshot_hash="b" * 64)

    assert build_scan_lock_key(first) == first.key
    assert build_scan_lock_key(changed) != build_scan_lock_key(first)


def test_final_strategy_key_changes_for_changed_market_snapshot_evidence() -> None:
    first = _snapshot(price=Decimal("10"))
    changed = _snapshot(price=Decimal("11"))
    request = _request()
    scan_lock = ScanLockRequest(
        plan=request.plan,
        market_session_date=request.market_session_date,
        scan_window_start=request.scan_window_start,
        scan_interval=request.scan_interval,
        market_timezone=request.market_timezone,
        configuration_snapshot_hash=request.configuration_snapshot.content_hash,
    )

    first_key = FinalStrategyIdentity.from_snapshot(scan_lock.key, first).key
    changed_key = FinalStrategyIdentity.from_snapshot(scan_lock.key, changed).key

    assert first.snapshot_id != changed.snapshot_id
    assert first.content_hash != changed.content_hash
    assert first_key != changed_key


def test_lock_is_acquired_before_any_provider_io() -> None:
    provider = _OfflineProvider([_snapshot()])
    outcome = _coordinator(provider, lock=_DenyingLock()).run(
        _request(trigger_source=TriggerSource.SCHEDULER)
    )

    assert outcome.disposition is RunDisposition.SCHEDULER_SKIPPED
    assert provider.calls == 0


def test_api_and_scheduler_race_produces_one_fetch_and_one_completed_run() -> None:
    provider = _BlockingOfflineProvider(_snapshot())
    coordinator = _coordinator(provider)
    api_outcomes: list[RunCoordinatorResult] = []
    api_thread = Thread(target=lambda: api_outcomes.append(coordinator.run(_request())))

    api_thread.start()
    assert provider.started.wait(timeout=1), "API run did not reach the provider"
    scheduler_outcome = coordinator.run(_request(trigger_source=TriggerSource.SCHEDULER))
    provider.release.set()
    api_thread.join(timeout=1)

    assert not api_thread.is_alive()
    api_outcome = api_outcomes[0]
    assert api_outcome.disposition is RunDisposition.COMPLETED
    assert scheduler_outcome.disposition is RunDisposition.SCHEDULER_SKIPPED
    assert provider.calls == 1
    assert api_outcome.strategy_run is not None
    assert scheduler_outcome.strategy_run is None


def test_completed_rerun_returns_the_stored_result_without_provider_io() -> None:
    provider = _OfflineProvider([_snapshot()])
    coordinator = _coordinator(provider)

    first = coordinator.run(_request())
    rerun = coordinator.run(_request())

    assert first.disposition is RunDisposition.COMPLETED
    assert rerun.disposition is RunDisposition.COMPLETED
    assert rerun.strategy_run == first.strategy_run
    assert provider.calls == 1


def test_stale_or_incomplete_market_evidence_cannot_yield_an_action() -> None:
    provider = _OfflineProvider([_snapshot(stale=True)])
    outcome = _coordinator(provider).run(_request())

    assert outcome.disposition is RunDisposition.DEGRADED
    assert outcome.strategy_run is None
    assert outcome.final_strategy_key is not None
    assert outcome.audit["market_data_actionable"] is False


def test_degraded_retry_retains_attempt_history_and_uses_changed_snapshot_key() -> None:
    degraded_snapshot = _snapshot(price=Decimal("10"), stale=True)
    recovered_snapshot = _snapshot(price=Decimal("11"))
    provider = _OfflineProvider([degraded_snapshot, recovered_snapshot])
    coordinator = _coordinator(provider)

    first = coordinator.run(_request())
    recovered = coordinator.run(_request())

    assert first.disposition is RunDisposition.DEGRADED
    assert recovered.disposition is RunDisposition.COMPLETED
    assert first.final_strategy_key != recovered.final_strategy_key
    assert recovered.strategy_run is not None
    assert recovered.audit["attempt_statuses"] == ["RUNNING", "DEGRADED", "RUNNING", "SUCCEEDED"]
    assert provider.calls == 2


def test_provider_failure_is_audited_and_retry_can_complete() -> None:
    provider = _OfflineProvider([RuntimeError("offline failure"), _snapshot()])
    coordinator = _coordinator(provider)

    failed = coordinator.run(_request())
    retried = coordinator.run(_request())

    assert failed.disposition is RunDisposition.FAILED
    assert failed.strategy_run is None
    assert failed.audit["failure_code"] == "MARKET_DATA_PROVIDER_ERROR"
    assert retried.disposition is RunDisposition.COMPLETED
    assert retried.audit["attempt_statuses"] == ["RUNNING", "FAILED", "RUNNING", "SUCCEEDED"]


def test_retry_limit_prevents_a_third_provider_call_for_an_incomplete_scan() -> None:
    provider = _OfflineProvider([_snapshot(stale=True), _snapshot(stale=True), _snapshot()])
    coordinator = _coordinator(provider, max_provider_attempts=2)

    first = coordinator.run(_request())
    second = coordinator.run(_request())
    exhausted = coordinator.run(_request())

    assert first.disposition is RunDisposition.DEGRADED
    assert second.disposition is RunDisposition.DEGRADED
    assert exhausted.disposition is RunDisposition.RETRY_EXHAUSTED
    assert exhausted.strategy_run is None
    assert exhausted.audit["attempt_statuses"] == ["RUNNING", "DEGRADED", "RUNNING", "DEGRADED"]
    assert provider.calls == 2
