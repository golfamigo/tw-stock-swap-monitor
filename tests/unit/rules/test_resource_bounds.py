"""Regression coverage for fixed rule-DSL resource and public-AST boundaries."""

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.rules.engine import RuleInput, evaluate_ruleset
from app.rules.evidence import (
    EvidenceStatus,
    EvidenceValue,
    RuleEvaluation,
    RuleEvidence,
    RulesetStatus,
)
from app.rules.parser import parse_expression, parse_rules
from app.rules.schema import (
    MAX_RAW_TRANSPORT_NODES,
    DecimalResourceLimitError,
    EvidenceKind,
    LiteralExpression,
    OperationExpression,
    Rule,
    RuleEvidenceResourceLimitError,
    RuleSafetyError,
    RuleSemanticError,
    RuleTextResourceLimitError,
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


def test_rule_transport_enforces_utf8_literal_expression_ruleset_and_identifier_limits() -> None:
    exact_literal = "a" * (16 * 1024)
    parsed_literal = parse_expression(exact_literal)
    assert isinstance(parsed_literal, LiteralExpression)
    assert parsed_literal.value == exact_literal
    with pytest.raises(RuleTextResourceLimitError, match="16 KiB"):
        parse_expression("a" * (16 * 1024 + 1))

    exact_rule_id = "r" * 256
    assert parse_rules(({"id": exact_rule_id, "expression": True},))[0].rule_id == exact_rule_id
    with pytest.raises(RuleTextResourceLimitError, match="256"):
        parse_rules(({"id": "r" * 257, "expression": True},))

    expression = {
        "or": [
            {
                "eq": [
                    {"var": "market.provider"},
                    "a" * (16 * 1024),
                ]
            }
            for _ in range(9)
        ]
    }
    with pytest.raises(RuleTextResourceLimitError, match="expression"):
        parse_expression(expression)
    with pytest.raises(RuleTextResourceLimitError, match="ruleset"):
        parse_rules([{"id": f"rule_{index}", "expression": expression} for index in range(9)])


def test_raw_transport_counts_list_depth_before_schema_validation() -> None:
    expression: object = True
    for _ in range(33):
        expression = [expression]

    with pytest.raises(RuleSafetyError, match="depth"):
        parse_expression(expression)


def test_runtime_evidence_enforces_string_identifier_and_complete_audit_limits() -> None:
    exact_string = "a" * (16 * 1024)
    assert EvidenceValue(kind=EvidenceKind.STRING, value=exact_string).value == exact_string
    with pytest.raises(RuleEvidenceResourceLimitError, match="16 KiB"):
        EvidenceValue(kind=EvidenceKind.STRING, value="a" * (16 * 1024 + 1))
    with pytest.raises(RuleEvidenceResourceLimitError, match="256"):
        RuleEvidence(
            rule_id="r" * 257,
            status=EvidenceStatus.MATCHED,
            operands=(),
            evaluated_paths=(),
            evaluated_at=datetime(2035, 1, 1, tzinfo=UTC),
        )
    with pytest.raises(RuleEvidenceResourceLimitError, match="RuleInput"):
        RuleInput(values={"market.provider": "p" * 257})

    with pytest.raises(RuleSemanticError, match="not registered"):
        RuleInput(values={"p" * 256: "value"})
    with pytest.raises(RuleEvidenceResourceLimitError, match="RuleInput evidence path"):
        RuleInput(values={"p" * 257: "value"})

    records = tuple(
        RuleEvidence(
            rule_id=f"rule_{index}",
            status=EvidenceStatus.MATCHED,
            operands=(EvidenceValue(kind=EvidenceKind.STRING, value=exact_string),),
            evaluated_paths=(),
            evaluated_at=datetime(2035, 1, 1, tzinfo=UTC),
        )
        for index in range(9)
    )
    with pytest.raises(RuleEvidenceResourceLimitError, match="RuleEvaluation"):
        RuleEvaluation(
            status=RulesetStatus.MATCHED,
            total_score=Decimal("1"),
            threshold=Decimal("1"),
            matched_rule_ids=tuple(record.rule_id for record in records),
            failed_rule_ids=(),
            missing_rule_ids=(),
            rule_evidence=records,
            evaluated_at=datetime(2035, 1, 1, tzinfo=UTC),
        )


def test_raw_transport_preflight_rejects_wide_containers_before_iteration() -> None:
    with pytest.raises(RuleSafetyError, match="node count"):
        parse_expression(_WideList())
    with pytest.raises(RuleSafetyError, match="node count"):
        parse_expression(_WideMapping())


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


class _WideList(list[object]):
    """A container that fails if preflight touches children before its width check."""

    def __len__(self) -> int:
        return MAX_RAW_TRANSPORT_NODES

    def __reversed__(self) -> Iterator[object]:
        raise AssertionError("wide list children must not be enqueued")


class _WideMapping(Mapping[str, object]):
    """A mapping that fails if preflight materializes entries before its width check."""

    def __getitem__(self, key: str) -> object:
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("wide mapping entries must not be materialized")

    def __len__(self) -> int:
        return MAX_RAW_TRANSPORT_NODES
