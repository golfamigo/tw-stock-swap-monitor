"""Session-reset VWAP plugin with Decimal-only strategy-boundary values."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from uuid import UUID

from app.calendar.base import TradingCalendarProvider
from app.data_sources.models import Bar, MarketDataSnapshot
from app.indicators.bars import (
    BarAggregationInputError,
    SessionLocation,
    locate_market_session,
    requested_session_locations,
    validate_bars_for_calendar,
)
from app.indicators.base import DECIMAL_CONTEXT, IndicatorResult, canonical_decimal


@dataclass(frozen=True, slots=True)
class SessionVwapIndicator:
    """Calculate VWAP for the latest observed calendar session of one configured instrument."""

    calendar: TradingCalendarProvider
    market: str
    instrument_id: UUID
    key: str = "session_vwap"

    def __post_init__(self) -> None:
        if not self.market.strip():
            raise ValueError("market must not be blank")
        if not self.key.strip():
            raise ValueError("indicator key must not be blank")

    def calculate(self, market_snapshot: MarketDataSnapshot) -> IndicatorResult:
        """Return latest-session VWAP or an explicit non-actionable state with evidence."""

        if not market_snapshot.is_actionable:
            return IndicatorResult.unavailable(
                key=self.key,
                reason="snapshot_not_actionable",
                evidence={"snapshot_id": market_snapshot.snapshot_id},
            )
        scoped_bars = tuple(
            bar for bar in market_snapshot.intraday_bars if bar.instrument_id == self.instrument_id
        )
        if not scoped_bars:
            return IndicatorResult.unavailable(
                key=self.key,
                reason="missing_instrument_bars",
                evidence={"instrument_id": str(self.instrument_id)},
            )
        try:
            validated_bars = validate_bars_for_calendar(
                scoped_bars, calendar=self.calendar, market=self.market
            )
            located = tuple(
                (bar, locate_market_session(bar, calendar=self.calendar, market=self.market))
                for bar in validated_bars
            )
            requested_locations = requested_session_locations(
                calendar=self.calendar,
                market=self.market,
                intraday_start=market_snapshot.request.intraday_start,
                intraday_end=market_snapshot.request.intraday_end,
            )
            if not requested_locations:
                return IndicatorResult.unavailable(
                    key=self.key,
                    reason="requested_range_outside_sessions",
                    evidence={"snapshot_id": market_snapshot.snapshot_id},
                )
            latest_bar, _ = max(located, key=lambda item: item[0].starts_at)
            latest_location = requested_locations[-1]
            active_bars = tuple(
                bar
                for bar, location in located
                if location.session_date == latest_location.session_date
                and location.segment == latest_location.segment
            )
            trailing_coverage = _validate_active_session_coverage(
                active_bars, latest_location, market_snapshot
            )
        except BarAggregationInputError as error:
            return IndicatorResult.unavailable(
                key=self.key,
                reason="invalid_source_bars",
                evidence={
                    "instrument_id": str(self.instrument_id),
                    "snapshot_id": market_snapshot.snapshot_id,
                    "validation_error": str(error),
                },
            )
        total_volume = sum((bar.volume for bar in active_bars), Decimal("0"))
        evidence = _session_evidence(latest_location)
        evidence["observed_bars"] = str(len(active_bars))
        evidence["latest_bar_start"] = latest_bar.starts_at.isoformat()
        if trailing_coverage is not None:
            coverage_gap, covered_end, required_end = trailing_coverage
            evidence["coverage_gap"] = coverage_gap
            evidence["covered_end"] = covered_end.isoformat()
            evidence["required_start"] = latest_location.session.opens_at.isoformat()
            evidence["required_end"] = required_end.isoformat()
            return IndicatorResult.unavailable(
                key=self.key,
                reason="incomplete_source_coverage",
                evidence=evidence,
            )
        if total_volume.is_zero():
            return IndicatorResult.unavailable(
                key=self.key,
                reason="zero_volume",
                evidence=evidence,
            )
        with localcontext(DECIMAL_CONTEXT):
            weighted_total = sum(
                (_weighted_typical_price(bar) for bar in active_bars), Decimal("0")
            )
            value = canonical_decimal(weighted_total / total_volume)
        return IndicatorResult.available(key=self.key, value=value, evidence=evidence)


def _weighted_typical_price(bar: Bar) -> Decimal:
    typical_price = (bar.high + bar.low + bar.close) / Decimal("3")
    return typical_price * bar.volume


def _validate_active_session_coverage(
    active_bars: tuple[Bar, ...], location: SessionLocation, market_snapshot: MarketDataSnapshot
) -> tuple[str, datetime, datetime] | None:
    """Require a session VWAP source to cover from session open through request end."""

    session_timezone = location.session.opens_at.tzinfo
    required_start = location.session.opens_at
    required_end = min(
        location.session.closes_at,
        market_snapshot.request.intraday_end.astimezone(session_timezone),
    )
    if required_end <= required_start:
        raise BarAggregationInputError("active session falls outside the requested range")

    expected_start = required_start
    for bar in sorted(active_bars, key=lambda item: item.starts_at):
        local_start = bar.starts_at.astimezone(session_timezone)
        local_end = bar.ends_at.astimezone(session_timezone)
        if local_start != expected_start:
            if expected_start == required_start and local_start > expected_start:
                return ("missing_session_open", expected_start, required_end)
            raise BarAggregationInputError("active session bars have incomplete coverage")
        if local_end > required_end:
            raise BarAggregationInputError("active session bars exceed the requested range")
        expected_start = local_end
    if expected_start != required_end:
        coverage_gap = "missing_session_open" if expected_start == required_start else "trailing"
        return (coverage_gap, expected_start, required_end)
    return None


def _session_evidence(location: SessionLocation) -> dict[str, str]:
    return {
        "session_date": location.session_date.isoformat(),
        "session_segment": str(location.segment),
        "session_start": location.session.opens_at.isoformat(),
        "session_end": location.session.closes_at.isoformat(),
    }
