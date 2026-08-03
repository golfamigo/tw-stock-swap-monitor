"""Deterministic evaluation of parsed rules against flat registered evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from app.rules.evidence import (
    EvaluatedPath,
    EvidenceStatus,
    EvidenceValue,
    RuleEvaluation,
    RuleEvidence,
    RulesetStatus,
    canonical_decimal,
)
from app.rules.schema import (
    EvidenceKind,
    Expression,
    LiteralExpression,
    M0M1EvidenceRegistry,
    OperationExpression,
    PathExpression,
    ProtectedRuleInputError,
    Rule,
    RuleEvaluationError,
)

MAX_EVALUATION_STEPS = 512
_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


class EvaluationPurpose(StrEnum):
    """The only high-level uses permitted for one supplied source evidence set."""

    ANALYSIS = "analysis"
    SELL = "sell"
    ACTION = "action"


@dataclass(frozen=True, slots=True)
class RuleInput:
    """Flat evidence and a generic protected-source guard for safe rule evaluation."""

    values: Mapping[str, object]
    protected_source: bool = False
    purpose: EvaluationPurpose = EvaluationPurpose.ANALYSIS

    def __post_init__(self) -> None:
        if not isinstance(self.values, Mapping):
            raise TypeError("rule input values must be a mapping")
        if not isinstance(self.protected_source, bool):
            raise TypeError("protected_source must be a Boolean")
        if not isinstance(self.purpose, EvaluationPurpose):
            raise TypeError("purpose must be an EvaluationPurpose")
        if self.protected_source:
            raise ProtectedRuleInputError("a protected source cannot be used for rule evaluation")
        object.__setattr__(self, "values", M0M1EvidenceRegistry.controlled_values(self.values))


@dataclass(slots=True)
class _EvaluationBudget:
    """A monotonic whole-ruleset execution budget independent of AST validation."""

    remaining: int = MAX_EVALUATION_STEPS

    def consume(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise RuleEvaluationError("rule evaluation budget exhausted")


@dataclass(slots=True)
class _Trace:
    """Mutable execution-local trace converted into frozen audit evidence at the boundary."""

    operands: list[EvidenceValue]
    evaluated_paths: list[EvaluatedPath]


def evaluate_ruleset(
    rules: Sequence[Rule],
    *,
    rule_input: RuleInput,
    threshold: Decimal,
    evaluated_at: datetime,
) -> RuleEvaluation:
    """Evaluate parsed rules without mutation, coercion, traversal, or dynamic execution."""

    if not isinstance(rule_input, RuleInput):
        raise TypeError("rule_input must be a RuleInput")
    _require_aware(evaluated_at, field_name="evaluated_at")
    normalized_threshold = _require_score(threshold, label="threshold")
    parsed_rules = tuple(rules)
    if any(not isinstance(rule, Rule) for rule in parsed_rules):
        raise TypeError("rules must contain parsed Rule values")
    rule_ids = tuple(rule.rule_id for rule in parsed_rules)
    if len(set(rule_ids)) != len(rule_ids):
        raise RuleEvaluationError("rules must have unique ids")
    values = _prepare_values(rule_input.values)
    budget = _EvaluationBudget()
    matched_rule_ids: list[str] = []
    failed_rule_ids: list[str] = []
    missing_rule_ids: list[str] = []
    records: list[RuleEvidence] = []
    total_score = Decimal("0")
    missing_score = Decimal("0")

    for rule in parsed_rules:
        trace = _Trace(operands=[], evaluated_paths=[])
        outcome = _evaluate_expression(
            rule.expression,
            values=values,
            budget=budget,
            trace=trace,
        )
        if outcome.is_missing:
            rule_status = EvidenceStatus.MISSING
            missing_rule_ids.append(rule.rule_id)
            missing_score = _add_scores(missing_score, rule.weight)
        elif outcome.kind is not EvidenceKind.BOOLEAN:
            raise RuleEvaluationError("parsed rule did not produce a Boolean outcome")
        elif outcome.value:
            rule_status = EvidenceStatus.MATCHED
            matched_rule_ids.append(rule.rule_id)
            total_score = _add_scores(total_score, rule.weight)
        else:
            rule_status = EvidenceStatus.FAILED
            failed_rule_ids.append(rule.rule_id)
        records.append(
            RuleEvidence(
                rule_id=rule.rule_id,
                status=rule_status,
                operands=tuple(trace.operands),
                evaluated_paths=tuple(trace.evaluated_paths),
                evaluated_at=evaluated_at,
            )
        )

    potential_score = _add_scores(total_score, missing_score)
    if total_score >= normalized_threshold:
        ruleset_status = RulesetStatus.MATCHED
    elif potential_score >= normalized_threshold:
        ruleset_status = RulesetStatus.INSUFFICIENT
    else:
        ruleset_status = RulesetStatus.FAILED
    return RuleEvaluation(
        status=ruleset_status,
        total_score=total_score,
        threshold=normalized_threshold,
        matched_rule_ids=tuple(matched_rule_ids),
        failed_rule_ids=tuple(failed_rule_ids),
        missing_rule_ids=tuple(missing_rule_ids),
        rule_evidence=tuple(records),
        evaluated_at=evaluated_at,
    )


def _prepare_values(values: Mapping[str, object]) -> Mapping[str, EvidenceValue]:
    evidence_schema = M0M1EvidenceRegistry.schema()
    normalized: dict[str, EvidenceValue] = {}
    for path, raw_value in values.items():
        try:
            kind = evidence_schema.kind_for(path)
        except Exception as error:
            raise RuleEvaluationError(str(error)) from error
        normalized[path] = EvidenceValue(
            kind=kind,
            value=cast(Decimal | bool | datetime | str | None, raw_value),
        )
    return MappingProxyType(dict(sorted(normalized.items())))


def _evaluate_expression(
    expression: Expression,
    *,
    values: Mapping[str, EvidenceValue],
    budget: _EvaluationBudget,
    trace: _Trace,
) -> EvidenceValue:
    budget.consume()
    if isinstance(expression, LiteralExpression):
        result = EvidenceValue(kind=expression.kind, value=expression.value)
    elif isinstance(expression, PathExpression):
        result = values.get(expression.path, EvidenceValue.missing(expression.kind))
        trace.evaluated_paths.append(EvaluatedPath(path=expression.path, value=result))
    elif isinstance(expression, OperationExpression):
        result = _evaluate_operation(expression, values=values, budget=budget, trace=trace)
    else:
        raise RuleEvaluationError("expression is not a supported parsed AST node")
    trace.operands.append(result)
    return result


def _evaluate_operation(
    expression: OperationExpression,
    *,
    values: Mapping[str, EvidenceValue],
    budget: _EvaluationBudget,
    trace: _Trace,
) -> EvidenceValue:
    if expression.operator == "if":
        condition = _evaluate_expression(
            expression.operands[0], values=values, budget=budget, trace=trace
        )
        if condition.is_missing:
            return EvidenceValue.missing(expression.kind)
        if condition.kind is not EvidenceKind.BOOLEAN or not isinstance(condition.value, bool):
            raise RuleEvaluationError("if condition did not evaluate to Boolean evidence")
        branch = expression.operands[1] if condition.value else expression.operands[2]
        return _evaluate_expression(branch, values=values, budget=budget, trace=trace)

    operands = tuple(
        _evaluate_expression(operand, values=values, budget=budget, trace=trace)
        for operand in expression.operands
    )
    if expression.operator == "exists":
        return EvidenceValue(kind=EvidenceKind.BOOLEAN, value=not operands[0].is_missing)
    if any(operand.is_missing for operand in operands):
        return EvidenceValue.missing(expression.kind)
    if expression.operator == "and":
        return EvidenceValue(
            kind=EvidenceKind.BOOLEAN,
            value=all(_boolean_value(operand) for operand in operands),
        )
    if expression.operator == "or":
        return EvidenceValue(
            kind=EvidenceKind.BOOLEAN,
            value=any(_boolean_value(operand) for operand in operands),
        )
    if expression.operator == "not":
        return EvidenceValue(kind=EvidenceKind.BOOLEAN, value=not _boolean_value(operands[0]))
    if expression.operator == "eq":
        return EvidenceValue(
            kind=EvidenceKind.BOOLEAN,
            value=operands[0].value == operands[1].value,
        )
    if expression.operator == "ne":
        return EvidenceValue(
            kind=EvidenceKind.BOOLEAN,
            value=operands[0].value != operands[1].value,
        )
    if expression.operator == "lt":
        return EvidenceValue(kind=EvidenceKind.BOOLEAN, value=_compare("lt", operands))
    if expression.operator == "lte":
        return EvidenceValue(kind=EvidenceKind.BOOLEAN, value=_compare("lte", operands))
    if expression.operator == "gt":
        return EvidenceValue(kind=EvidenceKind.BOOLEAN, value=_compare("gt", operands))
    if expression.operator == "gte":
        return EvidenceValue(kind=EvidenceKind.BOOLEAN, value=_compare("gte", operands))
    if expression.operator == "in":
        left = _string_value(operands[0])
        right = _string_value(operands[1])
        return EvidenceValue(kind=EvidenceKind.BOOLEAN, value=left in right)
    if expression.operator in {"add", "subtract", "multiply", "divide"}:
        return EvidenceValue(
            kind=EvidenceKind.DECIMAL,
            value=_evaluate_decimal_operation(expression.operator, operands),
        )
    raise RuleEvaluationError("parsed expression has an unsupported operator")


def _evaluate_decimal_operation(operator: str, operands: Sequence[EvidenceValue]) -> Decimal:
    values = tuple(_decimal_value(operand) for operand in operands)
    with localcontext(_DECIMAL_CONTEXT):
        if operator == "add":
            return canonical_decimal(sum(values, Decimal("0")))
        if operator == "subtract":
            return canonical_decimal(values[0] - values[1])
        if operator == "multiply":
            result = Decimal("1")
            for value in values:
                result *= value
            return canonical_decimal(result)
        if operator == "divide":
            if values[1].is_zero():
                raise RuleEvaluationError("divide denominator is zero at evaluation time")
            return canonical_decimal(values[0] / values[1])
    raise AssertionError("only Decimal operators are dispatched here")


def _compare(operator: str, operands: Sequence[EvidenceValue]) -> bool:
    left, right = operands
    if left.kind is EvidenceKind.DECIMAL and right.kind is EvidenceKind.DECIMAL:
        return _compare_decimals(operator, _decimal_value(left), _decimal_value(right))
    if left.kind is EvidenceKind.DATETIME and right.kind is EvidenceKind.DATETIME:
        return _compare_datetimes(operator, _datetime_value(left), _datetime_value(right))
    raise RuleEvaluationError("comparison operands must share a Decimal or datetime type")


def _compare_decimals(operator: str, left: Decimal, right: Decimal) -> bool:
    if operator == "lt":
        return left < right
    if operator == "lte":
        return left <= right
    if operator == "gt":
        return left > right
    if operator == "gte":
        return left >= right
    raise AssertionError("only comparison operators are dispatched here")


def _compare_datetimes(operator: str, left: datetime, right: datetime) -> bool:
    if operator == "lt":
        return left < right
    if operator == "lte":
        return left <= right
    if operator == "gt":
        return left > right
    if operator == "gte":
        return left >= right
    raise AssertionError("only comparison operators are dispatched here")


def _boolean_value(value: EvidenceValue) -> bool:
    if value.kind is not EvidenceKind.BOOLEAN or not isinstance(value.value, bool):
        raise RuleEvaluationError("Boolean operator received non-Boolean evidence")
    return value.value


def _decimal_value(value: EvidenceValue) -> Decimal:
    if value.kind is not EvidenceKind.DECIMAL or not isinstance(value.value, Decimal):
        raise RuleEvaluationError("Decimal operator received non-Decimal evidence")
    return value.value


def _datetime_value(value: EvidenceValue) -> datetime:
    if value.kind is not EvidenceKind.DATETIME or not isinstance(value.value, datetime):
        raise RuleEvaluationError("datetime operator received non-datetime evidence")
    return value.value


def _string_value(value: EvidenceValue) -> str:
    if value.kind is not EvidenceKind.STRING or not isinstance(value.value, str):
        raise RuleEvaluationError("string operator received non-string evidence")
    return value.value


def _require_score(value: Decimal, *, label: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise RuleEvaluationError(f"{label} must be a finite Decimal")
    if value < Decimal("0"):
        raise RuleEvaluationError(f"{label} must not be negative")
    return canonical_decimal(value)


def _add_scores(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_DECIMAL_CONTEXT):
        return canonical_decimal(left + right)


def _require_aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuleEvaluationError(f"{field_name} must be timezone-aware")
