"""Validation coverage for the restricted JSON-Logic rule DSL."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.rules.engine import EvaluationPurpose, RuleInput, evaluate_ruleset
from app.rules.parser import parse_expression, parse_rule, parse_rules
from app.rules.schema import (
    EvidenceKind,
    EvidenceSchema,
    ProtectedRuleInputError,
    RuleSafetyError,
    RuleSemanticError,
    RuleTransportError,
)


@pytest.fixture
def evidence_schema() -> EvidenceSchema:
    return EvidenceSchema(
        {
            "source.enabled": EvidenceKind.BOOLEAN,
            "source.last_price": EvidenceKind.DECIMAL,
            "source.observed_at": EvidenceKind.DATETIME,
            "candidate.label": EvidenceKind.STRING,
            "candidate.observed_at": EvidenceKind.DATETIME,
            "market.denominator": EvidenceKind.DECIMAL,
            "run.ready": EvidenceKind.BOOLEAN,
        }
    )


@pytest.mark.parametrize(
    ("expression", "error_type"),
    [
        ({"unknown": [True]}, RuleTransportError),
        ({"eq": [True, True], "ne": [True, False]}, RuleTransportError),
        ({"not": [True, False]}, RuleSemanticError),
        ({"var": "source.__class__"}, RuleSemanticError),
        ({"var": "source.unknown"}, RuleSemanticError),
        ({"add": [{"var": "candidate.label"}, 1]}, RuleSemanticError),
        ({"lt": [{"var": "source.enabled"}, True]}, RuleSemanticError),
        ({"if": [{"var": "source.last_price"}, True, False]}, RuleSemanticError),
        ({"divide": [1, 0]}, RuleSemanticError),
        (lambda: True, RuleTransportError),
    ],
)
def test_parse_expression_rejects_unsafe_transport_and_semantic_inputs(
    evidence_schema: EvidenceSchema,
    expression: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        parse_expression(expression, evidence_schema=evidence_schema)


def test_parse_expression_rejects_overdepth_and_oversize_ast(
    evidence_schema: EvidenceSchema,
) -> None:
    overdeep: object = True
    for _ in range(16):
        overdeep = {"not": [overdeep]}

    oversized = _binary_and_tree([True] * 65)

    with pytest.raises(RuleSafetyError, match="depth"):
        parse_expression(overdeep, evidence_schema=evidence_schema)
    with pytest.raises(RuleSafetyError, match="node"):
        parse_expression(oversized, evidence_schema=evidence_schema)


def test_parse_rules_rejects_duplicate_ids_and_non_decimal_scores(
    evidence_schema: EvidenceSchema,
) -> None:
    duplicate_rules = (
        {"id": "one", "expression": True},
        {"id": "one", "expression": False},
    )
    with pytest.raises(RuleSemanticError, match="unique"):
        parse_rules(duplicate_rules, evidence_schema=evidence_schema)
    with pytest.raises(RuleTransportError, match="weight"):
        parse_rule(
            {"id": "weighted", "weight": "not-a-number", "expression": True},
            evidence_schema=evidence_schema,
        )


@pytest.mark.parametrize("purpose", [EvaluationPurpose.SELL, EvaluationPurpose.ACTION])
def test_protected_source_is_rejected_before_sell_or_action_rule_evaluation(
    evidence_schema: EvidenceSchema, purpose: EvaluationPurpose
) -> None:
    rule = parse_rule(
        {"id": "source_check", "expression": {"var": "source.enabled"}},
        evidence_schema=evidence_schema,
    )
    rule_input = RuleInput(
        values={"source.enabled": True},
        protected_source=True,
        purpose=purpose,
    )

    with pytest.raises(ProtectedRuleInputError):
        evaluate_ruleset(
            (rule,),
            evidence_schema=evidence_schema,
            rule_input=rule_input,
            threshold=Decimal("1"),
            evaluated_at=datetime(2035, 1, 1, tzinfo=UTC),
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
