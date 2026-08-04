# Task 9 Safety-Boundary Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close unauthorized sale-source, allocation-precision, and DSL/audit resource-limit gaps without changing configuration canonical v1 or starting Task 10.

**Architecture:** Keep authorization in the pure domain and enforce it independently in coordinator, sizing, and transitions. Normalize only `SizingStage` allocation weights into integer basis points while retaining configuration snapshots' current Decimal canonical v1 contract. Add a pure bounded audit/evidence serialization boundary: it performs iterative structural validation before recursive freezing, and streams canonical JSON byte measurement without materializing an oversized payload.

**Tech Stack:** Python 3.12, stdlib `Decimal`/`json`, FastAPI project domain layer, Pydantic v2 configuration snapshots, pytest, Ruff, mypy, GitHub Actions Ubuntu/Windows/PostgreSQL jobs.

---

## Constraints that every task must preserve

- Do not alter `app.application.configuration.CANONICAL_FORMAT_VERSION`, `canonical_json`, or `canonical_content_hash`; configuration canonical v1 retains NFC normalization, custom scalar encoding, byte-sorted keys, version prefix, zero-byte delimiter, and SHA-256 contract.
- A sizing stage's source configuration remains a Decimal. `allocation_basis_points` is the sole post-construction domain authority; the Decimal public view is derived from it.
- Do not mutate a `Position`, send a notification, place an order, access a broker, or call a live provider.
- Do not start Task 10 or add HTTP body handling. Document that pre-JSON HTTP body limits remain Task 10 work.
- Preserve every existing commit. Create the three remediation commits below; do not amend, squash, or rebase.

## File map

| File | Change responsibility |
| --- | --- |
| `app/domain/errors.py` | Typed sale-source and bounded-audit evidence errors. |
| `app/domain/invariants.py` | Pure source-only and quantity-aware rotation-sale invariants. |
| `app/domain/entities.py` | StrategyRun preflight before freeze and 256 KiB output measurement. |
| `app/domain/evidence_payload.py` | New stdlib-only structural preflight and streaming persistence-shaped canonical byte counter. |
| `app/services/rotation_run.py` | Carry typed sizing sale quantity into `TransitionRequest`. |
| `app/application/run_coordinator.py` | Validate repository-loaded sale source with the source invariant. |
| `app/state_machine/machine.py` | Source-ID and state-event quantity guard enforcement. |
| `app/sizing/base.py` | Basis-point constructor normalization and compatible Decimal view. |
| `app/sizing/engine.py` | Full sale invariant and exact fraction derived from bp. |
| `app/rules/schema.py` | Shared DSL text/identifier limits and typed limit errors. |
| `app/rules/parser.py` | Incremental expression/ruleset byte measurement and list-depth correction. |
| `app/rules/engine.py`, `app/rules/evidence.py` | RuleInput/RuleEvaluation limits and runtime-string checks. |
| `app/persistence/evidence.py` | Reuse the domain evidence serialization contract so SQL JSON and domain measurement match. |
| `docs/rule-dsl.md` | Limit table, error semantics, real registry-path examples, Task 10 boundary. |
| `tests/unit/domain/test_invariants.py` | Pure sale-source and quantity invariant regression tests. |
| `tests/unit/domain/test_ownership.py` | StrategyRun structural and byte-bound tests. |
| `tests/unit/domain/test_evidence_payload.py` | New streaming encoder/preflight tests. |
| `tests/unit/sizing/test_engine.py` | Basis-point normalization and full sale-authorization tests. |
| `tests/unit/state_machine/test_transitions.py` | Event-specific source/quantity transition contracts. |
| `tests/integration/test_coordinator_safety_hardening.py` | Repository-authoritative ordinary, protected, and closed source cases. |
| `tests/unit/rules/test_resource_bounds.py`, `tests/unit/rules/test_validation.py` | DSL string, expression/ruleset, evidence, and list-depth regressions. |

