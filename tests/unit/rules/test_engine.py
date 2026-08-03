"""Evaluation and evidence coverage for the restricted rule DSL."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.rules.engine import RuleInput, evaluate_ruleset
from app.rules.evidence import EvidenceStatus, RulesetStatus
from app.rules.parser import parse_rule, parse_rules
from app.rules.schema import EvidenceKind, EvidenceSchema, RuleEvaluationError


@pytest.fixture
def evidence_schema() -> EvidenceSchema:
    return EvidenceSchema(
        {
            "source.last_price": EvidenceKind.DECIMAL,
            "source.session_vwap": EvidenceKind.DECIMAL,
            "source.optional_price": EvidenceKind.DECIMAL,
            "source.observed_at": EvidenceKind.DATETIME,
            "candidate.observed_at": EvidenceKind.DATETIME,
            "candidate.label": EvidenceKind.STRING,
            "candidate.allowed_labels": EvidenceKind.STRING,
            "market.denominator": EvidenceKind.DECIMAL,
        }
    )


def test_ruleset_reports_matched_failed_and_typed_missing_evidence(
    evidence_schema: EvidenceSchema,
) -> None:
    rules = parse_rules(
        (
            {
                "id": "price_check",
                "weight": 2,
                "expression": {
                    "lt": [{"var": "source.last_price"}, {"var": "source.session_vwap"}]
                },
            },
            {
                "id": "label_check",
                "expression": {"eq": [{"var": "candidate.label"}, "control"]},
            },
            {
                "id": "missing_check",
                "expression": {"gte": [{"var": "source.optional_price"}, 1]},
            },
        ),
        evidence_schema=evidence_schema,
    )
    evaluated_at = datetime(2035, 1, 1, 9, 30, tzinfo=UTC)

    result = evaluate_ruleset(
        rules,
        evidence_schema=evidence_schema,
        rule_input=RuleInput(
            values={
                "source.last_price": Decimal("10"),
                "source.session_vwap": Decimal("11"),
                "candidate.label": "different",
            }
        ),
        threshold=Decimal("2"),
        evaluated_at=evaluated_at,
    )

    assert result.status is RulesetStatus.MATCHED
    assert result.total_score == Decimal("2")
    assert result.threshold == Decimal("2")
    assert result.matched_rule_ids == ("price_check",)
    assert result.failed_rule_ids == ("label_check",)
    assert result.missing_rule_ids == ("missing_check",)
    assert result.evaluated_at == evaluated_at
    missing = result.rule_evidence[2]
    assert missing.status is EvidenceStatus.MISSING
    assert missing.evaluated_paths[0].path == "source.optional_price"
    assert missing.evaluated_paths[0].value.kind is EvidenceKind.DECIMAL
    assert missing.evaluated_paths[0].value.is_missing


def test_ruleset_is_insufficient_when_missing_evidence_can_change_threshold(
    evidence_schema: EvidenceSchema,
) -> None:
    rules = parse_rules(
        (
            {"id": "matched", "weight": 1, "expression": True},
            {
                "id": "unknown",
                "weight": 1,
                "expression": {"gte": [{"var": "source.optional_price"}, 1]},
            },
        ),
        evidence_schema=evidence_schema,
    )

    result = evaluate_ruleset(
        rules,
        evidence_schema=evidence_schema,
        rule_input=RuleInput(values={}),
        threshold=Decimal("2"),
        evaluated_at=datetime(2035, 1, 1, tzinfo=UTC),
    )

    assert result.status is RulesetStatus.INSUFFICIENT
    assert result.matched_rule_ids == ("matched",)
    assert result.failed_rule_ids == ()
    assert result.missing_rule_ids == ("unknown",)


def test_datetime_and_string_operations_are_typed_and_audited(
    evidence_schema: EvidenceSchema,
) -> None:
    rule = parse_rule(
        {
            "id": "typed_operations",
            "expression": {
                "and": [
                    {
                        "lt": [
                            {"var": "source.observed_at"},
                            {"var": "candidate.observed_at"},
                        ]
                    },
                    {
                        "in": [
                            {"var": "candidate.label"},
                            {"var": "candidate.allowed_labels"},
                        ]
                    },
                ]
            },
        },
        evidence_schema=evidence_schema,
    )
    evaluated_at = datetime(2035, 1, 1, tzinfo=UTC)

    result = evaluate_ruleset(
        (rule,),
        evidence_schema=evidence_schema,
        rule_input=RuleInput(
            values={
                "source.observed_at": datetime(2035, 1, 1, 9, 0, tzinfo=UTC),
                "candidate.observed_at": datetime(2035, 1, 1, 9, 1, tzinfo=UTC),
                "candidate.label": "approved",
                "candidate.allowed_labels": "approved,other",
            }
        ),
        threshold=Decimal("1"),
        evaluated_at=evaluated_at,
    )

    assert result.status is RulesetStatus.MATCHED
    assert result.rule_evidence[0].status is EvidenceStatus.MATCHED
    assert tuple(path.path for path in result.rule_evidence[0].evaluated_paths) == (
        "source.observed_at",
        "candidate.observed_at",
        "candidate.label",
        "candidate.allowed_labels",
    )
    assert {operand.kind for operand in result.rule_evidence[0].operands} == {
        EvidenceKind.BOOLEAN,
        EvidenceKind.DATETIME,
        EvidenceKind.STRING,
    }


def test_runtime_zero_division_and_naive_datetime_are_rejected(
    evidence_schema: EvidenceSchema,
) -> None:
    divide_rule = parse_rule(
        {
            "id": "divide",
            "expression": {
                "gt": [
                    {
                        "divide": [
                            {"var": "source.last_price"},
                            {"var": "market.denominator"},
                        ]
                    },
                    0,
                ]
            },
        },
        evidence_schema=evidence_schema,
    )
    datetime_rule = parse_rule(
        {
            "id": "datetime",
            "expression": {
                "lt": [
                    {"var": "source.observed_at"},
                    {"var": "candidate.observed_at"},
                ]
            },
        },
        evidence_schema=evidence_schema,
    )
    evaluated_at = datetime(2035, 1, 1, tzinfo=UTC)

    with pytest.raises(RuleEvaluationError, match="zero"):
        evaluate_ruleset(
            (divide_rule,),
            evidence_schema=evidence_schema,
            rule_input=RuleInput(
                values={
                    "source.last_price": Decimal("1"),
                    "market.denominator": Decimal("0"),
                }
            ),
            threshold=Decimal("1"),
            evaluated_at=evaluated_at,
        )
    with pytest.raises(RuleEvaluationError, match="timezone-aware"):
        evaluate_ruleset(
            (datetime_rule,),
            evidence_schema=evidence_schema,
            rule_input=RuleInput(
                values={
                    "source.observed_at": datetime(2035, 1, 1, 9, 0),
                    "candidate.observed_at": datetime(2035, 1, 1, 9, 1, tzinfo=UTC),
                }
            ),
            threshold=Decimal("1"),
            evaluated_at=evaluated_at,
        )
