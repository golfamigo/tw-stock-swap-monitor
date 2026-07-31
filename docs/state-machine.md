# Rotation State Machine

## State dimensions

The state machine records a system recommendation. It does not represent brokerage execution. ExecutionStatus and actual Position quantities are separately persisted and only a future confirmation workflow may update positions.

| Current state | Event | Guard | Next state | Side effect | Idempotency behavior |
| --- | --- | --- | --- | --- | --- |
| IDLE | PLAN_ACTIVATED | plan valid | WATCHING | record transition | same event is no-op |
| WATCHING | DATA_DEGRADED | data missing or stale | DATA_DEGRADED | retain evidence; prohibit action | update once per data snapshot |
| DATA_DEGRADED | DATA_RECOVERED | validated fresh data | WATCHING | record recovery | same data snapshot is no-op |
| WATCHING | NEAR_SIGNAL | rules meet configured near guard | NEAR_TRIGGER | persist evaluation | same run returns prior outcome |
| NEAR_TRIGGER | ACTION_SIGNAL | action guard and valid candidate exist | ACTION_PENDING | persist score and sizing | same run returns prior outcome |
| ACTION_PENDING | DECISION_VALIDATED | sizing and safety invariants pass | ACTION_NOTIFIED | emit notification intent only | fingerprint deduplicates intent |
| ACTION_PENDING | VALIDATION_FAILED | any invariant fails | WATCHING | record failure reason | no notification |
| ACTION_NOTIFIED | EXECUTION_CONFIRMED | user confirms valid transaction | PARTIALLY_EXECUTED or STAGE_COMPLETED | future M5 updates positions | confirmation ID unique |
| PARTIALLY_EXECUTED | NEXT_STAGE_ELIGIBLE | stage rule passes | ACTION_PENDING | request next deterministic evaluation | stage/run key unique |
| STAGE_COMPLETED | ALL_STAGES_DONE | final configured stage completed | ROTATION_COMPLETED | close recommendation lifecycle | terminal no-op |
| WATCHING or NEAR_TRIGGER | SIGNAL_INVALIDATED | configured invalidation rule passes | INVALIDATED | persist evidence | same run no-op |
| any nonterminal | PLAN_PAUSED | authorized user action | PAUSED | audit pause | repeated pause no-op |
| PAUSED | PLAN_RESUMED | authorized user action | WATCHING | audit resume | repeated resume no-op |

Invalid transitions are rejected with a typed TransitionNotAllowed error. Transition guards are events and validated rule/sizing outputs; the finite state set and allowable edges are code-level safety invariants.

## Protected positions

Protected positions are excluded before rules run, rejected by SizingEngine input validation, and denied as sale sources by every transition guard. This three-layer defense remains even if a future LLM or a malformed API request recommends a sale.

## Notifications and execution

ACTION_NOTIFIED means only that a notification intent was recorded. It never implies a notification provider succeeded, a user agreed, or holdings changed. A notification retry affects delivery state, not rotation state. A future confirmed execution includes transaction data, validates it against the decision, and moves execution status independently.