## Task 1: Restrict every recommendation to an authorized open source position

**Files:**
- Modify: `app/domain/errors.py`
- Modify: `app/domain/invariants.py`
- Modify: `app/application/run_coordinator.py`
- Modify: `app/services/rotation_run.py`
- Modify: `app/state_machine/machine.py`
- Modify: `app/sizing/engine.py`
- Test: `tests/unit/domain/test_invariants.py`
- Test: `tests/unit/state_machine/test_transitions.py`
- Test: `tests/unit/sizing/test_engine.py`
- Test: `tests/integration/test_coordinator_safety_hardening.py`

- [ ] **Step 1: Add failing pure-domain tests for source authorization and quantity-aware sale authorization.**

```python
with pytest.raises(UnauthorizedRotationSourceError, match="source"):
    ensure_authorized_rotation_source(plan, ordinary_same_portfolio_position)

with pytest.raises(PositionNotOpenError):
    ensure_authorized_rotation_source(plan, closed_listed_source)

with pytest.raises(NonPositiveSaleQuantityError):
    ensure_authorized_rotation_sale(plan, listed_open_source, Quantity(Decimal("0")))

with pytest.raises(SaleQuantityExceedsPositionError):
    ensure_authorized_rotation_sale(plan, listed_open_source, Quantity(Decimal("11")))
```

Run: `python -m pytest tests/unit/domain/test_invariants.py -q`  
Expected: FAIL because the two source-specific invariants and typed zero-quantity error do not exist.

- [ ] **Step 2: Implement the two pure invariants and typed errors without weakening generic reference checks.**

```python
def ensure_authorized_rotation_source(plan: RotationPlan, position: Position) -> None:
    if position.position_id not in plan.source_position_ids:
        raise UnauthorizedRotationSourceError("position is not an authorized rotation source")
    if position.portfolio_id != plan.portfolio_id:
        raise UnauthorizedRotationSourceError("source position is outside the plan portfolio")
    if position.status is not PositionStatus.OPEN:
        raise PositionNotOpenError("only OPEN source positions are eligible for sale")
    if (
        position.role is PositionRole.PROTECTED_CORE
        or position.position_id in plan.protected_position_ids
    ):
        raise ProtectedPositionSaleError("protected positions are never eligible for sale")

def ensure_authorized_rotation_sale(
    plan: RotationPlan, position: Position, sale_quantity: Quantity
) -> None:
    ensure_authorized_rotation_source(plan, position)
    if sale_quantity.value <= Decimal("0"):
        raise NonPositiveSaleQuantityError("sale quantity must be positive")
    if sale_quantity.value > position.quantity.value:
        raise SaleQuantityExceedsPositionError("sale quantity exceeds open position quantity")
```

Keep `ensure_plan_reference_is_authorized` generic: candidate groups and protected references still need its existing ownership behavior. Do not make `Quantity` reject zero globally; it remains valid for non-sale calculations and zero purchase stages.

- [ ] **Step 3: Add failing boundary tests before changing application code.**

```python
# coordinator: a same-portfolio normal position absent from source_position_ids
assert outcome.disposition is RunDisposition.FAILED
assert persisted_state.state is RecommendationState.NEAR_TRIGGER

# state machine: action signal rejects an ordinary non-source and a CLOSED listed source
with pytest.raises(TransitionNotAllowed):
    machine.transition(request)

# sizing: a protected ID, a protected role, a closed source, a non-source, zero sale,
# and oversale all raise their typed domain error.
```

Use a plan with one listed OPEN source and a distinct same-portfolio ordinary position. Keep the evaluator input limited to `sale_source_position_id`; the test must prove the coordinator resolves the authoritative repository position instead of accepting a caller object.

- [ ] **Step 4: Thread source IDs and typed sale quantity through the transition contract.**

