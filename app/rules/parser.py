"""Transport and semantic validation for the safe JSON-Logic subset."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext

from jsonschema import Draft202012Validator, ValidationError  # type: ignore[import-untyped]

from app.rules.schema import (
    MAX_AST_NODES,
    MAX_EXPRESSION_DEPTH,
    MAX_RAW_TRANSPORT_DEPTH,
    MAX_RAW_TRANSPORT_NODES,
    MAX_RULESET_AST_NODES,
    MAX_RULESET_RULES,
    EvidenceKind,
    Expression,
    LiteralExpression,
    M0M1EvidenceRegistry,
    OperationExpression,
    PathExpression,
    Rule,
    RuleSafetyError,
    RuleSemanticError,
    RuleTransportError,
    require_bounded_decimal,
)

_ALLOWED_OPERATORS = frozenset(
    {
        "and",
        "or",
        "not",
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "add",
        "subtract",
        "multiply",
        "divide",
        "in",
        "exists",
        "if",
    }
)
_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)

RULE_EXPRESSION_JSON_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://rotation-monitor.example/schemas/rule-expression.json",
    "$ref": "#/$defs/expression",
    "$defs": {
        "path": {
            "type": "object",
            "required": ["var"],
            "additionalProperties": False,
            "properties": {
                "var": {
                    "type": "string",
                    "pattern": (
                        "^(source|candidate|market|run)\\.[A-Za-z_][A-Za-z0-9_]*"
                        "(\\.[A-Za-z_][A-Za-z0-9_]*)*$"
                    ),
                }
            },
        },
        "literal": {"type": ["string", "number", "boolean", "null"]},
        "operands": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {"$ref": "#/$defs/expression"},
        },
        "expression": {
            "oneOf": [
                {"$ref": "#/$defs/path"},
                {"$ref": "#/$defs/literal"},
                {
                    "type": "object",
                    "minProperties": 1,
                    "maxProperties": 1,
                    "additionalProperties": False,
                    "properties": {
                        operator: {"$ref": "#/$defs/operands"}
                        for operator in sorted(_ALLOWED_OPERATORS)
                    },
                },
            ]
        },
    },
}
_RULE_EXPRESSION_VALIDATOR = Draft202012Validator(RULE_EXPRESSION_JSON_SCHEMA)


@dataclass(slots=True)
class _ParseBudget:
    """One monotonic counter shared by recursive AST construction."""

    nodes: int = 0

    def consume_node(self, depth: int) -> None:
        if depth > MAX_EXPRESSION_DEPTH:
            raise RuleSafetyError(f"expression depth exceeds {MAX_EXPRESSION_DEPTH}")
        self.nodes += 1
        if self.nodes > MAX_AST_NODES:
            raise RuleSafetyError(f"expression node count exceeds {MAX_AST_NODES}")


def validate_expression_transport(raw: object) -> None:
    """Apply the documented Draft 2020-12 JSON Schema before semantic validation."""

    _preflight_raw_expression(raw)
    try:
        _RULE_EXPRESSION_VALIDATOR.validate(raw)
    except ValidationError as error:
        raise RuleTransportError(
            "rule expression fails JSON Schema transport validation"
        ) from error


def parse_expression(raw: object) -> Expression:
    """Parse one JSON-shaped expression into a typed AST with fixed safety limits."""

    validate_expression_transport(raw)
    return _parse_expression(
        raw,
        budget=_ParseBudget(),
        depth=1,
    )


def parse_rule(raw: object) -> Rule:
    """Validate one generic configuration rule without changing configuration merge behavior."""

    if not isinstance(raw, dict):
        raise RuleTransportError("rule must be a JSON object")
    allowed_keys = {"id", "expression", "weight"}
    if set(raw) - allowed_keys or "id" not in raw or "expression" not in raw:
        raise RuleTransportError("rule must contain only id, expression, and optional weight")
    rule_id = raw["id"]
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise RuleTransportError("rule id must be a non-blank string")
    raw_weight = raw.get("weight", 1)
    if isinstance(raw_weight, bool) or not isinstance(raw_weight, int | float):
        raise RuleTransportError("rule weight must be a JSON number")
    weight = _decimal_from_json_number(raw_weight, label="rule weight")
    if weight <= Decimal("0"):
        raise RuleSemanticError("rule weight must be positive")
    expression = parse_expression(raw["expression"])
    return Rule(rule_id=rule_id, expression=expression, weight=weight)


def parse_rules(raw_rules: object) -> tuple[Rule, ...]:
    """Parse an ordered generic rule list and reject duplicate identifiers."""

    if not isinstance(raw_rules, list | tuple):
        raise RuleTransportError("rules must be a JSON array")
    if len(raw_rules) > MAX_RULESET_RULES:
        raise RuleSafetyError(f"ruleset rule count exceeds {MAX_RULESET_RULES}")
    rules: list[Rule] = []
    total_nodes = 0
    for raw_rule in raw_rules:
        rule = parse_rule(raw_rule)
        total_nodes += _count_expression_ast_nodes(rule.expression)
        if total_nodes > MAX_RULESET_AST_NODES:
            raise RuleSafetyError(f"ruleset AST node count exceeds {MAX_RULESET_AST_NODES}")
        rules.append(rule)
    parsed_rules = tuple(rules)
    rule_ids = tuple(rule.rule_id for rule in parsed_rules)
    if len(set(rule_ids)) != len(rule_ids):
        raise RuleSemanticError("rule ids must be unique")
    return parsed_rules


def validate_expression_ast(expression: Expression) -> None:
    """Revalidate a public AST iteratively before direct evaluator use."""

    stack: list[tuple[object, int]] = [(expression, 1)]
    operations: list[OperationExpression] = []
    node_count = 0
    while stack:
        node, depth = stack.pop()
        if depth > MAX_EXPRESSION_DEPTH:
            raise RuleSafetyError(f"expression depth exceeds {MAX_EXPRESSION_DEPTH}")
        node_count += 1
        if node_count > MAX_AST_NODES:
            raise RuleSafetyError(f"expression node count exceeds {MAX_AST_NODES}")
        if isinstance(node, LiteralExpression):
            _validate_public_literal(node)
            continue
        if isinstance(node, PathExpression):
            expected_kind = M0M1EvidenceRegistry.schema().kind_for(node.path)
            if node.kind is not expected_kind:
                raise RuleSemanticError("public path expression kind does not match the registry")
            continue
        if not isinstance(node, OperationExpression):
            raise RuleSemanticError("public expression contains an unsupported AST node")
        if not isinstance(node.operator, str) or node.operator not in _ALLOWED_OPERATORS:
            raise RuleSemanticError("public operation expression has an unsupported operator")
        if not isinstance(node.operands, tuple):
            raise RuleSemanticError("public operation operands must be a tuple")
        if not isinstance(node.kind, EvidenceKind):
            raise RuleSemanticError("public operation expression kind must be an EvidenceKind")
        operations.append(node)
        for operand in reversed(node.operands):
            stack.append((operand, depth + 1))

    for operation in reversed(operations):
        operand_kinds: list[EvidenceKind] = []
        for operand in operation.operands:
            if not isinstance(operand, LiteralExpression | PathExpression | OperationExpression):
                raise RuleSemanticError("public operation has an unsupported operand node")
            if not isinstance(operand.kind, EvidenceKind):
                raise RuleSemanticError("public operation operand kind must be an EvidenceKind")
            operand_kinds.append(operand.kind)
        result_kind = _validate_operator(operation.operator, tuple(operation.operands))
        if operation.kind is not result_kind:
            raise RuleSemanticError("public operation result kind does not match its operands")


def _preflight_raw_expression(raw: object) -> int:
    """Iteratively enforce raw JSON expression limits before recursive JSON Schema validation."""

    stack: list[tuple[object, int]] = [(raw, 1)]
    node_count = 0
    while stack:
        node, depth = stack.pop()
        if depth > MAX_RAW_TRANSPORT_DEPTH:
            raise RuleSafetyError(f"raw transport depth exceeds {MAX_RAW_TRANSPORT_DEPTH}")
        node_count += 1
        if node_count > MAX_RAW_TRANSPORT_NODES:
            raise RuleSafetyError(f"raw transport node count exceeds {MAX_RAW_TRANSPORT_NODES}")
        if isinstance(node, Mapping):
            for child in reversed(tuple(node.values())):
                stack.append((child, depth + 1))
            continue
        if isinstance(node, list):
            for child in reversed(node):
                stack.append((child, depth))
            continue
    return node_count


def _count_expression_ast_nodes(expression: Expression) -> int:
    """Count an already parser-bounded semantic AST without using raw transport containers."""

    stack: list[Expression] = [expression]
    node_count = 0
    while stack:
        node = stack.pop()
        node_count += 1
        if node_count > MAX_RULESET_AST_NODES:
            return node_count
        if isinstance(node, OperationExpression):
            stack.extend(reversed(node.operands))
    return node_count


def _validate_public_literal(expression: LiteralExpression) -> None:
    if not isinstance(expression.kind, EvidenceKind):
        raise RuleSemanticError("public literal kind must be an EvidenceKind")
    if expression.kind is EvidenceKind.DECIMAL:
        if not isinstance(expression.value, Decimal):
            raise RuleSemanticError("public Decimal literals must hold Decimal values")
        require_bounded_decimal(expression.value)


def _parse_expression(
    raw: object,
    *,
    budget: _ParseBudget,
    depth: int,
) -> Expression:
    budget.consume_node(depth)
    if raw is None:
        return LiteralExpression(kind=EvidenceKind.NULL, value=None)
    if isinstance(raw, bool):
        return LiteralExpression(kind=EvidenceKind.BOOLEAN, value=raw)
    if isinstance(raw, str):
        return LiteralExpression(kind=EvidenceKind.STRING, value=raw)
    if isinstance(raw, int | float):
        if isinstance(raw, bool):
            raise AssertionError("Boolean values are handled before JSON numbers")
        return LiteralExpression(
            kind=EvidenceKind.DECIMAL,
            value=_decimal_from_json_number(raw, label="literal"),
        )
    if not isinstance(raw, dict):
        raise RuleTransportError("expressions must be JSON scalar values or objects")
    if set(raw) == {"var"}:
        path = raw["var"]
        if not isinstance(path, str):
            raise RuleTransportError("var path must be a string")
        return PathExpression(path=path, kind=M0M1EvidenceRegistry.schema().kind_for(path))
    if len(raw) != 1:
        raise RuleTransportError("operator objects must contain exactly one allowed operator")
    operator, raw_operands = next(iter(raw.items()))
    if not isinstance(operator, str) or operator not in _ALLOWED_OPERATORS:
        raise RuleTransportError("operator is not allowed")
    if not isinstance(raw_operands, list):
        raise RuleTransportError("operator operands must be a JSON array")
    if not 1 <= len(raw_operands) <= 32:
        raise RuleTransportError("operator operands must contain between 1 and 32 values")
    operands = tuple(
        _parse_expression(
            operand,
            budget=budget,
            depth=depth + 1,
        )
        for operand in raw_operands
    )
    result_kind = _validate_operator(operator, operands)
    return OperationExpression(operator=operator, operands=operands, kind=result_kind)


def _validate_operator(operator: str, operands: tuple[Expression, ...]) -> EvidenceKind:
    operand_kinds = tuple(operand.kind for operand in operands)
    if operator in {"and", "or"}:
        _require_minimum_arity(operator, operands, minimum=1)
        _require_all_kinds(operator, operand_kinds, EvidenceKind.BOOLEAN)
        return EvidenceKind.BOOLEAN
    if operator == "not":
        _require_arity(operator, operands, expected=1)
        _require_all_kinds(operator, operand_kinds, EvidenceKind.BOOLEAN)
        return EvidenceKind.BOOLEAN
    if operator in {"eq", "ne"}:
        _require_arity(operator, operands, expected=2)
        if operand_kinds[0] is not operand_kinds[1]:
            raise RuleSemanticError(f"{operator} operands must have the same type")
        return EvidenceKind.BOOLEAN
    if operator in {"lt", "lte", "gt", "gte"}:
        _require_arity(operator, operands, expected=2)
        if operand_kinds[0] is not operand_kinds[1] or operand_kinds[0] not in {
            EvidenceKind.DECIMAL,
            EvidenceKind.DATETIME,
        }:
            raise RuleSemanticError(f"{operator} operands must both be Decimal or datetime")
        return EvidenceKind.BOOLEAN
    if operator in {"add", "multiply"}:
        _require_minimum_arity(operator, operands, minimum=2)
        _require_all_kinds(operator, operand_kinds, EvidenceKind.DECIMAL)
        return EvidenceKind.DECIMAL
    if operator in {"subtract", "divide"}:
        _require_arity(operator, operands, expected=2)
        _require_all_kinds(operator, operand_kinds, EvidenceKind.DECIMAL)
        if operator == "divide":
            denominator = _static_decimal(operands[1])
            if denominator is not None and denominator.is_zero():
                raise RuleSemanticError("divide denominator is statically zero")
        return EvidenceKind.DECIMAL
    if operator == "in":
        _require_arity(operator, operands, expected=2)
        _require_all_kinds(operator, operand_kinds, EvidenceKind.STRING)
        return EvidenceKind.BOOLEAN
    if operator == "exists":
        _require_arity(operator, operands, expected=1)
        return EvidenceKind.BOOLEAN
    if operator == "if":
        _require_arity(operator, operands, expected=3)
        if operand_kinds[0] is not EvidenceKind.BOOLEAN:
            raise RuleSemanticError("if condition must be Boolean")
        if operand_kinds[1] is not operand_kinds[2]:
            raise RuleSemanticError("if branches must have the same type")
        return operand_kinds[1]
    raise AssertionError("all allowed operators are validated above")


def _require_arity(operator: str, operands: Sequence[Expression], *, expected: int) -> None:
    if len(operands) != expected:
        raise RuleSemanticError(f"{operator} requires exactly {expected} operands")


def _require_minimum_arity(operator: str, operands: Sequence[Expression], *, minimum: int) -> None:
    if len(operands) < minimum:
        raise RuleSemanticError(f"{operator} requires at least {minimum} operands")


def _require_all_kinds(
    operator: str, operand_kinds: Sequence[EvidenceKind], expected: EvidenceKind
) -> None:
    if any(kind is not expected for kind in operand_kinds):
        raise RuleSemanticError(f"{operator} operands must all be {expected.value}")


def _decimal_from_json_number(value: int | float, *, label: str) -> Decimal:
    if isinstance(value, float) and not math.isfinite(value):
        raise RuleTransportError(f"{label} must be finite")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise RuleTransportError(f"{label} must be Decimal-compatible") from error
    if not decimal.is_finite():
        raise RuleTransportError(f"{label} must be finite")
    return _canonical_decimal(decimal)


def _static_decimal(expression: Expression) -> Decimal | None:
    if isinstance(expression, LiteralExpression):
        if expression.kind is not EvidenceKind.DECIMAL:
            return None
        assert isinstance(expression.value, Decimal)
        return expression.value
    if (
        not isinstance(expression, OperationExpression)
        or expression.kind is not EvidenceKind.DECIMAL
    ):
        return None
    operands = tuple(_static_decimal(operand) for operand in expression.operands)
    if any(operand is None for operand in operands):
        return None
    values = tuple(operand for operand in operands if operand is not None)
    with localcontext(_DECIMAL_CONTEXT):
        if expression.operator == "add":
            return _canonical_decimal(sum(values, Decimal("0")))
        if expression.operator == "subtract":
            return _canonical_decimal(values[0] - values[1])
        if expression.operator == "multiply":
            product = Decimal("1")
            for value in values:
                product *= value
            return _canonical_decimal(product)
        if expression.operator == "divide":
            if values[1].is_zero():
                return None
            return _canonical_decimal(values[0] / values[1])
    return None


def _canonical_decimal(value: Decimal) -> Decimal:
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
