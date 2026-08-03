"""Decimal-only, configuration-driven fee, tax, slippage, and rounding calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.domain.values import require_finite_decimal

MAX_ROUNDING_DECIMAL_DIGITS = 1_000
MAX_ROUNDING_DECIMAL_EXPONENT = 1_000
MAX_ROUNDING_SCALE_DIFFERENCE = 1_000


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
        _validate_rounding_decimal(self.money_quantum, field_name="money_quantum")
        if self.money_quantum <= Decimal("0"):
            raise CostError("money_quantum must be positive")
        if not isinstance(self.money_rounding, RoundingMode):
            raise TypeError("money_rounding must be a RoundingMode")
        if not isinstance(self.quantity_rounding, RoundingMode):
            raise TypeError("quantity_rounding must be a RoundingMode")

    def money(self, value: Decimal) -> Decimal:
        """Quantize a finite money value by the supplied policy only."""

        require_finite_decimal(value, field_name="money value")
        return _round_to_quantum(value, self.money_quantum, self.money_rounding)

    def money_not_increasing(self, value: Decimal) -> Decimal:
        """Round monetary credit down to a quantum without increasing available funding."""

        require_finite_decimal(value, field_name="money value")
        return _floor_to_quantum(value, self.money_quantum)


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
    gross_value = rounding.money(multiply_decimals(quantity, price))
    slippage_cost = rounding.money(multiply_decimals(gross_value, slippage_profile.purchase_rate))
    fee = rounding.money(
        multiply_decimals(
            add_decimals(gross_value, slippage_cost), fee_tax_profile.purchase_fee_rate
        )
    )
    total_cash = rounding.money(add_decimals(add_decimals(gross_value, slippage_cost), fee))
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
    gross_value = rounding.money_not_increasing(multiply_decimals(quantity, price))
    slippage_cost = rounding.money(multiply_decimals(gross_value, slippage_profile.sale_rate))
    post_slippage_value = subtract_decimals(gross_value, slippage_cost)
    fee = rounding.money(multiply_decimals(post_slippage_value, fee_tax_profile.sale_fee_rate))
    tax = rounding.money(multiply_decimals(post_slippage_value, fee_tax_profile.sale_tax_rate))
    net_proceeds = rounding.money_not_increasing(
        subtract_decimals(subtract_decimals(post_slippage_value, fee), tax)
    )
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


def add_decimals(left: Decimal, right: Decimal) -> Decimal:
    """Add validated Decimal operands without ambient-context rounding."""

    left_coefficient, left_exponent = _decimal_coefficient_and_exponent(
        left, field_name="left Decimal"
    )
    right_coefficient, right_exponent = _decimal_coefficient_and_exponent(
        right, field_name="right Decimal"
    )
    result_exponent = min(left_exponent, right_exponent)
    left_shift = left_exponent - result_exponent
    right_shift = right_exponent - result_exponent
    _validate_scaled_coefficient(left_coefficient, left_shift, field_name="left Decimal")
    _validate_scaled_coefficient(right_coefficient, right_shift, field_name="right Decimal")
    result_coefficient = (left_coefficient * (10**left_shift)) + (
        right_coefficient * (10**right_shift)
    )
    return _decimal_from_coefficient(result_coefficient, result_exponent)


def subtract_decimals(left: Decimal, right: Decimal) -> Decimal:
    """Subtract validated Decimal operands without ambient-context rounding."""

    left_coefficient, left_exponent = _decimal_coefficient_and_exponent(
        left, field_name="left Decimal"
    )
    right_coefficient, right_exponent = _decimal_coefficient_and_exponent(
        right, field_name="right Decimal"
    )
    result_exponent = min(left_exponent, right_exponent)
    left_shift = left_exponent - result_exponent
    right_shift = right_exponent - result_exponent
    _validate_scaled_coefficient(left_coefficient, left_shift, field_name="left Decimal")
    _validate_scaled_coefficient(right_coefficient, right_shift, field_name="right Decimal")
    result_coefficient = (left_coefficient * (10**left_shift)) - (
        right_coefficient * (10**right_shift)
    )
    return _decimal_from_coefficient(result_coefficient, result_exponent)


def multiply_decimals(left: Decimal, right: Decimal) -> Decimal:
    """Multiply validated Decimal operands without ambient-context rounding."""

    left_coefficient, left_exponent = _decimal_coefficient_and_exponent(
        left, field_name="left Decimal"
    )
    right_coefficient, right_exponent = _decimal_coefficient_and_exponent(
        right, field_name="right Decimal"
    )
    result_exponent = left_exponent + right_exponent
    if abs(result_exponent) > MAX_ROUNDING_DECIMAL_EXPONENT:
        raise CostError("Decimal product scale exceeds the rounding resource limit")
    if (
        _coefficient_digit_count(left_coefficient) + _coefficient_digit_count(right_coefficient)
        > MAX_ROUNDING_DECIMAL_DIGITS
    ):
        raise CostError("Decimal product digit count exceeds the rounding resource limit")
    return _decimal_from_coefficient(left_coefficient * right_coefficient, result_exponent)


def floor_divide_decimals(numerator: Decimal, denominator: Decimal) -> int:
    """Return an exact bounded Decimal floor quotient without ambient-context division."""

    numerator_coefficient, numerator_exponent = _decimal_coefficient_and_exponent(
        numerator, field_name="numerator"
    )
    denominator_coefficient, denominator_exponent = _decimal_coefficient_and_exponent(
        denominator, field_name="denominator"
    )
    if denominator_coefficient <= 0:
        raise CostError("denominator must be positive")
    exponent_difference = numerator_exponent - denominator_exponent
    if abs(exponent_difference) > MAX_ROUNDING_SCALE_DIFFERENCE:
        raise CostError("Decimal scale difference exceeds the rounding resource limit")
    if exponent_difference >= 0:
        _validate_scaled_coefficient(
            numerator_coefficient, exponent_difference, field_name="numerator"
        )
        scaled_numerator = numerator_coefficient * (10**exponent_difference)
        scaled_denominator = denominator_coefficient
    else:
        scaled_numerator = numerator_coefficient
        _validate_scaled_coefficient(
            denominator_coefficient, -exponent_difference, field_name="denominator"
        )
        scaled_denominator = denominator_coefficient * (10 ** (-exponent_difference))
    return int(scaled_numerator // scaled_denominator)


def _floor_to_quantum(value: Decimal, quantum: Decimal) -> Decimal:
    """Return the exact mathematical floor multiple without context-rounded division."""

    numerator, denominator, quantum_coefficient, quantum_exponent = _quantum_ratio(value, quantum)
    increments = numerator // denominator
    return _decimal_from_coefficient(increments * quantum_coefficient, quantum_exponent)


def _round_to_quantum(value: Decimal, quantum: Decimal, rounding: RoundingMode) -> Decimal:
    """Return an exact configured multiple without context-rounded division."""

    numerator, denominator, quantum_coefficient, quantum_exponent = _quantum_ratio(value, quantum)
    increments = _round_ratio(numerator, denominator, rounding)
    return _decimal_from_coefficient(increments * quantum_coefficient, quantum_exponent)


def _quantum_ratio(value: Decimal, quantum: Decimal) -> tuple[int, int, int, int]:
    value_coefficient, value_exponent = _decimal_coefficient_and_exponent(
        value, field_name="money value"
    )
    quantum_coefficient, quantum_exponent = _decimal_coefficient_and_exponent(
        quantum, field_name="money_quantum"
    )
    exponent_difference = value_exponent - quantum_exponent
    if abs(exponent_difference) > MAX_ROUNDING_SCALE_DIFFERENCE:
        raise CostError("Decimal scale difference exceeds the rounding resource limit")
    if exponent_difference >= 0:
        _validate_scaled_coefficient(
            value_coefficient, exponent_difference, field_name="money value"
        )
        numerator = value_coefficient * (10**exponent_difference)
        denominator = quantum_coefficient
    else:
        numerator = value_coefficient
        _validate_scaled_coefficient(
            quantum_coefficient, -exponent_difference, field_name="money_quantum"
        )
        denominator = quantum_coefficient * (10 ** (-exponent_difference))
    return numerator, denominator, quantum_coefficient, quantum_exponent


def _round_ratio(numerator: int, denominator: int, rounding: RoundingMode) -> int:
    floor = numerator // denominator
    remainder = numerator - (floor * denominator)
    if rounding is RoundingMode.DOWN:
        return floor if numerator >= 0 or remainder == 0 else floor + 1
    comparison = (remainder * 2) - denominator
    if comparison < 0:
        return floor
    if comparison > 0:
        return floor + 1
    if rounding is RoundingMode.HALF_UP:
        return floor + 1 if numerator >= 0 else floor
    if rounding is RoundingMode.HALF_EVEN:
        return floor if floor % 2 == 0 else floor + 1
    raise AssertionError("unsupported rounding mode")


def _decimal_coefficient_and_exponent(value: Decimal, *, field_name: str) -> tuple[int, int]:
    decimal_tuple = value.as_tuple()
    if not isinstance(decimal_tuple.exponent, int):
        raise AssertionError("finite Decimal must have an integer exponent")
    _validate_rounding_decimal(value, field_name=field_name)
    coefficient = int("".join(str(digit) for digit in decimal_tuple.digits))
    if decimal_tuple.sign:
        coefficient = -coefficient
    return coefficient, decimal_tuple.exponent


def _validate_rounding_decimal(value: Decimal, *, field_name: str) -> None:
    decimal_tuple = value.as_tuple()
    if not isinstance(decimal_tuple.exponent, int):
        raise AssertionError("finite Decimal must have an integer exponent")
    if len(decimal_tuple.digits) > MAX_ROUNDING_DECIMAL_DIGITS:
        raise CostError(f"{field_name} digit count exceeds the rounding resource limit")
    if abs(decimal_tuple.exponent) > MAX_ROUNDING_DECIMAL_EXPONENT:
        raise CostError(f"{field_name} scale exceeds the rounding resource limit")


def _decimal_from_coefficient(coefficient: int, exponent: int) -> Decimal:
    if abs(exponent) > MAX_ROUNDING_DECIMAL_EXPONENT:
        raise CostError("Decimal result scale exceeds the rounding resource limit")
    if _coefficient_digit_count(coefficient) > MAX_ROUNDING_DECIMAL_DIGITS:
        raise CostError("Decimal result digit count exceeds the rounding resource limit")
    sign = 1 if coefficient < 0 else 0
    digits = tuple(int(digit) for digit in str(abs(coefficient)))
    return Decimal((sign, digits, exponent))


def _validate_scaled_coefficient(coefficient: int, scale: int, *, field_name: str) -> None:
    if scale > MAX_ROUNDING_SCALE_DIFFERENCE:
        raise CostError(f"{field_name} scale exceeds the rounding resource limit")
    if _coefficient_digit_count(coefficient) + scale > MAX_ROUNDING_DECIMAL_DIGITS:
        raise CostError(f"{field_name} digit count exceeds the rounding resource limit")


def _coefficient_digit_count(coefficient: int) -> int:
    return len(str(abs(coefficient)))