```python
@dataclass(frozen=True, slots=True)
class TransitionRequest:
    # existing state, event, guards fields
    plan: RotationPlan
    sale_source_position: Position | None = None
    sale_quantity: Quantity | None = None

def _reject_unauthorized_sale_source(self, request: TransitionRequest) -> None:
    if request.event not in self._ACTION_OR_SALE_EVENTS:
        return
    position = request.sale_source_position
    if position is None:
        raise TransitionNotAllowed("transition requires an authoritative sale source position")
    ensure_authorized_rotation_source(request.plan, position)
    if (
        request.event is RecommendationEvent.DECISION_VALIDATED
        and request.guards.sizing_outcome is SizingOutcome.PASSED
    ):
        if request.sale_quantity is None:
            raise TransitionNotAllowed("validated sizing requires an authoritative sale quantity")
        ensure_authorized_rotation_sale(request.plan, position, request.sale_quantity)
```

Do not fabricate a `Quantity` for `ACTION_SIGNAL`. `NEXT_STAGE_ELIGIBLE` only opens the next recommendation stage, so it requires the authorized source but no quantity. `DECISION_VALIDATED` with `SizingOutcome.PASSED` requires a non-`None` `Quantity` obtained from typed sizing evidence; the request constructor rejects a quantity on unrelated events if that would make evidence ambiguous.

Pass `plan` and the typed quantity from `RotationEvaluation` through `RotationRunService.build_strategy_run`. Replace the existing separate `plan_portfolio_id` and `protected_position_ids` request fields with the immutable plan, so the state machine calls the same source and sale invariants as the other two boundaries. Add `sale_quantity: Quantity | None` to `RotationEvaluation`, validate its type, and only permit it for sizing-passed decision validation. The coordinator calls `ensure_authorized_rotation_source(plan, position)` immediately after `PositionRepository.get` and before `build_strategy_run`. The sizing engine replaces its current composition of generic checks with `ensure_authorized_rotation_sale`.

- [ ] **Step 5: Audit all existing `Quantity(0)` uses and retain only non-sale semantics.**

Run: `rg -n "Quantity\\(" app tests -g '*.py'`  

Classify every zero call site:

```text
Allowed: `Quantity(Decimal("0"))` created for a non-actionable purchase stage or a generic calculation result.
Rejected: `SizingRequest.source_sale_quantity=Quantity(Decimal("0"))` and any
TransitionRequest.sale_quantity=Quantity(Decimal("0"))` for DECISION_VALIDATED/PASSED.
```

Do not replace generic `Quantity` with a positive-only type. Add an explicit sizing and transition regression for zero sale quantity, and keep existing no-purchase stage tests passing.

- [ ] **Step 6: Run focused checks and create the first independent remediation commit.**

Run:

```powershell
python -m pytest tests/unit/domain/test_invariants.py tests/unit/sizing/test_engine.py tests/unit/state_machine/test_transitions.py tests/integration/test_coordinator_safety_hardening.py
python -m mypy app tests
python -m ruff check app/domain app/application app/services app/state_machine app/sizing tests/unit/domain tests/unit/sizing tests/unit/state_machine tests/integration/test_coordinator_safety_hardening.py
python -m ruff format --check app/domain app/application app/services app/state_machine app/sizing tests/unit/domain tests/unit/sizing tests/unit/state_machine tests/integration/test_coordinator_safety_hardening.py
git diff --check
```

Expected: all selected tests and static checks pass.

```powershell
git add -- app/domain/errors.py app/domain/invariants.py app/application/run_coordinator.py app/services/rotation_run.py app/state_machine/machine.py app/sizing/engine.py tests/unit/domain/test_invariants.py tests/unit/sizing/test_engine.py tests/unit/state_machine/test_transitions.py tests/integration/test_coordinator_safety_hardening.py
git commit -m "fix: restrict rotation sale sources"
```

## Task 2: Normalize staged allocations to exact basis points

**Files:**
- Modify: `app/sizing/base.py`
- Modify: `app/sizing/engine.py`
- Test: `tests/unit/sizing/test_engine.py`
- Test: `tests/unit/config/test_resolution.py`

- [ ] **Step 1: Add failing constructor and configuration-total tests.**

```python
assert SizingStage("first", Decimal("0.30000")).allocation_basis_points == 3000
assert SizingStage("first", Decimal("0.3")).allocation_weight == Decimal("0.3000")

