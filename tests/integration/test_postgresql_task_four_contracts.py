"""PostgreSQL-only Task 4 persistence contracts, enabled solely by CI database URL."""

from __future__ import annotations

import os
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from app.application.configuration import (
    CANONICAL_FORMAT_VERSION,
    ResolvedConfigurationSnapshot,
    canonical_content_hash,
    canonical_json,
)
from app.application.configuration_snapshots import PersistedConfigurationSnapshot
from app.domain.access import AccessContext
from app.domain.entities import Position
from app.domain.enums import PositionRole, PositionStatus, Scope
from app.domain.errors import IdempotencyConflictError, PositionAlreadyOpenError
from app.domain.values import InstrumentRef, Ownership, Quantity
from app.persistence.mappers import position_to_model
from app.persistence.models import Base, InstrumentModel, PortfolioModel, UserModel
from app.persistence.repositories import (
    SqlAlchemyConfigurationSnapshotRepository,
    SqlAlchemyPositionRepository,
)
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")
POSTGRES_ONLY = pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="requires CI-only POSTGRES_TEST_DATABASE_URL",
)
NOW = datetime(2026, 8, 1, 9, tzinfo=UTC)


@pytest.fixture
def engine() -> Generator[Engine, None, None]:
    assert POSTGRES_TEST_DATABASE_URL is not None
    database_engine = create_engine(POSTGRES_TEST_DATABASE_URL)
    try:
        yield database_engine
    finally:
        database_engine.dispose()


def _context(user_id: UUID) -> AccessContext:
    return AccessContext(
        actor_user_id=user_id,
        is_administrator=False,
        request_id=uuid4(),
        authentication_method="postgres-contract-test",
    )


def _seed_portfolio(
    session: Session,
    *,
    user_id: UUID | None = None,
    portfolio_id: UUID | None = None,
    instrument_id: UUID | None = None,
) -> tuple[UUID, UUID, UUID]:
    user_id = user_id or uuid4()
    portfolio_id = portfolio_id or uuid4()
    instrument_id = instrument_id or uuid4()
    session.add(UserModel(user_id=user_id, created_at=NOW))
    session.flush()
    session.add_all(
        (
            PortfolioModel(portfolio_id=portfolio_id, user_id=user_id, created_at=NOW),
            InstrumentModel(
                instrument_id=instrument_id,
                symbol=f"PG{instrument_id.hex[:20]}",
                created_at=NOW,
            ),
        )
    )
    session.commit()
    return user_id, portfolio_id, instrument_id


def _position(*, portfolio_id: UUID, instrument_id: UUID, opened_at: datetime) -> Position:
    return Position(
        position_id=uuid4(),
        portfolio_id=portfolio_id,
        instrument=InstrumentRef(instrument_id),
        quantity=Quantity(Decimal("10.00")),
        role=PositionRole.NORMAL,
        status=PositionStatus.OPEN,
        opened_at=opened_at,
    )


def test_seed_portfolio_flushes_parent_before_foreign_key_child_on_sqlite() -> None:
    """Keep the PostgreSQL fixture's parent/child write order reproducible locally."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        owner_id, portfolio_id, instrument_id = _seed_portfolio(session)

        assert session.get(UserModel, owner_id) is not None
        assert session.get(PortfolioModel, portfolio_id) is not None
        assert session.get(InstrumentModel, instrument_id) is not None


def test_sqlite_partial_open_index_keeps_distinct_closed_history_and_outer_work() -> None:
    """A distinct closed record must not replace the open position's primary key."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        owner_id, portfolio_id, instrument_id = _seed_portfolio(session)
        repository = SqlAlchemyPositionRepository(session)
        opened = _position(portfolio_id=portfolio_id, instrument_id=instrument_id, opened_at=NOW)
        closed_history = replace(opened.close(NOW), position_id=uuid4())
        repository.add(opened, access_context=_context(owner_id))
        repository.add(closed_history, access_context=_context(owner_id))
        session.commit()

        outer_instrument_id = uuid4()
        session.add(
            InstrumentModel(
                instrument_id=outer_instrument_id,
                symbol=f"PG{outer_instrument_id.hex[:20]}",
                created_at=NOW,
            )
        )
        with pytest.raises(PositionAlreadyOpenError):
            repository.add(
                _position(portfolio_id=portfolio_id, instrument_id=instrument_id, opened_at=NOW),
                access_context=_context(owner_id),
            )
        session.commit()

        assert repository.get(opened.position_id, access_context=_context(owner_id)) == opened
        assert (
            repository.get(closed_history.position_id, access_context=_context(owner_id))
            == closed_history
        )
        assert session.get(InstrumentModel, outer_instrument_id) is not None


@POSTGRES_ONLY
def test_postgres_partial_open_index_keeps_closed_history_and_outer_transaction(
    engine: Engine,
) -> None:
    with Session(engine) as session:
        owner_id, portfolio_id, instrument_id = _seed_portfolio(session)
        repository = SqlAlchemyPositionRepository(session)
        opened = _position(portfolio_id=portfolio_id, instrument_id=instrument_id, opened_at=NOW)
        closed_history = replace(opened.close(NOW), position_id=uuid4())
        repository.add(opened, access_context=_context(owner_id))
        repository.add(closed_history, access_context=_context(owner_id))
        session.commit()
        outer_instrument_id = uuid4()
        session.add(
            InstrumentModel(
                instrument_id=outer_instrument_id,
                symbol=f"PG{outer_instrument_id.hex[:20]}",
                created_at=NOW,
            )
        )

        with pytest.raises(PositionAlreadyOpenError):
            repository.add(
                _position(portfolio_id=portfolio_id, instrument_id=instrument_id, opened_at=NOW),
                access_context=_context(owner_id),
            )
        session.commit()
        assert session.get(InstrumentModel, outer_instrument_id) is not None


