"""Safe, framework-free types for the restricted rule expression language."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

MAX_EXPRESSION_DEPTH = 16
MAX_AST_NODES = 128
MAX_RULESET_RULES = 512
MAX_RULESET_AST_NODES = 512
MAX_EVALUATION_STEPS = 512
MAX_DECIMAL_COEFFICIENT_DIGITS = 256
MAX_DECIMAL_ABSOLUTE_EXPONENT = 256
MAX_DECIMAL_SERIALIZED_CHARACTERS = 512

_PATH_PATTERN = re.compile(
    r"^(source|candidate|market|run)\.[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"
)


class RuleError(ValueError):
    """Base class for expected rule transport, semantic, and evaluation failures."""


class RuleTransportError(RuleError):
    """A rule did not match the JSON transport shape accepted by the DSL."""


class RuleSemanticError(RuleError):
    """A well-shaped expression violated a typed DSL rule."""


class RuleSafetyError(RuleSemanticError):
    """A fixed expression-depth or AST-size safety limit was exceeded."""


class RuleEvaluationError(RuleError):
    """Validated rules could not be evaluated against supplied evidence."""


class ProtectedRuleInputError(RuleEvaluationError):
    """A protected source was offered for a sell or action evaluation."""


class DecimalResourceLimitError(RuleEvaluationError):
    """A Decimal could exceed the fixed evidence and canonicalization resource limits."""


class EvidenceKind(StrEnum):
    """The only runtime value kinds a registered evidence field may expose."""

    BOOLEAN = "boolean"
    DECIMAL = "decimal"
    DATETIME = "datetime"
    STRING = "string"
    NULL = "null"


_M0_M1_FIELD_KINDS: dict[str, EvidenceKind] = {
    # Quote.price and Quote.as_of.
    "source.last_price": EvidenceKind.DECIMAL,
    "source.last_price_as_of": EvidenceKind.DATETIME,
    # IndicatorResult values published by the M0/M1 indicator plugins.
    "source.same_time_volume_ratio": EvidenceKind.DECIMAL,
    "source.session_vwap": EvidenceKind.DECIMAL,
    # MarketDataSnapshot and DataQuality fields.
    "market.data_quality.bars_complete": EvidenceKind.BOOLEAN,
    "market.data_quality.confidence": EvidenceKind.DECIMAL,
    "market.data_quality.source_timestamp": EvidenceKind.DATETIME,
    "market.data_quality.stale": EvidenceKind.BOOLEAN,
    "market.is_actionable": EvidenceKind.BOOLEAN,
    "market.provider": EvidenceKind.STRING,
    "market.snapshot_id": EvidenceKind.STRING,
}


@dataclass(frozen=True, slots=True)
class EvidenceSchema:
    """The immutable M0/M1 allow-list of exact market and indicator evidence paths."""

    fields: Mapping[str, EvidenceKind] = field(default_factory=lambda: _M0_M1_FIELD_KINDS)

    def __post_init__(self) -> None:
        if not isinstance(self.fields, Mapping):
            raise TypeError("evidence schema fields must be a mapping")
        normalized: dict[str, EvidenceKind] = {}
        for path, kind in self.fields.items():
            _validate_registered_path(path)
            if not isinstance(kind, EvidenceKind) or kind is EvidenceKind.NULL:
                raise TypeError("evidence schema field kinds must be non-null EvidenceKind values")
            normalized[path] = kind
        if normalized != _M0_M1_FIELD_KINDS:
            raise RuleSemanticError("evidence schema is fixed to the M0/M1 registry")
        object.__setattr__(self, "fields", MappingProxyType(dict(sorted(normalized.items()))))

    def kind_for(self, path: str) -> EvidenceKind:
        """Resolve one explicitly registered path without walking object attributes."""

        if not isinstance(path, str):
            raise RuleSemanticError("evidence path must be a string")
        try:
            return self.fields[path]
        except KeyError as error:
            raise RuleSemanticError(f"evidence path is not registered: {path}") from error


class M0M1EvidenceRegistry:
    """The only approved field registry and controlled-evidence factory for M0/M1 rules."""

    @classmethod
    def schema(cls) -> EvidenceSchema:
        """Return the complete immutable M0/M1 evidence schema."""

        return EvidenceSchema()

    @classmethod
    def controlled_values(cls, values: Mapping[str, object]) -> Mapping[str, object]:
        """Validate exact paths before constructing test or application rule evidence."""

        if not isinstance(values, Mapping):
            raise TypeError("registered rule values must be a mapping")
        schema = cls.schema()
        normalized: dict[str, object] = {}
        for path, value in values.items():
            schema.kind_for(path)
            normalized[path] = value
        return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True, slots=True)
class LiteralExpression:
    """A scalar JSON literal normalized at the transport boundary."""

    kind: EvidenceKind
    value: Decimal | bool | datetime | str | None


@dataclass(frozen=True, slots=True)
class PathExpression:
    """A validated reference to one exact field in an evidence schema."""

    path: str
    kind: EvidenceKind


@dataclass(frozen=True, slots=True)
class OperationExpression:
    """A semantically validated operator with typed child expressions."""

    operator: str
    operands: tuple[Expression, ...]
    kind: EvidenceKind


type Expression = LiteralExpression | PathExpression | OperationExpression


@dataclass(frozen=True, slots=True)
class Rule:
    """One parsed, scored Boolean rule ready for deterministic evaluation."""

    rule_id: str
    expression: Expression
    weight: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise RuleSemanticError("rule id must be a non-blank string")
        if not isinstance(self.weight, Decimal) or not self.weight.is_finite():
            raise RuleSemanticError("rule weight must be a finite Decimal")
        require_bounded_decimal(self.weight)
        if self.weight <= Decimal("0"):
            raise RuleSemanticError("rule weight must be positive")
        if self.expression.kind is not EvidenceKind.BOOLEAN:
            raise RuleSemanticError("rule expression must evaluate to a Boolean")
        object.__setattr__(self, "rule_id", self.rule_id.strip())


def _validate_registered_path(path: object) -> None:
    if not isinstance(path, str) or _PATH_PATTERN.fullmatch(path) is None:
        raise RuleSemanticError("evidence paths must use an allowed root and identifier segments")
    if any(segment.startswith("__") for segment in path.split(".")):
        raise RuleSemanticError("dunder-style evidence paths are not permitted")


def require_bounded_decimal(value: Decimal) -> Decimal:
    """Reject finite Decimals that could allocate unbounded canonical evidence text."""

    if not isinstance(value, Decimal) or not value.is_finite():
        raise DecimalResourceLimitError("Decimal values must be finite")
    decimal_tuple = value.as_tuple()
    if not isinstance(decimal_tuple.exponent, int):
        raise DecimalResourceLimitError("finite Decimal values require an integer exponent")
    coefficient_digits = len(decimal_tuple.digits)
    if coefficient_digits > MAX_DECIMAL_COEFFICIENT_DIGITS:
        raise DecimalResourceLimitError("Decimal coefficient exceeds the fixed digit limit")
    if abs(decimal_tuple.exponent) > MAX_DECIMAL_ABSOLUTE_EXPONENT:
        raise DecimalResourceLimitError("Decimal exponent exceeds the fixed magnitude limit")
    serialized_characters = _decimal_serialized_characters(
        coefficient_digits=coefficient_digits,
        exponent=decimal_tuple.exponent,
        negative=bool(decimal_tuple.sign),
    )
    if serialized_characters > MAX_DECIMAL_SERIALIZED_CHARACTERS:
        raise DecimalResourceLimitError("Decimal canonical form exceeds the fixed size limit")
    return value


def _decimal_serialized_characters(
    *, coefficient_digits: int, exponent: int, negative: bool
) -> int:
    sign_characters = 1 if negative else 0
    if exponent >= 0:
        return sign_characters + coefficient_digits + exponent
    decimal_point = coefficient_digits + exponent
    if decimal_point > 0:
        return sign_characters + coefficient_digits + 1
    return sign_characters + 2 + (-decimal_point) + coefficient_digits
