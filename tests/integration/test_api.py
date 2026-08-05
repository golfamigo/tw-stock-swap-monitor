"""Integration contracts for the guarded dry-run HTTP adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from app.api.dependencies import RunRequestBuilder
from app.application.run_coordinator import (
    ExistingResultReference,
    RunCoordinator,
    RunCoordinatorResult,
    RunDisposition,
    TriggerSource,
)
from app.main import create_application
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

PLAN_ID = UUID("00000000-0000-0000-0000-000000001001")
SCAN_LOCK_KEY = "a" * 64


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
