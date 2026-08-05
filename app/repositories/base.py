"""Common framework-independent types for scoped repository ports."""

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from app.domain.access import AccessContext
from app.domain.entities import Portfolio


class PortfolioOwnerLookup(Protocol):
    """Resolve a portfolio's owning user before an access decision is made."""

    def owner_id_for(self, portfolio_id: UUID) -> UUID | None:
        """Return the owner of an existing portfolio, or ``None`` if it is absent."""


class ScopedPortfolioStore(Protocol):
    """Expose the explicit portfolio ownership state required by scoped adapters."""

    @property
    def portfolio_owners(self) -> Mapping[UUID, UUID]:
        """Map each portfolio identifier to its owning user identifier."""

    def register_portfolio(self, portfolio: Portfolio, *, access_context: AccessContext) -> None:
        """Register a portfolio after authorizing its owner or an administrator."""
