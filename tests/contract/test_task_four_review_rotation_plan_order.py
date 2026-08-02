"""Regression contracts for preserving rotation-plan tuple priority through SQL storage."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from app.domain.access import AccessContext
from app.domain.entities import RotationPlan
from app.domain.enums import PositionRole, PositionStatus
from app.persistence.models import (
    Base,
    CandidateGroupModel,
    InstrumentModel,
    PortfolioModel,
    PositionModel,
    UserModel,
    rotation_plan_candidate_groups,
    rotation_plan_protected_positions,
    rotation_plan_source_positions,
)
from app.persistence.repositories import SqlAlchemyRotationPlanRepository
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

NOW = datetime(2026, 8, 1, 9, tzinfo=UTC)


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _context(owner_id: UUID) -> AccessContext:
    return AccessContext(
        actor_user_id=owner_id,
        is_administrator=False,
        request_id=_uuid(99),
        authentication_method="contract-test",
    )


def test_sql_rotation_plan_round_trip_preserves_each_tuple_ordinal() -> None:
    owner_id, portfolio_id, plan_id = _uuid(100), _uuid(101), _uuid(102)
    candidate_group_ids = (_uuid(20), _uuid(2), _uuid(11))
    source_position_ids = (_uuid(40), _uuid(4))
    protected_position_ids = (_uuid(30), _uuid(3))
    plan = RotationPlan(
        rotation_plan_id=plan_id,
        portfolio_id=portfolio_id,
        candidate_group_ids=candidate_group_ids,
        source_position_ids=source_position_ids,
        protected_position_ids=protected_position_ids,
        created_at=NOW,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                UserModel(user_id=owner_id, created_at=NOW),
                PortfolioModel(portfolio_id=portfolio_id, user_id=owner_id, created_at=NOW),
                *(
                    CandidateGroupModel(
                        candidate_group_id=candidate_group_id,
                        user_id=owner_id,
                        created_at=NOW,
                    )
                    for candidate_group_id in candidate_group_ids
                ),
                *(
                    InstrumentModel(
                        instrument_id=instrument_id,
                        market="TWSE",
                        symbol=f"ORDER{instrument_id.int}",
                        created_at=NOW,
                    )
                    for instrument_id in (_uuid(70), _uuid(71), _uuid(72), _uuid(73))
                ),
                *(
                    PositionModel(
                        position_id=position_id,
                        portfolio_id=portfolio_id,
                        instrument_id=instrument_id,
                        quantity=Decimal("10.00"),
                        role=PositionRole.NORMAL.value,
                        status=PositionStatus.OPEN.value,
                        opened_at=NOW,
                        closed_at=None,
                    )
                    for position_id, instrument_id in zip(
                        (*source_position_ids, *protected_position_ids),
                        (_uuid(70), _uuid(71), _uuid(72), _uuid(73)),
                        strict=True,
                    )
                ),
            )
        )
        session.commit()
        repository = SqlAlchemyRotationPlanRepository(session)
        repository.add(plan, access_context=_context(owner_id))
        session.commit()

        assert repository.get(plan_id, access_context=_context(owner_id)) == plan
        candidate_group_rows: list[tuple[UUID, int]] = [
            (candidate_group_id, ordinal)
            for candidate_group_id, ordinal in session.execute(
                select(
                    rotation_plan_candidate_groups.c.candidate_group_id,
                    rotation_plan_candidate_groups.c.ordinal,
                )
                .where(rotation_plan_candidate_groups.c.rotation_plan_id == plan_id)
                .order_by(rotation_plan_candidate_groups.c.ordinal)
            ).tuples()
        ]
        assert candidate_group_rows == [
            (candidate_group_id, ordinal)
            for ordinal, candidate_group_id in enumerate(candidate_group_ids)
        ]
        source_position_rows: list[tuple[UUID, int]] = [
            (position_id, ordinal)
            for position_id, ordinal in session.execute(
                select(
                    rotation_plan_source_positions.c.position_id,
                    rotation_plan_source_positions.c.ordinal,
                )
                .where(rotation_plan_source_positions.c.rotation_plan_id == plan_id)
                .order_by(rotation_plan_source_positions.c.ordinal)
            ).tuples()
        ]
        assert source_position_rows == [
            (position_id, ordinal) for ordinal, position_id in enumerate(source_position_ids)
        ]
        protected_position_rows: list[tuple[UUID, int]] = [
            (position_id, ordinal)
            for position_id, ordinal in session.execute(
                select(
                    rotation_plan_protected_positions.c.position_id,
                    rotation_plan_protected_positions.c.ordinal,
                )
                .where(rotation_plan_protected_positions.c.rotation_plan_id == plan_id)
                .order_by(rotation_plan_protected_positions.c.ordinal)
            ).tuples()
        ]
        assert protected_position_rows == [
            (position_id, ordinal) for ordinal, position_id in enumerate(protected_position_ids)
        ]


def test_association_ordinal_constraints_are_frozen_in_the_initial_migration() -> None:
    source = Path("migrations/versions/0001_foundation.py").read_text(encoding="utf-8")

    for table_name in (
        "rotation_plan_candidate_groups",
        "rotation_plan_source_positions",
        "rotation_plan_protected_positions",
    ):
        assert table_name in source
    assert "ordinal" in source
    assert "ck_rotation_plan_association_ordinal" in source
    assert "uq_rotation_plan_association_ordinal" in source
