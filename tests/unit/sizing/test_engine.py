"""Behavior tests for deterministic, Decimal-only staged sizing."""

import importlib
import importlib.util
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from app.domain.entities import CandidateGroup, Position, RotationPlan
from app.domain.enums import PositionRole, PositionStatus
from app.domain.errors import (
    CandidateInstrumentUnauthorizedError,
    NonDecimalValueError,
    NonPositiveSaleQuantityError,
    ProtectedPositionSaleError,
    SaleQuantityExceedsPositionError,
    UnauthorizedRotationSourceError,
)
from app.domain.values import InstrumentRef, Quantity
from app.sizing.base import SizingError
from app.sizing.costs import CostError, calculate_purchase_cost, calculate_sale_proceeds


def _api() -> Any:
    assert importlib.util.find_spec("app.sizing") is not None
    return importlib.import_module("app.sizing")


def _created_at() -> datetime:
    return datetime(2026, 1, 5, 9, tzinfo=UTC)


def _context(
    *, source_role: PositionRole = PositionRole.ROTATION_SOURCE
) -> tuple[RotationPlan, CandidateGroup, UUID, Position, InstrumentRef]:
    owner_id = uuid4()
    candidate = InstrumentRef(UUID(int=3))
    candidate_group = CandidateGroup(
        candidate_group_id=uuid4(),
        user_id=owner_id,
        instruments=(candidate,),
        created_at=_created_at(),
    )
    source = Position(
        position_id=uuid4(),
        portfolio_id=uuid4(),
        instrument=InstrumentRef(uuid4()),
        quantity=Quantity(Decimal("20")),
        role=source_role,
        status=PositionStatus.OPEN,
        opened_at=_created_at(),
    )
    plan = RotationPlan(
        rotation_plan_id=uuid4(),
        portfolio_id=source.portfolio_id,
        candidate_group_ids=(candidate_group.candidate_group_id,),
        source_position_ids=(source.position_id,),
        protected_position_ids=(),
        created_at=_created_at(),
    )
    return plan, candidate_group, owner_id, source, candidate


def _configuration(api: Any) -> Any:
    return api.SizingConfiguration(
        stages=(
            api.SizingStage(stage_id="first", allocation_weight=Decimal("0.2")),
            api.SizingStage(stage_id="second", allocation_weight=Decimal("0.3")),
            api.SizingStage(stage_id="third", allocation_weight=Decimal("0.5")),
        ),
        fee_tax_profile=api.FeeTaxProfile(
            purchase_fee_rate=Decimal("0.01"),
            sale_fee_rate=Decimal("0.01"),
            sale_tax_rate=Decimal("0.02"),
        ),
        slippage_profile=api.SlippageProfile(
            purchase_rate=Decimal("0.02"), sale_rate=Decimal("0.01")
        ),
        reserve=Decimal("12"),
        minimum_quantity=Decimal("1"),
        lot_quantity=Decimal("10"),
        odd_lot_policy=api.OddLotPolicy.ALLOWED,
        rounding=api.DecimalRoundingPolicy(
            money_quantum=Decimal("0.01"),
            money_rounding=api.RoundingMode.HALF_EVEN,
            quantity_rounding=api.RoundingMode.DOWN,
        ),
    )


def _request(api: Any, *, source_role: PositionRole = PositionRole.ROTATION_SOURCE) -> Any:
    plan, candidate_group, owner_id, source, candidate = _context(source_role=source_role)
    return api.SizingRequest(
        plan=plan,
        candidate_group=candidate_group,
        portfolio_owner_id=owner_id,
        source_position=source,
        source_sale_quantity=Quantity(Decimal("10")),
        source_sale_price=Decimal("20"),
        candidate=candidate,
        candidate_price=Decimal("10"),
        available_cash=Decimal("100"),
        configuration=_configuration(api),
    )


def test_sizing_three_configured_stages_accounts_for_costs_reserve_and_decimal_quantities() -> None:
    api = _api()
    request = _request(api)

    result = api.DeterministicSizingEngine().size(request)

    assert result.status is api.SizingStatus.ACTIONABLE
    assert result.source_sale.source_position_id == request.source_position.position_id
    assert tuple(stage.stage_id for stage in result.stages) == ("first", "second", "third")
    assert tuple(stage.target_value for stage in result.stages) == (
        Decimal("56.01"),
        Decimal("84.01"),
        Decimal("140.03"),
    )
    assert tuple(stage.purchase_quantity.value for stage in result.stages) == (
        Decimal("5"),
        Decimal("8"),
        Decimal("13"),
    )
    assert result.net_sale_proceeds == Decimal("192.06")
    assert result.required_purchase_value == Decimal("260")
    assert result.configured_costs == Decimal("7.86")
    assert result.reserve == Decimal("12")
    assert result.total_required_cash == Decimal("279.86")
    assert result.total_required_cash <= result.available_cash + result.net_sale_proceeds
    assert result.remaining_cash == Decimal("12.2")