with pytest.raises(SizingError, match="basis points"):
    SizingStage("first", Decimal("0.30001"))

assert SizingConfiguration(stages=(bp(3333), bp(3333), bp(3334)), ...)
with pytest.raises(SizingError, match="10,000"):
    SizingConfiguration(stages=(bp(3333), bp(3333), bp(3333)), ...)
```

Set a deliberately small Decimal context around construction and sizing to prove no ambient rounding produces a different bp result. Add a configuration-canonicalization regression that `Decimal("0.3")` and `Decimal("0.3000")` still produce the same existing v1 JSON/hash; do not change configuration production code.

Run: `python -m pytest tests/unit/sizing/test_engine.py tests/unit/config/test_resolution.py -q`  
Expected: FAIL because stages currently retain raw Decimal weights and sum raw Decimals.

- [ ] **Step 2: Make basis points the only stored stage authority.**

```python
@dataclass(frozen=True, slots=True)
class SizingStage:
    stage_id: str
    allocation_basis_points: int

    def __init__(self, stage_id: str, allocation_weight: Decimal) -> None:
        # Validate exact Decimal input and set only allocation_basis_points.

    @property
    def allocation_weight(self) -> Decimal:
        return Decimal(self.allocation_basis_points).scaleb(-4)
```

Implement the dataclass with `init=False` and the exact custom initializer shown above, preserving both current positional and keyword calls such as `SizingStage("first", Decimal("0.3"))` and `SizingStage(stage_id="first", allocation_weight=Decimal("0.3"))`. Reject blank or more-than-256-byte UTF-8 `stage_id` values. Derive the integer without ambient Decimal arithmetic: inspect `Decimal.as_tuple()`, remove only trailing zero coefficient digits, and reject values whose resulting scale cannot represent an integer number of basis points. Do not retain raw Decimal input as a field. The public `allocation_weight` view is always derived from `allocation_basis_points` and renders four fractional places.

`SizingConfiguration.__post_init__` sums `allocation_basis_points` as Python integers and requires exactly `10_000`. `DeterministicSizingEngine` uses `stage.allocation_weight`, which is now the derived exact view, for target calculations.

- [ ] **Step 3: Prove configuration snapshots remain on canonical v1.**

Do not add bp fields to `LayerPatchSchema`, `ResolvedConfigurationSchema`, `canonical_json`, `canonical_content_hash`, persistence configuration codecs, or migrations. Add a regression that compares the current v1 canonical JSON/hash for semantically equivalent Decimal configuration values, and assert `CANONICAL_FORMAT_VERSION == "1"` in the test fixture.

- [ ] **Step 4: Run focused checks and create the second independent remediation commit.**

Run:

```powershell
python -m pytest tests/unit/sizing/test_engine.py tests/unit/config/test_resolution.py
python -m mypy app tests
python -m ruff check app/sizing tests/unit/sizing tests/unit/config/test_resolution.py
python -m ruff format --check app/sizing tests/unit/sizing tests/unit/config/test_resolution.py
git diff --check
```

Expected: accepted 30/30/40 and 33.33/33.33/33.34 allocations retain exact totals; invalid precision and 9,999 bp fail.

```powershell
git add -- app/sizing/base.py app/sizing/engine.py tests/unit/sizing/test_engine.py tests/unit/config/test_resolution.py
git commit -m "fix: normalize sizing allocations to basis points"
```

## Task 3: Bound DSL and StrategyRun payloads before persistence

**Files:**
- Create: `app/domain/evidence_payload.py`
- Modify: `app/domain/errors.py`
- Modify: `app/domain/entities.py`
- Modify: `app/persistence/evidence.py`
- Modify: `app/rules/schema.py`
- Modify: `app/rules/parser.py`
- Modify: `app/rules/engine.py`
- Modify: `app/rules/evidence.py`
- Modify: `docs/rule-dsl.md`
- Test: `tests/unit/domain/test_evidence_payload.py`
- Test: `tests/unit/domain/test_ownership.py`
- Test: `tests/unit/rules/test_resource_bounds.py`
- Test: `tests/unit/rules/test_validation.py`

- [ ] **Step 1: Add failing byte-limit and structural-preflight tests.**

```python
assert measure_payload_bytes({"text": "é" * 8192}, limit=16 * 1024, boundary="test") > 16 * 1024
with pytest.raises(CanonicalJsonSizeLimitError, match="boundary=test"):
    measure_payload_bytes({"text": "é" * 8192}, limit=16 * 1024, boundary="test")

