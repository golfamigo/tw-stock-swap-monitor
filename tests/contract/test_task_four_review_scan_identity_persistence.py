"""Regression contracts for durable, auditable pre-provider scan identity."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from app.domain.access import AccessContext
from app.domain.entities import RotationPlan
from app.persistence.models import (
    Base,
    LogicalScanRunModel,
    PortfolioModel,
    RotationPlanModel,
    ScanAttemptModel,
    UserModel,
)
from app.persistence.repositories import SqlAlchemyLogicalScanRepository
from app.repositories.locks import ScanLockRequest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

NOW = datetime(2026, 8, 1, 1, tzinfo=UTC)


def _context(owner_id: UUID) -> AccessContext:
    return AccessContext(
        actor_user_id=owner_id,
        request_id=uuid4(),
        authentication_method="scan-identity-contract",
    )


def _plan(owner_id: UUID) -> RotationPlan:
    return RotationPlan(
        rotation_plan_id=uuid4(),
        portfolio_id=uuid4(),
        candidate_group_ids=(uuid4(),),
        source_position_ids=(),
        protected_position_ids=(),
        created_at=NOW,
    )


def _request(plan: RotationPlan, *, configuration_hash: str = "a" * 64) -> ScanLockRequest:
    return ScanLockRequest(
        plan=plan,
        market_session_date="2026-08-01",
        scan_window_start=NOW,
        scan_interval="PT3M",
        market_timezone="Asia/Taipei",
        configuration_snapshot_hash=configuration_hash,
    )


def _seed_plan(session: Session, *, owner_id: UUID, plan: RotationPlan) -> None:
    session.add(UserModel(user_id=owner_id, created_at=NOW))
    session.flush()
    session.add_all(
        (
            PortfolioModel(portfolio_id=plan.portfolio_id, user_id=owner_id, created_at=NOW),
            RotationPlanModel(
                rotation_plan_id=plan.rotation_plan_id,
                portfolio_id=plan.portfolio_id,
                created_at=NOW,
            ),
        )
    )
    session.commit()


def test_sql_repository_round_trips_complete_scan_identity_and_reconstructs_the_lock_key() -> None:
    owner_id = uuid4()
    plan = _plan(owner_id)
    request = _request(plan)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_plan(session, owner_id=owner_id, plan=plan)
        repository = SqlAlchemyLogicalScanRepository(session)
        created = repository.create_or_recover(
            request=request, created_at=NOW, access_context=_context(owner_id)
        )
        session.commit()
        session.expire_all()

        restored = repository.get(created.logical_scan_run_id, access_context=_context(owner_id))

    assert restored.configuration_snapshot_hash == request.configuration_snapshot_hash
    assert restored.market_timezone == "Asia/Taipei"
    assert restored.scan_identity_format_version == request.identity.format_version
    assert restored.identity.key == request.key


def test_sql_repository_rejects_a_stored_lock_key_with_mismatched_identity_components() -> None:
    owner_id = uuid4()
    plan = _plan(owner_id)
    request = _request(plan)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_plan(session, owner_id=owner_id, plan=plan)
        session.add(
            LogicalScanRunModel(
                logical_scan_run_id=uuid4(),
                rotation_plan_id=plan.rotation_plan_id,
                market_session_date=request.market_session_date,
                scan_window_start=request.scan_window_start,
                scan_interval=request.scan_interval,
                market_timezone=request.market_timezone,
                configuration_snapshot_hash="b" * 64,
                scan_identity_format_version=request.identity.format_version,
                scan_lock_key=request.key,
                status="RUNNING",
                completed_at=None,
                created_at=NOW,
            )
        )
        session.commit()

        with pytest.raises(ValueError, match="identity"):
            SqlAlchemyLogicalScanRepository(session).get_by_lock_key(
                request=request, access_context=_context(owner_id)
            )


def test_models_and_migration_reject_blank_scan_attempt_correlation_ids() -> None:
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in Base.metadata.tables["scan_attempts"].constraints
        if constraint.name is not None and hasattr(constraint, "sqltext")
    }
    migration = Path("migrations/versions/0001_foundation.py").read_text(encoding="utf-8")

    assert "> 0" in constraints["ck_scan_attempt_evidence_bounds"]
    assert "length(trigger_correlation_id) > 0" in migration
    assert "length(actor_correlation_id) > 0" in migration


def test_sqlite_rejects_a_blank_scan_attempt_correlation_id() -> None:
    owner_id = uuid4()
    plan = _plan(owner_id)
    request = _request(plan)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_plan(session, owner_id=owner_id, plan=plan)
        scan = SqlAlchemyLogicalScanRepository(session).create_or_recover(
            request=request, created_at=NOW, access_context=_context(owner_id)
        )
        session.add(
            ScanAttemptModel(
                scan_attempt_id=uuid4(),
                logical_scan_run_id=scan.logical_scan_run_id,
                attempt_number=1,
                status="RUNNING",
                configuration_snapshot_hash=request.configuration_snapshot_hash,
                market_data_snapshot_id=None,
                market_data_content_hash=None,
                trigger_correlation_id="",
                actor_correlation_id="worker:contract",
                started_at=NOW,
                completed_at=None,
                failure_code=None,
                failure_detail=None,
                recovery_of_attempt_id=None,
                duplicate_of_attempt_id=None,
                final_strategy_run_id=None,
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()