def test_sizing_stage_normalizes_exact_allocation_weights_to_basis_points() -> None:
    api = _api()

    assert api.SizingStage("first", Decimal("0.30000")).allocation_basis_points == 3000
    assert api.SizingStage("first", Decimal("0.3")).allocation_weight == Decimal("0.3000")
    with localcontext(Context(prec=2)):
        stage = api.SizingStage("third", Decimal("0.3334"))
        assert stage.allocation_basis_points == 3334
        assert stage.allocation_weight == Decimal("0.3334")
    with pytest.raises(SizingError, match="basis points"):
        api.SizingStage("first", Decimal("0.30001"))


def test_sizing_configuration_requires_exactly_ten_thousand_allocation_basis_points() -> None:
    api = _api()
    configuration = _configuration(api)
    complete_stages = (
        api.SizingStage("first", Decimal("0.3333")),
        api.SizingStage("second", Decimal("0.3333")),
        api.SizingStage("third", Decimal("0.3334")),
    )

    normalized = replace(configuration, stages=complete_stages)

    assert tuple(stage.allocation_basis_points for stage in normalized.stages) == (
        3333,
        3333,
        3334,
    )
    with pytest.raises(SizingError, match="10,000"):
        replace(
            configuration,
            stages=(
                api.SizingStage("first", Decimal("0.3333")),
                api.SizingStage("second", Decimal("0.3333")),
                api.SizingStage("third", Decimal("0.3333")),
            ),
        )


def test_sizing_result_is_invariant_to_ambient_decimal_precision() -> None:
    api = _api()
    request = _request(api)
    configuration = replace(
        request.configuration,
        stages=(
            api.SizingStage("first", Decimal("0.3333")),
            api.SizingStage("second", Decimal("0.3333")),
            api.SizingStage("third", Decimal("0.3334")),
        ),
    )
    baseline = api.DeterministicSizingEngine().size(replace(request, configuration=configuration))

    with localcontext(Context(prec=2)):
        low_precision = api.DeterministicSizingEngine().size(
            replace(request, configuration=configuration)
        )

    assert low_precision == baseline


def test_sizing_prohibits_odd_lots_when_configuration_requires_whole_lots() -> None:
    api = _api()
    request = _request(api)
    prohibited_configuration = api.SizingConfiguration(
        stages=request.configuration.stages,
        fee_tax_profile=request.configuration.fee_tax_profile,
        slippage_profile=request.configuration.slippage_profile,
        reserve=request.configuration.reserve,
        minimum_quantity=request.configuration.minimum_quantity,
        lot_quantity=request.configuration.lot_quantity,
        odd_lot_policy=api.OddLotPolicy.PROHIBITED,
        rounding=request.configuration.rounding,
    )
    request = api.SizingRequest(
        plan=request.plan,
        candidate_group=request.candidate_group,
        portfolio_owner_id=request.portfolio_owner_id,
        source_position=request.source_position,
        source_sale_quantity=request.source_sale_quantity,
        source_sale_price=request.source_sale_price,
        candidate=request.candidate,
        candidate_price=request.candidate_price,
        available_cash=request.available_cash,
        configuration=prohibited_configuration,
    )

    result = api.DeterministicSizingEngine().size(request)

    assert all(
        stage.purchase_quantity.value % Decimal("10") == Decimal("0") for stage in result.stages
    )


