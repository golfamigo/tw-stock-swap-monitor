from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from app.domain.entities import CandidateGroup, Position, RotationPlan
from app.domain.enums import PositionRole, PositionStatus
from app.domain.errors import (
    CandidateInstrumentUnauthorizedError,
    NonPositivePositionQuantityError,
    PlanReferenceUnauthorizedError,
    PositionAlreadyOpenError,
    PositionNotOpenError,
    ProtectedPositionSaleError,
    SaleQuantityExceedsPositionError,
)
from app.domain.invariants import (
    ensure_candidate_instrument_is_authorized,
    ensure_open_position_representation_is_unique,
    ensure_plan_reference_is_authorized,
    ensure_position_is_sellable,
)
from app.domain.values import InstrumentRef, Quantity


def aware_at() -> datetime:
    return datetime(2026, 1, 5, 9, tzinfo=UTC)


def position(
    *,
    portfolio_id: UUID,
    instrument_id: UUID,
    role: PositionRole = PositionRole.ROTATION_SOURCE,
    status: PositionStatus = PositionStatus.OPEN,
) -> Position:
    return Position(
        position_id=uuid4(),
        portfolio_id=portfolio_id,
        instrument=InstrumentRef(instrument_id=instrument_id),
        quantity=Quantity(Decimal("10")),
        role=role,
        status=status,
        opened_at=aware_at(),
        closed_at=aware_at() if status is PositionStatus.CLOSED else None,
    )


def plan(
    *, portfolio_id: UUID, group_ids: tuple[UUID, ...], source_position_id: UUID
) -> RotationPlan:
    return RotationPlan(
        rotation_plan_id=uuid4(),
        portfolio_id=portfolio_id,
        candidate_group_ids=group_ids,
        source_position_ids=(source_position_id,),
        protected_position_ids=(),
        created_at=aware_at(),
    )


def test_protected_position_can_never_enter_a_sale_path() -> None:
    protected = position(
        portfolio_id=uuid4(), instrument_id=uuid4(), role=PositionRole.PROTECTED_CORE
    )

    with pytest.raises(ProtectedPositionSaleError):
        ensure_position_is_sellable(protected, Quantity(Decimal("1")))


def test_only_open_positions_can_be_sold() -> None:
    closed = position(portfolio_id=uuid4(), instrument_id=uuid4(), status=PositionStatus.CLOSED)

    with pytest.raises(PositionNotOpenError):
        ensure_position_is_sellable(closed, Quantity(Decimal("1")))


def test_sale_quantity_cannot_exceed_open_position_quantity() -> None:
    source = position(portfolio_id=uuid4(), instrument_id=uuid4())

    with pytest.raises(SaleQuantityExceedsPositionError):
        ensure_position_is_sellable(source, Quantity(Decimal("10.01")))


def test_position_rejects_zero_quantity_while_generic_quantity_remains_valid() -> None:
    zero_quantity = Quantity(Decimal("0"))

    with pytest.raises(NonPositivePositionQuantityError, match="positive"):
        Position(
            position_id=uuid4(),
            portfolio_id=uuid4(),
            instrument=InstrumentRef(uuid4()),
            quantity=zero_quantity,
            role=PositionRole.ROTATION_SOURCE,
            status=PositionStatus.OPEN,
            opened_at=aware_at(),
        )


def test_plan_source_must_belong_to_the_plan_portfolio() -> None:
    group_id = uuid4()
    source = position(portfolio_id=uuid4(), instrument_id=uuid4())
    rotation_plan = plan(
        portfolio_id=uuid4(), group_ids=(group_id,), source_position_id=source.position_id
    )

    with pytest.raises(PlanReferenceUnauthorizedError):
        ensure_plan_reference_is_authorized(rotation_plan, source, portfolio_owner_id=uuid4())


def test_plan_candidate_group_must_be_owned_by_the_portfolio_user() -> None:
    portfolio_id = uuid4()
    owner_id = uuid4()
    group = CandidateGroup(
        candidate_group_id=uuid4(),
        user_id=uuid4(),
        instruments=(InstrumentRef(uuid4()),),
        created_at=aware_at(),
    )
    rotation_plan = plan(
        portfolio_id=portfolio_id,
        group_ids=(group.candidate_group_id,),
        source_position_id=uuid4(),
    )

    with pytest.raises(PlanReferenceUnauthorizedError):
        ensure_plan_reference_is_authorized(rotation_plan, group, portfolio_owner_id=owner_id)


def test_candidate_instrument_must_belong_to_the_group_attached_to_the_plan() -> None:
    portfolio_id = uuid4()
    owner_id = uuid4()
    permitted_instrument = InstrumentRef(uuid4())
    group = CandidateGroup(
        candidate_group_id=uuid4(),
        user_id=owner_id,
        instruments=(permitted_instrument,),
        created_at=aware_at(),
    )
    rotation_plan = plan(
        portfolio_id=portfolio_id,
        group_ids=(group.candidate_group_id,),
        source_position_id=uuid4(),
    )

    with pytest.raises(CandidateInstrumentUnauthorizedError):
        ensure_candidate_instrument_is_authorized(
            rotation_plan, group, InstrumentRef(uuid4()), portfolio_owner_id=owner_id
        )


def test_plan_authorizes_each_attached_candidate_group_and_rejects_unattached_groups() -> None:
    portfolio_id = uuid4()
    owner_id = uuid4()
    first_group = CandidateGroup(
        candidate_group_id=uuid4(),
        user_id=owner_id,
        instruments=(InstrumentRef(uuid4()),),
        created_at=aware_at(),
    )
    second_group = CandidateGroup(
        candidate_group_id=uuid4(),
        user_id=owner_id,
        instruments=(InstrumentRef(uuid4()),),
        created_at=aware_at(),
    )
    unattached_group = CandidateGroup(
        candidate_group_id=uuid4(),
        user_id=owner_id,
        instruments=(InstrumentRef(uuid4()),),
        created_at=aware_at(),
    )
    rotation_plan = plan(
        portfolio_id=portfolio_id,
        group_ids=(first_group.candidate_group_id, second_group.candidate_group_id),
        source_position_id=uuid4(),
    )

    ensure_plan_reference_is_authorized(rotation_plan, first_group, portfolio_owner_id=owner_id)
    ensure_plan_reference_is_authorized(rotation_plan, second_group, portfolio_owner_id=owner_id)
    with pytest.raises(PlanReferenceUnauthorizedError, match="not attached"):
        ensure_plan_reference_is_authorized(
            rotation_plan, unattached_group, portfolio_owner_id=owner_id
        )


def test_closed_history_is_retained_while_only_one_open_representation_is_allowed() -> None:
    portfolio_id = uuid4()
    instrument_id = uuid4()
    open_position = position(portfolio_id=portfolio_id, instrument_id=instrument_id)
    closed_one = position(
        portfolio_id=portfolio_id, instrument_id=instrument_id, status=PositionStatus.CLOSED
    )
    closed_two = position(
        portfolio_id=portfolio_id, instrument_id=instrument_id, status=PositionStatus.CLOSED
    )

    ensure_open_position_representation_is_unique((open_position, closed_one, closed_two))

    duplicate_open = position(portfolio_id=portfolio_id, instrument_id=instrument_id)
    with pytest.raises(PositionAlreadyOpenError):
        ensure_open_position_representation_is_unique(
            (open_position, closed_one, closed_two, duplicate_open)
        )
