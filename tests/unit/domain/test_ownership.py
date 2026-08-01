from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from app.domain.access import AccessContext
from app.domain.entities import (
    CandidateGroup,
    Instrument,
    Portfolio,
    Position,
    RotationPlan,
    StrategyRun,
    User,
)
from app.domain.enums import PositionRole, PositionStatus, Scope
from app.domain.errors import (
    AuthorizationDenied,
    InvalidStrategyRunEvidenceError,
    NaiveDatetimeError,
    NonDecimalValueError,
    NonFiniteDecimalError,
    NotFoundForActor,
)
from app.domain.values import (
    ConfigurationSnapshotRef,
    InstrumentRef,
    MarketSession,
    Money,
    Ownership,
    Quantity,
)


def aware_at(hour: int = 9) -> datetime:
    return datetime(2026, 1, 5, hour, tzinfo=UTC)


def mapping_cycle() -> dict[str, object]:
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    return cycle


def list_cycle() -> list[object]:
    cycle: list[object] = []
    cycle.append(cycle)
    return cycle


def tuple_cycle() -> tuple[object, ...]:
    nested_list: list[object] = []
    cycle = (nested_list,)
    nested_list.append(cycle)
    return cycle


def test_scope_ownership_requires_the_expected_owner_identifier() -> None:
    owner_id = uuid4()

    assert Ownership(Scope.SYSTEM, None).owner_id is None
    assert Ownership(Scope.USER, owner_id).owner_id == owner_id
    assert Ownership(Scope.PORTFOLIO, owner_id).owner_id == owner_id

    with pytest.raises(ValueError):
        Ownership(Scope.SYSTEM, owner_id)
    with pytest.raises(ValueError):
        Ownership(Scope.USER, None)
    with pytest.raises(ValueError):
        Ownership(Scope.PORTFOLIO, None)


def test_ownership_rejects_raw_scope_strings_before_identity_checks() -> None:
    with pytest.raises(ValueError):
        Ownership("SYSTEM", None)  # type: ignore[arg-type]


def test_access_context_authorizes_by_scope_and_resolved_portfolio_owner() -> None:
    actor_id = uuid4()
    other_user_id = uuid4()
    portfolio_id = uuid4()
    context = AccessContext(
        actor_user_id=actor_id, request_id=uuid4(), authentication_method="test"
    )

    assert context.can_read(Ownership(Scope.SYSTEM, None))
    assert context.can_read(Ownership(Scope.USER, actor_id))
    assert not context.can_read(Ownership(Scope.USER, other_user_id))
    assert context.can_read(Ownership(Scope.PORTFOLIO, portfolio_id), portfolio_owner_id=actor_id)
    assert not context.can_read(
        Ownership(Scope.PORTFOLIO, portfolio_id), portfolio_owner_id=other_user_id
    )
    assert not context.can_mutate(Ownership(Scope.SYSTEM, None))

    administrator = AccessContext(
        actor_user_id=other_user_id,
        is_administrator=True,
        request_id=uuid4(),
        authentication_method="test",
    )
    assert administrator.can_read(Ownership(Scope.USER, actor_id))
    assert administrator.can_mutate(Ownership(Scope.SYSTEM, None))


def test_access_context_hides_unowned_records_and_denies_system_mutation() -> None:
    actor_id = uuid4()
    context = AccessContext(
        actor_user_id=actor_id, request_id=uuid4(), authentication_method="test"
    )

    with pytest.raises(NotFoundForActor):
        context.require_read(Ownership(Scope.USER, uuid4()))
    with pytest.raises(AuthorizationDenied):
        context.require_mutation(Ownership(Scope.SYSTEM, None))


def test_datetime_value_objects_reject_naive_timestamps() -> None:
    naive = datetime(2026, 1, 5, 9)

    with pytest.raises(NaiveDatetimeError):
        MarketSession(opens_at=naive, closes_at=aware_at(13))

    session = MarketSession(opens_at=aware_at(), closes_at=aware_at(13))
    assert session.closes_at - session.opens_at == timedelta(hours=4)


def test_money_and_quantity_are_finite_decimal_values_only() -> None:
    money = Money(amount=Decimal("12.50"), currency="TWD")
    quantity = Quantity(value=Decimal("10"))

    assert money.amount == Decimal("12.50")
    assert quantity.value == Decimal("10")

    with pytest.raises(NonDecimalValueError):
        Money(amount=12.5, currency="TWD")  # type: ignore[arg-type]
    with pytest.raises(NonDecimalValueError):
        Quantity(value=10)  # type: ignore[arg-type]
    with pytest.raises(NonFiniteDecimalError):
        Money(amount=Decimal("NaN"), currency="TWD")
    with pytest.raises(NonFiniteDecimalError):
        Quantity(value=Decimal("Infinity"))


