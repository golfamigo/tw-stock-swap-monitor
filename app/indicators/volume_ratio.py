"""Calendar-aware same-time volume-ratio plugin."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from app.calendar.base import TradingCalendarProvider
from app.data_sources.models import Bar, MarketDataSnapshot
from app.indicators.bars import (
    BarAggregationInputError,
    RequestedCoverageGap,
    SessionLocation,
    aggregate_bars,
    locate_market_session,
    requested_range_coverage_gap,
    requested_session_locations,
)
from app.indicators.base import (
    IndicatorInputError,
    IndicatorResult,
    canonical_timedelta_microseconds,
    divide_decimals,
)

_MAX_CALENDAR_LOOKBACK_DAYS = 3_660


@dataclass(frozen=True, slots=True)
class SameTimeVolumeRatioIndicator:
    """Compare one session-aligned bar volume against complete same-slot history."""

    calendar: TradingCalendarProvider
    market: str
    instrument_id: UUID
    bar_width: timedelta
    lookback_days: int
    history: tuple[MarketDataSnapshot, ...] = field(default_factory=tuple)
    key: str = "same_time_volume_ratio"

    def __post_init__(self) -> None:
        if not self.market.strip():
            raise ValueError("market must not be blank")
        if self.bar_width <= timedelta():
            raise ValueError("bar_width must be positive")
        if self.lookback_days <= 0:
            raise ValueError("lookback_days must be positive")
        if not self.key.strip():
            raise ValueError("indicator key must not be blank")
        object.__setattr__(self, "history", tuple(self.history))

    def calculate(self, market_snapshot: MarketDataSnapshot) -> IndicatorResult:
        """Return Decimal ratio only when every required calendar reference is complete."""

        if not market_snapshot.is_actionable:
            return IndicatorResult.unavailable(
                key=self.key,
                reason="snapshot_not_actionable",
                evidence={"snapshot_id": market_snapshot.snapshot_id},
            )
        current_source_bars = tuple(
            bar for bar in market_snapshot.intraday_bars if bar.instrument_id == self.instrument_id
        )
        try:
            requested_locations = requested_session_locations(
                calendar=self.calendar,
                market=self.market,
                intraday_start=market_snapshot.request.intraday_start,
                intraday_end=market_snapshot.request.intraday_end,
            )
            if not requested_locations:
                return IndicatorResult.unavailable(
                    key=self.key,
                    reason="current_request_market_closed",
                    evidence=_no_effective_session_evidence(
                        instrument_id=self.instrument_id,
                        snapshot_id=market_snapshot.snapshot_id,
                        request=market_snapshot,
                        status_key="current_market_status",
                    ),
                )
            current_coverage_gap = requested_range_coverage_gap(
                current_source_bars,
                calendar=self.calendar,
                market=self.market,
                intraday_start=market_snapshot.request.intraday_start,
                intraday_end=market_snapshot.request.intraday_end,
            )
            current_bars = aggregate_bars(
                current_source_bars,
                calendar=self.calendar,
                market=self.market,
                width=self.bar_width,
            )
        except BarAggregationInputError as error:
            return IndicatorResult.unavailable(
                key=self.key,
                reason="invalid_current_source_bars",
                evidence={
                    "instrument_id": str(self.instrument_id),
                    "snapshot_id": market_snapshot.snapshot_id,
                    "validation_error": str(error),
                },
            )
        if current_coverage_gap is not None:
            return IndicatorResult.unavailable(
                key=self.key,
                reason="incomplete_current_source_coverage",
                evidence=_coverage_evidence(
                    current_coverage_gap,
                    instrument_id=self.instrument_id,
                    snapshot_id=market_snapshot.snapshot_id,
                ),
            )
        if not current_bars:
            return IndicatorResult.unavailable(
                key=self.key,
                reason="missing_current_bar",
                evidence={"instrument_id": str(self.instrument_id)},
            )
        current_location = requested_locations[-1]
        required_current_end = min(
            current_location.session.closes_at,
            market_snapshot.request.intraday_end.astimezone(
                current_location.session.closes_at.tzinfo
            ),
        )
        located_current_bars = tuple(
            (bar, locate_market_session(bar, calendar=self.calendar, market=self.market))
            for bar in current_bars
        )
        target_current_bars = tuple(
            bar
            for bar, location in located_current_bars
            if location.session_date == current_location.session_date
            and location.segment == current_location.segment
        )
        current_bar = next(
            (
                bar
                for bar in target_current_bars
                if bar.ends_at.astimezone(current_location.session.closes_at.tzinfo)
                == required_current_end
            ),
            None,
        )
        if current_bar is None:
            latest_complete_end = max(
                (bar.ends_at for bar in target_current_bars),
                default=current_location.session.opens_at,
            )
            return IndicatorResult.unavailable(
                key=self.key,
                reason="incomplete_current_source_coverage",
                evidence={
                    "instrument_id": str(self.instrument_id),
                    "latest_complete_end": latest_complete_end.isoformat(),
                    "required_end": required_current_end.isoformat(),
                    "session_date": current_location.session_date.isoformat(),
                    "session_segment": str(current_location.segment),
                    "snapshot_id": market_snapshot.snapshot_id,
                },
            )
        expected_dates = _previous_trading_dates(
            calendar=self.calendar,
            market=self.market,
            before=current_location.session_date,
            count=self.lookback_days,
        )
        if len(expected_dates) != self.lookback_days:
            return IndicatorResult.unavailable(
                key=self.key,
                reason="calendar_lookback_unavailable",
                evidence=_evidence(
                    current_bar, current_location, self.lookback_days, self.bar_width
                ),
            )

        history_by_date = _history_by_session_date(
            self.history,
            calendar=self.calendar,
            market=self.market,
            current_date=current_location.session_date,
        )
        reference_volumes: list[Decimal] = []
        absent_dates: list[date] = []
        incomplete_dates: list[date] = []
        mismatched_dates: list[date] = []
        invalid_history_dates: list[date] = []
        invalid_history_errors: list[str] = []
        interval_mismatch_dates: list[date] = []
        missing_reference_dates: list[date] = []
        for historical_date in expected_dates:
            historical_snapshot = history_by_date.get(historical_date)
            if historical_snapshot is None:
                absent_dates.append(historical_date)
                continue
            if not historical_snapshot.is_actionable:
                incomplete_dates.append(historical_date)
                continue
            if _history_bar_dates_mismatch(
                historical_snapshot,
                calendar=self.calendar,
                market=self.market,
                instrument_id=self.instrument_id,
                expected_date=historical_date,
            ):
                mismatched_dates.append(historical_date)
                continue
            try:
                historical_requested_locations = requested_session_locations(
                    calendar=self.calendar,
                    market=self.market,
                    intraday_start=historical_snapshot.request.intraday_start,
                    intraday_end=historical_snapshot.request.intraday_end,
                )
                if not historical_requested_locations:
                    evidence = _no_effective_session_evidence(
                        instrument_id=self.instrument_id,
                        snapshot_id=historical_snapshot.snapshot_id,
                        request=historical_snapshot,
                        status_key="history_market_status",
                    )
                    evidence["ineligible_history_session_dates"] = historical_date.isoformat()
                    return IndicatorResult.unavailable(
                        key=self.key,
                        reason="history_request_market_closed",
                        evidence=evidence,
                    )
                historical_coverage_gap = requested_range_coverage_gap(
                    tuple(
                        bar
                        for bar in historical_snapshot.intraday_bars
                        if bar.instrument_id == self.instrument_id
                    ),
                    calendar=self.calendar,
                    market=self.market,
                    intraday_start=historical_snapshot.request.intraday_start,
                    intraday_end=historical_snapshot.request.intraday_end,
                )
                if historical_coverage_gap is not None:
                    evidence = _coverage_evidence(
                        historical_coverage_gap,
                        instrument_id=self.instrument_id,
                        snapshot_id=historical_snapshot.snapshot_id,
                    )
                    evidence["incomplete_history_session_dates"] = historical_date.isoformat()
                    evidence["history_coverage_gap"] = historical_coverage_gap.kind
                    return IndicatorResult.unavailable(
                        key=self.key,
                        reason="incomplete_history_source_coverage",
                        evidence=evidence,
                    )
                reference_bar, interval_mismatch = _matching_reference_bar(
                    historical_snapshot,
                    calendar=self.calendar,
                    market=self.market,
                    instrument_id=self.instrument_id,
                    bar_width=self.bar_width,
                    current_bar=current_bar,
                    current_location=current_location,
                )
            except BarAggregationInputError as error:
                invalid_history_dates.append(historical_date)
                invalid_history_errors.append(f"{historical_date.isoformat()}: {error}")
                continue
            if interval_mismatch:
                interval_mismatch_dates.append(historical_date)
                continue
            if reference_bar is None:
                missing_reference_dates.append(historical_date)
                continue
            reference_volumes.append(reference_bar.volume)

        evidence = _evidence(current_bar, current_location, self.lookback_days, self.bar_width)
        missing_dates = (*absent_dates, *incomplete_dates, *missing_reference_dates)
        if invalid_history_dates:
            evidence["invalid_history_session_dates"] = _date_list(tuple(invalid_history_dates))
            evidence["validation_error"] = "; ".join(invalid_history_errors)
            return IndicatorResult.unavailable(
                key=self.key,
                reason="invalid_history_source_bars",
                evidence=evidence,
            )
        if interval_mismatch_dates:
            evidence["interval_mismatch_session_dates"] = _date_list(tuple(interval_mismatch_dates))
            return IndicatorResult.unavailable(
                key=self.key,
                reason="history_interval_mismatch",
                evidence=evidence,
            )
        if mismatched_dates:
            evidence["mismatched_history_dates"] = _date_list(tuple(mismatched_dates))
            return IndicatorResult.unavailable(
                key=self.key,
                reason="history_date_mismatch",
                evidence=evidence,
            )
        if absent_dates:
            evidence["missing_session_dates"] = _date_list(missing_dates)
            return IndicatorResult.unavailable(
                key=self.key,
                reason="insufficient_history",
                evidence=evidence,
            )
        if incomplete_dates:
            evidence["missing_session_dates"] = _date_list(missing_dates)
            return IndicatorResult.unavailable(
                key=self.key,
                reason="incomplete_history",
                evidence=evidence,
            )
        if missing_reference_dates:
            evidence["missing_session_dates"] = _date_list(missing_dates)
            return IndicatorResult.unavailable(
                key=self.key,
                reason="missing_reference_bar",
                evidence=evidence,
            )

        reference_average = divide_decimals(
            sum(reference_volumes, Decimal("0")), Decimal(len(reference_volumes))
        )
        if reference_average.is_zero():
            return IndicatorResult.unavailable(
                key=self.key,
                reason="zero_reference_volume",
                evidence=evidence,
            )
        return IndicatorResult.available(
            key=self.key,
            value=divide_decimals(current_bar.volume, reference_average),
            evidence=evidence,
        )


def _previous_trading_dates(
    *, calendar: TradingCalendarProvider, market: str, before: date, count: int
) -> tuple[date, ...]:
    found: list[date] = []
    candidate = before
    for _ in range(_MAX_CALENDAR_LOOKBACK_DAYS):
        candidate -= timedelta(days=1)
        if calendar.is_holiday(market, candidate):
            continue
        if calendar.sessions_for(market, candidate):
            found.append(candidate)
            if len(found) == count:
                return tuple(found)
    return tuple(found)


def _history_by_session_date(
    history: tuple[MarketDataSnapshot, ...],
    *,
    calendar: TradingCalendarProvider,
    market: str,
    current_date: date,
) -> dict[date, MarketDataSnapshot]:
    market_timezone = calendar.market_timezone(market)
    result: dict[date, MarketDataSnapshot] = {}
    for historical_snapshot in history:
        session_date = historical_snapshot.request.intraday_start.astimezone(market_timezone).date()
        if session_date >= current_date:
            continue
        if session_date in result:
            raise IndicatorInputError(
                "history must contain at most one snapshot per market session date"
            )
        result[session_date] = historical_snapshot
    return result


def _history_bar_dates_mismatch(
    historical_snapshot: MarketDataSnapshot,
    *,
    calendar: TradingCalendarProvider,
    market: str,
    instrument_id: UUID,
    expected_date: date,
) -> bool:
    """Reject history whose raw bars do not belong to the snapshot's market date."""

    market_timezone = calendar.market_timezone(market)
    return any(
        bar.starts_at.astimezone(market_timezone).date() != expected_date
        for bar in historical_snapshot.intraday_bars
        if bar.instrument_id == instrument_id
    )


