"""Regression coverage for fixed rule-DSL resource and public-AST boundaries."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.rules.engine import RuleInput, evaluate_ruleset
from app.rules.evidence import EvidenceValue
from app.rules.parser import parse_expression, parse_rules
from app.rules.schema import (
    DecimalResourceLimitError,
    EvidenceKind,
    OperationExpression,
    Rule,
    RuleSafetyError,
    RuleSemanticError,
)


def test_raw_transport_preflight_rejects_extreme_nesting_without_recursion() -> None:
    expression: object = True
    for _ in range(2_000):
        expression = {"not": [expression]}

    with pytest.raises(RuleSafetyError, match="depth"):
        parse_expression(expression)


def test_raw_preflight_bounds_malformed_multikey_nesting_before_schema_validation() -> None:
    expression: object = True
    for _ in range(2_000):
        expression = {"not": [expression]}
    malformed = {"not": [expression], "extra": True}

    with pytest.raises(RuleSafetyError, match="depth"):
        parse_expression(malformed)


def test_raw_preflight_preserves_full_valid_semantic_ast_capacity() -> None:
    expression = _binary_and_tree([{"var": "market.is_actionable"}] * 64)
    rule = Rule(
        rule_id="capacity",
        expression=parse_expression(expression),
        weight=Decimal("1"),
    )

    result = evaluate_ruleset(
        (rule,),
        rule_input=RuleInput(values={"market.is_actionable": True}),
        threshold=Decimal("1"),
        evaluated_at=datetime(2035, 1, 1, tzinfo=UTC),
    )

    assert result.matched_rule_ids == ("capacity",)


def test_parse_rules_rejects_aggregate_ruleset_limit_before_rule_parsing() -> None:
    raw_rules = [{"id": f"rule_{index}", "expression": True} for index in range(513)]

    with pytest.raises(RuleSafetyError, match="rule count"):
        parse_rules(raw_rules)


def test_evidence_rejects_unbounded_decimal_before_canonicalization() -> None:
    with pytest.raises(DecimalResourceLimitError):
        EvidenceValue(kind=EvidenceKind.DECIMAL, value=Decimal("1E+1000000000"))


def test_evaluator_rejects_handcrafted_invalid_public_ast_without_index_error() -> None:
    malformed_rule = Rule(
        rule_id="malformed",
        expression=OperationExpression(
            operator="not",
            operands=(),
            kind=EvidenceKind.BOOLEAN,
        ),
        weight=Decimal("1"),
    )

    with pytest.raises(RuleSemanticError, match="exactly 1 operands"):
        evaluate_ruleset(
            (malformed_rule,),
            rule_input=RuleInput(values={}),
            threshold=Decimal("1"),
            evaluated_at=datetime(2035, 1, 1, tzinfo=UTC),
        )


def _binary_and_tree(leaves: list[dict[str, str]]) -> object:
    current: list[object] = list(leaves)
    while len(current) > 1:
        paired: list[object] = []
        for index in range(0, len(current), 2):
            if index + 1 == len(current):
                paired.append(current[index])
            else:
                paired.append({"and": [current[index], current[index + 1]]})
        current = paired
    return current[0]
