"""Evaluation and evidence coverage for the restricted rule DSL."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.rules.engine import RuleInput, evaluate_ruleset
from app.rules.evidence import EvidenceStatus, RulesetStatus
from app.rules.parser import parse_rule, parse_rules
from app.rules.schema import EvidenceKind, RuleEvaluationError


def test_ruleset_reports_matched_failed_and_typed_missing_evidence() -> None:
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
                "expression": {"eq": [{"var": "market.provider"}, "control"]},
            },
            {
                "id": "missing_check",
                "expression": {"gte": [{"var": "source.same_time_volume_ratio"}, 1]},
            },
        ),
    )
    evaluated_at = datetime(2035, 1, 1, 9, 30, tzinfo=UTC)

    result = evaluate_ruleset(
        rules,
        rule_input=RuleInput(
            values={
                "source.last_price": Decimal("10"),
                "source.session_vwap": Decimal("11"),
                "market.provider": "different",
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
    assert missing.evaluated_paths[0].path == "source.same_time_volume_ratio"
    assert missing.evaluated_paths[0].value.kind is EvidenceKind.DECIMAL
    assert missing.evaluated_paths[0].value.is_missing


def test_ruleset_is_insufficient_when_missing_evidence_can_change_threshold() -> None:
    rules = parse_rules(
        (
            {"id": "matched", "weight": 1, "expression": True},
            {
                "id": "unknown",
                "weight": 1,
                "expression": {"gte": [{"var": "source.same_time_volume_ratio"}, 1]},
            },
        ),
    )

    result = evaluate_ruleset(
        rules,
        rule_input=RuleInput(values={}),
        threshold=Decimal("2"),
        evaluated_at=datetime(2035, 1, 1, tzinfo=UTC),
    )

    assert result.status is RulesetStatus.INSUFFICIENT
    assert result.matched_rule_ids == ("matched",)
    assert result.failed_rule_ids == ()
    assert result.missing_rule_ids == ("unknown",)


def test_datetime_and_string_operations_are_typed_and_audited() -> None:
    rule = parse_rule(
        {
            "id": "typed_operations",
            "expression": {
                "and": [
                    {
                        "lt": [
                            {"var": "source.last_price_as_of"},
                            {"var": "market.data_quality.source_timestamp"},
                        ]
                    },
                    {
                        "in": [
                            {"var": "market.provider"},
                            {"var": "market.snapshot_id"},
                        ]
                    },
                ]
            },
        },
    )
    evaluated_at = datetime(2035, 1, 1, tzinfo=UTC)

    result = evaluate_ruleset(
        (rule,),
        rule_input=RuleInput(
            values={
                "source.last_price_as_of": datetime(2035, 1, 1, 9, 0, tzinfo=UTC),
                "market.data_quality.source_timestamp": datetime(2035, 1, 1, 9, 1, tzinfo=UTC),
                "market.provider": "approved",
                "market.snapshot_id": "approved,other",
            }
        ),
        threshold=Decimal("1"),
        evaluated_at=evaluated_at,
    )

    assert result.status is RulesetStatus.MATCHED
    assert result.rule_evidence[0].status is EvidenceStatus.MATCHED
    assert tuple(path.path for path in result.rule_evidence[0].evaluated_paths) == (
        "source.last_price_as_of",
        "market.data_quality.source_timestamp",
        "market.provider",
        "market.snapshot_id",
    )
    assert {operand.kind for operand in result.rule_evidence[0].operands} == {
        EvidenceKind.BOOLEAN,
        EvidenceKind.DATETIME,
        EvidenceKind.STRING,
    }


def test_runtime_zero_division_and_naive_datetime_are_rejected() -> None:
    divide_rule = parse_rule(
        {
            "id": "divide",
            "expression": {
                "gt": [
                    {
                        "divide": [
                            {"var": "source.last_price"},
                            {"var": "source.session_vwap"},
                        ]
                    },
                    0,
                ]
            },
        },
    )
    datetime_rule = parse_rule(
        {
            "id": "datetime",
            "expression": {
                "lt": [
                    {"var": "source.last_price_as_of"},
                    {"var": "market.data_quality.source_timestamp"},
                ]
            },
        },
    )
    evaluated_at = datetime(2035, 1, 1, tzinfo=UTC)

    with pytest.raises(RuleEvaluationError, match="zero"):
        evaluate_ruleset(
            (divide_rule,),
            rule_input=RuleInput(
                values={
                    "source.last_price": Decimal("1"),
                    "source.session_vwap": Decimal("0"),
                }
            ),
            threshold=Decimal("1"),
            evaluated_at=evaluated_at,
        )
    with pytest.raises(RuleEvaluationError, match="timezone-aware"):
        evaluate_ruleset(
            (datetime_rule,),
            rule_input=RuleInput(
                values={
                    "source.last_price_as_of": datetime(2035, 1, 1, 9, 0),
                    "market.data_quality.source_timestamp": datetime(2035, 1, 1, 9, 1, tzinfo=UTC),
                }
            ),
            threshold=Decimal("1"),
            evaluated_at=evaluated_at,
        )
