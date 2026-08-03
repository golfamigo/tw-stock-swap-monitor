"""Deterministic staged sizing with explicit Decimal funding evidence."""

from __future__ import annotations

from decimal import Decimal

from app.domain.errors import ProtectedPositionSaleError
from app.domain.invariants import (
    ensure_candidate_instrument_is_authorized,
    ensure_plan_reference_is_authorized,
    ensure_position_is_sellable,
)
from app.domain.values import Quantity
from app.sizing.base import (
    OddLotPolicy,
    SizingConfiguration,
    SizingRequest,
    SizingResult,
    SizingStage,
    SizingStatus,
    SourceSaleAudit,
    StageSizing,
)
from app.sizing.costs import (
    SaleProceeds,
    add_decimals,
    calculate_purchase_cost,
    calculate_sale_proceeds,
    floor_divide_decimals,
    multiply_decimals,
    subtract_decimals,
)

MAX_SIZING_ADJUSTMENT_SEARCH_STEPS = 4_096


class DeterministicSizingEngine:
    """Produce a safe non-mutating recommendation from explicitly supplied market rules."""

    def size(self, request: SizingRequest) -> SizingResult:
        """Calculate sale proceeds and stage quantities, enforcing funding after rounding."""

        if not isinstance(request, SizingRequest):
            raise TypeError("request must be a SizingRequest")
        if request.source_position.position_id in request.plan.protected_position_ids:
            raise ProtectedPositionSaleError("protected positions are never eligible for sale")
        ensure_position_is_sellable(request.source_position, request.source_sale_quantity)
        ensure_plan_reference_is_authorized(
            request.plan,
            request.source_position,
            portfolio_owner_id=request.portfolio_owner_id,
        )
        ensure_candidate_instrument_is_authorized(
            request.plan,
            request.candidate_group,
            request.candidate,
            portfolio_owner_id=request.portfolio_owner_id,
        )
        sale_proceeds = calculate_sale_proceeds(
            quantity=request.source_sale_quantity.value,
            price=request.source_sale_price,
            fee_tax_profile=request.configuration.fee_tax_profile,
            slippage_profile=request.configuration.slippage_profile,
            rounding=request.configuration.rounding,
        )
        source_sale = self._source_sale(request.source_sale_quantity, sale_proceeds)
        funding = add_decimals(request.available_cash, sale_proceeds.net_proceeds)
        if funding < request.configuration.reserve:
            return self._non_actionable_result(
                request=request,
                source_sale=source_sale,
                target_value=Decimal("0"),
                status=SizingStatus.INSUFFICIENT_CASH,
                reason="funding_does_not_cover_reserve",
            )
        spendable = subtract_decimals(funding, request.configuration.reserve)
        quantities = self._initial_quantities(
            request.configuration, request.candidate_price, spendable
        )
        stages = self._stage_results(
            request.configuration, request.candidate_price, spendable, quantities
        )
        stages = self._enforce_funding(
            request.configuration, request.candidate_price, funding, stages
        )
        if all(stage.purchase_quantity.value.is_zero() for stage in stages):
            return self._result(
                status=SizingStatus.NO_PURCHASE_QUANTITY,
                source_sale=source_sale,
                stages=stages,
                available_cash=request.available_cash,
                net_sale_proceeds=sale_proceeds.net_proceeds,
                reserve=request.configuration.reserve,
                reason="configured_minimum_or_lot_prevents_purchase",
            )
        return self._result(
            status=SizingStatus.ACTIONABLE,
            source_sale=source_sale,
            stages=stages,
            available_cash=request.available_cash,
            net_sale_proceeds=sale_proceeds.net_proceeds,
            reserve=request.configuration.reserve,
        )

    def _initial_quantities(
        self, configuration: SizingConfiguration, candidate_price: Decimal, spendable: Decimal
    ) -> tuple[Decimal, ...]:
        unit_cost = calculate_purchase_cost(
            quantity=Decimal("1"),
            price=candidate_price,
            fee_tax_profile=configuration.fee_tax_profile,
            slippage_profile=configuration.slippage_profile,
            rounding=configuration.rounding,
        ).total_cash
        if unit_cost <= Decimal("0"):
            return tuple(Decimal("0") for _ in configuration.stages)
        return tuple(
            self._quantity_for_target(
                configuration=configuration,
                target_value=configuration.rounding.money_not_increasing(
                    multiply_decimals(spendable, stage.allocation_weight)
                ),
                unit_cost=unit_cost,
            )
            for stage in configuration.stages
        )

    def _quantity_for_target(
        self, *, configuration: SizingConfiguration, target_value: Decimal, unit_cost: Decimal
    ) -> Decimal:
        increment = self._quantity_increment(configuration)
        increment_cost = multiply_decimals(unit_cost, increment)
        increments = floor_divide_decimals(target_value, increment_cost)
        quantity = multiply_decimals(Decimal(increments), increment)
        if quantity < configuration.minimum_quantity:
            return Decimal("0")
        return quantity

    def _stage_results(
        self,
        configuration: SizingConfiguration,
        candidate_price: Decimal,
        spendable: Decimal,
        quantities: tuple[Decimal, ...],
    ) -> tuple[StageSizing, ...]:
        return tuple(
            self._stage_result(
                configuration=configuration,
                stage=stage,
                candidate_price=candidate_price,
                target_value=configuration.rounding.money_not_increasing(
                    multiply_decimals(spendable, stage.allocation_weight)
                ),
                quantity=quantity,
            )
            for stage, quantity in zip(configuration.stages, quantities, strict=True)
        )

    def _stage_result(
        self,
        *,
        configuration: SizingConfiguration,
        stage: SizingStage,
        candidate_price: Decimal,
        target_value: Decimal,
        quantity: Decimal,
    ) -> StageSizing:
        cost = calculate_purchase_cost(
            quantity=quantity,
            price=candidate_price,
            fee_tax_profile=configuration.fee_tax_profile,
            slippage_profile=configuration.slippage_profile,
            rounding=configuration.rounding,
        )
        return StageSizing(
            stage_id=stage.stage_id,
            allocation_weight=stage.allocation_weight,
            target_value=target_value,
            purchase_quantity=Quantity(quantity),
            required_purchase_value=cost.gross_value,
            slippage_cost=cost.slippage_cost,
            fee=cost.fee,
            total_cash=cost.total_cash,
        )

    def _enforce_funding(
        self,
        configuration: SizingConfiguration,
        candidate_price: Decimal,
        funding: Decimal,
        stages: tuple[StageSizing, ...],
    ) -> tuple[StageSizing, ...]:
        quantities = [stage.purchase_quantity.value for stage in stages]
        for adjustment_index in range(len(quantities) - 1, -1, -1):
            if self._total_cash(configuration, quantities, candidate_price) <= funding:
                break
            if quantities[adjustment_index] < configuration.minimum_quantity:
                continue
            adjusted_quantity = self._max_affordable_stage_quantity(
                configuration=configuration,
                quantities=quantities,
                candidate_price=candidate_price,
                funding=funding,
                adjustment_index=adjustment_index,
            )
            if adjusted_quantity is None:
                return self._stage_results(
                    configuration,
                    candidate_price,
                    Decimal("0"),
                    tuple(Decimal("0") for _ in stages),
                )
            quantities[adjustment_index] = adjusted_quantity
        if self._total_cash(configuration, quantities, candidate_price) > funding:
            return self._stage_results(
                configuration,
                candidate_price,
                Decimal("0"),
                tuple(Decimal("0") for _ in stages),
            )
        return tuple(
            self._stage_result(
                configuration=configuration,
                stage=configured_stage,
                candidate_price=candidate_price,
                target_value=existing_stage.target_value,
                quantity=quantity,
            )
            for configured_stage, existing_stage, quantity in zip(
                configuration.stages, stages, quantities, strict=True
            )
        )

    def _max_affordable_stage_quantity(
        self,
        *,
        configuration: SizingConfiguration,
        quantities: list[Decimal],
        candidate_price: Decimal,
        funding: Decimal,
        adjustment_index: int,
    ) -> Decimal | None:
        increment = self._quantity_increment(configuration)
        lower_increments = 0
        upper_increments = floor_divide_decimals(quantities[adjustment_index], increment)
        search_steps = 0
        while lower_increments < upper_increments:
            if search_steps >= MAX_SIZING_ADJUSTMENT_SEARCH_STEPS:
                return None
            search_steps += 1
            midpoint = (lower_increments + upper_increments + 1) // 2
            candidate_quantity = multiply_decimals(Decimal(midpoint), increment)
            candidate_quantities = list(quantities)
            candidate_quantities[adjustment_index] = candidate_quantity
            if self._total_cash(configuration, candidate_quantities, candidate_price) <= funding:
                lower_increments = midpoint
            else:
                upper_increments = midpoint - 1
        return multiply_decimals(Decimal(lower_increments), increment)

    def _total_cash(
        self,
        configuration: SizingConfiguration,
        quantities: list[Decimal],
        candidate_price: Decimal,
    ) -> Decimal:
        purchase_total = configuration.reserve
        for quantity in quantities:
            purchase_cost = calculate_purchase_cost(
                quantity=quantity,
                price=candidate_price,
                fee_tax_profile=configuration.fee_tax_profile,
                slippage_profile=configuration.slippage_profile,
                rounding=configuration.rounding,
            )
            purchase_total = add_decimals(purchase_total, purchase_cost.total_cash)
        return purchase_total

    def _decrement_quantity(self, configuration: SizingConfiguration, quantity: Decimal) -> Decimal:
        next_quantity = subtract_decimals(quantity, self._quantity_increment(configuration))
        if next_quantity < configuration.minimum_quantity:
            return Decimal("0")
        return next_quantity

    def _quantity_increment(self, configuration: SizingConfiguration) -> Decimal:
        if configuration.odd_lot_policy is OddLotPolicy.ALLOWED:
            return configuration.minimum_quantity
        return configuration.lot_quantity

    def _source_sale(self, quantity: Quantity, proceeds: SaleProceeds) -> SourceSaleAudit:
        return SourceSaleAudit(
            quantity=quantity,
            gross_value=proceeds.gross_value,
            slippage_cost=proceeds.slippage_cost,
            fee=proceeds.fee,
            tax=proceeds.tax,
            net_proceeds=proceeds.net_proceeds,
        )

    def _non_actionable_result(
        self,
        *,
        request: SizingRequest,
        source_sale: SourceSaleAudit,
        target_value: Decimal,
        status: SizingStatus,
        reason: str,
    ) -> SizingResult:
        zero_stages = tuple(
            self._stage_result(
                configuration=request.configuration,
                stage=stage,
                candidate_price=request.candidate_price,
                target_value=target_value,
                quantity=Decimal("0"),
            )
            for stage in request.configuration.stages
        )
        return self._result(
            status=status,
            source_sale=source_sale,
            stages=zero_stages,
            available_cash=request.available_cash,
            net_sale_proceeds=source_sale.net_proceeds,
            reserve=request.configuration.reserve,
            reason=reason,
        )

    def _result(
        self,
        *,
        status: SizingStatus,
        source_sale: SourceSaleAudit,
        stages: tuple[StageSizing, ...],
        available_cash: Decimal,
        net_sale_proceeds: Decimal,
        reserve: Decimal,
        reason: str | None = None,
    ) -> SizingResult:
        required_purchase_value = Decimal("0")
        total_purchase_cash = Decimal("0")
        for stage in stages:
            required_purchase_value = add_decimals(
                required_purchase_value, stage.required_purchase_value
            )
            total_purchase_cash = add_decimals(total_purchase_cash, stage.total_cash)
        configured_costs = subtract_decimals(total_purchase_cash, required_purchase_value)
        total_required_cash = add_decimals(total_purchase_cash, reserve)
        funding = add_decimals(available_cash, net_sale_proceeds)
        remaining_cash = subtract_decimals(funding, total_required_cash)
        if remaining_cash < Decimal("0"):
            remaining_cash = Decimal("0")
        return SizingResult(
            status=status,
            actionable=status is SizingStatus.ACTIONABLE,
            source_sale=source_sale,
            stages=stages,
            available_cash=available_cash,
            net_sale_proceeds=net_sale_proceeds,
            reserve=reserve,
            required_purchase_value=required_purchase_value,
            configured_costs=configured_costs,
            total_required_cash=total_required_cash,
            remaining_cash=remaining_cash,
            reason=reason,
        )
