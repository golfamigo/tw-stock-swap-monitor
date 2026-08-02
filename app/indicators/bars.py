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
class RequestedCoverageGap:
    """The first uncovered point in one calendar session of a request range."""

    location: SessionLocation
    kind: str
    covered_end: datetime
    required_start: datetime
    required_end: datetime


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

    A calendar-short final window is emitted only when source bars continuously cover its
    complete interval through the session close. Leading, trailing, and interior gaps are
    dropped rather than represented as an ordinary complete ``Bar``.
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
        aggregate = _aggregate_bucket(grouped_bars, source_timezone)
        if aggregate is not None:
            aggregated.append(aggregate)
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


def requested_session_locations(
    *,
    calendar: TradingCalendarProvider,
    market: str,
    intraday_start: datetime,
    intraday_end: datetime,
) -> tuple[SessionLocation, ...]:
    """Return calendar sessions overlapping a left-closed, right-open request range."""

    if not market.strip():
        raise BarAggregationInputError("market must not be blank")
    require_timezone_aware(intraday_start, field_name="intraday_start")
    require_timezone_aware(intraday_end, field_name="intraday_end")
    market_timezone = calendar.market_timezone(market)
    local_start = intraday_start.astimezone(market_timezone)
    local_end = intraday_end.astimezone(market_timezone)
    if local_end <= local_start:
        raise BarAggregationInputError("intraday request range must be positive")

    locations: list[SessionLocation] = []
    session_date = local_start.date()
    final_date = local_end.date()
    while session_date <= final_date:
        if not calendar.is_holiday(market, session_date):
            for segment, session in enumerate(calendar.sessions_for(market, session_date)):
                if max(session.opens_at, local_start) < min(session.closes_at, local_end):
                    locations.append(
                        SessionLocation(
                            session_date=session_date,
                            segment=segment,
                            session=session,
                        )
                    )
        session_date += timedelta(days=1)
    return tuple(locations)


def requested_range_coverage_gap(
    bars: Sequence[Bar],
    *,
    calendar: TradingCalendarProvider,
    market: str,
    intraday_start: datetime,
    intraday_end: datetime,
) -> RequestedCoverageGap | None:
    """Find the first source-coverage gap across calendar sessions in a request range."""

    source_bars = validate_bars_for_calendar(bars, calendar=calendar, market=market)
    locations = requested_session_locations(
        calendar=calendar,
        market=market,
        intraday_start=intraday_start,
        intraday_end=intraday_end,
    )
    market_timezone = calendar.market_timezone(market)
    located_bars = tuple(
        (bar, locate_market_session(bar, calendar=calendar, market=market)) for bar in source_bars
    )
    for location in locations:
        required_start = max(location.session.opens_at, intraday_start.astimezone(market_timezone))
        required_end = min(location.session.closes_at, intraday_end.astimezone(market_timezone))
        expected_start = required_start
        session_bars = sorted(
            (
                bar
                for bar, bar_location in located_bars
                if bar_location.session_date == location.session_date
                and bar_location.segment == location.segment
            ),
            key=lambda item: item.starts_at,
        )
        for bar in session_bars:
            local_start = bar.starts_at.astimezone(market_timezone)
            local_end = bar.ends_at.astimezone(market_timezone)
            if local_start != expected_start:
                return RequestedCoverageGap(
                    location=location,
                    kind="leading" if expected_start == required_start else "interior",
                    covered_end=expected_start,
                    required_start=required_start,
                    required_end=required_end,
                )
            if local_end > required_end:
                return RequestedCoverageGap(
                    location=location,
                    kind="trailing",
                    covered_end=expected_start,
                    required_start=required_start,
                    required_end=required_end,
                )
            expected_start = local_end
        if expected_start != required_end:
            return RequestedCoverageGap(
                location=location,
                kind="trailing",
                covered_end=expected_start,
                required_start=required_start,
                required_end=required_end,
            )
    return None


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


def _aggregate_bucket(bucketed_bars: list[_BucketedBar], output_timezone: tzinfo) -> Bar | None:
    ordered = sorted(bucketed_bars, key=lambda item: item.bar.starts_at)
    first = ordered[0]
    if not _covers_bucket_continuously(ordered, first.bucket_start, first.bucket_end):
        return None
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


def _covers_bucket_continuously(
    bucketed_bars: list[_BucketedBar], bucket_start: datetime, bucket_end: datetime
) -> bool:
    expected_start = bucket_start
    for bucketed_bar in bucketed_bars:
        if bucketed_bar.bar.starts_at != expected_start:
            return False
        expected_start = bucketed_bar.bar.ends_at
    return expected_start == bucket_end
