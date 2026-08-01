"""Authorization decisions over explicit ownership boundaries."""

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from app.domain.enums import Scope
from app.domain.errors import AuthorizationDenied, NotFoundForActor
from app.domain.values import Ownership


@dataclass(frozen=True, slots=True)
class AccessContext:
    """Authenticated actor metadata used by every scoped command and query."""

    actor_user_id: UUID
    is_administrator: bool = False
    request_id: UUID = field(default_factory=uuid4)
    authentication_method: str = "unspecified"
    requested_ownership: Ownership | None = None

    def __post_init__(self) -> None:
        if not self.authentication_method.strip():
            raise ValueError("authentication_method must not be blank")

    def can_read(self, ownership: Ownership, *, portfolio_owner_id: UUID | None = None) -> bool:
        """Return whether the actor may read an already-resolved resource."""

        if self.is_administrator or ownership.scope is Scope.SYSTEM:
            return True
        if ownership.scope is Scope.USER:
            return ownership.owner_id == self.actor_user_id
        return portfolio_owner_id == self.actor_user_id

    def can_mutate(self, ownership: Ownership, *, portfolio_owner_id: UUID | None = None) -> bool:
        """Return whether the actor may mutate an already-resolved resource."""

        if self.is_administrator:
            return True
        if ownership.scope is Scope.SYSTEM:
            return False
        if ownership.scope is Scope.USER:
            return ownership.owner_id == self.actor_user_id
        return portfolio_owner_id == self.actor_user_id

    def require_read(self, ownership: Ownership, *, portfolio_owner_id: UUID | None = None) -> None:
        """Raise the enumeration-safe error when a resource is not readable by this actor."""

        if not self.can_read(ownership, portfolio_owner_id=portfolio_owner_id):
            raise NotFoundForActor("resource was not found for actor")

    def require_mutation(
        self, ownership: Ownership, *, portfolio_owner_id: UUID | None = None
    ) -> None:
        """Raise a suitable error when this actor cannot mutate the resource."""

        if self.can_mutate(ownership, portfolio_owner_id=portfolio_owner_id):
            return
        if ownership.scope is Scope.SYSTEM:
            raise AuthorizationDenied("only administrators may mutate SYSTEM resources")
        raise NotFoundForActor("resource was not found for actor")
