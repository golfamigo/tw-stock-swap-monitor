"""Shared deterministic fixtures for indicator tests."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from app.data_sources.models import Bar, DataQuality, MarketDataRequest, MarketDataSnapshot, Quote
from app.domain.entities import Instrument
from app.domain.values import MarketSession

MARKET = "market-a"
TIMEZONE = ZoneInfo("Asia/Taipei")
INSTRUMENT_A = UUID("00000000-0000-0000-0000-000000000601")
INSTRUMENT_B = UUID("00000000-0000-0000-0000-000000000602")


class FixtureCalendar:
    """A local calendar with a lunch break and explicitly configured full-day closures."""

    def __init__(
        self,
        *,
        holidays: frozenset[date] = frozenset(),
        sessions: tuple[tuple[int, int, int, int], ...] = ((9, 0, 11, 0), (13, 0, 14, 0)),
    ) -> None:
        self.holidays = holidays
        self._session_times = sessions

    def market_timezone(self, market: str) -> ZoneInfo:
        if market != MARKET:
            raise ValueError("unexpected market")
        return TIMEZONE

    def is_holiday(self, market: str, session_date: date) -> bool:
        self.market_timezone(market)
        return session_date in self.holidays

    def sessions_for(self, market: str, session_date: date) -> tuple[MarketSession, ...]:
        self.market_timezone(market)
        if self.is_holiday(market, session_date):
            return ()
        return tuple(
            MarketSession(
                opens_at=datetime(
                    session_date.year,
                    session_date.month,
                    session_date.day,
                    start_hour,
                    start_minute,
                    tzinfo=TIMEZONE,
                ),
                closes_at=datetime(
                    session_date.year,
                    session_date.month,
                    session_date.day,
                    end_hour,
                    end_minute,
                    tzinfo=TIMEZONE,
                ),
            )
            for start_hour, start_minute, end_hour, end_minute in self._session_times
        )


def at(session_date: date, hour: int, minute: int) -> datetime:
    """Return one fixed market-local timestamp."""

    return datetime(
        session_date.year, session_date.month, session_date.day, hour, minute, tzinfo=TIMEZONE
    )


def bar(
    session_date: date,
    hour: int,
    minute: int,
    *,
    instrument_id: UUID = INSTRUMENT_A,
    duration: timedelta = timedelta(minutes=1),
    open_price: Decimal = Decimal("10"),
    high: Decimal | None = None,
    low: Decimal | None = None,
    close: Decimal | None = None,
    volume: Decimal = Decimal("1"),
) -> Bar:
    """Build a valid, timezone-aware OHLCV bar without float coercion."""

    resolved_close = close if close is not None else open_price
    return Bar(
        instrument_id=instrument_id,
        starts_at=at(session_date, hour, minute),
        ends_at=at(session_date, hour, minute) + duration,
        open=open_price,
        high=high if high is not None else max(open_price, resolved_close),
        low=low if low is not None else min(open_price, resolved_close),
        close=resolved_close,
        volume=volume,
    )


def snapshot(
    session_date: date,
    intraday_bars: tuple[Bar, ...],
    *,
    stale: bool = False,
    bars_complete: bool = True,
    missing_fields: tuple[str, ...] = (),
) -> MarketDataSnapshot:
    """Build complete snapshot evidence for one generic instrument and date."""

    requested_at = at(session_date, 15, 0)
    instrument = Instrument(
        instrument_id=INSTRUMENT_A,
        market=MARKET,
        symbol="instrument-a",
        created_at=at(date(2030, 1, 1), 8, 0),
    )
    request = MarketDataRequest(
        request_id=UUID("00000000-0000-0000-0000-000000000603"),
        instruments=(instrument,),
        requested_at=requested_at,
        intraday_start=at(session_date, 9, 0),
        intraday_end=at(session_date, 14, 0),
        intraday_interval=timedelta(minutes=1),
        daily_start=session_date,
        daily_end=session_date + timedelta(days=1),
        maximum_data_delay=timedelta(minutes=5),
    )
    quality = DataQuality(
        provider="fixture-source",
        fetched_at=requested_at,
        source_timestamp=requested_at - timedelta(minutes=1),
        delay=timedelta(minutes=1),
        stale=stale,
        missing_fields=missing_fields,
        bars_complete=bars_complete,
        anomalies=(),
        confidence=Decimal("1"),
    )
    return MarketDataSnapshot(
        provider="fixture-source",
        request=request,
        quotes=(Quote(instrument_id=INSTRUMENT_A, as_of=requested_at, price=Decimal("10")),),
        intraday_bars=intraday_bars,
        daily_bars=(bar(session_date, 9, 0),),
        quality=quality,
    )
