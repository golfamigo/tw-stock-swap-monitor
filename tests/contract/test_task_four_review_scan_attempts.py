"""Regression contracts for auditable, recoverable market-data scan attempts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from app.domain.access import AccessContext
from app.domain.entities import Portfolio, RotationPlan, ScanAttempt, StrategyRun
from app.domain.enums import ScanAttemptRecoveryDecision, ScanAttemptStatus
from app.domain.values import ConfigurationSnapshotRef, IdempotencyKey
from app.persistence.models import (
    Base,
    ConfigurationSnapshotModel,
    PortfolioModel,
    RotationPlanModel,
    ScanAttemptModel,
    StrategyRunModel,
    UserModel,
)
from app.repositories.locks import ScanLockRequest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

NOW = datetime(2026, 8, 1, 9, tzinfo=UTC)


def _context(user_id: UUID) -> AccessContext:
    return AccessContext(
        actor_user_id=user_id,
        is_administrator=False,
        request_id=uuid4(),
        authentication_method="contract-test",
    )


def _plan(owner_id: UUID) -> RotationPlan:
    portfolio = Portfolio(portfolio_id=uuid4(), user_id=owner_id, created_at=NOW)
    return RotationPlan(
        rotation_plan_id=uuid4(),
        portfolio_id=portfolio.portfolio_id,
        candidate_group_ids=(uuid4(),),
        source_position_ids=(),
        protected_position_ids=(),
        created_at=NOW,
    )


def _status(name: str) -> ScanAttemptStatus:
    return ScanAttemptStatus[name]


def _decision(name: str) -> ScanAttemptRecoveryDecision:
    return ScanAttemptRecoveryDecision[name]


def _attempt(
    *,
    logical_scan_run_id: UUID,
    attempt_number: int,
    status: ScanAttemptStatus,
    completed_at: datetime | None,
    market_data_snapshot_id: str | None = None,
    market_data_content_hash: str | None = None,
    failure_code: str | None = None,
    failure_detail: str | None = None,
    recovery_of_attempt_id: UUID | None = None,
    duplicate_of_attempt_id: UUID | None = None,
    final_strategy_run_id: UUID | None = None,
) -> ScanAttempt:
    return ScanAttempt(
        scan_attempt_id=uuid4(),
        logical_scan_run_id=logical_scan_run_id,
        attempt_number=attempt_number,
        status=status,
        configuration_snapshot_hash="a" * 64,
        market_data_snapshot_id=market_data_snapshot_id,
        market_data_content_hash=market_data_content_hash,
        trigger_correlation_id="scheduler:2026-08-01T09:00:00+08:00",
        actor_correlation_id="worker:contract-test",
        started_at=NOW,
        completed_at=completed_at,
        failure_code=failure_code,
        failure_detail=failure_detail,
        recovery_of_attempt_id=recovery_of_attempt_id,
        duplicate_of_attempt_id=duplicate_of_attempt_id,
        final_strategy_run_id=final_strategy_run_id,
    )


def _strategy_run(plan: RotationPlan) -> StrategyRun:
    return StrategyRun(
        strategy_run_id=uuid4(),
        rotation_plan_id=plan.rotation_plan_id,
        portfolio_id=plan.portfolio_id,
        configuration_snapshot=ConfigurationSnapshotRef(uuid4(), "a" * 64, NOW),
        market_data_snapshot_id="complete-market-snapshot",
        state_transition="SCANNING->ACTION_PENDING",
        outputs={"source": "scan-attempt-contract"},
        occurred_at=NOW,
    )


def test_scan_attempt_statuses_expose_recovery_decisions_and_reject_invalid_evidence() -> None:
    logical_scan_id = uuid4()
    running = _attempt(
        logical_scan_run_id=logical_scan_id,
        attempt_number=1,
        status=_status("RUNNING"),
        completed_at=None,
    )
    failed = _attempt(
        logical_scan_run_id=logical_scan_id,
        attempt_number=2,
        status=_status("FAILED"),
        completed_at=NOW + timedelta(minutes=1),
        failure_code="MARKET_TIMEOUT",
        failure_detail="provider timed out before an immutable market snapshot was received",
        recovery_of_attempt_id=running.scan_attempt_id,
    )
    degraded = _attempt(
        logical_scan_run_id=logical_scan_id,
        attempt_number=3,
        status=_status("DEGRADED"),
        completed_at=NOW + timedelta(minutes=2),
        market_data_snapshot_id="partial-market-snapshot",
        market_data_content_hash="b" * 64,
        failure_code="MISSING_REQUIRED_FIELD",
        failure_detail="provider omitted required volume evidence",
        recovery_of_attempt_id=failed.scan_attempt_id,
    )
    succeeded = _attempt(
        logical_scan_run_id=logical_scan_id,
        attempt_number=4,
        status=_status("SUCCEEDED"),
        completed_at=NOW + timedelta(minutes=3),
        market_data_snapshot_id="complete-market-snapshot",
        market_data_content_hash="c" * 64,
        recovery_of_attempt_id=degraded.scan_attempt_id,
        final_strategy_run_id=uuid4(),
    )

    assert running.recovery_decision is _decision("RESUME")
    assert failed.recovery_decision is _decision("RETRY")
    assert degraded.recovery_decision is _decision("RETRY")
    assert succeeded.recovery_decision is _decision("FINALIZE")

    with pytest.raises(ValueError, match="failure_code"):
        _attempt(
            logical_scan_run_id=logical_scan_id,
            attempt_number=5,
            status=_status("FAILED"),
            completed_at=NOW,
        )
    with pytest.raises(ValueError, match="market_data"):
        _attempt(
            logical_scan_run_id=logical_scan_id,
            attempt_number=6,
            status=_status("SUCCEEDED"),
            completed_at=NOW,
        )
    with pytest.raises(ValueError, match="attempt_number"):
        _attempt(
            logical_scan_run_id=logical_scan_id,
            attempt_number=0,
            status=_status("RUNNING"),
            completed_at=None,
        )


def test_in_memory_attempt_audit_retains_failure_degradation_recovery_and_success() -> None:
    from app.persistence.in_memory import (
        InMemoryLogicalScanRepository,
        InMemoryStrategyRunRepository,
    )

    owner_id = uuid4()
    plan = _plan(owner_id)
    repository = InMemoryLogicalScanRepository({plan.portfolio_id: owner_id})
    request = ScanLockRequest(
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
    failed = _attempt(
        logical_scan_run_id=scan.logical_scan_run_id,
        attempt_number=1,
        status=_status("FAILED"),
        completed_at=NOW + timedelta(minutes=1),
        failure_code="MARKET_TIMEOUT",
        failure_detail="provider timed out before snapshot creation",
    )
    degraded = _attempt(
        logical_scan_run_id=scan.logical_scan_run_id,
        attempt_number=2,
        status=_status("DEGRADED"),
        completed_at=NOW + timedelta(minutes=2),
        market_data_snapshot_id="partial-market-snapshot",
        market_data_content_hash="b" * 64,
        failure_code="MISSING_REQUIRED_FIELD",
        failure_detail="volume evidence is unavailable",
        recovery_of_attempt_id=failed.scan_attempt_id,
    )
    succeeded = _attempt(
        logical_scan_run_id=scan.logical_scan_run_id,
        attempt_number=3,
        status=_status("SUCCEEDED"),
        completed_at=NOW + timedelta(minutes=3),
        market_data_snapshot_id="complete-market-snapshot",
        market_data_content_hash="c" * 64,
        recovery_of_attempt_id=degraded.scan_attempt_id,
        final_strategy_run_id=None,
    )

    repository.record_attempt(attempt=failed, access_context=_context(owner_id))
    repository.record_attempt(attempt=degraded, access_context=_context(owner_id))
    strategy_runs = InMemoryStrategyRunRepository(
        {plan.portfolio_id: owner_id}, logical_scan_repository=repository
    )
    final_run = _strategy_run(plan)
    strategy_runs.record_or_get(
        plan=plan,
        run=final_run,
        idempotency_key=IdempotencyKey("attempt-audit-final"),
        logical_scan_run_id=scan.logical_scan_run_id,
        access_context=_context(owner_id),
    )
    succeeded = replace(succeeded, final_strategy_run_id=final_run.strategy_run_id)
    repository.record_attempt(attempt=succeeded, access_context=_context(owner_id))

    assert repository.list_attempts(
        logical_scan_run_id=scan.logical_scan_run_id, access_context=_context(owner_id)
    ) == (failed, degraded, succeeded)


def test_recovery_chain_requires_the_previous_retryable_attempt_and_stops_after_success() -> None:
    from app.persistence.in_memory import (
        InMemoryLogicalScanRepository,
        InMemoryStrategyRunRepository,
    )

    owner_id = uuid4()
    plan = _plan(owner_id)
    repository = InMemoryLogicalScanRepository({plan.portfolio_id: owner_id})
    request = ScanLockRequest(
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
    failed = _attempt(
        logical_scan_run_id=scan.logical_scan_run_id,
        attempt_number=1,
        status=_status("FAILED"),
        completed_at=NOW,
        failure_code="MARKET_TIMEOUT",
        failure_detail="provider timeout",
    )
    repository.record_attempt(attempt=failed, access_context=_context(owner_id))
    succeeded = _attempt(
        logical_scan_run_id=scan.logical_scan_run_id,
        attempt_number=2,
        status=_status("SUCCEEDED"),
        completed_at=NOW + timedelta(minutes=1),
        market_data_snapshot_id="complete-market-snapshot",
        market_data_content_hash="b" * 64,
        recovery_of_attempt_id=failed.scan_attempt_id,
        final_strategy_run_id=None,
    )
    strategy_runs = InMemoryStrategyRunRepository(
        {plan.portfolio_id: owner_id}, logical_scan_repository=repository
    )
    final_run = _strategy_run(plan)
    strategy_runs.record_or_get(
        plan=plan,
        run=final_run,
        idempotency_key=IdempotencyKey("recovery-chain-final"),
        logical_scan_run_id=scan.logical_scan_run_id,
        access_context=_context(owner_id),
    )
    succeeded = replace(succeeded, final_strategy_run_id=final_run.strategy_run_id)
    repository.record_attempt(attempt=succeeded, access_context=_context(owner_id))

    with pytest.raises(ValueError, match="completed logical scans"):
        repository.record_attempt(
            attempt=_attempt(
                logical_scan_run_id=scan.logical_scan_run_id,
                attempt_number=3,
                status=_status("FAILED"),
                completed_at=NOW + timedelta(minutes=2),
                failure_code="LATE_FAILURE",
                failure_detail="must not restart after a successful attempt",
                recovery_of_attempt_id=succeeded.scan_attempt_id,
            ),
            access_context=_context(owner_id),
        )


def test_sql_recovery_chain_preserves_auditable_attempt_statuses() -> None:
    from app.persistence.repositories import SqlAlchemyLogicalScanRepository

    owner_id = uuid4()
    plan = _plan(owner_id)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    request = ScanLockRequest(
        plan=plan,
        market_session_date="2026-08-01",
        scan_window_start=NOW,
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        configuration_snapshot_hash="a" * 64,
    )

    with Session(engine) as session:
        session.add_all(
            (
                UserModel(user_id=owner_id, created_at=NOW),
                PortfolioModel(portfolio_id=plan.portfolio_id, user_id=owner_id, created_at=NOW),
                RotationPlanModel(
                    rotation_plan_id=plan.rotation_plan_id,
                    portfolio_id=plan.portfolio_id,
                    created_at=NOW,
                ),
            )
        )
        session.commit()
        repository = SqlAlchemyLogicalScanRepository(session)
        scan = repository.create_or_recover(
            request=request, created_at=NOW, access_context=_context(owner_id)
        )
        final_strategy_run_id = uuid4()
        configuration_snapshot_id = uuid4()
        session.add_all(
            (
                ConfigurationSnapshotModel(
                    snapshot_id=configuration_snapshot_id,
                    content_hash="a" * 64,
                    canonical_format_version="1",
                    parent_versions=[],
                    merged_payload={},
                    canonical_json="{}",
                    config_version=1,
                    target_scope="USER",
                    target_owner_id=owner_id,
                    target_reference_id=plan.rotation_plan_id,
                    runtime_expires_at=None,
                    created_at=NOW,
                    created_by=owner_id,
                ),
                StrategyRunModel(
                    strategy_run_id=final_strategy_run_id,
                    rotation_plan_id=plan.rotation_plan_id,
                    portfolio_id=plan.portfolio_id,
                    logical_scan_run_id=scan.logical_scan_run_id,
                    configuration_snapshot_id=configuration_snapshot_id,
                    configuration_content_hash="a" * 64,
                    configuration_snapshot_created_at=NOW,
                    market_data_snapshot_id="complete-market-snapshot",
                    state_transition="SCANNING->ACTION_PENDING",
                    outputs={},
                    occurred_at=NOW,
                    idempotency_key="scan-attempt-contract",
                ),
            )
        )
        session.flush()
        failed = _attempt(
            logical_scan_run_id=scan.logical_scan_run_id,
            attempt_number=1,
            status=_status("FAILED"),
            completed_at=NOW,
            failure_code="MARKET_TIMEOUT",
            failure_detail="provider timeout",
        )
        succeeded = _attempt(
            logical_scan_run_id=scan.logical_scan_run_id,
            attempt_number=2,
            status=_status("SUCCEEDED"),
            completed_at=NOW + timedelta(minutes=1),
            market_data_snapshot_id="complete-market-snapshot",
            market_data_content_hash="b" * 64,
            recovery_of_attempt_id=failed.scan_attempt_id,
            final_strategy_run_id=final_strategy_run_id,
        )
        repository.record_attempt(attempt=failed, access_context=_context(owner_id))
        repository.attach_final_strategy_run(
            logical_scan_run_id=scan.logical_scan_run_id,
            plan=plan,
            strategy_run_id=final_strategy_run_id,
            completed_at=NOW + timedelta(minutes=1),
            access_context=_context(owner_id),
        )
        repository.record_attempt(attempt=succeeded, access_context=_context(owner_id))
        session.commit()

        assert repository.list_attempts(
            logical_scan_run_id=scan.logical_scan_run_id, access_context=_context(owner_id)
        ) == (failed, succeeded)


def test_sql_scan_attempt_round_trip_preserves_recovery_audit_evidence() -> None:
    from app.persistence.mappers import scan_attempt_from_model, scan_attempt_to_model

    attempt = _attempt(
        logical_scan_run_id=uuid4(),
        attempt_number=2,
        status=_status("DEGRADED"),
        completed_at=NOW + timedelta(minutes=1),
        market_data_snapshot_id="partial-market-snapshot",
        market_data_content_hash="b" * 64,
        failure_code="MISSING_REQUIRED_FIELD",
        failure_detail="a required market field is missing",
        recovery_of_attempt_id=uuid4(),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(scan_attempt_to_model(attempt))
        session.commit()
        model = session.get(ScanAttemptModel, attempt.scan_attempt_id)
        assert model is not None
        restored = scan_attempt_from_model(model)

    assert restored == attempt


def test_database_rejects_nonpositive_scan_attempt_numbers() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            ScanAttemptModel(
                scan_attempt_id=uuid4(),
                logical_scan_run_id=uuid4(),
                attempt_number=0,
                status="FAILED",
                configuration_snapshot_hash="a" * 64,
                market_data_snapshot_id=None,
                market_data_content_hash=None,
                trigger_correlation_id="scheduler:test",
                actor_correlation_id="worker:test",
                started_at=NOW,
                completed_at=NOW,
                failure_code="MARKET_TIMEOUT",
                failure_detail="provider timeout",
                recovery_of_attempt_id=None,
                duplicate_of_attempt_id=None,
                final_strategy_run_id=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_frozen_initial_migration_carries_scan_attempt_recovery_constraints() -> None:
    source = Path("migrations/versions/0001_foundation.py").read_text(encoding="utf-8")

    assert "ck_scan_attempt_number_positive" in source
    assert "configuration_snapshot_hash" in source
    assert "ck_scan_attempt_status" in source