with pytest.raises(InvalidStrategyRunEvidenceError, match="depth"):
    StrategyRun(..., outputs=nested_mapping_or_list(33))

with pytest.raises(InvalidStrategyRunEvidenceError, match="256 KiB"):
    StrategyRun(..., outputs=oversized_outputs)
```

Cover exact-boundary acceptance and one-byte-over rejection for UTF-8 literals, identifiers, full expressions, full rulesets, `RuleInput`, `RuleEvaluation`, and `StrategyRun.outputs`. Add lists nested 33 levels to prove lists advance the raw transport depth exactly like mappings. Include cycles, a non-string map key, `float("nan")`, `float("inf")`, a non-finite/unbounded Decimal, an unsupported object, and a timezone-naive datetime in preflight coverage.

Run: `python -m pytest tests/unit/domain/test_evidence_payload.py tests/unit/domain/test_ownership.py tests/unit/rules/test_resource_bounds.py tests/unit/rules/test_validation.py -q`  
Expected: FAIL because no common preflight/counter or string/ruleset limits exist.

- [ ] **Step 2: Create one pure streaming evidence-payload boundary.**

In `app/domain/evidence_payload.py`, define fixed constants and typed errors:

```python
MAX_IDENTIFIER_UTF8_BYTES = 256
MAX_STRING_UTF8_BYTES = 16 * 1024
MAX_STRATEGY_RUN_OUTPUT_UTF8_BYTES = 256 * 1024
MAX_STRATEGY_RUN_OUTPUT_DEPTH = 32
MAX_STRATEGY_RUN_OUTPUT_NODES = 8_192

class CanonicalJsonSizeLimitError(InvalidStrategyRunEvidenceError):
    def __init__(self, *, boundary: str, limit_bytes: int, observed_at_least_bytes: int) -> None: ...

def preflight_evidence_payload(value: object) -> None: ...
def measure_encoded_evidence_bytes(value: object, *, limit_bytes: int, boundary: str) -> int: ...
```

Preflight iteratively visits mappings, lists, and tuples before `_freeze_evidence`. It counts each container/scalar node, increments depth for both mappings and sequences, tracks only active container IDs for cycles, checks string/key byte limits via `len(value.encode("utf-8"))`, and rejects unsupported values before encoding. It preserves the existing supported evidence scalar set: `None`, `bool`, `int`, `str`, finite/bounded `Decimal`, `UUID`, and timezone-aware `datetime`.

Stream the same reversible envelope shape currently used by `app.persistence.evidence.encode_evidence` directly into a single `json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).iterencode(...)`-based chunk counter. Never concatenate all chunks. Counter errors include only boundary, limit, and observed-at-least bytes. Move or re-export the shared codec contract so the persistence mapper writes exactly the measured encoding; add a byte-for-byte test comparing streamed output to persistence encoding for supported nested values.

- [ ] **Step 3: Apply StrategyRun preflight before recursive freeze.**

```python
if not isinstance(self.outputs, Mapping):
    raise InvalidStrategyRunEvidenceError("outputs must be a mapping")
