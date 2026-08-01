"""Deterministic ownership, reference, and position safety invariants."""

from collections.abc import Iterable
from typing import Protocol
from uuid import UUID

from app.domain.entities import CandidateGroup, Position, RotationPlan
from app.domain.enums import PositionRole, PositionStatus, Scope
from app.domain.errors import (
    CandidateInstrumentUnauthorizedError,
    PlanReferenceUnauthorizedError,
    PositionAlreadyOpenError,
    PositionNotOpenError,
    ProtectedPositionSaleError,
    SaleQuantityExceedsPositionError,
)
from app.domain.values import InstrumentRef, Ownership, Quantity


class HasOwnership(Protocol):
    """A resource that can participate in a scoped plan reference check."""

    @property
    def ownership(self) -> Ownership: ...


def ensure_position_is_sellable(position: Position, sale_quantity: Quantity) -> None:
    """Reject protected, non-open, and oversize sale requests before sizing or execution."""

    if position.role is PositionRole.PROTECTED_CORE:
        raise ProtectedPositionSaleError("protected positions are never eligible for sale")
    if position.status is not PositionStatus.OPEN:
        raise PositionNotOpenError("only OPEN positions are eligible for sale")
    if sale_quantity.value > position.quantity.value:
        raise SaleQuantityExceedsPositionError("sale quantity exceeds open position quantity")


def ensure_plan_reference_is_authorized(
    plan: RotationPlan, reference: HasOwnership, *, portfolio_owner_id: UUID
) -> None:
    """Ensure a plan reference is global or belongs to the same portfolio user/boundary."""

    if isinstance(reference, Position):
        if reference.portfolio_id != plan.portfolio_id:
            raise PlanReferenceUnauthorizedError("position is outside the plan portfolio")
        if reference.position_id not in (
            *plan.source_position_ids,
            *plan.protected_position_ids,
        ):
            raise PlanReferenceUnauthorizedError("position is not attached to the plan")
        if (
            reference.position_id in plan.source_position_ids
            and reference.status is not PositionStatus.OPEN
        ):
            raise PositionNotOpenError("a source position must be OPEN")
        return

    if isinstance(reference, CandidateGroup):
        if reference.candidate_group_id not in plan.candidate_group_ids:
            raise PlanReferenceUnauthorizedError("candidate group is not attached to the plan")
        if reference.user_id != portfolio_owner_id:
            raise PlanReferenceUnauthorizedError("candidate group belongs to another user")
        return

    ownership = reference.ownership
    if ownership.scope is Scope.SYSTEM:
        return
    if ownership.scope is Scope.USER and ownership.owner_id == portfolio_owner_id:
        return
    if ownership.scope is Scope.PORTFOLIO and ownership.owner_id == plan.portfolio_id:
        return
    raise PlanReferenceUnauthorizedError("reference crosses the plan ownership boundary")


def ensure_candidate_instrument_is_authorized(
    plan: RotationPlan,
    candidate_group: CandidateGroup,
    instrument: InstrumentRef,
    *,
    portfolio_owner_id: UUID,
) -> None:
    """Ensure the chosen candidate is explicitly allowed by the plan's group."""

    ensure_plan_reference_is_authorized(
        plan, candidate_group, portfolio_owner_id=portfolio_owner_id
    )
    if instrument not in candidate_group.instruments:
        raise CandidateInstrumentUnauthorizedError(
            "instrument is not authorized by the candidate group"
        )


def ensure_open_position_representation_is_unique(positions: Iterable[Position]) -> None:
    """Allow unlimited CLOSED history but at most one OPEN row per portfolio/instrument pair."""

    open_pairs: set[tuple[UUID, UUID]] = set()
    for position in positions:
        if position.status is not PositionStatus.OPEN:
            continue
        pair = (position.portfolio_id, position.instrument.instrument_id)
        if pair in open_pairs:
            raise PositionAlreadyOpenError(
                "only one OPEN position is allowed for a portfolio/instrument pair"
            )
        open_pairs.add(pair)
