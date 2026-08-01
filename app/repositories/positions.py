"""Typed port for portfolio-scoped position history."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.domain.access import AccessContext
from app.domain.entities import Position


class PositionRepository(Protocol):
    """Persist and retrieve positions only through an authenticated actor context."""

    def add(self, position: Position, *, access_context: AccessContext) -> None:
        """Store one position after mutation authorization and uniqueness checks."""

    def get(self, position_id: UUID, *, access_context: AccessContext) -> Position:
        """Read one position or hide it when it is not visible to the actor."""

    def list_for_portfolio(
        self, portfolio_id: UUID, *, access_context: AccessContext
    ) -> Sequence[Position]:
        """Return a portfolio's positions after scope validation."""