@POSTGRES_ONLY
def test_postgres_uuid_and_json_snapshot_round_trip(engine: Engine) -> None:
    with Session(engine) as session:
        owner_id, _, _ = _seed_portfolio(session)
        payload: dict[str, object] = {
            "configuration_name": "postgres-contract",
            "settings": {"source": "postgres"},
            "rules": [],
            "keyed_items": [],
            "extensions": {},
            "nullable_note": None,
        }
        snapshot = PersistedConfigurationSnapshot(
            snapshot_id=uuid4(),
            resolved_snapshot=ResolvedConfigurationSnapshot(
                payload=payload,
                parent_versions=(),
                created_by=owner_id,
                created_at=NOW,
                runtime_expires_at=None,
                canonical_format_version=CANONICAL_FORMAT_VERSION,
                content_hash=canonical_content_hash(payload),
                canonical_json=canonical_json(payload),
            ),
            config_version=1,
            target_ownership=Ownership(Scope.USER, owner_id),
            target_reference_id=uuid4(),
        )
        repository = SqlAlchemyConfigurationSnapshotRepository(session)
        recorded = repository.record_or_get(snapshot=snapshot, access_context=_context(owner_id))
        session.commit()

        assert repository.get(snapshot.snapshot_id, access_context=_context(owner_id)) == recorded


@POSTGRES_ONLY
def test_postgres_rejects_a_second_snapshot_for_the_same_target_version(engine: Engine) -> None:
    with Session(engine) as session:
        owner_id, _, _ = _seed_portfolio(session)
        target_reference_id = uuid4()

        def snapshot(configuration_name: str) -> PersistedConfigurationSnapshot:
            payload: dict[str, object] = {
                "configuration_name": configuration_name,
                "settings": {"source": "postgres"},
                "rules": [],
                "keyed_items": [],
                "extensions": {},
                "nullable_note": None,
            }
            return PersistedConfigurationSnapshot(
                snapshot_id=uuid4(),
                resolved_snapshot=ResolvedConfigurationSnapshot(
                    payload=payload,
                    parent_versions=(),
                    created_by=owner_id,
                    created_at=NOW,
                    runtime_expires_at=None,
                    canonical_format_version=CANONICAL_FORMAT_VERSION,
                    content_hash=canonical_content_hash(payload),
                    canonical_json=canonical_json(payload),
                ),
                config_version=1,
                target_ownership=Ownership(Scope.USER, owner_id),
                target_reference_id=target_reference_id,
            )

        repository = SqlAlchemyConfigurationSnapshotRepository(session)
        repository.record_or_get(snapshot=snapshot("first"), access_context=_context(owner_id))
        with pytest.raises(IdempotencyConflictError):
            repository.record_or_get(
                snapshot=snapshot("contender"), access_context=_context(owner_id)
            )


@POSTGRES_ONLY
def test_postgres_timezone_round_trip_normalizes_an_iana_market_time(engine: Engine) -> None:
    taipei_open = datetime(2026, 8, 1, 9, tzinfo=ZoneInfo("Asia/Taipei"))
    with Session(engine) as session:
        owner_id, portfolio_id, instrument_id = _seed_portfolio(session)
        position = _position(
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            opened_at=taipei_open,
        )
        repository = SqlAlchemyPositionRepository(session)
        repository.add(position, access_context=_context(owner_id))
        session.commit()

        restored = repository.get(position.position_id, access_context=_context(owner_id))
    assert restored.opened_at == taipei_open.astimezone(UTC)


@POSTGRES_ONLY
def test_postgres_concurrent_open_position_conflict_is_mapped_without_data_loss(
    engine: Engine,
) -> None:
    with Session(engine) as primary_session:
        owner_id, portfolio_id, instrument_id = _seed_portfolio(primary_session)
        first = _position(portfolio_id=portfolio_id, instrument_id=instrument_id, opened_at=NOW)
        primary_session.add(position_to_model(first))
        primary_session.flush()
        contender_started = Event()

        def contend() -> type[Exception] | None:
            with Session(engine) as contender_session:
                contender_started.set()
                try:
                    SqlAlchemyPositionRepository(contender_session).add(
                        _position(
                            portfolio_id=portfolio_id,
                            instrument_id=instrument_id,
                            opened_at=NOW,
                        ),
                        access_context=_context(owner_id),
                    )
                    contender_session.commit()
                except PositionAlreadyOpenError as error:
                    contender_session.rollback()
                    return type(error)
            return None

        with ThreadPoolExecutor(max_workers=1) as executor:
            contender = executor.submit(contend)
            assert contender_started.wait(timeout=5)
            primary_session.commit()
            assert contender.result(timeout=10) is PositionAlreadyOpenError
