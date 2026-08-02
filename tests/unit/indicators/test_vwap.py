"""Session VWAP indicator behavior."""

from datetime import date, timedelta
from decimal import Decimal

from app.indicators.vwap import SessionVwapIndicator
from tests.unit.indicators.conftest import INSTRUMENT_A, MARKET, FixtureCalendar, bar, snapshot


def test_calculates_session_vwap_as_an_exact_decimal() -> None:
    session_date = date(2030, 1, 2)
    indicator = SessionVwapIndicator(
        calendar=FixtureCalendar(), market=MARKET, instrument_id=INSTRUMENT_A
    )
    market_snapshot = snapshot(
        session_date,
        (
            bar(
                session_date,
                9,
                0,
                high=Decimal("13"),
                low=Decimal("7"),
                close=Decimal("10"),
                volume=Decimal("2"),
            ),
            bar(
                session_date,
                9,
                1,
                high=Decimal("16"),
                low=Decimal("10"),
                close=Decimal("13"),
                volume=Decimal("3"),
            ),
        ),
    )

    result = indicator.calculate(market_snapshot)

    assert result.actionable
    assert result.value == Decimal("11.8")
    assert type(result.value) is Decimal
    assert result.evidence["session_segment"] == "0"


def test_resets_vwap_at_each_calendar_session() -> None:
    session_date = date(2030, 1, 2)
    indicator = SessionVwapIndicator(
        calendar=FixtureCalendar(), market=MARKET, instrument_id=INSTRUMENT_A
    )
    market_snapshot = snapshot(
        session_date,
        (
            bar(
                session_date,
                10,
                59,
                open_price=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100"),
                volume=Decimal("9"),
            ),
            bar(
                session_date,
                13,
                0,
                open_price=Decimal("20"),
                high=Decimal("20"),
                low=Decimal("20"),
                close=Decimal("20"),
                volume=Decimal("2"),
            ),
        ),
    )

    result = indicator.calculate(market_snapshot)

    assert result.actionable
    assert result.value == Decimal("20")
    assert result.evidence["session_segment"] == "1"


def test_retains_missing_state_for_zero_volume() -> None:
    session_date = date(2030, 1, 2)
    indicator = SessionVwapIndicator(
        calendar=FixtureCalendar(), market=MARKET, instrument_id=INSTRUMENT_A
    )

    result = indicator.calculate(
        snapshot(session_date, (bar(session_date, 9, 0, volume=Decimal("0")),))
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == "zero_volume"


def test_quality_gate_prevents_an_actionable_vwap_metric() -> None:
    session_date = date(2030, 1, 2)
    indicator = SessionVwapIndicator(
        calendar=FixtureCalendar(), market=MARKET, instrument_id=INSTRUMENT_A
    )

    result = indicator.calculate(snapshot(session_date, (bar(session_date, 9, 0),), stale=True))

    assert not result.actionable
    assert result.value is None
    assert result.reason == "snapshot_not_actionable"


def test_overlapping_raw_bars_cannot_produce_an_actionable_vwap() -> None:
    session_date = date(2030, 1, 2)
    indicator = SessionVwapIndicator(
        calendar=FixtureCalendar(), market=MARKET, instrument_id=INSTRUMENT_A
    )

    result = indicator.calculate(
        snapshot(
            session_date,
            (
                bar(session_date, 9, 0, duration=timedelta(minutes=2)),
                bar(session_date, 9, 1, duration=timedelta(minutes=2)),
            ),
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == "invalid_source_bars"
    assert "overlap" in result.evidence["validation_error"]


def test_out_of_session_raw_bar_cannot_produce_an_actionable_vwap() -> None:
    session_date = date(2030, 1, 2)
    indicator = SessionVwapIndicator(
        calendar=FixtureCalendar(), market=MARKET, instrument_id=INSTRUMENT_A
    )

    result = indicator.calculate(snapshot(session_date, (bar(session_date, 11, 0),)))

    assert not result.actionable
    assert result.value is None
    assert result.reason == "invalid_source_bars"
    assert "outside a trading session" in result.evidence["validation_error"]


def test_strategy_boundary_vwap_never_leaks_binary_float_drift() -> None:
    session_date = date(2030, 1, 2)
    indicator = SessionVwapIndicator(
        calendar=FixtureCalendar(), market=MARKET, instrument_id=INSTRUMENT_A
    )
    market_snapshot = snapshot(
        session_date,
        (
            bar(
                session_date,
                9,
                0,
                open_price=Decimal("0.1"),
                high=Decimal("0.1"),
                low=Decimal("0.1"),
                close=Decimal("0.1"),
                volume=Decimal("1"),
            ),
            bar(
                session_date,
                9,
                1,
                open_price=Decimal("0.2"),
                high=Decimal("0.2"),
                low=Decimal("0.2"),
                close=Decimal("0.2"),
                volume=Decimal("1"),
            ),
        ),
    )

    result = indicator.calculate(market_snapshot)

    assert result.value == Decimal("0.15")
    assert result.value.is_finite()
