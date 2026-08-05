"""Regression coverage for the review-required DSL input boundaries."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.rules.engine import EvaluationPurpose, RuleInput
from app.rules.parser import (
    RULE_EXPRESSION_JSON_SCHEMA,
    parse_expression,
    validate_expression_transport,
)
from app.rules.schema import (
    EvidenceKind,
    M0M1EvidenceRegistry,
    ProtectedRuleInputError,
    RuleSemanticError,
    RuleTransportError,
)
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]


def test_documented_draft_2020_12_schema_rejects_invalid_transport_before_semantics() -> None:
    Draft202012Validator.check_schema(RULE_EXPRESSION_JSON_SCHEMA)

    with pytest.raises(RuleTransportError, match="JSON Schema"):
        validate_expression_transport({"and": "not-an-array"})
    with pytest.raises(RuleTransportError, match="JSON Schema"):
        parse_expression({"eq": [True, False], "ne": [True, False]})


def test_protected_sources_are_rejected_at_every_rule_input_boundary() -> None:
    with pytest.raises(ProtectedRuleInputError):
        RuleInput(
            values=M0M1EvidenceRegistry.controlled_values({"source.last_price": Decimal("1")}),
            protected_source=True,
            purpose=EvaluationPurpose.ANALYSIS,
        )


def test_m0_m1_registry_allows_published_values_but_rejects_arbitrary_paths() -> None:
    schema = M0M1EvidenceRegistry.schema()
    assert schema.kind_for("source.last_price") is EvidenceKind.DECIMAL
    assert schema.kind_for("source.session_vwap") is EvidenceKind.DECIMAL
    assert schema.kind_for("source.last_price_as_of") is EvidenceKind.DATETIME
    assert schema.kind_for("market.is_actionable") is EvidenceKind.BOOLEAN
    assert schema.kind_for("market.provider") is EvidenceKind.STRING

    values = M0M1EvidenceRegistry.controlled_values(
        {
            "source.last_price": Decimal("1"),
            "source.last_price_as_of": datetime(2035, 1, 1, tzinfo=UTC),
        }
    )
    assert values["source.last_price"] == Decimal("1")

    for path in ("source.symbol", "source.threshold"):
        with pytest.raises(RuleSemanticError, match="not registered"):
            parse_expression({"var": path})
        with pytest.raises(RuleSemanticError, match="not registered"):
            M0M1EvidenceRegistry.controlled_values({path: "untrusted"})
