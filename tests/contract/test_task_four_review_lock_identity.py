"""Regression contracts for the review-required canonical scan lock identity."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from app.domain.entities import RotationPlan
from app.repositories.locks import ScanLockRequest


def _plan() -> RotationPlan:
    return RotationPlan(
        rotation_plan_id=uuid4(),
        portfolio_id=uuid4(),
        candidate_group_ids=(uuid4(),),
        source_position_ids=(),
        protected_position_ids=(),
        created_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
    )


def _request(
    *,
    plan: RotationPlan,
    scan_window_start: datetime,
    configuration_snapshot_hash: str = "a" * 64,
    scan_interval: str = "PT3M",
) -> ScanLockRequest:
    return ScanLockRequest(
        plan=plan,
        market_session_date="2026-08-01",
        scan_window_start=scan_window_start,
        scan_interval=scan_interval,
        market_timezone="Asia/Taipei",
        configuration_snapshot_hash=configuration_snapshot_hash,
    )


def test_scan_lock_key_normalizes_equivalent_instants_in_the_market_timezone() -> None:
    plan = _plan()
    taipei_time = datetime(2026, 8, 1, 9, tzinfo=timezone(timedelta(hours=8)))
    utc_time = taipei_time.astimezone(UTC)

    assert (
        _request(plan=plan, scan_window_start=taipei_time).key
        == _request(plan=plan, scan_window_start=utc_time).key
    )


def test_scan_lock_key_changes_with_the_resolved_configuration_snapshot_hash() -> None:
    plan = _plan()
    window = datetime(2026, 8, 1, 1, tzinfo=UTC)

    assert (
        _request(plan=plan, scan_window_start=window, configuration_snapshot_hash="a" * 64).key
        != _request(plan=plan, scan_window_start=window, configuration_snapshot_hash="b" * 64).key
    )


def test_scan_lock_key_is_a_sha256_digest_for_delimiter_containing_identity_data() -> None:
    request = _request(
        plan=_plan(),
        scan_window_start=datetime(2026, 8, 1, 1, tzinfo=UTC),
        scan_interval="PT3M|unambiguous",
    )

    assert len(request.key) == 64
    assert all(character in "0123456789abcdef" for character in request.key)


def test_scan_lock_request_rejects_a_non_sha256_configuration_snapshot_hash() -> None:
    with pytest.raises(ValueError, match="configuration_snapshot_hash"):
        _request(
            plan=_plan(),
            scan_window_start=datetime(2026, 8, 1, 1, tzinfo=UTC),
            configuration_snapshot_hash="untrusted",
        )
