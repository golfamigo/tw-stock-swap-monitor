"""Integration contracts for the guarded dry-run HTTP adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Never, cast
from uuid import UUID

from app import main as application_main
from app.api.dependencies import RunRequestBuilder
from app.application.configuration import (
    CANONICAL_FORMAT_VERSION,
    ResolvedConfigurationSnapshot,
    canonical_content_hash,
    canonical_json,
)
from app.application.configuration_snapshots import PersistedConfigurationSnapshot
from app.application.run_coordinator import (
    ExistingResultReference,
    RunCoordinator,
    RunCoordinatorRequest,
    RunCoordinatorResult,
    RunDisposition,
    TriggerSource,
)
from app.data_sources.models import MarketDataRequest
from app.domain.access import AccessContext
from app.domain.entities import Instrument, RotationPlan
from app.domain.enums import Scope
from app.domain.values import ConfigurationSnapshotRef, Ownership
from app.main import create_application
from app.persistence.in_memory import (
    InMemoryChildIntentRepository,
    InMemoryConfigurationSnapshotRepository,
    InMemoryLogicalScanRepository,
    InMemoryPositionRepository,
    InMemoryRecommendationStateRepository,
    InMemoryRotationPlanRepository,
    InMemoryStrategyRunRepository,
)
from app.repositories.locks import LockLease, ScanLockRequest
from app.services.rotation_run import RotationEvaluation
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from starlette.types import Message
from starlette.types import Scope as ASGIScope

PLAN_ID = UUID("00000000-0000-0000-0000-000000001001")
SCAN_LOCK_KEY = "a" * 64
OVERSIZED_ADMIN_BODY_BYTES = 32_768
COMPOSITION_NOW = datetime(2026, 8, 5, tzinfo=UTC)
COMPOSITION_OWNER_ID = UUID("00000000-0000-0000-0000-000000001101")
COMPOSITION_PORTFOLIO_ID = UUID("00000000-0000-0000-0000-000000001102")
COMPOSITION_PLAN_ID = UUID("00000000-0000-0000-0000-000000001103")
COMPOSITION_GROUP_ID = UUID("00000000-0000-0000-0000-000000001104")
COMPOSITION_CONFIG_ID = UUID("00000000-0000-0000-0000-000000001105")
COMPOSITION_INSTRUMENT_ID = UUID("00000000-0000-0000-0000-000000001106")


@dataclass(frozen=True, slots=True)
class _ScheduledRequest:
    rotation_plan_id: UUID
    trigger_source: TriggerSource


class _SharedCoordinator:
    """A deterministic shared coordinator double with duplicate-key behavior."""

    def __init__(self) -> None:
        self.calls: list[_ScheduledRequest] = []
        self._completed_plan_ids: set[UUID] = set()

    def run(self, request: _ScheduledRequest) -> RunCoordinatorResult:
        self.calls.append(request)
        if request.rotation_plan_id in self._completed_plan_ids:
            disposition = (
                RunDisposition.API_CONFLICT
                if request.trigger_source is TriggerSource.API
                else RunDisposition.SCHEDULER_SKIPPED
            )
            return RunCoordinatorResult(
                disposition=disposition,
                strategy_run=None,
                scan_lock_key=SCAN_LOCK_KEY,
                logical_scan_run_id=None,
                final_strategy_key=None,
                audit={},
                existing_result=ExistingResultReference(
                    scan_lock_key=SCAN_LOCK_KEY,
                    logical_scan_run_id=None,
                ),
            )
        self._completed_plan_ids.add(request.rotation_plan_id)
        return RunCoordinatorResult(
            disposition=RunDisposition.COMPLETED,
            strategy_run=None,
            scan_lock_key=SCAN_LOCK_KEY,
            logical_scan_run_id=None,
            final_strategy_key=None,
            audit={},
        )


def _request_builder(rotation_plan_id: UUID, *, trigger_source: TriggerSource) -> _ScheduledRequest:
    return _ScheduledRequest(rotation_plan_id=rotation_plan_id, trigger_source=trigger_source)


def _client(monkeypatch: MonkeyPatch) -> tuple[TestClient, _SharedCoordinator]:
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-admin-token")
    coordinator = _SharedCoordinator()
    return (
        TestClient(
            create_application(
                coordinator=cast(RunCoordinator, coordinator),
                request_builder=cast(RunRequestBuilder, _request_builder),
            )
        ),
        coordinator,
    )


def _headers(token: str = "test-admin-token") -> dict[str, str]:
    return {"X-Admin-Token": token}


def test_health_readiness_and_status_are_typed_and_secret_free(monkeypatch: MonkeyPatch) -> None:
    client, _ = _client(monkeypatch)

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.get("/status").json() == {"status": "ready", "run_once_enabled": True}


def test_run_once_rejects_an_absent_or_invalid_admin_token(monkeypatch: MonkeyPatch) -> None:
    client, coordinator = _client(monkeypatch)
    payload = {"rotation_plan_id": str(PLAN_ID), "dry_run": True}

    assert client.post("/admin/run-once", json=payload).status_code == 401
    assert (
        client.post("/admin/run-once", headers=_headers("wrong"), json=payload).status_code == 401
    )
    assert coordinator.calls == []


def test_run_once_requires_a_plan_identifier(monkeypatch: MonkeyPatch) -> None:
    client, coordinator = _client(monkeypatch)

    response = client.post("/admin/run-once", headers=_headers(), json={"dry_run": True})

    assert response.status_code == 422
    assert coordinator.calls == []


def test_run_once_rejects_every_non_dry_run_request(monkeypatch: MonkeyPatch) -> None:
    client, coordinator = _client(monkeypatch)

    response = client.post(
        "/admin/run-once",
        headers=_headers(),
        json={"rotation_plan_id": str(PLAN_ID), "dry_run": False},
    )

    assert response.status_code == 422
    assert coordinator.calls == []


def test_run_once_rejects_a_declared_oversized_body_before_parsing(
    monkeypatch: MonkeyPatch,
) -> None:
    client, coordinator = _client(monkeypatch)

    response = client.post(
        "/admin/run-once",
        headers={
            **_headers(),
            "Content-Length": str(OVERSIZED_ADMIN_BODY_BYTES),
        },
        content=b"{}",
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body exceeds configured limit"}
    assert coordinator.calls == []


def test_run_once_rejects_oversized_streamed_body_without_content_length(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-admin-token")
    coordinator = _SharedCoordinator()
    application = create_application(
        coordinator=cast(RunCoordinator, coordinator),
        request_builder=cast(RunRequestBuilder, _request_builder),
    )
    received: list[Message] = [
        {
            "type": "http.request",
            "body": (
                b'{"rotation_plan_id":"00000000-0000-0000-0000-000000001001",'
                b'"dry_run":true,"padding":"'
            ),
            "more_body": True,
        },
        {
            "type": "http.request",
            "body": (b"x" * OVERSIZED_ADMIN_BODY_BYTES) + b'"}',
            "more_body": False,
        },
    ]
    sent: list[Message] = []
    scope: ASGIScope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/admin/run-once",
        "raw_path": b"/admin/run-once",
        "query_string": b"",
        "root_path": "",
        "headers": (
            (b"content-type", b"application/json"),
            (b"x-admin-token", b"test-admin-token"),
        ),
        "client": ("testclient", 0),
        "server": ("testserver", 80),
    }

    async def receive() -> Message:
        return received.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(application(scope, receive, send))

    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b'{"detail":"request body exceeds configured limit"}'
    assert coordinator.calls == []


def test_scheduler_and_api_share_one_coordinator_duplicate_key(monkeypatch: MonkeyPatch) -> None:
    client, coordinator = _client(monkeypatch)

    scheduler_result = coordinator.run(
        _request_builder(PLAN_ID, trigger_source=TriggerSource.SCHEDULER)
    )
    response = client.post(
        "/admin/run-once",
        headers=_headers(),
        json={"rotation_plan_id": str(PLAN_ID), "dry_run": True},
    )

    assert scheduler_result.disposition is RunDisposition.COMPLETED
    assert response.status_code == 409
    assert response.json() == {
        "disposition": RunDisposition.API_CONFLICT.value,
        "scan_lock_key": SCAN_LOCK_KEY,
        "logical_scan_run_id": None,
        "final_strategy_key": None,
        "existing_result": {"scan_lock_key": SCAN_LOCK_KEY, "logical_scan_run_id": None},
    }
    assert [call.trigger_source for call in coordinator.calls] == [
        TriggerSource.SCHEDULER,
        TriggerSource.API,
    ]


def test_unconfigured_application_never_allows_an_anonymous_run_once(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
    client = TestClient(create_application())

    response = client.post(
        "/admin/run-once",
        json={"rotation_plan_id": str(PLAN_ID), "dry_run": True},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "admin run-once is disabled"}


def test_an_empty_administrative_token_disables_run_once(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_TOKEN", "")
    coordinator = _SharedCoordinator()
    client = TestClient(
        create_application(
            coordinator=cast(RunCoordinator, coordinator),
            request_builder=cast(RunRequestBuilder, _request_builder),
        )
    )

    response = client.post(
        "/admin/run-once",
        headers={"X-Admin-Token": ""},
        json={"rotation_plan_id": str(PLAN_ID), "dry_run": True},
    )

    assert response.status_code == 503
    assert coordinator.calls == []


def test_default_factory_has_safe_run_once_dependencies_when_token_is_configured(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-admin-token")
    client = TestClient(create_application())

    assert client.get("/status").json() == {"status": "ready", "run_once_enabled": True}
    response = client.post(
        "/admin/run-once",
        headers=_headers(),
        json={"rotation_plan_id": str(PLAN_ID), "dry_run": True},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "rotation plan was not found"}


class _DenyingLock:
    def acquire_scan_lock(
        self, request: ScanLockRequest, *, access_context: AccessContext
    ) -> LockLease | None:
        del request, access_context
        return None

    def release(self, lease: LockLease, *, access_context: AccessContext) -> None:
        del lease, access_context


class _NoProvider:
    provider_name = "in-memory-placeholder"

    def __init__(self) -> None:
        self.calls = 0

    def get_market_snapshot(self, request: MarketDataRequest) -> Never:
        del request
        self.calls += 1
        raise AssertionError("the denied lock must prevent provider access")

    def get_quotes(self, request: MarketDataRequest) -> Never:
        del request
        raise AssertionError("the coordinator requires atomic snapshots")

    def get_intraday_bars(self, request: MarketDataRequest) -> Never:
        del request
        raise AssertionError("the coordinator requires atomic snapshots")

    def get_daily_bars(self, request: MarketDataRequest) -> Never:
        del request
        raise AssertionError("the coordinator requires atomic snapshots")


def _composition_context() -> AccessContext:
    return AccessContext(
        actor_user_id=COMPOSITION_OWNER_ID,
        request_id=UUID("00000000-0000-0000-0000-000000001107"),
        authentication_method="integration-admin-token",
    )


def _composition_plan() -> RotationPlan:
    return RotationPlan(
        rotation_plan_id=COMPOSITION_PLAN_ID,
        portfolio_id=COMPOSITION_PORTFOLIO_ID,
        candidate_group_ids=(COMPOSITION_GROUP_ID,),
        source_position_ids=(),
        protected_position_ids=(),
        created_at=COMPOSITION_NOW,
    )


def _composition_configuration(plan: RotationPlan) -> PersistedConfigurationSnapshot:
    payload: dict[str, object] = {
        "configuration_name": "placeholder-dry-run",
        "settings": {},
        "rules": [],
        "keyed_items": [],
        "extensions": {},
        "nullable_note": None,
    }
    resolved = ResolvedConfigurationSnapshot(
        payload=payload,
        parent_versions=(),
        created_by=COMPOSITION_OWNER_ID,
        created_at=COMPOSITION_NOW,
        runtime_expires_at=None,
        canonical_format_version=CANONICAL_FORMAT_VERSION,
        content_hash=canonical_content_hash(payload),
        canonical_json=canonical_json(payload),
    )
    return PersistedConfigurationSnapshot(
        snapshot_id=COMPOSITION_CONFIG_ID,
        resolved_snapshot=resolved,
        config_version=1,
        target_ownership=Ownership(Scope.PORTFOLIO, plan.portfolio_id),
        target_reference_id=plan.rotation_plan_id,
    )


def _composition_market_request() -> MarketDataRequest:
    instrument = Instrument(
        instrument_id=COMPOSITION_INSTRUMENT_ID,
        market="placeholder-market",
        symbol="PLACEHOLDER",
        created_at=COMPOSITION_NOW,
    )
    return MarketDataRequest(
        request_id=UUID("00000000-0000-0000-0000-000000001108"),
        instruments=(instrument,),
        requested_at=COMPOSITION_NOW,
        intraday_start=COMPOSITION_NOW,
        intraday_end=COMPOSITION_NOW + timedelta(minutes=1),
        intraday_interval=timedelta(minutes=1),
        daily_start=date(2026, 8, 4),
        daily_end=date(2026, 8, 5),
        maximum_data_delay=timedelta(),
    )


def _unreachable_evaluator(*_: object) -> RotationEvaluation:
    raise AssertionError("the denied lock must prevent evaluation")


def test_provided_in_memory_composition_reaches_the_shared_coordinator(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-admin-token")
    plan = _composition_plan()
    context = _composition_context()
    portfolio_owners = {COMPOSITION_PORTFOLIO_ID: COMPOSITION_OWNER_ID}
    plans = InMemoryRotationPlanRepository(
        portfolio_owners,
        candidate_group_owners={COMPOSITION_GROUP_ID: COMPOSITION_OWNER_ID},
        position_portfolios={},
    )
    plans.add(plan, access_context=context)
    configurations = InMemoryConfigurationSnapshotRepository(portfolio_owners)
    configuration = _composition_configuration(plan)
    configurations.record_or_get(snapshot=configuration, access_context=context)
    scans = InMemoryLogicalScanRepository(portfolio_owners)
    child_intents = InMemoryChildIntentRepository(portfolio_owners)
    provider = _NoProvider()
    positions = InMemoryPositionRepository(portfolio_owners)
    recommendation_states = InMemoryRecommendationStateRepository(portfolio_owners)
    strategy_runs = InMemoryStrategyRunRepository(
        portfolio_owners,
        logical_scan_repository=scans,
        child_intent_repository=child_intents,
    )
    template = RunCoordinatorRequest(
        plan=plan,
        configuration_snapshot=ConfigurationSnapshotRef(
            COMPOSITION_CONFIG_ID,
            configuration.resolved_snapshot.content_hash,
            COMPOSITION_NOW,
        ),
        market_session_date="2026-08-05",
        scan_window_start=COMPOSITION_NOW,
        scan_interval="PT1M",
        market_timezone="UTC",
        market_data_request=_composition_market_request(),
        access_context=context,
        trigger_source=TriggerSource.SCHEDULER,
        correlation_id="composition-integration",
    )
    composition = application_main.create_in_memory_run_once_composition(
        rotation_plans=plans,
        configuration_snapshots=configurations,
        positions=positions,
        recommendation_states=recommendation_states,
        child_intents=child_intents,
        logical_scans=scans,
        strategy_runs=strategy_runs,
        locks=_DenyingLock(),
        market_data=provider,
        evaluator=_unreachable_evaluator,
        now=lambda: COMPOSITION_NOW,
        request_templates={COMPOSITION_PLAN_ID: template},
    )
    client = TestClient(create_application(composition=composition))

    response = client.post(
        "/admin/run-once",
        headers=_headers(),
        json={"rotation_plan_id": str(COMPOSITION_PLAN_ID), "dry_run": True},
    )

    assert response.status_code == 409
    assert response.json()["disposition"] == RunDisposition.API_CONFLICT.value
    assert provider.calls == 0
