"""Calendar-aligned bar aggregation behavior."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from app.data_sources.models import Bar
from app.domain.errors import NaiveDatetimeError
from app.indicators.bars import BarAggregationInputError, aggregate_bars
from tests.unit.indicators.conftest import (
    INSTRUMENT_A,
    INSTRUMENT_B,
    MARKET,
    TIMEZONE,
    FixtureCalendar,
    at,
    bar,
)


def test_aggregates_three_minute_left_closed_right_open_windows() -> None:
    session_date = date(2030, 1, 2)
    calendar = FixtureCalendar()
    source = (
        bar(session_date, 9, 0, open_price=Decimal("10"), high=Decimal("12"), volume=Decimal("2")),
        bar(
            session_date,
            9,
            1,
            open_price=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("12"),
            volume=Decimal("3"),
        ),
        bar(
            session_date,
            9,
            2,
            open_price=Decimal("12"),
            high=Decimal("14"),
            close=Decimal("13"),
            volume=Decimal("5"),
        ),
        bar(session_date, 9, 3, open_price=Decimal("20"), close=Decimal("21"), volume=Decimal("7")),
        bar(session_date, 9, 4, open_price=Decimal("21"), close=Decimal("22"), volume=Decimal("8")),
        bar(session_date, 9, 5, open_price=Decimal("22"), close=Decimal("23"), volume=Decimal("9")),
    )

    aggregated = aggregate_bars(
        source, calendar=calendar, market=MARKET, width=timedelta(minutes=3)
    )

    assert [(item.starts_at, item.ends_at) for item in aggregated] == [
        (at(session_date, 9, 0), at(session_date, 9, 3)),
        (at(session_date, 9, 3), at(session_date, 9, 6)),
    ]
    assert aggregated[0].open == Decimal("10")
    assert aggregated[0].high == Decimal("14")
    assert aggregated[0].low == Decimal("9")
    assert aggregated[0].close == Decimal("13")
    assert aggregated[0].volume == Decimal("10")
    assert aggregated[0].starts_at.tzinfo == TIMEZONE


def test_aggregates_fifteen_minute_windows_from_each_session_open() -> None:
    session_date = date(2030, 1, 2)
    calendar = FixtureCalendar()
    source = (
        *(
            bar(session_date, 9, minute, duration=timedelta(minutes=3))
            for minute in range(0, 15, 3)
        ),
        *(
            bar(session_date, 9, minute, duration=timedelta(minutes=3))
            for minute in range(15, 30, 3)
        ),
    )

    aggregated = aggregate_bars(
        source, calendar=calendar, market=MARKET, width=timedelta(minutes=15)
    )

    assert [(item.starts_at, item.ends_at, item.volume) for item in aggregated] == [
        (at(session_date, 9, 0), at(session_date, 9, 15), Decimal("5")),
        (at(session_date, 9, 15), at(session_date, 9, 30), Decimal("5")),
    ]


def test_resets_alignment_for_each_session_and_never_bridges_lunch_break() -> None:
    session_date = date(2030, 1, 2)
    calendar = FixtureCalendar()
    source = (
        *(
            bar(session_date, 10, minute, duration=timedelta(minutes=3))
            for minute in range(45, 57, 3)
        ),
        bar(session_date, 10, 57, duration=timedelta(minutes=3), volume=Decimal("4")),
        *(
            bar(session_date, 13, minute, duration=timedelta(minutes=3))
            for minute in range(0, 15, 3)
        ),
    )

    aggregated = aggregate_bars(
        source, calendar=calendar, market=MARKET, width=timedelta(minutes=15)
    )

    assert [(item.starts_at, item.ends_at, item.volume) for item in aggregated] == [
        (at(session_date, 10, 45), at(session_date, 11, 0), Decimal("8")),
        (at(session_date, 13, 0), at(session_date, 13, 15), Decimal("5")),
    ]


def test_returns_fully_covered_final_partial_interval_closed_at_session_close() -> None:
    session_date = date(2030, 1, 2)
    calendar = FixtureCalendar(sessions=((9, 0, 9, 32),))

    aggregated = aggregate_bars(
        (
            bar(session_date, 9, 30, duration=timedelta(minutes=1), volume=Decimal("8")),
            bar(session_date, 9, 31, duration=timedelta(minutes=1), volume=Decimal("9")),
        ),
        calendar=calendar,
        market=MARKET,
        width=timedelta(minutes=15),
    )

    assert [(item.starts_at, item.ends_at, item.volume) for item in aggregated] == [
        (at(session_date, 9, 30), at(session_date, 9, 32), Decimal("17"))
    ]


def test_rejects_holiday_and_overlapping_source_bars() -> None:
    session_date = date(2030, 1, 7)
    holiday_calendar = FixtureCalendar(holidays=frozenset({session_date}))
    with pytest.raises(BarAggregationInputError, match="holiday"):
        aggregate_bars(
            (bar(session_date, 9, 0),),
            calendar=holiday_calendar,
            market=MARKET,
            width=timedelta(minutes=3),
        )

    trading_date = date(2030, 1, 8)
    with pytest.raises(BarAggregationInputError, match="overlap"):
        aggregate_bars(
            (
                bar(trading_date, 9, 0, duration=timedelta(minutes=2)),
                bar(trading_date, 9, 1, duration=timedelta(minutes=2)),
            ),
            calendar=FixtureCalendar(),
            market=MARKET,
            width=timedelta(minutes=3),
        )


def test_rejects_naive_or_misaligned_source_input() -> None:
    session_date = date(2030, 1, 2)
    with pytest.raises(NaiveDatetimeError):
        bar(
            session_date,
            9,
            0,
        ).__class__(
            instrument_id=INSTRUMENT_A,
            starts_at=datetime(2030, 1, 2, 9, 0),
            ends_at=datetime(2030, 1, 2, 9, 1),
            open=Decimal("10"),
            high=Decimal("10"),
            low=Decimal("10"),
            close=Decimal("10"),
            volume=Decimal("1"),
        )

    with pytest.raises(BarAggregationInputError, match="one aggregation window"):
        aggregate_bars(
            (bar(session_date, 9, 14, duration=timedelta(minutes=3)),),
            calendar=FixtureCalendar(),
            market=MARKET,
            width=timedelta(minutes=15),
        )


def test_aggregation_keeps_instruments_separate_within_the_same_window() -> None:
    session_date = date(2030, 1, 2)

    aggregated = aggregate_bars(
        (
            bar(session_date, 9, 0, instrument_id=INSTRUMENT_A, volume=Decimal("2")),
            bar(session_date, 9, 1, instrument_id=INSTRUMENT_A, volume=Decimal("3")),
            bar(session_date, 9, 2, instrument_id=INSTRUMENT_A, volume=Decimal("4")),
            bar(session_date, 9, 0, instrument_id=INSTRUMENT_B, volume=Decimal("5")),
            bar(session_date, 9, 1, instrument_id=INSTRUMENT_B, volume=Decimal("7")),
            bar(session_date, 9, 2, instrument_id=INSTRUMENT_B, volume=Decimal("8")),
        ),
        calendar=FixtureCalendar(),
        market=MARKET,
        width=timedelta(minutes=3),
    )

    assert {(item.instrument_id, item.volume) for item in aggregated} == {
        (INSTRUMENT_A, Decimal("9")),
        (INSTRUMENT_B, Decimal("20")),
    }


def test_rejects_mixed_aware_timezones_at_the_aggregation_boundary() -> None:
    session_date = date(2030, 1, 2)
    market_bar = bar(session_date, 9, 0)
    utc_start = at(session_date, 9, 1).astimezone(UTC)
    utc_bar = Bar(
        instrument_id=INSTRUMENT_A,
        starts_at=utc_start,
        ends_at=utc_start + timedelta(minutes=1),
        open=Decimal("10"),
        high=Decimal("10"),
        low=Decimal("10"),
        close=Decimal("10"),
        volume=Decimal("1"),
    )

    with pytest.raises(BarAggregationInputError, match="one timezone"):
        aggregate_bars(
            (market_bar, utc_bar),
            calendar=FixtureCalendar(),
            market=MARKET,
            width=timedelta(minutes=3),
        )


@pytest.mark.parametrize(
    "source_minutes",
    ((1, 2), (0, 1), (0, 2)),
    ids=("leading-gap", "trailing-gap", "interior-gap"),
)
def test_drops_aggregate_windows_without_continuous_source_coverage(
    source_minutes: tuple[int, int],
) -> None:
    session_date = date(2030, 1, 2)

    aggregated = aggregate_bars(
        tuple(bar(session_date, 9, minute) for minute in source_minutes),
        calendar=FixtureCalendar(),
        market=MARKET,
        width=timedelta(minutes=3),
    )

    assert aggregated == ()
