"""Deterministic, offline market-data provider for M0/M1."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from functools import lru_cache
from hashlib import sha256

from app.calendar.base import TradingCalendarProvider
from app.data_sources.models import (
    Bar,
    DataQuality,
    MarketDataRequest,
    MarketDataSnapshot,
    Quote,
    canonical_datetime,
)
from app.domain.entities import Instrument

_MINIMUM_GENERATED_BASE_UNITS = 400
# The largest downward adjustment is 0.025 and the largest spread is 0.01,
# so the 0.04 floor (at the fixed 0.0001 OHLC scale) remains strictly positive.
_PRICE_UNIT_EXPONENT = -4
_HEX_TO_DECIMAL_PAIRS = str.maketrans(
    {character: f"{value:02d}" for value, character in enumerate("0123456789abcdef")}
)


@dataclass(frozen=True, slots=True)
class MockMarketDataProvider:
    """Generate reproducible prices from only injected configuration and request evidence."""

    provider_name: str
    seed: int
    calendar: TradingCalendarProvider
    source_delay: timedelta = timedelta()

    def __post_init__(self) -> None:
        if not self.provider_name.strip():
            raise ValueError("provider_name must not be blank")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise TypeError("seed must be an integer")
        if self.source_delay < timedelta():
            raise ValueError("source_delay must not be negative")

    def get_quotes(self, request: MarketDataRequest) -> MarketDataSnapshot:
        """Return deterministic quotes without producing any network traffic."""

        return self._snapshot(request, quotes=self._quotes(request))

    def get_intraday_bars(self, request: MarketDataRequest) -> MarketDataSnapshot:
        """Return calendar-bounded bars using left-closed, right-open input semantics."""

        intraday_bars, saw_closed_market = self._intraday_bars(request)
        return self._snapshot(
            request,
            intraday_bars=intraday_bars,
            market_closed=not intraday_bars and saw_closed_market,
        )

    def get_daily_bars(self, request: MarketDataRequest) -> MarketDataSnapshot:
        """Return one daily OHLCV bar for each configured open market date."""

        daily_bars, saw_closed_market = self._daily_bars(request)
        return self._snapshot(
            request,
            daily_bars=daily_bars,
            market_closed=not daily_bars and saw_closed_market,
        )

    def get_market_snapshot(self, request: MarketDataRequest) -> MarketDataSnapshot:
        """Return all deterministic evidence types for one complete request."""

        intraday_bars, intraday_saw_closed_market = self._intraday_bars(request)
        daily_bars, daily_saw_closed_market = self._daily_bars(request)
        return self._snapshot(
            request,
            quotes=self._quotes(request),
            intraday_bars=intraday_bars,
            daily_bars=daily_bars,
            market_closed=(
                (not intraday_bars and intraday_saw_closed_market)
                or (not daily_bars and daily_saw_closed_market)
            ),
        )

    def _snapshot(
        self,
        request: MarketDataRequest,
        *,
        quotes: tuple[Quote, ...] = (),
        intraday_bars: tuple[Bar, ...] = (),
        daily_bars: tuple[Bar, ...] = (),
        market_closed: bool = False,
    ) -> MarketDataSnapshot:
        return MarketDataSnapshot(
            provider=self.provider_name,
            request=request,
            quotes=quotes,
            intraday_bars=intraday_bars,
            daily_bars=daily_bars,
            quality=self._quality(request, market_closed=market_closed),
        )

    def _quality(self, request: MarketDataRequest, *, market_closed: bool = False) -> DataQuality:
        source_timestamp = request.requested_at.astimezone(UTC) - self.source_delay
        return DataQuality(
            provider=self.provider_name,
            fetched_at=request.requested_at,
            source_timestamp=source_timestamp,
            delay=self.source_delay,
            stale=self.source_delay > request.maximum_data_delay,
            missing_fields=(),
            bars_complete=True,
            anomalies=("market_closed",) if market_closed else (),
            confidence=Decimal("1"),
        )

    def _quotes(self, request: MarketDataRequest) -> tuple[Quote, ...]:
        return tuple(
            Quote(
                instrument_id=instrument.instrument_id,
                as_of=request.requested_at,
                price=self._price(instrument, "quote", request.requested_at, request.requested_at),
            )
            for instrument in request.instruments
        )

    def _intraday_bars(self, request: MarketDataRequest) -> tuple[tuple[Bar, ...], bool]:
        bars: list[Bar] = []
        saw_closed_market = False
        for instrument in request.instruments:
            has_effective_session_overlap = False
            timezone = self.calendar.market_timezone(instrument.market)
            intraday_start = request.intraday_start.astimezone(timezone)
            intraday_end = request.intraday_end.astimezone(timezone)
            session_date = intraday_start.date()
            final_date = intraday_end.date()
            while session_date <= final_date:
                if self.calendar.is_holiday(instrument.market, session_date):
                    saw_closed_market = True
                    session_date += timedelta(days=1)
                    continue
                sessions = self.calendar.sessions_for(instrument.market, session_date)
                if not sessions:
                    saw_closed_market = True
                    session_date += timedelta(days=1)
                    continue
                for session in sessions:
                    bar_start = max(session.opens_at, intraday_start)
                    bar_limit = min(session.closes_at, intraday_end)
                    if bar_start >= bar_limit:
                        continue
                    has_effective_session_overlap = True
                    _require_session_grid_alignment(
                        session.opens_at,
                        bar_start,
                        bar_limit,
                        request.intraday_interval,
                    )
                    while bar_start < bar_limit:
                        bar_end = bar_start + request.intraday_interval
                        bars.append(self._bar(instrument, "intraday", bar_start, bar_end))
                        bar_start = bar_end
                session_date += timedelta(days=1)
            if not has_effective_session_overlap:
                saw_closed_market = True
        return tuple(bars), saw_closed_market

    def _daily_bars(self, request: MarketDataRequest) -> tuple[tuple[Bar, ...], bool]:
        bars: list[Bar] = []
        saw_closed_market = False
        session_date = request.daily_start
        while session_date < request.daily_end:
            for instrument in request.instruments:
                if self.calendar.is_holiday(instrument.market, session_date):
                    saw_closed_market = True
                    continue
                sessions = self.calendar.sessions_for(instrument.market, session_date)
                if not sessions:
                    saw_closed_market = True
                    continue
                bars.append(
                    self._bar(
                        instrument,
                        "daily",
                        sessions[0].opens_at,
                        sessions[-1].closes_at,
                    )
                )
            session_date += timedelta(days=1)
        return tuple(bars), saw_closed_market

    def _bar(
        self, instrument: Instrument, kind: str, starts_at: datetime, ends_at: datetime
    ) -> Bar:
        with localcontext() as context:
            _configure_mock_decimal_context(context, self.seed)
            seed_prefix = _seed_price_prefix(self.seed)
            open_units = self._price_unit_suffix(instrument, f"{kind}:open", starts_at, ends_at)
            direction_units = (
                self._number(instrument, f"{kind}:direction", starts_at, ends_at) % 501 - 250
            )
            close_units = open_units + direction_units
            spread_units = self._number(instrument, f"{kind}:spread", starts_at, ends_at) % 100 + 1
            volume = Decimal(
                self._number(instrument, f"{kind}:volume", starts_at, ends_at) % 1_000_000 + 1
            )
            return Bar(
                instrument_id=instrument.instrument_id,
                starts_at=starts_at,
                ends_at=ends_at,
                open=_decimal_from_units(seed_prefix, open_units),
                high=_decimal_from_units(seed_prefix, max(open_units, close_units) + spread_units),
                low=_decimal_from_units(seed_prefix, min(open_units, close_units) - spread_units),
                close=_decimal_from_units(seed_prefix, close_units),
                volume=volume,
            )

    def _price(
        self, instrument: Instrument, kind: str, starts_at: datetime, ends_at: datetime
    ) -> Decimal:
        with localcontext() as context:
            _configure_mock_decimal_context(context, self.seed)
            return _decimal_from_units(
                _seed_price_prefix(self.seed),
                self._price_unit_suffix(instrument, kind, starts_at, ends_at),
            )

    def _price_unit_suffix(
        self, instrument: Instrument, kind: str, starts_at: datetime, ends_at: datetime
    ) -> int:
        """Return exact low-order OHLC units without carrying into the seed prefix."""

        variation = self._number(instrument, kind, starts_at, ends_at) % 99_000
        return _MINIMUM_GENERATED_BASE_UNITS + 100 + variation

    def _number(
        self, instrument: Instrument, kind: str, starts_at: datetime, ends_at: datetime
    ) -> int:
        payload = "\x1f".join(
            (
                _seed_text(self.seed),
                self.provider_name,
                str(instrument.instrument_id),
                instrument.market,
                instrument.symbol,
                kind,
                canonical_datetime(starts_at),
                canonical_datetime(ends_at),
            )
        )
        return int.from_bytes(sha256(payload.encode("utf-8")).digest(), byteorder="big")


def _mock_decimal_precision(seed: int) -> int:
    """Preserve four fractional OHLC places beyond a seed's full integer magnitude."""

    return max(28, len(_seed_price_prefix(seed)) + 5)


