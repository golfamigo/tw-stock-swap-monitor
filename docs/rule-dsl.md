# Rule DSL

## Goal and safety boundary

Rules use a validated subset of JSON Logic. The evaluator never calls eval, exec, import machinery, attribute traversal, or arbitrary Python callables. Expressions are parsed into a typed internal AST before evaluation. A rule can only read registered field paths from the run evidence model.

Allowed operators are and, or, not, eq, ne, lt, lte, gt, gte, add, subtract, multiply, divide, in, exists, and if. Arithmetic is limited to Decimal-compatible values. Date-time comparisons require timezone-aware datetimes. String operations are equality and membership only. Unknown operators and schema paths are rejected at configuration-validation time.

## Wire JSON Schema

~~~json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://rotation-monitor.example/schemas/rule-expression.json",
  "$ref": "#/$defs/expression",
  "$defs": {
    "path": {
      "type": "object",
      "required": ["var"],
      "additionalProperties": false,
      "properties": {
        "var": {
          "type": "string",
          "pattern": "^(source|candidate|market|run)\\.[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*)*$"
        }
      }
    },
    "literal": {
      "type": ["string", "number", "boolean", "null"]
    },
    "operands": {
      "type": "array",
      "minItems": 1,
      "maxItems": 32,
      "items": { "$ref": "#/$defs/expression" }
    },
    "expression": {
      "oneOf": [
        { "$ref": "#/$defs/path" },
        { "$ref": "#/$defs/literal" },
        {
          "type": "object",
          "minProperties": 1,
          "maxProperties": 1,
          "additionalProperties": false,
          "properties": {
            "and": { "$ref": "#/$defs/operands" },
            "or": { "$ref": "#/$defs/operands" },
            "not": { "$ref": "#/$defs/operands" },
            "eq": { "$ref": "#/$defs/operands" },
            "ne": { "$ref": "#/$defs/operands" },
            "lt": { "$ref": "#/$defs/operands" },
            "lte": { "$ref": "#/$defs/operands" },
            "gt": { "$ref": "#/$defs/operands" },
            "gte": { "$ref": "#/$defs/operands" },
            "add": { "$ref": "#/$defs/operands" },
            "subtract": { "$ref": "#/$defs/operands" },
            "multiply": { "$ref": "#/$defs/operands" },
            "divide": { "$ref": "#/$defs/operands" },
            "in": { "$ref": "#/$defs/operands" },
            "exists": { "$ref": "#/$defs/operands" },
            "if": { "$ref": "#/$defs/operands" }
          }
        }
      ]
    }
  }
}
~~~

The schema is a transport-level guard. A second semantic validator checks arity, registered path existence, operand types, division by zero possibility where statically known, and that an if condition is Boolean. The M0/M1 registry defines only metrics that the indicator and market-data models publish.

## Evaluation constraints

The engine enforces a maximum expression depth of 16, at most 128 AST nodes, and a monotonic evaluation budget. These are non-configurable safety invariants. Missing path values yield MISSING evidence and make that rule neither matched nor failed; they never coerce to zero, empty string, or false. A ruleset aggregation can be INSUFFICIENT when missing evidence means its threshold cannot be evaluated safely.

## Evidence result

Each RuleEvaluation includes result status, total score, threshold, matched rule IDs, failed rule IDs, missing rule IDs, per-rule typed operands, evaluated paths, and timestamp. Evidence is suitable for audit and later LLM input but contains no secrets.

## Example

~~~json
{
  "and": [
    { "lt": [{ "var": "source.last_price" }, { "var": "source.session_vwap" }] },
    { "gte": [{ "var": "source.scans_below_vwap" }, { "var": "source.required_below_scans" }] }
  ]
}
~~~

The threshold and field values come from the resolved profile and evidence model. No company symbol or fixed trading threshold is part of the implementation.
