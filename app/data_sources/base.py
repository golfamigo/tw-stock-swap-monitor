"""Typed market-data provider port."""

from typing import Protocol, runtime_checkable

from app.data_sources.models import MarketDataRequest, MarketDataSnapshot


@runtime_checkable
class MarketDataProvider(Protocol):
    """Retrieve typed market data without exposing a provider-specific response shape."""

    @property
    def provider_name(self) -> str:
        """Return the stable identifier retained in snapshot quality evidence."""

    def get_quotes(self, request: MarketDataRequest) -> MarketDataSnapshot:
        """Return quote evidence for every requested instrument."""

    def get_intraday_bars(self, request: MarketDataRequest) -> MarketDataSnapshot:
        """Return session-aware intraday bars for the requested left-closed interval."""

    def get_daily_bars(self, request: MarketDataRequest) -> MarketDataSnapshot:
        """Return complete daily bars for [daily_start, daily_end), with end date excluded."""

    def get_market_snapshot(self, request: MarketDataRequest) -> MarketDataSnapshot:
        """Return quote, intraday, daily, and quality evidence in one immutable snapshot."""