def test_domain_aggregates_expose_their_ownership_boundaries() -> None:
    user_id = uuid4()
    portfolio_id = uuid4()
    instrument_id = uuid4()
    position_id = uuid4()
    group_id = uuid4()
    plan_id = uuid4()
    snapshot_id = uuid4()
    run_id = uuid4()
    instrument_ref = InstrumentRef(instrument_id=instrument_id)
    snapshot = ConfigurationSnapshotRef(
        snapshot_id=snapshot_id, content_hash="a" * 64, created_at=aware_at()
    )
    user = User(user_id=user_id, created_at=aware_at())
    portfolio = Portfolio(portfolio_id=portfolio_id, user_id=user_id, created_at=aware_at())
    instrument = Instrument(instrument_id=instrument_id, symbol="TEST", created_at=aware_at())
    position = Position(
        position_id=position_id,
        portfolio_id=portfolio_id,
        instrument=instrument_ref,
        quantity=Quantity(Decimal("10")),
        role=PositionRole.NORMAL,
        status=PositionStatus.OPEN,
        opened_at=aware_at(),
    )
    group = CandidateGroup(
        candidate_group_id=group_id,
        user_id=user_id,
        instruments=(instrument_ref,),
        created_at=aware_at(),
    )
    plan = RotationPlan(
        rotation_plan_id=plan_id,
        portfolio_id=portfolio_id,
        candidate_group_ids=(group_id,),
        source_position_ids=(position_id,),
        protected_position_ids=(),
        created_at=aware_at(),
    )
    run = StrategyRun(
        strategy_run_id=run_id,
        rotation_plan_id=plan_id,
        portfolio_id=portfolio_id,
        configuration_snapshot=snapshot,
        market_data_snapshot_id="market-snapshot",
        state_transition="PENDING->ACTION_NOTIFIED",
        outputs={"chosen_instrument": str(instrument_id)},
        occurred_at=aware_at(),
    )

    assert user.ownership == Ownership(Scope.USER, user_id)
    assert portfolio.ownership == Ownership(Scope.USER, user_id)
    assert instrument.ownership == Ownership(Scope.SYSTEM, None)
    assert position.ownership == Ownership(Scope.PORTFOLIO, portfolio_id)
    assert group.ownership == Ownership(Scope.USER, user_id)
    assert plan.ownership == Ownership(Scope.PORTFOLIO, portfolio_id)
    assert run.ownership == Ownership(Scope.PORTFOLIO, portfolio_id)
    with pytest.raises(TypeError):
        run.outputs["chosen_instrument"] = "other"  # type: ignore[index]


def test_position_rejects_raw_role_and_status_strings_that_bypass_invariants() -> None:
    with pytest.raises(ValueError):
        Position(
            position_id=uuid4(),
            portfolio_id=uuid4(),
            instrument=InstrumentRef(uuid4()),
            quantity=Quantity(Decimal("10")),
            role="PROTECTED_CORE",  # type: ignore[arg-type]
            status=PositionStatus.OPEN,
            opened_at=aware_at(),
        )
    with pytest.raises(ValueError):
        Position(
            position_id=uuid4(),
            portfolio_id=uuid4(),
            instrument=InstrumentRef(uuid4()),
            quantity=Quantity(Decimal("10")),
            role=PositionRole.NORMAL,
            status="OPEN",  # type: ignore[arg-type]
            opened_at=aware_at(),
        )


def test_strategy_run_recursively_freezes_nested_evidence() -> None:
    nested_outputs: dict[str, object] = {"result": {"score": Decimal("1.5")}}
    run = StrategyRun(
        strategy_run_id=uuid4(),
        rotation_plan_id=uuid4(),
        portfolio_id=uuid4(),
        configuration_snapshot=ConfigurationSnapshotRef(uuid4(), "a" * 64, aware_at()),
        market_data_snapshot_id="market-snapshot",
        state_transition="PENDING->ACTION_NOTIFIED",
        outputs=nested_outputs,
        occurred_at=aware_at(),
    )
    nested_result = run.outputs["result"]

    assert isinstance(nested_result, dict) is False
    with pytest.raises(TypeError):
        nested_result["score"] = Decimal("2")  # type: ignore[index]


