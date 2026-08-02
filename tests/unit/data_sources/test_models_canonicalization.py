"""Order-independent canonical market-data snapshot evidence."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from app.data_sources.models import Bar, DataQuality, MarketDataRequest, MarketDataSnapshot, Quote
from app.domain.entities import Instrument

MARKET = "source-a"
INSTRUMENT_A = Instrument(
    instrument_id=UUID("00000000-0000-0000-0000-000000000801"),
    market=MARKET,
    symbol="instrument-a",
    created_at=datetime(2030, 1, 1, tzinfo=UTC),
)
INSTRUMENT_B = Instrument(
    instrument_id=UUID("00000000-0000-0000-0000-000000000802"),
    market=MARKET,
    symbol="instrument-b",
    created_at=datetime(2030, 1, 1, tzinfo=UTC),
)


def _request(instruments: tuple[Instrument, ...]) -> MarketDataRequest:
    return MarketDataRequest(
        request_id=UUID("00000000-0000-0000-0000-000000000803"),
        instruments=instruments,
        requested_at=datetime(2030, 1, 2, 13, 0, tzinfo=UTC),
        intraday_start=datetime(2030, 1, 2, 10, 0, tzinfo=UTC),
        intraday_end=datetime(2030, 1, 2, 11, 0, tzinfo=UTC),
        intraday_interval=timedelta(minutes=15),
        daily_start=date(2030, 1, 2),
        daily_end=date(2030, 1, 3),
        maximum_data_delay=timedelta(minutes=5),
    )


def _quality() -> DataQuality:
    return DataQuality(
        provider="fixture-source",
        fetched_at=datetime(2030, 1, 2, 13, 0, tzinfo=UTC),
        source_timestamp=datetime(2030, 1, 2, 12, 59, tzinfo=UTC),
        delay=timedelta(minutes=1),
        stale=False,
        missing_fields=(),
        bars_complete=True,
        anomalies=(),
        confidence=Decimal("1"),
    )


def _bar(instrument: Instrument, *, starts_at: datetime, ends_at: datetime) -> Bar:
    return Bar(
        instrument_id=instrument.instrument_id,
        starts_at=starts_at,
        ends_at=ends_at,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10"),
        volume=Decimal("1"),
    )


def _snapshot(
    *,
    instruments: tuple[Instrument, ...],
    quotes: tuple[Quote, ...],
    intraday_bars: tuple[Bar, ...],
    daily_bars: tuple[Bar, ...],
) -> MarketDataSnapshot:
    return MarketDataSnapshot(
        provider="fixture-source",
        request=_request(instruments),
        quotes=quotes,
        intraday_bars=intraday_bars,
        daily_bars=daily_bars,
        quality=_quality(),
    )


def test_snapshot_canonical_evidence_ignores_input_order_and_equivalent_timezones() -> None:
    quote_time = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
    intraday_start = datetime(2030, 1, 2, 10, 0, tzinfo=UTC)
    intraday_end = datetime(2030, 1, 2, 10, 15, tzinfo=UTC)
    daily_end = datetime(2030, 1, 2, 11, 0, tzinfo=UTC)
    quote_a = Quote(instrument_id=INSTRUMENT_A.instrument_id, as_of=quote_time, price=Decimal("10"))
    quote_b = Quote(instrument_id=INSTRUMENT_B.instrument_id, as_of=quote_time, price=Decimal("11"))
    intraday_a = _bar(INSTRUMENT_A, starts_at=intraday_start, ends_at=intraday_end)
    intraday_b = _bar(INSTRUMENT_B, starts_at=intraday_start, ends_at=intraday_end)
    daily_a = _bar(INSTRUMENT_A, starts_at=intraday_start, ends_at=daily_end)
    daily_b = _bar(INSTRUMENT_B, starts_at=intraday_start, ends_at=daily_end)
    alternate_timezone = ZoneInfo("Etc/GMT-8")
    equivalent_quote_a = replace(quote_a, as_of=quote_time.astimezone(alternate_timezone))
    equivalent_intraday_a = _bar(
        INSTRUMENT_A,
        starts_at=intraday_start.astimezone(alternate_timezone),
        ends_at=intraday_end.astimezone(alternate_timezone),
    )
    equivalent_daily_a = _bar(
        INSTRUMENT_A,
        starts_at=intraday_start.astimezone(alternate_timezone),
        ends_at=daily_end.astimezone(alternate_timezone),
    )

    first = _snapshot(
        instruments=(INSTRUMENT_A, INSTRUMENT_B),
        quotes=(quote_a, quote_b),
        intraday_bars=(intraday_a, intraday_b),
        daily_bars=(daily_a, daily_b),
    )
    second = _snapshot(
        instruments=(INSTRUMENT_B, INSTRUMENT_A),
        quotes=(quote_b, equivalent_quote_a),
        intraday_bars=(intraday_b, equivalent_intraday_a),
        daily_bars=(daily_b, equivalent_daily_a),
    )

    assert second.request.instruments == first.request.instruments
    assert second.canonical_json == first.canonical_json
    assert second.content_hash == first.content_hash
    assert second.snapshot_id == first.snapshot_id


def test_snapshot_rejects_duplicate_quote_and_bar_identities() -> None:
    quote_time = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
    intraday_start = datetime(2030, 1, 2, 10, 0, tzinfo=UTC)
    intraday_end = datetime(2030, 1, 2, 10, 15, tzinfo=UTC)
    quote = Quote(instrument_id=INSTRUMENT_A.instrument_id, as_of=quote_time, price=Decimal("10"))
    intraday = _bar(INSTRUMENT_A, starts_at=intraday_start, ends_at=intraday_end)
    daily = _bar(
        INSTRUMENT_A,
        starts_at=intraday_start,
        ends_at=datetime(2030, 1, 2, 11, 0, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="duplicate quote identity"):
        _snapshot(
            instruments=(INSTRUMENT_A,),
            quotes=(quote, replace(quote, price=Decimal("11"))),
            intraday_bars=(intraday,),
            daily_bars=(daily,),
        )
    with pytest.raises(ValueError, match="duplicate intraday_bars identity"):
        _snapshot(
            instruments=(INSTRUMENT_A,),
            quotes=(quote,),
            intraday_bars=(intraday, replace(intraday, volume=Decimal("2"))),
            daily_bars=(daily,),
        )


def test_request_retains_duplicate_instrument_rejection_after_order_normalization() -> None:
    with pytest.raises(ValueError, match="instruments must be unique"):
        _request((INSTRUMENT_A, INSTRUMENT_A))