def _configure_mock_decimal_context(context: Context, seed: int) -> None:
    """Set precision and exponent bounds for exact price and OHLC construction."""

    price_adjusted = len(_seed_price_prefix(seed))
    context.prec = _mock_decimal_precision(seed)
    context.Emax = max(context.Emax, price_adjusted + 1)
    context.Emin = min(context.Emin, price_adjusted - context.prec - 1)


@lru_cache(maxsize=128)
def _seed_price_prefix(seed: int) -> str:
    """Injectively encode signed Python integers as Decimal-safe coefficient digits."""

    sign_marker = "1" if seed >= 0 else "2"
    return sign_marker + format(abs(seed), "x").translate(_HEX_TO_DECIMAL_PAIRS)


def _decimal_from_units(seed_prefix: str, units: int) -> Decimal:
    """Represent a seed prefix and exact low-order units at the fixed OHLC scale."""

    digits = tuple(int(digit) for digit in f"{seed_prefix}{units:05d}")
    return Decimal((0, digits, _PRICE_UNIT_EXPONENT))


def _seed_text(seed: int) -> str:
    """Encode every Python integer injectively without decimal-string digit limits."""

    return f"{'-' if seed < 0 else '+'}{abs(seed):x}"


def _require_session_grid_alignment(
    session_open: datetime,
    effective_start: datetime,
    effective_end: datetime,
    interval: timedelta,
) -> None:
    """Reject request edges that would force a short, non-grid source bar."""

    if (effective_start - session_open) % interval or (effective_end - session_open) % interval:
        raise ValueError("intraday request edges must align to the session grid")
