"""Decimal-only, configuration-driven fee, tax, slippage, and rounding calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.domain.values import require_finite_decimal


class CostError(ValueError):
    """Raised for invalid cost configuration or unrepresentable cost inputs."""


class RoundingMode(StrEnum):
    """Explicit Decimal quantization modes permitted by a sizing configuration."""

    DOWN = "ROUND_DOWN"
    HALF_EVEN = "ROUND_HALF_EVEN"
    HALF_UP = "ROUND_HALF_UP"


@dataclass(frozen=True, slots=True)
class DecimalRoundingPolicy:
    """The injected money and quantity quantization rules for one sizing request."""

    money_quantum: Decimal
    money_rounding: RoundingMode
    quantity_rounding: RoundingMode

    def __post_init__(self) -> None:
        require_finite_decimal(self.money_quantum, field_name="money_quantum")
        if self.money_quantum <= Decimal("0"):
            raise CostError("money_quantum must be positive")
        if not isinstance(self.money_rounding, RoundingMode):
            raise TypeError("money_rounding must be a RoundingMode")
        if not isinstance(self.quantity_rounding, RoundingMode):
            raise TypeError("quantity_rounding must be a RoundingMode")

    def money(self, value: Decimal) -> Decimal:
        """Quantize a finite money value by the supplied policy only."""

        require_finite_decimal(value, field_name="money value")
        increments = (value / self.money_quantum).to_integral_value(
            rounding=self.money_rounding.value
        )
        return increments * self.money_quantum


@dataclass(frozen=True, slots=True)
class FeeTaxProfile:
    """Injected purchase fee and sale fee/tax rates, all inclusive Decimal proportions."""

    purchase_fee_rate: Decimal
    sale_fee_rate: Decimal
    sale_tax_rate: Decimal

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.purchase_fee_rate, "purchase_fee_rate"),
            (self.sale_fee_rate, "sale_fee_rate"),
            (self.sale_tax_rate, "sale_tax_rate"),
        ):
            _require_rate(value, field_name=field_name)


@dataclass(frozen=True, slots=True)
class SlippageProfile:
    """Injected adverse purchase and sale slippage proportions."""

    purchase_rate: Decimal
    sale_rate: Decimal

    def __post_init__(self) -> None:
        _require_rate(self.purchase_rate, field_name="purchase_rate")
        _require_rate(self.sale_rate, field_name="sale_rate")


@dataclass(frozen=True, slots=True)
class PurchaseCost:
    """Exact purchase cash components after the configured money quantization."""

    gross_value: Decimal
    slippage_cost: Decimal
    fee: Decimal
    total_cash: Decimal


@dataclass(frozen=True, slots=True)
class SaleProceeds:
    """Exact net sale cash components after the configured money quantization."""

    gross_value: Decimal
    slippage_cost: Decimal
    fee: Decimal
    tax: Decimal
    net_proceeds: Decimal


def calculate_purchase_cost(
    *,
    quantity: Decimal,
    price: Decimal,
    fee_tax_profile: FeeTaxProfile,
    slippage_profile: SlippageProfile,
    rounding: DecimalRoundingPolicy,
) -> PurchaseCost:
    """Return purchase value plus configured slippage and fee without float coercion."""

    _require_nonnegative(quantity, field_name="purchase quantity")
    _require_positive(price, field_name="purchase price")
    gross_value = rounding.money(quantity * price)
    slippage_cost = rounding.money(gross_value * slippage_profile.purchase_rate)
    fee = rounding.money((gross_value + slippage_cost) * fee_tax_profile.purchase_fee_rate)
    total_cash = rounding.money(gross_value + slippage_cost + fee)
    return PurchaseCost(
        gross_value=gross_value,
        slippage_cost=slippage_cost,
        fee=fee,
        total_cash=total_cash,
    )


def calculate_sale_proceeds(
    *,
    quantity: Decimal,
    price: Decimal,
    fee_tax_profile: FeeTaxProfile,
    slippage_profile: SlippageProfile,
    rounding: DecimalRoundingPolicy,
) -> SaleProceeds:
    """Return net sale proceeds after configured adverse slippage, fee, and tax."""

    _require_nonnegative(quantity, field_name="sale quantity")
    _require_positive(price, field_name="sale price")
    gross_value = rounding.money(quantity * price)
    slippage_cost = rounding.money(gross_value * slippage_profile.sale_rate)
    post_slippage_value = gross_value - slippage_cost
    fee = rounding.money(post_slippage_value * fee_tax_profile.sale_fee_rate)
    tax = rounding.money(post_slippage_value * fee_tax_profile.sale_tax_rate)
    net_proceeds = rounding.money(post_slippage_value - fee - tax)
    return SaleProceeds(
        gross_value=gross_value,
        slippage_cost=slippage_cost,
        fee=fee,
        tax=tax,
        net_proceeds=net_proceeds,
    )


def _require_rate(value: Decimal, *, field_name: str) -> None:
    require_finite_decimal(value, field_name=field_name)
    if not Decimal("0") <= value <= Decimal("1"):
        raise CostError(f"{field_name} must be between zero and one")


def _require_nonnegative(value: Decimal, *, field_name: str) -> None:
    require_finite_decimal(value, field_name=field_name)
    if value < Decimal("0"):
        raise CostError(f"{field_name} must not be negative")


def _require_positive(value: Decimal, *, field_name: str) -> None:
    _require_nonnegative(value, field_name=field_name)
    if value.is_zero():
        raise CostError(f"{field_name} must be positive")