def test_sizing_full_lot_target_uses_lot_cost_for_quantity_increment() -> None:
    api = _api()
    request = _request(api)
    configuration = api.SizingConfiguration(
        stages=(api.SizingStage(stage_id="one_lot", allocation_weight=Decimal("1")),),
        fee_tax_profile=api.FeeTaxProfile(
            purchase_fee_rate=Decimal("0"), sale_fee_rate=Decimal("0"), sale_tax_rate=Decimal("0")
        ),
        slippage_profile=api.SlippageProfile(purchase_rate=Decimal("0"), sale_rate=Decimal("0")),
        reserve=Decimal("0"),
        minimum_quantity=Decimal("1"),
        lot_quantity=Decimal("10"),
        odd_lot_policy=api.OddLotPolicy.PROHIBITED,
        rounding=request.configuration.rounding,
    )
    request = api.SizingRequest(
        plan=request.plan,
        candidate_group=request.candidate_group,
        portfolio_owner_id=request.portfolio_owner_id,
        source_position=request.source_position,
        source_sale_quantity=Quantity(Decimal("1")),
        source_sale_price=request.source_sale_price,
        candidate=request.candidate,
        candidate_price=Decimal("10"),
        available_cash=Decimal("80"),
        configuration=configuration,
    )

    engine = api.DeterministicSizingEngine()

    assert engine._quantity_for_target(
        configuration=configuration,
        target_value=Decimal("100"),
        unit_cost=Decimal("10"),
    ) == Decimal("10")

    result = engine.size(request)

    assert result.status is api.SizingStatus.ACTIONABLE
    assert result.stages[0].target_value == Decimal("100")
    assert result.stages[0].purchase_quantity == Quantity(Decimal("10"))
    assert result.stages[0].total_cash == Decimal("100")


def test_sizing_returns_non_actionable_insufficient_cash_without_negative_quantities() -> None:
    api = _api()
    request = _request(api)
    request = api.SizingRequest(
        plan=request.plan,
        candidate_group=request.candidate_group,
        portfolio_owner_id=request.portfolio_owner_id,
        source_position=request.source_position,
        source_sale_quantity=Quantity(Decimal("1")),
        source_sale_price=Decimal("0.01"),
        candidate=request.candidate,
        candidate_price=request.candidate_price,
        available_cash=Decimal("0"),
        configuration=request.configuration,
    )

    result = api.DeterministicSizingEngine().size(request)

    assert result.status is api.SizingStatus.INSUFFICIENT_CASH
    assert result.actionable is False
    assert all(stage.purchase_quantity.value == Decimal("0") for stage in result.stages)
    assert result.remaining_cash == Decimal("0")


def test_sizing_rejects_a_protected_source_before_calculation() -> None:
    api = _api()

    with pytest.raises(ProtectedPositionSaleError):
        api.DeterministicSizingEngine().size(_request(api, source_role=PositionRole.PROTECTED_CORE))


def test_sizing_rejects_position_that_the_plan_categorizes_as_protected_not_source() -> None:
    api = _api()
    request = _request(api)
    protected_plan = RotationPlan(
        rotation_plan_id=request.plan.rotation_plan_id,
        portfolio_id=request.plan.portfolio_id,
        candidate_group_ids=request.plan.candidate_group_ids,
        source_position_ids=(),
        protected_position_ids=(request.source_position.position_id,),
        created_at=request.plan.created_at,
    )
    protected_request = api.SizingRequest(
        plan=protected_plan,
        candidate_group=request.candidate_group,
        portfolio_owner_id=request.portfolio_owner_id,
        source_position=request.source_position,
        source_sale_quantity=request.source_sale_quantity,
        source_sale_price=request.source_sale_price,
        candidate=request.candidate,
        candidate_price=request.candidate_price,
        available_cash=request.available_cash,
        configuration=request.configuration,
    )

    with pytest.raises(UnauthorizedRotationSourceError):
        api.DeterministicSizingEngine().size(protected_request)


def test_sizing_requires_an_explicit_plan_source_and_positive_sale_quantity() -> None:
    api = _api()
    request = _request(api)
    ordinary_position = Position(
        position_id=uuid4(),
        portfolio_id=request.plan.portfolio_id,
        instrument=InstrumentRef(uuid4()),
        quantity=request.source_position.quantity,
        role=PositionRole.NORMAL,
        status=PositionStatus.OPEN,
        opened_at=_created_at(),
    )

    with pytest.raises(UnauthorizedRotationSourceError, match="source"):
        api.DeterministicSizingEngine().size(replace(request, source_position=ordinary_position))
    with pytest.raises(NonPositiveSaleQuantityError, match="positive"):
        api.DeterministicSizingEngine().size(
            replace(request, source_sale_quantity=Quantity(Decimal("0")))
        )