def _matching_reference_bar(
    historical_snapshot: MarketDataSnapshot,
    *,
    calendar: TradingCalendarProvider,
    market: str,
    instrument_id: UUID,
    bar_width: timedelta,
    current_bar: Bar,
    current_location: SessionLocation,
) -> tuple[Bar | None, bool]:
    historical_bars = aggregate_bars(
        tuple(
            bar for bar in historical_snapshot.intraday_bars if bar.instrument_id == instrument_id
        ),
        calendar=calendar,
        market=market,
        width=bar_width,
    )
    current_geometry = _session_relative_geometry(
        current_bar, current_location, calendar=calendar, market=market
    )
    interval_mismatch = False
    for historical_bar in historical_bars:
        historical_location = locate_market_session(
            historical_bar, calendar=calendar, market=market
        )
        historical_geometry = _session_relative_geometry(
            historical_bar, historical_location, calendar=calendar, market=market
        )
        if historical_location.segment != current_location.segment:
            continue
        if historical_geometry[0] != current_geometry[0]:
            continue
        if historical_geometry == current_geometry:
            return historical_bar, False
        interval_mismatch = True
    return None, interval_mismatch


def _session_relative_geometry(
    bar: Bar,
    location: SessionLocation,
    *,
    calendar: TradingCalendarProvider,
    market: str,
) -> tuple[timedelta, timedelta]:
    """Return exact left/right window offsets from the relevant session open."""

    market_timezone = calendar.market_timezone(market)
    return (
        bar.starts_at.astimezone(market_timezone) - location.session.opens_at,
        bar.ends_at.astimezone(market_timezone) - location.session.opens_at,
    )


