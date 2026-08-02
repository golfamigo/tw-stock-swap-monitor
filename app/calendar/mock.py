"""A fixed, configuration-supplied trading calendar for offline tests and development."""

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.values import MarketSession


@dataclass(frozen=True, slots=True)
class MockTradingCalendar:
    """Serve one configured market day, including optional midday session breaks."""

    market: str
    timezone_name: str
    trading_day: date
    sessions: tuple[MarketSession, ...]
    holidays: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        if not self.market.strip():
            raise ValueError("market must not be blank")
        try:
            timezone = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone_name must be a valid IANA timezone") from error
        if not self.sessions:
            raise ValueError("sessions must not be empty")

        normalized_sessions = tuple(
            sorted(
                (
                    MarketSession(
                        opens_at=session.opens_at.astimezone(timezone),
                        closes_at=session.closes_at.astimezone(timezone),
                    )
                    for session in self.sessions
                ),
                key=lambda session: session.opens_at,
            )
        )
        for session in normalized_sessions:
            if (
                session.opens_at.date() != self.trading_day
                or session.closes_at.date() != self.trading_day
            ):
                raise ValueError("configured sessions must stay within trading_day")
        for previous, current in zip(normalized_sessions, normalized_sessions[1:], strict=False):
            if current.opens_at < previous.closes_at:
                raise ValueError("configured sessions must not overlap")

        object.__setattr__(self, "timezone_name", timezone.key)
        object.__setattr__(self, "sessions", normalized_sessions)
        object.__setattr__(self, "holidays", frozenset(self.holidays))

    def market_timezone(self, market: str) -> ZoneInfo:
        """Return the fixed IANA timezone after checking the configured market."""

        self._require_market(market)
        return ZoneInfo(self.timezone_name)

    def is_holiday(self, market: str, session_date: date) -> bool:
        """Return configured full-day closures without consulting an external calendar."""

        self._require_market(market)
        return session_date in self.holidays

    def sessions_for(self, market: str, session_date: date) -> tuple[MarketSession, ...]:
        """Return no sessions for holidays or dates outside the fixed mock trading day."""

        self._require_market(market)
        if session_date != self.trading_day or self.is_holiday(market, session_date):
            return ()
        return self.sessions

    def _require_market(self, market: str) -> None:
        if market != self.market:
            raise ValueError("market is not configured for this calendar")