def test_money_quantum_rounds_cost_audit_values_to_the_configured_increment() -> None:
    api = _api()
    rounding = api.DecimalRoundingPolicy(
        money_quantum=Decimal("0.05"),
        money_rounding=api.RoundingMode.HALF_UP,
        quantity_rounding=api.RoundingMode.DOWN,
    )
    zero_costs = api.FeeTaxProfile(
        purchase_fee_rate=Decimal("0"),
        sale_fee_rate=Decimal("0"),
        sale_tax_rate=Decimal("0"),
    )
    zero_slippage = api.SlippageProfile(purchase_rate=Decimal("0"), sale_rate=Decimal("0"))

    cost = calculate_purchase_cost(
        quantity=Decimal("1"),
        price=Decimal("1.02"),
        fee_tax_profile=zero_costs,
        slippage_profile=zero_slippage,
        rounding=rounding,
    )

    assert rounding.money(Decimal("1.02")) == Decimal("1.00")
    assert cost.gross_value == Decimal("1.00")
    assert cost.total_cash == Decimal("1.00")
    assert cost.total_cash % Decimal("0.05") == Decimal("0")


def test_money_not_increasing_preserves_the_true_quantum_floor_at_decimal_precision() -> None:
    api = _api()
    rounding = api.DecimalRoundingPolicy(
        money_quantum=Decimal("0.05"),
        money_rounding=api.RoundingMode.HALF_EVEN,
        quantity_rounding=api.RoundingMode.DOWN,
    )
    value = Decimal("1.049999999999999999999999999999")

    floored = rounding.money_not_increasing(value)

    assert floored == Decimal("1.00")
    assert floored <= value
    assert floored % Decimal("0.05") == Decimal("0")


def test_money_preserves_true_half_even_rounding_just_above_a_quantum_tie() -> None:
    api = _api()
    rounding = api.DecimalRoundingPolicy(
        money_quantum=Decimal("0.05"),
        money_rounding=api.RoundingMode.HALF_EVEN,
        quantity_rounding=api.RoundingMode.DOWN,
    )

    rounded = rounding.money(Decimal("1.025000000000000000000000000001"))

    assert rounded == Decimal("1.05")


def test_purchase_cost_preserves_high_precision_gross_before_quantum_rounding() -> None:
    api = _api()
    rounding = api.DecimalRoundingPolicy(
        money_quantum=Decimal("0.05"),
        money_rounding=api.RoundingMode.HALF_EVEN,
        quantity_rounding=api.RoundingMode.DOWN,
    )
    zero_costs = api.FeeTaxProfile(
        purchase_fee_rate=Decimal("0"),
        sale_fee_rate=Decimal("0"),
        sale_tax_rate=Decimal("0"),
    )
    zero_slippage = api.SlippageProfile(purchase_rate=Decimal("0"), sale_rate=Decimal("0"))

    cost = calculate_purchase_cost(
        quantity=Decimal("1"),
        price=Decimal("1.025000000000000000000000000001"),
        fee_tax_profile=zero_costs,
        slippage_profile=zero_slippage,
        rounding=rounding,
    )

    assert cost.gross_value == Decimal("1.05")
    assert cost.total_cash == Decimal("1.05")


def test_sale_credit_never_rounds_a_high_precision_sub_quantum_amount_upward() -> None:
    api = _api()
    rounding = api.DecimalRoundingPolicy(
        money_quantum=Decimal("0.05"),
        money_rounding=api.RoundingMode.HALF_EVEN,
        quantity_rounding=api.RoundingMode.DOWN,
    )
    zero_costs = api.FeeTaxProfile(
        purchase_fee_rate=Decimal("0"),
        sale_fee_rate=Decimal("0"),
        sale_tax_rate=Decimal("0"),
    )
    zero_slippage = api.SlippageProfile(purchase_rate=Decimal("0"), sale_rate=Decimal("0"))

    proceeds = calculate_sale_proceeds(
        quantity=Decimal("1"),
        price=Decimal("0.049999999999999999999999999999"),
        fee_tax_profile=zero_costs,
        slippage_profile=zero_slippage,
        rounding=rounding,
    )

    assert proceeds.gross_value == Decimal("0")
    assert proceeds.net_proceeds == Decimal("0")


def test_money_floor_rejects_huge_finite_decimal_exponents_before_scale_expansion() -> None:
    api = _api()
    rounding = api.DecimalRoundingPolicy(
        money_quantum=Decimal("0.05"),
        money_rounding=api.RoundingMode.HALF_EVEN,
        quantity_rounding=api.RoundingMode.DOWN,
    )

    with pytest.raises(CostError, match="scale"):
        rounding.money_not_increasing(Decimal("0E+1000000"))


