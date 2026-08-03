"""Framework-free indicator contracts and deterministic output values."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from app.data_sources.models import MarketDataSnapshot
from app.domain.values import require_finite_decimal

DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


class IndicatorError(ValueError):
    """Base class for invalid indicator inputs, plugins, and outputs."""


class IndicatorInputError(IndicatorError):
    """An indicator received data that cannot support a deterministic metric."""


class IndicatorResultError(IndicatorError):
    """An indicator attempted to expose an invalid strategy-boundary result."""


def canonical_decimal(value: Decimal) -> Decimal:
    """Return a finite Decimal in a stable, non-exponent form without float coercion."""

    require_finite_decimal(value, field_name="indicator value")
    if value.is_zero():
        return Decimal("0")
    return Decimal(_exact_decimal_text(value))


def _exact_decimal_text(value: Decimal) -> str:
    """Encode finite nonzero Decimals without context-dependent normalization."""

    decimal_tuple = value.as_tuple()
    if not isinstance(decimal_tuple.exponent, int):
        raise AssertionError("finite Decimal must have an integer exponent")
    digits = "".join(str(digit) for digit in decimal_tuple.digits).rstrip("0")
    exponent = decimal_tuple.exponent + (len(decimal_tuple.digits) - len(digits))
    sign = "-" if decimal_tuple.sign else ""
    if exponent >= 0:
        return f"{sign}{digits}{'0' * exponent}"
    decimal_point = len(digits) + exponent
    if decimal_point > 0:
        return f"{sign}{digits[:decimal_point]}.{digits[decimal_point:]}"
    return f"{sign}0.{('0' * -decimal_point)}{digits}"


def divide_decimals(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Divide finite Decimals under the fixed indicator precision and canonicalize the result."""

    require_finite_decimal(numerator, field_name="numerator")
    require_finite_decimal(denominator, field_name="denominator")
    if denominator.is_zero():
        raise IndicatorInputError("denominator must not be zero")
    with localcontext(DECIMAL_CONTEXT):
        return canonical_decimal(numerator / denominator)


def canonical_timedelta_microseconds(total_microseconds: int) -> str:
    """Encode a duration held as integer microseconds for auditable evidence."""

    return f"{total_microseconds}us"


@dataclass(frozen=True, slots=True)
class IndicatorResult:
    """One Decimal-only metric or explicit unavailable state for downstream strategy logic."""

    key: str
    value: Decimal | None
    actionable: bool
    reason: str | None = None
    evidence: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise IndicatorResultError("indicator key must not be blank")
        if self.value is None:
            if self.actionable:
                raise IndicatorResultError(
                    "an actionable indicator result requires a Decimal value"
                )
            if self.reason is None or not self.reason.strip():
                raise IndicatorResultError("an unavailable indicator result requires a reason")
        else:
            require_finite_decimal(self.value, field_name="indicator value")
            if not self.actionable:
                raise IndicatorResultError("a Decimal indicator value must be actionable")
            if self.reason is not None:
                raise IndicatorResultError("an actionable indicator result cannot have a reason")

        normalized_evidence: dict[str, str] = {}
        for evidence_key, evidence_value in self.evidence.items():
            if not isinstance(evidence_key, str) or not evidence_key.strip():
                raise IndicatorResultError("indicator evidence keys must be non-blank strings")
            if not isinstance(evidence_value, str):
                raise IndicatorResultError("indicator evidence values must be strings")
            normalized_evidence[evidence_key] = evidence_value
        object.__setattr__(self, "key", self.key.strip())
        if self.value is not None:
            object.__setattr__(self, "value", canonical_decimal(self.value))
        if self.reason is not None:
            object.__setattr__(self, "reason", self.reason.strip())
        object.__setattr__(
            self, "evidence", MappingProxyType(dict(sorted(normalized_evidence.items())))
        )

    @classmethod
    def available(
        cls, *, key: str, value: Decimal, evidence: Mapping[str, str]
    ) -> "IndicatorResult":
        """Build an actionable Decimal metric."""

        return cls(key=key, value=value, actionable=True, evidence=evidence)

    @classmethod
    def unavailable(
        cls, *, key: str, reason: str, evidence: Mapping[str, str]
    ) -> "IndicatorResult":
        """Build a non-actionable result while retaining the explanation and evidence."""

        return cls(key=key, value=None, actionable=False, reason=reason, evidence=evidence)


@runtime_checkable
class Indicator(Protocol):
    """Pluggable indicator contract kept independent from strategy-core dispatch."""

    @property
    def key(self) -> str:
        """Return the unique, configuration-facing plugin key."""

    def calculate(self, market_snapshot: MarketDataSnapshot) -> IndicatorResult:
        """Calculate a Decimal-only metric or an explicit unavailable state."""
