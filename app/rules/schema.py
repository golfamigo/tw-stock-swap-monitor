"""Safe, framework-free types for the restricted rule expression language."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

MAX_EXPRESSION_DEPTH = 16
MAX_AST_NODES = 128

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


class EvidenceKind(StrEnum):
    """The only runtime value kinds a registered evidence field may expose."""

    BOOLEAN = "boolean"
    DECIMAL = "decimal"
    DATETIME = "datetime"
    STRING = "string"
    NULL = "null"


@dataclass(frozen=True, slots=True)
class EvidenceSchema:
    """An immutable allow-list of exact flat evidence paths and their value kinds."""

    fields: Mapping[str, EvidenceKind]

    def __post_init__(self) -> None:
        if not isinstance(self.fields, Mapping):
            raise TypeError("evidence schema fields must be a mapping")
        normalized: dict[str, EvidenceKind] = {}
        for path, kind in self.fields.items():
            _validate_registered_path(path)
            if not isinstance(kind, EvidenceKind) or kind is EvidenceKind.NULL:
                raise TypeError("evidence schema field kinds must be non-null EvidenceKind values")
            normalized[path] = kind
        object.__setattr__(self, "fields", MappingProxyType(dict(sorted(normalized.items()))))

    def kind_for(self, path: str) -> EvidenceKind:
        """Resolve one explicitly registered path without walking object attributes."""

        if not isinstance(path, str):
            raise RuleSemanticError("evidence path must be a string")
        try:
            return self.fields[path]
        except KeyError as error:
            raise RuleSemanticError(f"evidence path is not registered: {path}") from error


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
