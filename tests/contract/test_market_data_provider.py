"""Contracts for the deterministic, offline market-data adapter."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from app.calendar.base import TradingCalendarProvider
from app.calendar.mock import MockTradingCalendar
from app.data_sources.base import MarketDataProvider
from app.data_sources.models import Bar, MarketDataRequest
from app.data_sources.providers.mock import MockMarketDataProvider
from app.domain.entities import Instrument
from app.domain.errors import NaiveDatetimeError, NonDecimalValueError
from app.domain.values import MarketSession

MARKET = "source-a"
TRADING_DAY = date(2030, 1, 2)
REQUESTED_AT = datetime(2030, 1, 2, 14, 0, tzinfo=UTC)


def _calendar(*, holidays: frozenset[date] = frozenset()) -> MockTradingCalendar:
    return MockTradingCalendar(
        market=MARKET,
        timezone_name="Etc/UTC",
        trading_day=TRADING_DAY,
        sessions=(
            MarketSession(
                opens_at=datetime(2030, 1, 2, 10, 0, tzinfo=UTC),
                closes_at=datetime(2030, 1, 2, 11, 0, tzinfo=UTC),
            ),
            MarketSession(
                opens_at=datetime(2030, 1, 2, 12, 0, tzinfo=UTC),
                closes_at=datetime(2030, 1, 2, 13, 0, tzinfo=UTC),
            ),
        ),
        holidays=holidays,
    )


def _request(
    *,
    interval: timedelta = timedelta(minutes=15),
    intraday_start: datetime = datetime(2030, 1, 2, 10, 0, tzinfo=UTC),
    intraday_end: datetime = datetime(2030, 1, 2, 13, 0, tzinfo=UTC),
) -> MarketDataRequest:
    return MarketDataRequest(
        request_id=UUID("00000000-0000-0000-0000-000000000101"),
        instruments=(
            Instrument(
                instrument_id=UUID("00000000-0000-0000-0000-000000000201"),
                market=MARKET,
                symbol="instrument-a",
                created_at=datetime(2030, 1, 1, tzinfo=UTC),
            ),
        ),
        requested_at=REQUESTED_AT,
        intraday_start=intraday_start,
        intraday_end=intraday_end,
        intraday_interval=interval,
        daily_start=TRADING_DAY,
        daily_end=TRADING_DAY + timedelta(days=1),
        maximum_data_delay=timedelta(minutes=5),
    )


def _provider(*, calendar: TradingCalendarProvider | None = None) -> MockMarketDataProvider:
    return MockMarketDataProvider(
        provider_name="mock-source-a",
        seed=17,
        calendar=calendar or _calendar(),
        source_delay=timedelta(minutes=1),
    )


def test_provider_and_calendar_satisfy_their_protocols() -> None:
    calendar = _calendar()
    provider = _provider(calendar=calendar)

    assert isinstance(calendar, TradingCalendarProvider)
    assert isinstance(provider, MarketDataProvider)
    assert calendar.market_timezone(MARKET).key == "Etc/UTC"


def test_intraday_bars_are_left_closed_right_open_and_do_not_bridge_breaks() -> None:
    snapshot = _provider().get_intraday_bars(_request())

    assert [(bar.starts_at, bar.ends_at) for bar in snapshot.intraday_bars] == [
        (datetime(2030, 1, 2, 10, 0, tzinfo=UTC), datetime(2030, 1, 2, 10, 15, tzinfo=UTC)),
        (datetime(2030, 1, 2, 10, 15, tzinfo=UTC), datetime(2030, 1, 2, 10, 30, tzinfo=UTC)),
        (datetime(2030, 1, 2, 10, 30, tzinfo=UTC), datetime(2030, 1, 2, 10, 45, tzinfo=UTC)),
        (datetime(2030, 1, 2, 10, 45, tzinfo=UTC), datetime(2030, 1, 2, 11, 0, tzinfo=UTC)),
        (datetime(2030, 1, 2, 12, 0, tzinfo=UTC), datetime(2030, 1, 2, 12, 15, tzinfo=UTC)),
        (datetime(2030, 1, 2, 12, 15, tzinfo=UTC), datetime(2030, 1, 2, 12, 30, tzinfo=UTC)),
        (datetime(2030, 1, 2, 12, 30, tzinfo=UTC), datetime(2030, 1, 2, 12, 45, tzinfo=UTC)),
        (datetime(2030, 1, 2, 12, 45, tzinfo=UTC), datetime(2030, 1, 2, 13, 0, tzinfo=UTC)),
    ]
    assert all(bar.starts_at < _request().intraday_end for bar in snapshot.intraday_bars)


def test_holiday_has_no_intraday_or_daily_bars() -> None:
    holiday_calendar = _calendar(holidays=frozenset({TRADING_DAY}))

    snapshot = _provider(calendar=holiday_calendar).get_market_snapshot(_request())

    assert snapshot.intraday_bars == ()
    assert snapshot.daily_bars == ()
    assert snapshot.quality.bars_complete
    assert snapshot.quality.missing_fields == ()
    assert "market_closed" in snapshot.quality.anomalies
    assert not snapshot.is_actionable


def test_provider_skips_holidays_even_if_calendar_returns_sessions() -> None:
    session = MarketSession(
        opens_at=datetime(2030, 1, 2, 10, 0, tzinfo=UTC),
        closes_at=datetime(2030, 1, 2, 11, 0, tzinfo=UTC),
    )
    calendar = _HolidayIgnoringCalendar(session=session)

    snapshot = _provider(calendar=calendar).get_market_snapshot(_request())

    assert snapshot.intraday_bars == ()
    assert snapshot.daily_bars == ()
    assert snapshot.quality.bars_complete
    assert snapshot.quality.missing_fields == ()
    assert "market_closed" in snapshot.quality.anomalies
    assert not snapshot.is_actionable


def test_daily_end_date_is_exclusive() -> None:
    request = replace(
        _request(),
        daily_start=TRADING_DAY - timedelta(days=1),
        daily_end=TRADING_DAY,
    )

    snapshot = _provider().get_daily_bars(request)

    assert snapshot.daily_bars == ()


def test_request_rejects_naive_times_and_non_positive_intervals() -> None:
    with pytest.raises(NaiveDatetimeError, match="intraday_start"):
        _request(intraday_start=datetime(2030, 1, 2, 10, 0))

    with pytest.raises(ValueError, match="intraday_interval"):
        _request(interval=timedelta())


def test_bar_rejects_missing_ohlcv_without_zero_substitution() -> None:
    with pytest.raises(NonDecimalValueError, match="open"):
        Bar(
            instrument_id=UUID("00000000-0000-0000-0000-000000000201"),
            starts_at=datetime(2030, 1, 2, 10, 0, tzinfo=UTC),
            ends_at=datetime(2030, 1, 2, 10, 15, tzinfo=UTC),
            open=None,  # type: ignore[arg-type]
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
            volume=Decimal("1"),
        )


class _HolidayIgnoringCalendar:
    """Calendar stub proving that the provider, not session lookup, closes holidays."""

    def __init__(self, *, session: MarketSession) -> None:
        self._session = session

    def market_timezone(self, market: str) -> ZoneInfo:
        if market != MARKET:
            raise ValueError("unexpected market")
        return ZoneInfo("Etc/UTC")

    def is_holiday(self, market: str, session_date: date) -> bool:
        if market != MARKET:
            raise ValueError("unexpected market")
        return session_date == TRADING_DAY

    def sessions_for(self, market: str, session_date: date) -> tuple[MarketSession, ...]:
        if market != MARKET:
            raise ValueError("unexpected market")
        return (self._session,)
