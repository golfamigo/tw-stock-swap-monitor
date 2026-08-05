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

The engine enforces a maximum expression depth of 16, at most 128 semantic AST nodes, a ruleset maximum of 512 rules and 512 aggregate semantic AST nodes, and a monotonic 512-step evaluation budget. Before Draft 2020-12 validation, a separate iterative raw-transport preflight limits all JSON Mapping and list subtrees to depth 32 and 512 nodes; both mapping and list children increase depth. This is deliberately larger than the semantic AST budget so valid 16-depth/128-node expressions remain valid. These are non-configurable safety invariants. Decimal evidence, arithmetic results, and scores are limited to 256 coefficient digits, an absolute exponent of 256, and a maximum 512-character canonical form before capture. Missing path values yield MISSING evidence and make that rule neither matched nor failed; they never coerce to zero, empty string, or false. A ruleset aggregation can be INSUFFICIENT when missing evidence means its threshold cannot be evaluated safely.

## UTF-8 resource boundaries

All limits use UTF-8 byte length, not Python character count. The canonical JSON counters use sorted keys, compact separators, `ensure_ascii=False`, and `allow_nan=False`, then count `JSONEncoder.iterencode()` chunks incrementally. JSON keys, quotation marks, escaping, brackets, and separators are included. Counters stop at the first chunk that exceeds the limit and return typed errors containing only the boundary name, limit, and observed-at-least byte count. They do not truncate, summarize, compress, or produce a missing/actionable substitute.

| Boundary | Maximum |
| --- | ---: |
| Identifier (`rule_id`, factor ID, stage ID, provider/snapshot identifier evidence) | 256 UTF-8 bytes |
| General string literal or runtime evidence string | 16 KiB UTF-8 |
| One DSL expression canonical JSON | 128 KiB UTF-8 |
| Complete DSL ruleset canonical JSON | 128 KiB UTF-8 |
| Normalized `RuleInput` canonical payload | 128 KiB UTF-8 |
| Complete `RuleEvaluation` audit evidence | 128 KiB UTF-8 |
| Complete `StrategyRun.outputs` reversible evidence envelope | 256 KiB UTF-8 |

The DSL validates text and complete transport payloads before JSON Schema traversal. Runtime evidence validates the same bounds after its registered path and typed value have been normalized. A limit exceedance is a typed resource-limit error, never `MISSING`, `False`, zero, or an actionable result. The application/API pre-parser HTTP request-body limit is deliberately deferred to Task 10; these limits protect validated payloads after parsing and do not replace an HTTP gateway limit.

## Evidence result

Each RuleEvaluation includes result status, total score, threshold, matched rule IDs, failed rule IDs, missing rule IDs, per-rule typed operands, evaluated paths, and timestamp. Evidence is suitable for audit and later LLM input but contains no secrets.

## Example

~~~json
{
  "and": [
    { "lt": [{ "var": "source.last_price" }, { "var": "source.session_vwap" }] },
    { "gte": [{ "var": "source.same_time_volume_ratio" }, 1] }
  ]
}
~~~

The threshold and field values come from the resolved profile and evidence model. No company symbol or fixed trading threshold is part of the implementation.
