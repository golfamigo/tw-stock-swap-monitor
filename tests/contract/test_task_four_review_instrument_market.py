"""Regression contracts for market-qualified global instrument identifiers."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from app.domain.entities import Instrument
from app.persistence.mappers import instrument_from_model, instrument_to_model
from app.persistence.models import Base, InstrumentModel
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

NOW = datetime(2026, 8, 1, 1, tzinfo=UTC)


def test_domain_instrument_requires_a_nonblank_market_identifier() -> None:
    instrument = Instrument(instrument_id=uuid4(), market="TWSE", symbol="2330", created_at=NOW)

    assert instrument.market == "TWSE"
    with pytest.raises(ValueError, match="market"):
        Instrument(instrument_id=uuid4(), market=" ", symbol="2330", created_at=NOW)


def test_instrument_mapper_round_trips_the_market_qualified_identity() -> None:
    instrument = Instrument(instrument_id=uuid4(), market="TWSE", symbol="2330", created_at=NOW)

    model = instrument_to_model(instrument)

    assert model.market == "TWSE"
    assert instrument_from_model(model) == instrument


def test_persistence_allows_same_symbol_in_distinct_markets_but_not_a_duplicate_pair() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                InstrumentModel(
                    instrument_id=uuid4(), market="TWSE", symbol="2330", created_at=NOW
                ),
                InstrumentModel(
                    instrument_id=uuid4(), market="TPEx", symbol="2330", created_at=NOW
                ),
            )
        )
        session.commit()

        session.add(
            InstrumentModel(instrument_id=uuid4(), market="TWSE", symbol="2330", created_at=NOW)
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_frozen_migration_uses_the_same_market_symbol_unique_constraint() -> None:
    migration = Path("migrations/versions/0001_foundation.py")

    assert "uq_instruments_market_symbol" in migration.read_text(encoding="utf-8")
