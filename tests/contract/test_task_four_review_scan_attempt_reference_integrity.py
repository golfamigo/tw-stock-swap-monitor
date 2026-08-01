"""Regression contracts for credible ScanAttempt cross-record audit references."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from app.domain.access import AccessContext
from app.domain.entities import Portfolio, RotationPlan, ScanAttempt, StrategyRun
from app.domain.enums import ScanAttemptStatus
from app.domain.values import ConfigurationSnapshotRef, IdempotencyKey
from app.persistence.models import (
    Base,
    ConfigurationSnapshotModel,
    PortfolioModel,
    RotationPlanModel,
    StrategyRunModel,
    UserModel,
)
from app.persistence.repositories import SqlAlchemyLogicalScanRepository
from app.repositories.locks import ScanLockRequest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

NOW = datetime(2026, 8, 1, 9, tzinfo=UTC)


def _context(owner_id: UUID) -> AccessContext:
    return AccessContext(
        actor_user_id=owner_id,
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


def _request(plan: RotationPlan, *, minute: int) -> ScanLockRequest:
    return ScanLockRequest(
        plan=plan,
        market_session_date="2026-08-01",
        scan_window_start=NOW + timedelta(minutes=minute),
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        configuration_snapshot_hash="a" * 64,
    )


def _attempt(
    *,
    logical_scan_run_id: UUID,
    attempt_number: int,
    status: ScanAttemptStatus,
    recovery_of_attempt_id: UUID | None = None,
    duplicate_of_attempt_id: UUID | None = None,
    final_strategy_run_id: UUID | None = None,
) -> ScanAttempt:
    succeeded = status is ScanAttemptStatus.SUCCEEDED
    duplicate = duplicate_of_attempt_id is not None
    failed = status is ScanAttemptStatus.FAILED
    return ScanAttempt(
        scan_attempt_id=uuid4(),
        logical_scan_run_id=logical_scan_run_id,
        attempt_number=attempt_number,
        status=status,
        configuration_snapshot_hash="a" * 64,
        market_data_snapshot_id="market-snapshot" if succeeded and not duplicate else None,
        market_data_content_hash="b" * 64 if succeeded and not duplicate else None,
        trigger_correlation_id="scheduler:contract",
        actor_correlation_id="worker:contract",
        started_at=NOW,
        completed_at=None if status is ScanAttemptStatus.RUNNING else NOW,
        failure_code="MARKET_TIMEOUT" if failed else None,
        failure_detail="provider timeout" if failed else None,
        recovery_of_attempt_id=recovery_of_attempt_id,
        duplicate_of_attempt_id=duplicate_of_attempt_id,
        final_strategy_run_id=final_strategy_run_id,
    )


def _strategy_run(plan: RotationPlan, *, strategy_run_id: UUID | None = None) -> StrategyRun:
    return StrategyRun(
        strategy_run_id=strategy_run_id or uuid4(),
        rotation_plan_id=plan.rotation_plan_id,
        portfolio_id=plan.portfolio_id,
        configuration_snapshot=ConfigurationSnapshotRef(uuid4(), "a" * 64, NOW),
        market_data_snapshot_id="market-snapshot",
        state_transition="SCANNING->ACTION_PENDING",
        outputs={"source": "scan-attempt-reference-contract"},
        occurred_at=NOW,
    )


def _persist_sql_strategy_run(
    session: Session,
    *,
    owner_id: UUID,
    plan: RotationPlan,
    logical_scan_run_id: UUID | None,
) -> UUID:
    configuration_snapshot_id = uuid4()
    strategy_run_id = uuid4()
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
                strategy_run_id=strategy_run_id,
                rotation_plan_id=plan.rotation_plan_id,
                portfolio_id=plan.portfolio_id,
                logical_scan_run_id=logical_scan_run_id,
                configuration_snapshot_id=configuration_snapshot_id,
                configuration_content_hash="a" * 64,
                configuration_snapshot_created_at=NOW,
                market_data_snapshot_id="market-snapshot",
                state_transition="SCANNING->ACTION_PENDING",
                outputs={"source": "scan-attempt-reference-contract"},
                occurred_at=NOW,
                idempotency_key=strategy_run_id.hex,
            ),
        )
    )
    session.flush()
    return strategy_run_id


def _seed_sql_plan(session: Session, *, owner_id: UUID, plan: RotationPlan) -> None:
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


def test_in_memory_rejects_a_forged_final_strategy_run_reference() -> None:
    from app.persistence.in_memory import InMemoryLogicalScanRepository

    owner_id = uuid4()
    plan = _plan(owner_id)
    repository = InMemoryLogicalScanRepository({plan.portfolio_id: owner_id})
    scan = repository.create_or_recover(
        request=_request(plan, minute=0), created_at=NOW, access_context=_context(owner_id)
    )

    with pytest.raises(ValueError, match="final strategy run"):
        repository.record_attempt(
            attempt=_attempt(
                logical_scan_run_id=scan.logical_scan_run_id,
                attempt_number=1,
                status=ScanAttemptStatus.SUCCEEDED,
                final_strategy_run_id=uuid4(),
            ),
            access_context=_context(owner_id),
        )


def test_in_memory_allows_only_same_scan_duplicate_references() -> None:
    from app.persistence.in_memory import InMemoryLogicalScanRepository

    owner_id = uuid4()
    plan = _plan(owner_id)
    repository = InMemoryLogicalScanRepository({plan.portfolio_id: owner_id})
    first_scan = repository.create_or_recover(
        request=_request(plan, minute=0), created_at=NOW, access_context=_context(owner_id)
    )
    first_success = _attempt(
        logical_scan_run_id=first_scan.logical_scan_run_id,
        attempt_number=1,
        status=ScanAttemptStatus.SUCCEEDED,
    )
    repository.record_attempt(attempt=first_success, access_context=_context(owner_id))
    duplicate = _attempt(
        logical_scan_run_id=first_scan.logical_scan_run_id,
        attempt_number=2,
        status=ScanAttemptStatus.SUCCEEDED,
        duplicate_of_attempt_id=first_success.scan_attempt_id,
    )
    repository.record_attempt(attempt=duplicate, access_context=_context(owner_id))


def test_in_memory_final_evidence_requires_an_actual_completed_final_run() -> None:
    from app.persistence.in_memory import (
        InMemoryLogicalScanRepository,
        InMemoryStrategyRunRepository,
    )

    owner_id = uuid4()
    plan = _plan(owner_id)
    repository = InMemoryLogicalScanRepository({plan.portfolio_id: owner_id})
    scan = repository.create_or_recover(
        request=_request(plan, minute=0), created_at=NOW, access_context=_context(owner_id)
    )
    strategy_runs = InMemoryStrategyRunRepository(
        {plan.portfolio_id: owner_id}, logical_scan_repository=repository
    )
    final_run = _strategy_run(plan)
    strategy_runs.record_or_get(
        plan=plan,
        run=final_run,
        idempotency_key=IdempotencyKey("completed-final"),
        logical_scan_run_id=scan.logical_scan_run_id,
        access_context=_context(owner_id),
    )

    repository.record_attempt(
        attempt=_attempt(
            logical_scan_run_id=scan.logical_scan_run_id,
            attempt_number=1,
            status=ScanAttemptStatus.SUCCEEDED,
            final_strategy_run_id=final_run.strategy_run_id,
        ),
        access_context=_context(owner_id),
    )


def test_in_memory_has_no_free_final_strategy_run_registration() -> None:
    from app.persistence.in_memory import InMemoryLogicalScanRepository

    assert not hasattr(InMemoryLogicalScanRepository({}), "register_strategy_run_reference")


def test_in_memory_rejects_a_running_scan_final_reference() -> None:
    from app.persistence.in_memory import InMemoryLogicalScanRepository

    owner_id = uuid4()
    plan = _plan(owner_id)
    repository = InMemoryLogicalScanRepository({plan.portfolio_id: owner_id})
    scan = repository.create_or_recover(
        request=_request(plan, minute=0), created_at=NOW, access_context=_context(owner_id)
    )

    with pytest.raises(ValueError, match="completed final"):
        repository.record_attempt(
            attempt=_attempt(
                logical_scan_run_id=scan.logical_scan_run_id,
                attempt_number=1,
                status=ScanAttemptStatus.SUCCEEDED,
                final_strategy_run_id=uuid4(),
            ),
            access_context=_context(owner_id),
        )


def test_in_memory_rejects_nonfinal_and_cross_scan_final_references() -> None:
    from app.persistence.in_memory import (
        InMemoryLogicalScanRepository,
        InMemoryStrategyRunRepository,
    )

    owner_id = uuid4()
    plan = _plan(owner_id)
    repository = InMemoryLogicalScanRepository({plan.portfolio_id: owner_id})
    strategy_runs = InMemoryStrategyRunRepository(
        {plan.portfolio_id: owner_id}, logical_scan_repository=repository
    )
    first_scan = repository.create_or_recover(
        request=_request(plan, minute=0), created_at=NOW, access_context=_context(owner_id)
    )
    second_scan = repository.create_or_recover(
        request=_request(plan, minute=3), created_at=NOW, access_context=_context(owner_id)
    )
    first_final = _strategy_run(plan)
    second_final = _strategy_run(plan)
    unlinked = _strategy_run(plan)
    strategy_runs.record_or_get(
        plan=plan,
        run=first_final,
        idempotency_key=IdempotencyKey("first-final"),
        logical_scan_run_id=first_scan.logical_scan_run_id,
        access_context=_context(owner_id),
    )
    strategy_runs.record_or_get(
        plan=plan,
        run=second_final,
        idempotency_key=IdempotencyKey("second-final"),
        logical_scan_run_id=second_scan.logical_scan_run_id,
        access_context=_context(owner_id),
    )
    strategy_runs.record_or_get(
        plan=plan,
        run=unlinked,
        idempotency_key=IdempotencyKey("unlinked-final"),
        access_context=_context(owner_id),
    )

    with pytest.raises(ValueError, match="actual final"):
        repository.record_attempt(
            attempt=_attempt(
                logical_scan_run_id=first_scan.logical_scan_run_id,
                attempt_number=1,
                status=ScanAttemptStatus.SUCCEEDED,
                final_strategy_run_id=unlinked.strategy_run_id,
            ),
            access_context=_context(owner_id),
        )
    with pytest.raises(ValueError, match="actual final"):
        repository.record_attempt(
            attempt=_attempt(
                logical_scan_run_id=second_scan.logical_scan_run_id,
                attempt_number=1,
                status=ScanAttemptStatus.SUCCEEDED,
                final_strategy_run_id=first_final.strategy_run_id,
            ),
            access_context=_context(owner_id),
        )


def test_in_memory_rejects_cross_scan_duplicate_and_recovery_references() -> None:
    from app.persistence.in_memory import InMemoryLogicalScanRepository

    owner_id = uuid4()
    plan = _plan(owner_id)
    repository = InMemoryLogicalScanRepository({plan.portfolio_id: owner_id})
    first_scan = repository.create_or_recover(
        request=_request(plan, minute=0), created_at=NOW, access_context=_context(owner_id)
    )
    first_success = _attempt(
        logical_scan_run_id=first_scan.logical_scan_run_id,
        attempt_number=1,
        status=ScanAttemptStatus.SUCCEEDED,
    )
    repository.record_attempt(attempt=first_success, access_context=_context(owner_id))
    second_scan = repository.create_or_recover(
        request=_request(plan, minute=3), created_at=NOW, access_context=_context(owner_id)
    )
    second_failure = _attempt(
        logical_scan_run_id=second_scan.logical_scan_run_id,
        attempt_number=1,
        status=ScanAttemptStatus.FAILED,
    )
    repository.record_attempt(attempt=second_failure, access_context=_context(owner_id))

    with pytest.raises(ValueError, match="same logical scan"):
        repository.record_attempt(
            attempt=_attempt(
                logical_scan_run_id=second_scan.logical_scan_run_id,
                attempt_number=2,
                status=ScanAttemptStatus.SUCCEEDED,
                recovery_of_attempt_id=second_failure.scan_attempt_id,
                duplicate_of_attempt_id=first_success.scan_attempt_id,
            ),
            access_context=_context(owner_id),
        )
    with pytest.raises(ValueError, match="same logical scan"):
        repository.record_attempt(
            attempt=_attempt(
                logical_scan_run_id=second_scan.logical_scan_run_id,
                attempt_number=2,
                status=ScanAttemptStatus.SUCCEEDED,
                recovery_of_attempt_id=first_success.scan_attempt_id,
            ),
            access_context=_context(owner_id),
        )


def test_sql_repository_rejects_a_forged_final_strategy_run_reference() -> None:
    owner_id = uuid4()
    plan = _plan(owner_id)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_sql_plan(session, owner_id=owner_id, plan=plan)
        repository = SqlAlchemyLogicalScanRepository(session)
        scan = repository.create_or_recover(
            request=_request(plan, minute=0), created_at=NOW, access_context=_context(owner_id)
        )

        with pytest.raises(ValueError, match="final strategy run"):
            repository.record_attempt(
                attempt=_attempt(
                    logical_scan_run_id=scan.logical_scan_run_id,
                    attempt_number=1,
                    status=ScanAttemptStatus.SUCCEEDED,
                    final_strategy_run_id=uuid4(),
                ),
                access_context=_context(owner_id),
            )


def test_sql_rejects_a_staged_final_run_before_scan_completion() -> None:
    owner_id = uuid4()
    plan = _plan(owner_id)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_sql_plan(session, owner_id=owner_id, plan=plan)
        repository = SqlAlchemyLogicalScanRepository(session)
        scan = repository.create_or_recover(
            request=_request(plan, minute=0), created_at=NOW, access_context=_context(owner_id)
        )
        staged_run_id = _persist_sql_strategy_run(
            session,
            owner_id=owner_id,
            plan=plan,
            logical_scan_run_id=scan.logical_scan_run_id,
        )

        with pytest.raises(ValueError, match="completed final"):
            repository.record_attempt(
                attempt=_attempt(
                    logical_scan_run_id=scan.logical_scan_run_id,
                    attempt_number=1,
                    status=ScanAttemptStatus.SUCCEEDED,
                    final_strategy_run_id=staged_run_id,
                ),
                access_context=_context(owner_id),
            )


def test_sql_accepts_the_completed_scan_actual_final_run_reference() -> None:
    owner_id = uuid4()
    plan = _plan(owner_id)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_sql_plan(session, owner_id=owner_id, plan=plan)
        repository = SqlAlchemyLogicalScanRepository(session)
        scan = repository.create_or_recover(
            request=_request(plan, minute=0), created_at=NOW, access_context=_context(owner_id)
        )
        final_run_id = _persist_sql_strategy_run(
            session,
            owner_id=owner_id,
            plan=plan,
            logical_scan_run_id=scan.logical_scan_run_id,
        )
        repository.attach_final_strategy_run(
            logical_scan_run_id=scan.logical_scan_run_id,
            plan=plan,
            strategy_run_id=final_run_id,
            completed_at=NOW,
            access_context=_context(owner_id),
        )

        repository.record_attempt(
            attempt=_attempt(
                logical_scan_run_id=scan.logical_scan_run_id,
                attempt_number=1,
                status=ScanAttemptStatus.SUCCEEDED,
                final_strategy_run_id=final_run_id,
            ),
            access_context=_context(owner_id),
        )


def test_models_and_frozen_migration_define_same_scan_reference_constraints() -> None:
    scan_attempt_constraint_names = {
        constraint.name for constraint in Base.metadata.tables["scan_attempts"].constraints
    }
    strategy_run_constraint_names = {
        constraint.name for constraint in Base.metadata.tables["strategy_runs"].constraints
    }
    migration = Path("migrations/versions/0001_foundation.py").read_text(encoding="utf-8")

    assert {
        "fk_scan_attempt_recovery_same_scan",
        "fk_scan_attempt_duplicate_same_scan",
        "fk_scan_attempt_final_strategy_same_scan",
    } <= scan_attempt_constraint_names
    assert "uq_strategy_run_logical_scan_identity" in strategy_run_constraint_names
    assert "fk_scan_attempt_recovery_same_scan" in migration
    assert "fk_scan_attempt_duplicate_same_scan" in migration
    assert "fk_scan_attempt_final_strategy_same_scan" in migration