def test_sizing_does_not_pre_round_available_cash_and_proceeds_into_an_action() -> None:
    api = _api()
    request = _request(api)
    high_precision_funding_configuration = api.SizingConfiguration(
        stages=(api.SizingStage(stage_id="only", allocation_weight=Decimal("1")),),
        fee_tax_profile=api.FeeTaxProfile(
            purchase_fee_rate=Decimal("0"),
            sale_fee_rate=Decimal("0"),
            sale_tax_rate=Decimal("0"),
        ),
        slippage_profile=api.SlippageProfile(purchase_rate=Decimal("0"), sale_rate=Decimal("0")),
        reserve=Decimal("0.05"),
        minimum_quantity=Decimal("1"),
        lot_quantity=Decimal("1"),
        odd_lot_policy=api.OddLotPolicy.ALLOWED,
        rounding=api.DecimalRoundingPolicy(
            money_quantum=Decimal("0.05"),
            money_rounding=api.RoundingMode.HALF_EVEN,
            quantity_rounding=api.RoundingMode.DOWN,
        ),
    )
    request = api.SizingRequest(
        plan=request.plan,
        candidate_group=request.candidate_group,
        portfolio_owner_id=request.portfolio_owner_id,
        source_position=request.source_position,
        source_sale_quantity=Quantity(Decimal("1")),
        source_sale_price=Decimal("0.05"),
        candidate=request.candidate,
        candidate_price=Decimal("0.05"),
        available_cash=Decimal("0.049999999999999999999999999999"),
        configuration=high_precision_funding_configuration,
    )

    result = api.DeterministicSizingEngine().size(request)

    assert result.actionable is False
    assert result.stages[0].purchase_quantity == Quantity(Decimal("0"))
    assert result.total_required_cash <= result.available_cash + result.net_sale_proceeds


def test_sizing_preserves_raw_cash_before_half_up_quantization_for_affordability() -> None:
    api = _api()
    request = _request(api)
    zero_cost_configuration = api.SizingConfiguration(
        stages=(api.SizingStage(stage_id="only", allocation_weight=Decimal("1")),),
        fee_tax_profile=api.FeeTaxProfile(
            purchase_fee_rate=Decimal("0"),
            sale_fee_rate=Decimal("0"),
            sale_tax_rate=Decimal("0"),
        ),
        slippage_profile=api.SlippageProfile(purchase_rate=Decimal("0"), sale_rate=Decimal("0")),
        reserve=Decimal("0"),
        minimum_quantity=Decimal("1"),
        lot_quantity=Decimal("1"),
        odd_lot_policy=api.OddLotPolicy.ALLOWED,
        rounding=api.DecimalRoundingPolicy(
            money_quantum=Decimal("0.05"),
            money_rounding=api.RoundingMode.HALF_UP,
            quantity_rounding=api.RoundingMode.DOWN,
        ),
    )
    request = api.SizingRequest(
        plan=request.plan,
        candidate_group=request.candidate_group,
        portfolio_owner_id=request.portfolio_owner_id,
        source_position=request.source_position,
        source_sale_quantity=Quantity(Decimal("1")),
        source_sale_price=Decimal("0.0001"),
        candidate=request.candidate,
        candidate_price=Decimal("0.04"),
        available_cash=Decimal("0.04"),
        configuration=zero_cost_configuration,
    )

    result = api.DeterministicSizingEngine().size(request)

    assert result.actionable is False
    assert result.status is api.SizingStatus.NO_PURCHASE_QUANTITY
    assert result.stages[0].purchase_quantity == Quantity(Decimal("0"))
    assert result.total_required_cash <= result.available_cash + result.net_sale_proceeds


def test_sizing_enforces_raw_aggregate_requirement_after_stage_cost_rounding() -> None:
    api = _api()
    request = _request(api)
    aggregate_rounding_configuration = api.SizingConfiguration(
        stages=(api.SizingStage(stage_id="only", allocation_weight=Decimal("1")),),
        fee_tax_profile=api.FeeTaxProfile(
            purchase_fee_rate=Decimal("0"),
            sale_fee_rate=Decimal("0"),
            sale_tax_rate=Decimal("0"),
        ),
        slippage_profile=api.SlippageProfile(purchase_rate=Decimal("0"), sale_rate=Decimal("0")),
        reserve=Decimal("0.02"),
        minimum_quantity=Decimal("1"),
        lot_quantity=Decimal("1"),
        odd_lot_policy=api.OddLotPolicy.ALLOWED,
        rounding=api.DecimalRoundingPolicy(
            money_quantum=Decimal("0.05"),
            money_rounding=api.RoundingMode.HALF_EVEN,
            quantity_rounding=api.RoundingMode.DOWN,
        ),
    )
    request = api.SizingRequest(
        plan=request.plan,
        candidate_group=request.candidate_group,
        portfolio_owner_id=request.portfolio_owner_id,
        source_position=request.source_position,
        source_sale_quantity=Quantity(Decimal("1")),
        source_sale_price=Decimal("0.0001"),
        candidate=request.candidate,
        candidate_price=Decimal("0.074"),
        available_cash=Decimal("0.21"),
        configuration=aggregate_rounding_configuration,
    )

    result = api.DeterministicSizingEngine().size(request)

    assert result.stages[0].purchase_quantity.value <= Decimal("2")
    assert result.total_required_cash <= result.available_cash + result.net_sale_proceeds


