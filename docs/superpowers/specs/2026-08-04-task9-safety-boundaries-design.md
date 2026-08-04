# Task 9 safety-boundary remediation design

## Purpose

This remediation closes three correctness and resource-safety gaps found at the Task 9 checkpoint. It remains recommendation-only: it does not mutate positions, place orders, send notifications, call a live market provider, or begin Task 10.

## Scope

1. Make a rotation plan's `source_position_ids` the exclusive authorization list for a recommended sale.
2. Make staged allocation weights an exact, bounded basis-point domain value.
3. Bound restricted-DSL and audit-evidence text using incremental canonical JSON measurement.

## 1. Authorized rotation-sale source

### Invariants

The domain will expose two composable pure invariants:

```python
ensure_authorized_rotation_source(plan: RotationPlan, position: Position) -> None
ensure_authorized_rotation_sale(
    plan: RotationPlan, position: Position, sale_quantity: Quantity
) -> None
```

`ensure_authorized_rotation_source` rejects a position unless all conditions hold:

- its ID is in `plan.source_position_ids`;
- its portfolio equals `plan.portfolio_id`;
- its status is `OPEN`;
- its role is not `PROTECTED_CORE`; and
- its ID is not in `plan.protected_position_ids`.

`ensure_authorized_rotation_sale` first applies source authorization, then requires a positive sale quantity no larger than the current open quantity. It is the only invariant that needs a sale quantity.

The existing generic `ensure_plan_reference_is_authorized` remains for checking plan references such as candidate groups. It is not a sale authorization check because it intentionally accepts either a source or protected reference.

### Boundaries

The same business predicate is independently enforced at each safety boundary:

| Boundary | Evidence available | Required check |
| --- | --- | --- |
| Run coordinator | Repository-loaded authoritative `Position` | `ensure_authorized_rotation_source` before a strategy run is built. |
| Sizing engine | Authoritative position and requested `Quantity` | `ensure_authorized_rotation_sale`. |
| State machine | Authoritative position, `source_position_ids`, and typed sizing-sale quantity where the event claims sizing passed | Action/sale events require an authorized source; a sizing-passed transition requires the complete sale invariant. |

`TransitionRequest` will carry the plan's source IDs in addition to protected IDs, and it will carry an optional typed `sale_quantity`. The event contract is explicit:

- `ACTION_SIGNAL` requires only the authoritative authorized source; it does not invent a quantity before sizing.
- `DECISION_VALIDATED` with `SizingOutcome.PASSED` requires `sale_quantity`. The application derives it from typed sizing evidence and the state machine applies `ensure_authorized_rotation_sale`; omission is a transition error.
- `NEXT_STAGE_ELIGIBLE` makes a next stage eligible after fresh rules, but does not itself claim a sized sale. It requires source authorization and no quantity. Any subsequent `DECISION_VALIDATED` again requires the complete quantity check.

The coordinator remains responsible for retrieving the position through the repository; evaluator-provided IDs are never trusted as position objects. A quantity offered to a sizing-passed transition is only accepted as typed sizing evidence, never as an unchecked scalar from a rule payload.

This rejects an ordinary same-portfolio position that is not attached to the plan, every protected position, and every closed position. It does not change position records or execution status.

### Failure behavior

Violations produce typed domain or transition errors, cause the coordinator to record a non-actionable failed attempt, and leave the durable recommendation state unchanged. No recommendation capable of selling an unauthorized position is persisted.

## 2. Allocation weights as basis points

### Canonical representation

`SizingStage` accepts a Decimal allocation input at its construction boundary only when it can be represented exactly as basis points:

```text
allocation_basis_points = allocation_weight * 10,000
```

The result must be an integer in the inclusive range `1..10_000`; no rounding, quantization, or ambient Decimal-context behavior is permitted. Therefore `0.3`, `0.3000`, and `0.30000` all normalize to `3000`, while `0.30001` is rejected.

The stage stores `allocation_basis_points: int` as the sole domain authority. A derived `allocation_weight` view returns the exact four-decimal Decimal fraction for existing consumers. Resolved configuration snapshots retain their current Decimal representation and existing canonical v1 serialization; basis points are an internal sizing-domain normalization only. The existing v1 Decimal canonicalizer already normalizes trailing zeroes, so semantically identical configuration inputs such as `0.3` and `0.3000` retain identical v1 identity without changing the hash contract.

`SizingConfiguration` verifies:

```text
sum(stage.allocation_basis_points for stage in stages) == 10_000
```

It neither fills a remainder nor changes a submitted weight. For example, three `0.3333` weights are rejected as 9,999 bp; the caller must supply a 3,334 bp final stage.

Sizing arithmetic derives its fraction from the stored integer using an explicit exact conversion. Monetary costs, quantities, and rounding policies retain their existing Decimal semantics; this remediation only defines the allocation-weight representation.

### Failure behavior

Invalid, non-finite, non-integral-basis-point, out-of-range, or non-totaling configurations raise `SizingError` at construction. No sizing result is produced from an ambiguous allocation.

## 3. Restricted DSL and audit resource boundaries

### Limits

All limits are UTF-8 bytes. The exact encoded payload is counted, including object keys, string escapes, quotes, punctuation, and container delimiters.

