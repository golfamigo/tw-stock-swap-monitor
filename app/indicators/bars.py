"""Calendar-aligned, pandas-backed OHLCV aggregation with Decimal outputs."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import cast
from uuid import UUID

import pandas as pd

from app.calendar.base import TradingCalendarProvider
from app.data_sources.models import Bar
from app.domain.values import MarketSession, require_timezone_aware
from app.indicators.base import IndicatorInputError


class BarAggregationInputError(IndicatorInputError):
    """Source bars cannot be grouped into safe market-calendar windows."""


@dataclass(frozen=True, slots=True)
class SessionLocation:
    """One bar's local market date and session segment."""

    session_date: date
    segment: int
    session: MarketSession


@dataclass(frozen=True, slots=True)
class _BucketedBar:
    """A source bar assigned to exactly one calendar-derived aggregation window."""

    bar: Bar
    bucket_start: datetime
    bucket_end: datetime


def locate_market_session(
    bar: Bar, *, calendar: TradingCalendarProvider, market: str
) -> SessionLocation:
    """Find the one session containing the full left-closed/right-open source interval."""

    if not market.strip():
        raise BarAggregationInputError("market must not be blank")
    require_timezone_aware(bar.starts_at, field_name="bar.starts_at")
    require_timezone_aware(bar.ends_at, field_name="bar.ends_at")
    market_timezone = calendar.market_timezone(market)
    local_start = bar.starts_at.astimezone(market_timezone)
    local_end = bar.ends_at.astimezone(market_timezone)
    session_date = local_start.date()
    if calendar.is_holiday(market, session_date):
        raise BarAggregationInputError("source bar falls on a market holiday")
    sessions = calendar.sessions_for(market, session_date)
    if not sessions:
        raise BarAggregationInputError("source bar falls outside a trading session")
    for segment, session in enumerate(sessions):
        if session.opens_at <= local_start < session.closes_at and local_end <= session.closes_at:
            return SessionLocation(session_date=session_date, segment=segment, session=session)
    raise BarAggregationInputError("source bar crosses or falls outside a trading session")


def aggregate_bars(
    bars: Sequence[Bar],
    *,
    calendar: TradingCalendarProvider,
    market: str,
    width: timedelta,
) -> tuple[Bar, ...]:
    """Aggregate observed bars in session-open-aligned left-closed/right-open windows.

    A final partial window is emitted only when it contains an observed source bar; its
    ``ends_at`` is the session close rather than a fabricated full-width boundary.
    """

    if width <= timedelta():
        raise BarAggregationInputError("aggregation width must be positive")
    source_bars = validate_bars_for_calendar(bars, calendar=calendar, market=market)
    if not source_bars:
        return ()
    source_timezone = source_bars[0].starts_at.tzinfo
    if source_timezone is None:
        raise BarAggregationInputError("source bar timestamps must be timezone-aware")
    bucketed = tuple(
        _bucket_bar(bar, calendar=calendar, market=market, width=width) for bar in source_bars
    )
    frame = _build_pandas_grouping_frame(bucketed)
    aggregated: list[Bar] = []
    for _, group in frame.groupby(["instrument_id", "bucket_start", "bucket_end"], sort=True):
        grouped_bars = cast(list[_BucketedBar], group["bucketed_bar"].tolist())
        aggregated.append(_aggregate_bucket(grouped_bars, source_timezone))
    return tuple(sorted(aggregated, key=lambda item: (item.starts_at, str(item.instrument_id))))


def validate_bars_for_calendar(
    bars: Sequence[Bar], *, calendar: TradingCalendarProvider, market: str
) -> tuple[Bar, ...]:
    """Validate raw bars share one ordered timezone and each stays in one session."""

    source_bars = tuple(bars)
    if not source_bars:
        return ()
    _validate_source_bars(source_bars)
    for bar in source_bars:
        locate_market_session(bar, calendar=calendar, market=market)
    return source_bars


def _validate_source_bars(source_bars: tuple[Bar, ...]) -> tzinfo:
    source_timezone = source_bars[0].starts_at.tzinfo
    if source_timezone is None:
        raise BarAggregationInputError("source bar timestamps must be timezone-aware")

    previous_by_instrument: dict[UUID, Bar] = {}
    for bar in source_bars:
        require_timezone_aware(bar.starts_at, field_name="bar.starts_at")
        require_timezone_aware(bar.ends_at, field_name="bar.ends_at")
        if bar.starts_at.tzinfo != source_timezone or bar.ends_at.tzinfo != source_timezone:
            raise BarAggregationInputError(
                "source bars must use one timezone to preserve output timezone"
            )
        previous = previous_by_instrument.get(bar.instrument_id)
        if previous is not None:
            if bar.starts_at < previous.starts_at:
                raise BarAggregationInputError("source bars must be ordered for one instrument")
            if bar.starts_at < previous.ends_at:
                raise BarAggregationInputError("source bars overlap for one instrument")
        previous_by_instrument[bar.instrument_id] = bar
    return source_timezone


def _bucket_bar(
    bar: Bar,
    *,
    calendar: TradingCalendarProvider,
    market: str,
    width: timedelta,
) -> _BucketedBar:
    location = locate_market_session(bar, calendar=calendar, market=market)
    local_start = bar.starts_at.astimezone(calendar.market_timezone(market))
    elapsed = local_start - location.session.opens_at
    bucket_number = elapsed // width
    bucket_start = location.session.opens_at + bucket_number * width
    bucket_end = min(bucket_start + width, location.session.closes_at)
    if bar.ends_at.astimezone(calendar.market_timezone(market)) > bucket_end:
        raise BarAggregationInputError("source bar must fit within one aggregation window")
    return _BucketedBar(bar=bar, bucket_start=bucket_start, bucket_end=bucket_end)


def _build_pandas_grouping_frame(bucketed: tuple[_BucketedBar, ...]) -> pd.DataFrame:
    index = pd.DatetimeIndex([item.bar.starts_at for item in bucketed], name="starts_at")
    if index.tz is None:
        raise BarAggregationInputError("pandas aggregation index must be timezone-aware")
    return pd.DataFrame(
        {
            "instrument_id": [item.bar.instrument_id for item in bucketed],
            "bucket_start": [item.bucket_start for item in bucketed],
            "bucket_end": [item.bucket_end for item in bucketed],
            "bucketed_bar": list(bucketed),
        },
        index=index,
    )


def _aggregate_bucket(bucketed_bars: list[_BucketedBar], output_timezone: tzinfo) -> Bar:
    ordered = sorted(bucketed_bars, key=lambda item: item.bar.starts_at)
    first = ordered[0]
    components = tuple(item.bar for item in ordered)
    return Bar(
        instrument_id=first.bar.instrument_id,
        starts_at=first.bucket_start.astimezone(output_timezone),
        ends_at=first.bucket_end.astimezone(output_timezone),
        open=components[0].open,
        high=max(item.high for item in components),
        low=min(item.low for item in components),
        close=components[-1].close,
        volume=sum((item.volume for item in components), Decimal("0")),
    )