def test_strategy_run_rejects_unsupported_mutable_evidence() -> None:
    with pytest.raises(InvalidStrategyRunEvidenceError):
        StrategyRun(
            strategy_run_id=uuid4(),
            rotation_plan_id=uuid4(),
            portfolio_id=uuid4(),
            configuration_snapshot=ConfigurationSnapshotRef(uuid4(), "a" * 64, aware_at()),
            market_data_snapshot_id="market-snapshot",
            state_transition="PENDING->ACTION_NOTIFIED",
            outputs={"raw": bytearray(b"mutable")},
            occurred_at=aware_at(),
        )


def test_strategy_run_rejects_unordered_set_evidence() -> None:
    with pytest.raises(InvalidStrategyRunEvidenceError):
        StrategyRun(
            strategy_run_id=uuid4(),
            rotation_plan_id=uuid4(),
            portfolio_id=uuid4(),
            configuration_snapshot=ConfigurationSnapshotRef(uuid4(), "a" * 64, aware_at()),
            market_data_snapshot_id="market-snapshot",
            state_transition="PENDING->ACTION_NOTIFIED",
            outputs={"unordered": {"value"}},
            occurred_at=aware_at(),
        )


def test_strategy_run_requires_mapping_outputs_root() -> None:
    with pytest.raises(InvalidStrategyRunEvidenceError):
        StrategyRun(
            strategy_run_id=uuid4(),
            rotation_plan_id=uuid4(),
            portfolio_id=uuid4(),
            configuration_snapshot=ConfigurationSnapshotRef(uuid4(), "a" * 64, aware_at()),
            market_data_snapshot_id="market-snapshot",
            state_transition="PENDING->ACTION_NOTIFIED",
            outputs=["not", "a", "mapping"],  # type: ignore[arg-type]
            occurred_at=aware_at(),
        )


@pytest.mark.parametrize(
    "cycle",
    [
        pytest.param(mapping_cycle(), id="mapping"),
        pytest.param(list_cycle(), id="list"),
        pytest.param(tuple_cycle(), id="tuple-through-list"),
    ],
)
def test_strategy_run_rejects_active_container_cycles(cycle: object) -> None:
    with pytest.raises(InvalidStrategyRunEvidenceError):
        StrategyRun(
            strategy_run_id=uuid4(),
            rotation_plan_id=uuid4(),
            portfolio_id=uuid4(),
            configuration_snapshot=ConfigurationSnapshotRef(uuid4(), "a" * 64, aware_at()),
            market_data_snapshot_id="market-snapshot",
            state_transition="PENDING->ACTION_NOTIFIED",
            outputs={"cycle": cycle},
            occurred_at=aware_at(),
        )


def test_candidate_group_copies_mutable_instrument_input_to_a_tuple() -> None:
    supplied_instruments = [InstrumentRef(uuid4())]
    group = CandidateGroup(
        candidate_group_id=uuid4(),
        user_id=uuid4(),
        instruments=supplied_instruments,  # type: ignore[arg-type]
        created_at=aware_at(),
    )

    supplied_instruments.append(InstrumentRef(uuid4()))

    assert isinstance(group.instruments, tuple)
    assert len(group.instruments) == 1


def test_rotation_plan_copies_mutable_reference_inputs_to_tuples() -> None:
    candidate_group_ids = [uuid4()]
    source_ids = [uuid4()]
    protected_ids: list[UUID] = []
    plan = RotationPlan(
        rotation_plan_id=uuid4(),
        portfolio_id=uuid4(),
        candidate_group_ids=candidate_group_ids,  # type: ignore[arg-type]
        source_position_ids=source_ids,  # type: ignore[arg-type]
        protected_position_ids=protected_ids,  # type: ignore[arg-type]
        created_at=aware_at(),
    )

    candidate_group_ids.append(uuid4())
    source_ids.append(uuid4())
    protected_ids.append(uuid4())

    assert plan.candidate_group_ids == (candidate_group_ids[0],)
    assert plan.source_position_ids == (source_ids[0],)
    assert plan.protected_position_ids == ()


def test_rotation_plan_rejects_duplicate_candidate_group_ids() -> None:
    candidate_group_id = uuid4()

    with pytest.raises(ValueError, match="candidate_group_ids must be unique"):
        RotationPlan(
            rotation_plan_id=uuid4(),
            portfolio_id=uuid4(),
            candidate_group_ids=(candidate_group_id, candidate_group_id),
            source_position_ids=(uuid4(),),
            protected_position_ids=(),
            created_at=aware_at(),
        )