| Resource | Limit |
| --- | ---: |
| Identifier (`rule_id`, factor ID, stage ID, provider/purpose/state-like identifier) | 256 bytes |
| One ordinary string literal or evidence string | 16 KiB |
| One expression canonical JSON | 128 KiB |
| Complete ruleset canonical JSON | 128 KiB |
| `RuleInput` canonical payload | 128 KiB |
| `RuleEvaluation` evidence canonical payload | 128 KiB |
| `StrategyRun.outputs` canonical payload | 256 KiB |

### Canonical measurement and existing hashes

Configuration canonical v1 is unchanged. Its NFC normalization, Decimal/datetime/UUID/Enum representations, byte-sorted keys, format-version prefix, zero-byte delimiter, and SHA-256 construction remain its only hash contract. This remediation neither rehashes existing snapshots nor replaces their encoder.

The new incremental counter is a size meter, not a configuration hash encoder. For each new bounded DSL or audit representation, it measures the exact canonical representation that the same boundary would persist or otherwise expose. It uses normalization equivalent to:

```python
json.JSONEncoder(
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).iterencode(payload)
```

The counter consumes chunks incrementally and adds `len(chunk.encode("utf-8"))`. Once a boundary is exceeded it raises a typed resource-limit error immediately. It does not first materialize a complete JSON string, truncate content, compress content, summarize content, or include the rejected raw content in an error. Existing or future hash-bearing payloads may use this counter only when its streamed bytes are proven identical to that payload's established canonical format; otherwise it remains measurement-only.

The counter rejects cycles, non-string mapping keys, unsupported types, and non-finite numeric values. The public DSL transport allows only JSON-compatible types; the internal audit serializer additionally has explicit canonical encodings for existing supported immutable evidence values such as Decimal, UUID, and timezone-aware datetime. Any size measurement and the eventual canonical representation use the same normalization.

### DSL validation and evidence

Raw transport preflight validates individual strings before JSON Schema evaluation and rejects an oversized single literal, path, operator key, or rule identifier. It applies depth advancement uniformly to both mappings and lists. The preflight then applies the incremental 128 KiB expression or ruleset boundary before recursive schema validation.

`EvidenceValue` validates runtime strings before they enter rule traces. `RuleInput` and the final `RuleEvaluation` each validate their complete canonical payloads against 128 KiB. This prevents oversized provider/adapter strings from becoming evidence even when they are not expression literals.

`StrategyRun.outputs` first receives an iterative structural preflight before recursive freeze or canonical measurement. It uses fixed, non-configurable limits of depth 32 and 8,192 nodes; both mappings and lists consume one depth level. The preflight rejects cycles, non-string keys, keys above the 256-byte identifier limit, strings above 16 KiB, unsupported values, non-finite or unbounded Decimal representations, and invalid timezone-aware datetime values. Consequently the existing recursive freeze only receives shallow, finite, supported data and cannot first fail with an uncontrolled `RecursionError`.

After preflight, `StrategyRun` freezes `outputs` and applies the 256 KiB canonical boundary before any repository or persistence mapper sees it. This makes in-memory, SQL, and future API adapters follow the same audit limit.

### Documentation and API boundary

`docs/rule-dsl.md` will state the limits, error behavior, and correct raw transport depth semantics. Its examples will reference only paths in the fixed M0/M1 registry.

An HTTP request-body limit before JSON parsing remains explicitly out of scope until Task 10. The DSL's post-parse protections do not claim to defend the server from an arbitrarily large HTTP body.

## Error contract

Resource-limit errors are typed and identify the boundary, configured fixed limit, and the observed-at-least byte count. They do not contain unbounded user input. Limit exceedance is a rejection, never a missing value and never an actionable result.

## Test plan

### Sale authorization

- Coordinator rejects a repository-loaded same-portfolio normal position absent from `source_position_ids`.
- Coordinator rejects a closed source and retains prior recommendation state.
- Sizing rejects a non-source, closed source, protected role, protected ID, non-positive quantity, and oversale.
- State-machine action events reject missing, non-source, closed, protected-role, and protected-ID positions; a valid source remains accepted.

### Basis points

- `0.3`, `0.3000`, and `0.30000` normalize to 3,000 bp and one canonical configuration representation.
- `0.30001`, non-finite values, zero, and values above one are rejected.
- 30/30/40 and 33.33/33.33/33.34 are accepted; 33.33/33.33/33.33 is rejected.
- Weight conversion and total validation are stable under deliberately reduced Decimal context precision.

### Resource limits

- UTF-8 multibyte strings are counted in bytes, with exact-boundary acceptance and one-byte-over rejection.
- Expression and complete ruleset checks stop at 128 KiB without materializing an oversized payload.
- List nesting consumes the same depth budget as mapping nesting.
- Oversized `RuleInput`, `RuleEvaluation`, and `StrategyRun.outputs` are rejected through typed errors.
- Encoder tests cover key/punctuation/escape accounting, cycles, non-string keys, unsupported values, and NaN/Infinity rejection.
- Documentation examples parse against the actual registry.

## Delivery and non-goals

The implementation will be split into independent remediation commits for sale authorization, allocation basis points, and DSL/audit bounds. Each change will be tested locally and through the existing CI matrix, then pushed to Draft PR #2. Task 10, HTTP request handling, automatic orders, broker integration, live market data, notifications, and position mutation remain out of scope.
