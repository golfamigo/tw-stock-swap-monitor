"""Repository contracts for the positive-quantity position invariant."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from app.domain.access import AccessContext
from app.domain.entities import Position
from app.domain.enums import PositionRole, PositionStatus
from app.domain.errors import NonPositivePositionQuantityError
from app.domain.values import InstrumentRef, Quantity
from app.persistence.in_memory import InMemoryPositionRepository
from app.persistence.models import Base, InstrumentModel, PortfolioModel, PositionModel, UserModel
from app.persistence.repositories import SqlAlchemyPositionRepository
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

NOW = datetime(2026, 8, 1, 1, tzinfo=UTC)


def _context(owner_id: UUID) -> AccessContext:
    return AccessContext(
        actor_user_id=owner_id,
        request_id=uuid4(),
        authentication_method="position-quantity-contract",
    )


def _position(*, portfolio_id: UUID, instrument_id: UUID) -> Position:
    return Position(
        position_id=uuid4(),
        portfolio_id=portfolio_id,
        instrument=InstrumentRef(instrument_id),
        quantity=Quantity(Decimal("1")),
        role=PositionRole.ROTATION_SOURCE,
        status=PositionStatus.OPEN,
        opened_at=NOW,
    )


def test_in_memory_and_sql_repositories_round_trip_the_same_positive_position() -> None:
    owner_id, portfolio_id, instrument_id = uuid4(), uuid4(), uuid4()
    position = _position(portfolio_id=portfolio_id, instrument_id=instrument_id)
    context = _context(owner_id)

    in_memory = InMemoryPositionRepository({portfolio_id: owner_id})
    in_memory.add(position, access_context=context)
    assert in_memory.get(position.position_id, access_context=context) == position

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserModel(user_id=owner_id, created_at=NOW))
        session.flush()
        session.add_all(
            (
                PortfolioModel(portfolio_id=portfolio_id, user_id=owner_id, created_at=NOW),
                InstrumentModel(
                    instrument_id=instrument_id,
                    market="TWSE",
                    symbol="POSITION-CONTRACT",
                    created_at=NOW,
                ),
            )
        )
        session.commit()

        sql = SqlAlchemyPositionRepository(session)
        sql.add(position, access_context=context)
        session.commit()
        assert sql.get(position.position_id, access_context=context) == position


def test_zero_quantity_is_rejected_in_domain_and_at_the_sql_boundary() -> None:
    owner_id, portfolio_id, instrument_id = uuid4(), uuid4(), uuid4()
    with pytest.raises(NonPositivePositionQuantityError, match="positive"):
        Position(
            position_id=uuid4(),
            portfolio_id=portfolio_id,
            instrument=InstrumentRef(instrument_id),
            quantity=Quantity(Decimal("0")),
            role=PositionRole.ROTATION_SOURCE,
            status=PositionStatus.OPEN,
            opened_at=NOW,
        )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserModel(user_id=owner_id, created_at=NOW))
        session.flush()
        session.add_all(
            (
                PortfolioModel(portfolio_id=portfolio_id, user_id=owner_id, created_at=NOW),
                InstrumentModel(
                    instrument_id=instrument_id,
                    market="TWSE",
                    symbol="ZERO-QUANTITY-CONTRACT",
                    created_at=NOW,
                ),
                PositionModel(
                    position_id=uuid4(),
                    portfolio_id=portfolio_id,
                    instrument_id=instrument_id,
                    quantity=Decimal("0"),
                    role=PositionRole.ROTATION_SOURCE.value,
                    status=PositionStatus.OPEN.value,
                    opened_at=NOW,
                    closed_at=None,
                ),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
