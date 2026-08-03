"""Deterministic staged sizing with explicit Decimal funding evidence."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal, localcontext

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
    calculate_purchase_cost,
    calculate_sale_proceeds,
)

_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


class DeterministicSizingEngine:
    """Produce a safe non-mutating recommendation from explicitly supplied market rules."""

    def size(self, request: SizingRequest) -> SizingResult:
        """Calculate sale proceeds and stage quantities, enforcing funding after rounding."""

        if not isinstance(request, SizingRequest):
            raise TypeError("request must be a SizingRequest")
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
        funding = request.configuration.rounding.money(
            request.available_cash + sale_proceeds.net_proceeds
        )
        if funding < request.configuration.reserve:
            return self._non_actionable_result(
                request=request,
                source_sale=source_sale,
                target_value=Decimal("0"),
                status=SizingStatus.INSUFFICIENT_CASH,
                reason="funding_does_not_cover_reserve",
            )
        spendable = request.configuration.rounding.money(funding - request.configuration.reserve)
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
        return tuple(
            self._quantity_for_target(
                configuration=configuration,
                target_value=configuration.rounding.money(spendable * stage.allocation_weight),
                unit_cost=unit_cost,
            )
            for stage in configuration.stages
        )

    def _quantity_for_target(
        self, *, configuration: SizingConfiguration, target_value: Decimal, unit_cost: Decimal
    ) -> Decimal:
        increment = self._quantity_increment(configuration)
        with localcontext(_DECIMAL_CONTEXT):
            raw_quantity = target_value / unit_cost
            increments = (raw_quantity / increment).to_integral_value(rounding=ROUND_DOWN)
        quantity = increments * increment
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
                target_value=configuration.rounding.money(spendable * stage.allocation_weight),
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
        while self._total_cash(configuration, quantities, candidate_price) > funding:
            decrement_index = self._last_reducible_stage(configuration, quantities)
            if decrement_index is None:
                return self._stage_results(
                    configuration,
                    candidate_price,
                    Decimal("0"),
                    tuple(Decimal("0") for _ in stages),
                )
            quantities[decrement_index] = self._decrement_quantity(
                configuration, quantities[decrement_index]
            )
        spendable = sum((stage.target_value for stage in stages), Decimal("0"))
        return self._stage_results(configuration, candidate_price, spendable, tuple(quantities))

    def _total_cash(
        self,
        configuration: SizingConfiguration,
        quantities: list[Decimal],
        candidate_price: Decimal,
    ) -> Decimal:
        purchase_total = sum(
            (
                calculate_purchase_cost(
                    quantity=quantity,
                    price=candidate_price,
                    fee_tax_profile=configuration.fee_tax_profile,
                    slippage_profile=configuration.slippage_profile,
                    rounding=configuration.rounding,
                ).total_cash
                for quantity in quantities
            ),
            configuration.reserve,
        )
        return configuration.rounding.money(purchase_total)

    def _last_reducible_stage(
        self, configuration: SizingConfiguration, quantities: list[Decimal]
    ) -> int | None:
        for index in range(len(quantities) - 1, -1, -1):
            if quantities[index] >= configuration.minimum_quantity:
                return index
        return None

    def _decrement_quantity(self, configuration: SizingConfiguration, quantity: Decimal) -> Decimal:
        next_quantity = quantity - self._quantity_increment(configuration)
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
        required_purchase_value = sum(
            (stage.required_purchase_value for stage in stages), Decimal("0")
        )
        total_purchase_cash = sum((stage.total_cash for stage in stages), Decimal("0"))
        configured_costs = total_purchase_cash - required_purchase_value
        total_required_cash = total_purchase_cash + reserve
        funding = available_cash + net_sale_proceeds
        remaining_cash = funding - total_required_cash
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