def _evidence(
    current_bar: Bar,
    location: SessionLocation,
    lookback_days: int,
    bar_width: timedelta,
) -> dict[str, str]:
    offset = (
        current_bar.starts_at.astimezone(location.session.opens_at.tzinfo)
        - location.session.opens_at
    )
    total_microseconds = (
        offset.days * 86_400_000_000 + offset.seconds * 1_000_000 + offset.microseconds
    )
    width_microseconds = (
        bar_width.days * 86_400_000_000 + bar_width.seconds * 1_000_000 + bar_width.microseconds
    )
    return {
        "bar_width_microseconds": canonical_timedelta_microseconds(width_microseconds),
        "lookback_days": str(lookback_days),
        "session_date": location.session_date.isoformat(),
        "session_offset": canonical_timedelta_microseconds(total_microseconds),
        "session_segment": str(location.segment),
    }


def _coverage_evidence(
    gap: RequestedCoverageGap, *, instrument_id: UUID, snapshot_id: str
) -> dict[str, str]:
    """Serialize calendar-aware source coverage evidence for unavailable results."""

    return {
        "instrument_id": str(instrument_id),
        "coverage_gap": gap.kind,
        "covered_end": gap.covered_end.isoformat(),
        "required_start": gap.required_start.isoformat(),
        "required_end": gap.required_end.isoformat(),
        "session_date": gap.location.session_date.isoformat(),
        "session_segment": str(gap.location.segment),
        "snapshot_id": snapshot_id,
    }


def _no_effective_session_evidence(
    *,
    instrument_id: UUID,
    snapshot_id: str,
    request: MarketDataSnapshot,
    status_key: str,
) -> dict[str, str]:
    """Record that a snapshot request overlaps no configured market session."""

    return {
        "instrument_id": str(instrument_id),
        status_key: "no_effective_session",
        "requested_start": request.request.intraday_start.isoformat(),
        "requested_end": request.request.intraday_end.isoformat(),
        "snapshot_id": snapshot_id,
    }


def _date_list(session_dates: tuple[date, ...]) -> str:
    return ",".join(item.isoformat() for item in sorted(session_dates))