def test_sizing_large_target_uses_exact_floor_and_bounded_adjustment() -> None:
    api = _api()
    request = _request(api)
    configuration = api.SizingConfiguration(
        stages=(api.SizingStage(stage_id="only", allocation_weight=Decimal("1")),),
        fee_tax_profile=api.FeeTaxProfile(
            purchase_fee_rate=Decimal("0"),
            sale_fee_rate=Decimal("0"),
            sale_tax_rate=Decimal("0"),
        ),
        slippage_profile=api.SlippageProfile(purchase_rate=Decimal("0"), sale_rate=Decimal("0")),
        reserve=Decimal("0.01"),
        minimum_quantity=Decimal("1"),
        lot_quantity=Decimal("1"),
        odd_lot_policy=api.OddLotPolicy.ALLOWED,
        rounding=api.DecimalRoundingPolicy(
            money_quantum=Decimal("0.01"),
            money_rounding=api.RoundingMode.HALF_EVEN,
            quantity_rounding=api.RoundingMode.DOWN,
        ),
    )
    request = api.SizingRequest(
        plan=request.plan,
        candidate_group=request.candidate_group,
        portfolio_owner_id=request.portfolio_owner_id,
        source_position=request.source_position,
        source_sale_quantity=Quantity(Decimal("1")),
        source_sale_price=Decimal("0.01"),
        candidate=request.candidate,
        candidate_price=Decimal("0.01"),
        available_cash=Decimal("9999999999999999999999999999.99"),
        configuration=configuration,
    )
    expected_quantity = Decimal("999999999999999999999999999999")
    engine = api.DeterministicSizingEngine()

    assert (
        engine._quantity_for_target(
            configuration=configuration,
            target_value=Decimal("9999999999999999999999999999.99"),
            unit_cost=Decimal("0.01"),
        )
        == expected_quantity
    )
    assert engine._decrement_quantity(configuration, Decimal("1E+30")) == expected_quantity

    result = engine.size(request)

    assert result.stages[0].purchase_quantity == Quantity(expected_quantity)
    assert result.total_required_cash <= result.available_cash + result.net_sale_proceeds


def test_sizing_rejects_overselling_and_unavailable_candidates() -> None:
    api = _api()
    request = _request(api)
    oversell_request = api.SizingRequest(
        plan=request.plan,
        candidate_group=request.candidate_group,
        portfolio_owner_id=request.portfolio_owner_id,
        source_position=request.source_position,
        source_sale_quantity=Quantity(Decimal("20.01")),
        source_sale_price=request.source_sale_price,
        candidate=request.candidate,
        candidate_price=request.candidate_price,
        available_cash=request.available_cash,
        configuration=request.configuration,
    )
    unavailable_request = api.SizingRequest(
        plan=request.plan,
        candidate_group=request.candidate_group,
        portfolio_owner_id=request.portfolio_owner_id,
        source_position=request.source_position,
        source_sale_quantity=request.source_sale_quantity,
        source_sale_price=request.source_sale_price,
        candidate=InstrumentRef(uuid4()),
        candidate_price=request.candidate_price,
        available_cash=request.available_cash,
        configuration=request.configuration,
    )

    with pytest.raises(SaleQuantityExceedsPositionError):
        api.DeterministicSizingEngine().size(oversell_request)
    with pytest.raises(CandidateInstrumentUnauthorizedError):
        api.DeterministicSizingEngine().size(unavailable_request)


def test_sizing_rejects_float_configuration_without_coercion() -> None:
    api = _api()

    with pytest.raises(NonDecimalValueError):
        api.SizingStage(stage_id="only", allocation_weight=cast(Decimal, 1.0))
