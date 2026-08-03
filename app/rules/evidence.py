"""Immutable, typed audit evidence emitted by the safe rule evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from app.rules.schema import EvidenceKind, RuleEvaluationError, require_bounded_decimal


class EvidenceStatus(StrEnum):
    """The outcome for one Boolean rule."""

    MATCHED = "matched"
    FAILED = "failed"
    MISSING = "missing"


class RulesetStatus(StrEnum):
    """The safe aggregate outcome after considering matched and missing scores."""

    MATCHED = "matched"
    FAILED = "failed"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class EvidenceValue:
    """One canonical typed value, or a typed missing value, without coercion."""

    kind: EvidenceKind
    value: Decimal | bool | datetime | str | None
    is_missing: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EvidenceKind):
            raise TypeError("evidence value kind must be an EvidenceKind")
        if not isinstance(self.is_missing, bool):
            raise TypeError("is_missing must be a Boolean")
        if self.is_missing:
            if self.value is not None or self.kind is EvidenceKind.NULL:
                raise RuleEvaluationError("missing evidence requires a non-null type and no value")
            return
        _validate_value_type(self.kind, self.value)
        if self.kind is EvidenceKind.DECIMAL:
            assert isinstance(self.value, Decimal)
            object.__setattr__(self, "value", canonical_decimal(self.value))
        if self.kind is EvidenceKind.DATETIME:
            assert isinstance(self.value, datetime)
            object.__setattr__(self, "value", self.value.astimezone(UTC))

    @classmethod
    def missing(cls, kind: EvidenceKind) -> EvidenceValue:
        """Create a typed missing value for an absent registered evidence path."""

        return cls(kind=kind, value=None, is_missing=True)


@dataclass(frozen=True, slots=True)
class EvaluatedPath:
    """One field path read during evaluation and the exact typed value observed."""

    path: str
    value: EvidenceValue

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("evaluated path must be a non-blank string")


@dataclass(frozen=True, slots=True)
class RuleEvidence:
    """A per-rule audit record retaining all typed operands and resolved paths."""

    rule_id: str
    status: EvidenceStatus
    operands: tuple[EvidenceValue, ...]
    evaluated_paths: tuple[EvaluatedPath, ...]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise ValueError("rule evidence id must be a non-blank string")
        if not isinstance(self.status, EvidenceStatus):
            raise TypeError("rule evidence status must be an EvidenceStatus")
        _require_aware(self.evaluated_at, field_name="evaluated_at")
        object.__setattr__(self, "rule_id", self.rule_id.strip())
        object.__setattr__(self, "operands", tuple(self.operands))
        object.__setattr__(self, "evaluated_paths", tuple(self.evaluated_paths))


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    """The reproducible aggregate decision and every per-rule evidence record."""

    status: RulesetStatus
    total_score: Decimal
    threshold: Decimal
    matched_rule_ids: tuple[str, ...]
    failed_rule_ids: tuple[str, ...]
    missing_rule_ids: tuple[str, ...]
    rule_evidence: tuple[RuleEvidence, ...]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.status, RulesetStatus):
            raise TypeError("ruleset status must be a RulesetStatus")
        for value, label in ((self.total_score, "total_score"), (self.threshold, "threshold")):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise RuleEvaluationError(f"{label} must be a finite Decimal")
            if value < Decimal("0"):
                raise RuleEvaluationError(f"{label} must not be negative")
        _require_aware(self.evaluated_at, field_name="evaluated_at")
        object.__setattr__(self, "total_score", canonical_decimal(self.total_score))
        object.__setattr__(self, "threshold", canonical_decimal(self.threshold))
        object.__setattr__(self, "matched_rule_ids", tuple(self.matched_rule_ids))
        object.__setattr__(self, "failed_rule_ids", tuple(self.failed_rule_ids))
        object.__setattr__(self, "missing_rule_ids", tuple(self.missing_rule_ids))
        object.__setattr__(self, "rule_evidence", tuple(self.rule_evidence))


def canonical_decimal(value: Decimal) -> Decimal:
    """Return a finite Decimal in a reproducible non-exponent representation."""

    if not isinstance(value, Decimal) or not value.is_finite():
        raise RuleEvaluationError("Decimal evidence must be finite")
    require_bounded_decimal(value)
    if value.is_zero():
        return Decimal("0")
    decimal_tuple = value.as_tuple()
    if not isinstance(decimal_tuple.exponent, int):
        raise AssertionError("finite Decimal must have an integer exponent")
    digits = "".join(str(digit) for digit in decimal_tuple.digits).rstrip("0")
    exponent = decimal_tuple.exponent + (len(decimal_tuple.digits) - len(digits))
    sign = "-" if decimal_tuple.sign else ""
    if exponent >= 0:
        return Decimal(f"{sign}{digits}{'0' * exponent}")
    decimal_point = len(digits) + exponent
    if decimal_point > 0:
        return Decimal(f"{sign}{digits[:decimal_point]}.{digits[decimal_point:]}")
    return Decimal(f"{sign}0.{('0' * -decimal_point)}{digits}")


def _validate_value_type(kind: EvidenceKind, value: object) -> None:
    if kind is EvidenceKind.DECIMAL:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise RuleEvaluationError("Decimal evidence must be a finite Decimal")
        return
    if kind is EvidenceKind.BOOLEAN:
        if not isinstance(value, bool):
            raise RuleEvaluationError("Boolean evidence must be a Boolean")
        return
    if kind is EvidenceKind.STRING:
        if not isinstance(value, str):
            raise RuleEvaluationError("string evidence must be a string")
        return
    if kind is EvidenceKind.DATETIME:
        if not isinstance(value, datetime):
            raise RuleEvaluationError("datetime evidence must be a datetime")
        _require_aware(value, field_name="datetime evidence")
        return
    if kind is EvidenceKind.NULL and value is None:
        return
    raise RuleEvaluationError("null evidence must be None")


def _require_aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuleEvaluationError(f"{field_name} must be timezone-aware")
