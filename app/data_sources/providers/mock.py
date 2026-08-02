"""Deterministic, offline market-data provider for M0/M1."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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

        intraday_bars, saw_holiday = self._intraday_bars(request)
        return self._snapshot(
            request,
            intraday_bars=intraday_bars,
            market_closed=not intraday_bars and saw_holiday,
        )

    def get_daily_bars(self, request: MarketDataRequest) -> MarketDataSnapshot:
        """Return one daily OHLCV bar for each configured open market date."""

        daily_bars, saw_holiday = self._daily_bars(request)
        return self._snapshot(
            request,
            daily_bars=daily_bars,
            market_closed=not daily_bars and saw_holiday,
        )

    def get_market_snapshot(self, request: MarketDataRequest) -> MarketDataSnapshot:
        """Return all deterministic evidence types for one complete request."""

        intraday_bars, intraday_saw_holiday = self._intraday_bars(request)
        daily_bars, daily_saw_holiday = self._daily_bars(request)
        return self._snapshot(
            request,
            quotes=self._quotes(request),
            intraday_bars=intraday_bars,
            daily_bars=daily_bars,
            market_closed=(
                not intraday_bars and not daily_bars and (intraday_saw_holiday or daily_saw_holiday)
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
        saw_holiday = False
        for instrument in request.instruments:
            timezone = self.calendar.market_timezone(instrument.market)
            session_date = request.intraday_start.astimezone(timezone).date()
            final_date = request.intraday_end.astimezone(timezone).date()
            while session_date <= final_date:
                if self.calendar.is_holiday(instrument.market, session_date):
                    saw_holiday = True
                    session_date += timedelta(days=1)
                    continue
                for session in self.calendar.sessions_for(instrument.market, session_date):
                    bar_start = max(session.opens_at, request.intraday_start)
                    bar_limit = min(session.closes_at, request.intraday_end)
                    while bar_start < bar_limit:
                        bar_end = min(bar_start + request.intraday_interval, bar_limit)
                        bars.append(self._bar(instrument, "intraday", bar_start, bar_end))
                        bar_start = bar_end
                session_date += timedelta(days=1)
        return tuple(bars), saw_holiday

    def _daily_bars(self, request: MarketDataRequest) -> tuple[tuple[Bar, ...], bool]:
        bars: list[Bar] = []
        saw_holiday = False
        session_date = request.daily_start
        while session_date < request.daily_end:
            for instrument in request.instruments:
                if self.calendar.is_holiday(instrument.market, session_date):
                    saw_holiday = True
                    continue
                sessions = self.calendar.sessions_for(instrument.market, session_date)
                if sessions:
                    bars.append(
                        self._bar(
                            instrument,
                            "daily",
                            sessions[0].opens_at,
                            sessions[-1].closes_at,
                        )
                    )
            session_date += timedelta(days=1)
        return tuple(bars), saw_holiday

    def _bar(
        self, instrument: Instrument, kind: str, starts_at: datetime, ends_at: datetime
    ) -> Bar:
        open_price = self._price(instrument, f"{kind}:open", starts_at, ends_at)
        direction = Decimal(
            self._number(instrument, f"{kind}:direction", starts_at, ends_at) % 501 - 250
        )
        close_price = open_price + direction / Decimal("10000")
        spread = Decimal(self._number(instrument, f"{kind}:spread", starts_at, ends_at) % 100 + 1)
        spread /= Decimal("10000")
        volume = Decimal(
            self._number(instrument, f"{kind}:volume", starts_at, ends_at) % 1_000_000 + 1
        )
        return Bar(
            instrument_id=instrument.instrument_id,
            starts_at=starts_at,
            ends_at=ends_at,
            open=open_price,
            high=max(open_price, close_price) + spread,
            low=min(open_price, close_price) - spread,
            close=close_price,
            volume=volume,
        )

    def _price(
        self, instrument: Instrument, kind: str, starts_at: datetime, ends_at: datetime
    ) -> Decimal:
        seed_component = _nonnegative_seed(self.seed)
        variation = self._number(instrument, kind, starts_at, ends_at) % 100_000
        return Decimal(seed_component * 1_000_000 + variation + 1) / Decimal("100")

    def _number(
        self, instrument: Instrument, kind: str, starts_at: datetime, ends_at: datetime
    ) -> int:
        payload = "\x1f".join(
            (
                str(self.seed),
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


def _nonnegative_seed(seed: int) -> int:
    """Injectively encode signed Python integers so distinct seeds change every price."""

    return seed * 2 if seed >= 0 else -seed * 2 - 1