preflight_evidence_payload(self.outputs)
frozen_outputs = _freeze_evidence(self.outputs, active_container_ids=set())
measure_encoded_evidence_bytes(
    frozen_outputs,
    limit_bytes=MAX_STRATEGY_RUN_OUTPUT_UTF8_BYTES,
    boundary="StrategyRun.outputs",
)
object.__setattr__(self, "outputs", frozen_outputs)
```

This must execute before any recursive freeze and before SQL/in-memory repositories. It does not change `app.application.configuration.canonical_json` or configuration hash behavior.

- [ ] **Step 4: Add DSL-specific limits at all transport and evidence boundaries.**

In `app/rules/schema.py`, define and export typed DSL resource errors plus these limits:

```python
class RuleTextResourceLimitError(RuleSafetyError): ...
class RuleEvidenceResourceLimitError(RuleEvaluationError): ...

MAX_DSL_EXPRESSION_UTF8_BYTES = 128 * 1024
MAX_DSL_RULESET_UTF8_BYTES = 128 * 1024
MAX_RULE_INPUT_UTF8_BYTES = 128 * 1024
MAX_RULE_EVALUATION_UTF8_BYTES = 128 * 1024
```

Use the incrementing JSON counter in `validate_expression_transport` before Draft 2020-12 validation and in `parse_rules` for the complete raw ruleset. Preserve the raw limits of depth 32/nodes 512 and increment list child depth with `depth + 1`. Enforce 16 KiB for every literal/path/operator/rule-ID string before JSON Schema can traverse it, and 256 bytes for rule IDs.

`RuleInput.__post_init__` validates this canonical typed payload after registry path/type normalization:

```python
{"values": {path: evidence_value_payload(value) for path, value in normalized_values.items()}}
```

`EvidenceValue` rejects runtime strings above 16 KiB; `RuleEvidence` validates rule IDs; and `RuleEvaluation.__post_init__` measures this complete evidence payload after tuple freezing:

```python
{
    "failed_rule_ids": list(self.failed_rule_ids),
    "matched_rule_ids": list(self.matched_rule_ids),
    "missing_rule_ids": list(self.missing_rule_ids),
    "rules": [
        {
            "evaluated_at": evidence_value_payload(record.evaluated_at),
            "evaluated_paths": [
                {"path": path.path, "value": evidence_value_payload(path.value)}
                for path in record.evaluated_paths
            ],
            "operands": [evidence_value_payload(value) for value in record.operands],
            "rule_id": record.rule_id,
            "status": record.status.value,
        }
        for record in self.rule_evidence
    ],
    "status": self.status.value,
    "threshold": evidence_value_payload(self.threshold),
    "total_score": evidence_value_payload(self.total_score),
}
```

`evidence_value_payload` is one tested helper in `app.rules.evidence` that returns an explicit mapping with `kind`, `is_missing`, and a canonically encoded `value`; it never serializes a dataclass implicitly. Validate `Rule.rule_id` and `SizingStage.stage_id` at 256 UTF-8 bytes, validate fixed registry paths at the same limit, and in `RuleInput` validate identifier-valued registry fields (`market.provider`, `market.snapshot_id`) at 256 bytes while ordinary string evidence remains at 16 KiB. Enum-backed evaluation purpose and recommendation state values are code-defined and do not accept arbitrary caller strings. There is no factor-ID domain type in M0/M1; when it is introduced it must use the same 256-byte identifier validator.

Transport and literal overages raise `RuleTextResourceLimitError`; RuleInput and RuleEvaluation overages raise `RuleEvidenceResourceLimitError`; each wraps only safe boundary/limit/observed-at-least metadata. The outcome on every limit exceedance is never `MISSING`, `False`, zero, truncation, compression, or an actionable rule result.

- [ ] **Step 5: Correct documentation against the fixed registry.**

Update `docs/rule-dsl.md` with the complete UTF-8 table, incremental/rejection behavior, mapping/list depth rule, and Task 10 pre-parse HTTP-body non-goal. Replace the stale example paths `source.scans_below_vwap` and `source.required_below_scans` with registered paths such as `source.last_price`, `source.session_vwap`, and `source.same_time_volume_ratio`. Add a parser test that loads the documented JSON example and verifies it parses under `M0M1EvidenceRegistry.schema()`.

- [ ] **Step 6: Run focused checks and create the third independent remediation commit.**

Run:

```powershell
python -m pytest tests/unit/domain/test_evidence_payload.py tests/unit/domain/test_ownership.py tests/unit/rules/test_resource_bounds.py tests/unit/rules/test_validation.py
python -m mypy app tests
python -m ruff check app/domain app/persistence app/rules tests/unit/domain tests/unit/rules
python -m ruff format --check app/domain app/persistence app/rules tests/unit/domain tests/unit/rules
git diff --check
```

Expected: all payload boundaries reject safely without retaining or emitting unbounded content.

```powershell
git add -- app/domain/evidence_payload.py app/domain/errors.py app/domain/entities.py app/persistence/evidence.py app/rules/schema.py app/rules/parser.py app/rules/engine.py app/rules/evidence.py docs/rule-dsl.md tests/unit/domain/test_evidence_payload.py tests/unit/domain/test_ownership.py tests/unit/rules/test_resource_bounds.py tests/unit/rules/test_validation.py
git commit -m "fix: bound rule and audit evidence payloads"
```

## Task 4: Final verification, publication, and Task 9 checkpoint

**Files:** No code changes unless a verification failure identifies a defect; any defect belongs in a new focused remediation commit, never an amendment of Tasks 1–3.

- [ ] **Step 1: Run the complete local verification suite from the remediation head.**

```powershell
python -m pytest
python -m mypy
python -m ruff check .
python -m ruff format --check .
git diff --check
git status --short
```

Expected: pytest, full mypy (including tests), Ruff lint, formatting, and diff check pass; status is clean before publication.

- [ ] **Step 2: Check the committed scope before publishing.**

```powershell
git log --oneline 4b8a2ee..HEAD
git diff --check 4b8a2ee..HEAD
git status --short
```

Expected: exactly these independent commits in order:

```text
fix: restrict rotation sale sources
fix: normalize sizing allocations to basis points
fix: bound rule and audit evidence payloads
```

Inspect staged/committed paths for `.env`, `.local.env`, secrets, credentials, absolute local paths, Python caches, or generated artifacts. Do not publish any of them.

- [ ] **Step 3: Push only after the local suite is green, then restore GitHub identity.**

```powershell
gh auth switch --user golfamigo
gh api user --jq .login
git push origin feature/m0-m1-implementation
gh auth switch --user jctixtw-star
gh api user --jq .login
```

Expected: the two identity reads report `golfamigo` immediately before the write and `jctixtw-star` immediately after it. Update the existing Draft PR #2 only; do not merge, rebase, squash, or begin Task 10.

- [ ] **Step 4: Await the existing four-job CI matrix.**

Required successful jobs:

```text
Ubuntu
Windows
PostgreSQL persistence contracts
Runtime PostgreSQL smoke
```

If a job fails, report the failed job and concise failure evidence. Do not start Task 10. If all jobs succeed, report the full head SHA, the three remediation commits, local summaries, CI run URL, and clean `git status --short`, then stop at the Task 9 checkpoint for review.

## Explicit non-goals

- No FastAPI request-size middleware or reverse-proxy body limit; this must be implemented before JSON parsing in Task 10.
- No database migration, broker connector, automatic order, live quote, LLM call, notification send, or position mutation.
- No configuration canonical v2, hash recalculation, or representation change from Decimal snapshot values to basis points.
