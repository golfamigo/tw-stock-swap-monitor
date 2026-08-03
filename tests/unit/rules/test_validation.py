"""Validation coverage for the restricted JSON-Logic rule DSL."""

from decimal import Decimal

import pytest
from app.rules.engine import EvaluationPurpose, RuleInput
from app.rules.parser import parse_expression, parse_rule, parse_rules
from app.rules.schema import (
    ProtectedRuleInputError,
    RuleSafetyError,
    RuleSemanticError,
    RuleTransportError,
)


@pytest.mark.parametrize(
    ("expression", "error_type"),
    [
        ({"unknown": [True]}, RuleTransportError),
        ({"eq": [True, True], "ne": [True, False]}, RuleTransportError),
        ({"not": [True, False]}, RuleSemanticError),
        ({"var": "source.__class__"}, RuleSemanticError),
        ({"var": "source.unknown"}, RuleSemanticError),
        ({"add": [{"var": "market.provider"}, 1]}, RuleSemanticError),
        ({"lt": [{"var": "market.is_actionable"}, True]}, RuleSemanticError),
        ({"if": [{"var": "source.last_price"}, True, False]}, RuleSemanticError),
        ({"divide": [1, 0]}, RuleSemanticError),
        (lambda: True, RuleTransportError),
    ],
)
def test_parse_expression_rejects_unsafe_transport_and_semantic_inputs(
    expression: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        parse_expression(expression)


def test_parse_expression_rejects_overdepth_and_oversize_ast() -> None:
    overdeep: object = True
    for _ in range(16):
        overdeep = {"not": [overdeep]}

    oversized = _binary_and_tree([True] * 65)

    with pytest.raises(RuleSafetyError, match="depth"):
        parse_expression(overdeep)
    with pytest.raises(RuleSafetyError, match="node"):
        parse_expression(oversized)


def test_parse_rules_rejects_duplicate_ids_and_non_decimal_scores() -> None:
    duplicate_rules = (
        {"id": "one", "expression": True},
        {"id": "one", "expression": False},
    )
    with pytest.raises(RuleSemanticError, match="unique"):
        parse_rules(duplicate_rules)
    with pytest.raises(RuleTransportError, match="weight"):
        parse_rule({"id": "weighted", "weight": "not-a-number", "expression": True})


@pytest.mark.parametrize("purpose", [EvaluationPurpose.SELL, EvaluationPurpose.ACTION])
def test_protected_source_is_rejected_before_sell_or_action_rule_evaluation(
    purpose: EvaluationPurpose,
) -> None:
    with pytest.raises(ProtectedRuleInputError):
        RuleInput(
            values={"source.last_price": Decimal("1")},
            protected_source=True,
            purpose=purpose,
        )


def _binary_and_tree(leaves: list[bool]) -> object:
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
