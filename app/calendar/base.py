"""Typed calendar port used by market-data adapters."""

from datetime import date
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from app.domain.values import MarketSession


@runtime_checkable
class TradingCalendarProvider(Protocol):
    """Supply the IANA timezone and configured sessions for a market."""

    def market_timezone(self, market: str) -> ZoneInfo:
        """Return the market's configured IANA timezone."""

    def is_holiday(self, market: str, session_date: date) -> bool:
        """Return whether the supplied market is closed for the full date."""

    def sessions_for(self, market: str, session_date: date) -> tuple[MarketSession, ...]:
        """Return ordered non-overlapping sessions for one local market date."""
