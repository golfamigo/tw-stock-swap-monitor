# Rotation State Machine

## State dimensions

The state machine records a system recommendation. It does not represent brokerage execution. ExecutionStatus and actual Position quantities are separately persisted and only a future confirmation workflow may update positions. Recommendation invalidation never rolls back a confirmed transaction.

## Transition table

| Current state | Event | Guard | Next state | Side effect | Idempotency behavior |
| --- | --- | --- | --- | --- | --- |
| IDLE | PLAN_ACTIVATED | plan valid | WATCHING | record transition | same event is no-op |
| WATCHING | DATA_DEGRADED | data missing or stale | DATA_DEGRADED | retain evidence; prohibit action | update once per data snapshot |
| NEAR_TRIGGER | DATA_DEGRADED | data missing or stale | DATA_DEGRADED | expire near signal | same event is no-op |
| ACTION_PENDING | DATA_DEGRADED | data missing or stale | DATA_DEGRADED | cancel unsent action intent; prohibit action | same run is no-op |
| ACTION_NOTIFIED | DATA_DEGRADED | data missing or stale | DATA_DEGRADED | mark recommendation stale; no holding change | notification fingerprint is not resent |
| PARTIALLY_EXECUTED | DATA_DEGRADED | data missing or stale | WAITING_CONFIRMATION | halt remaining stages; retain execution status | same data snapshot is no-op |
| DATA_DEGRADED | DATA_RECOVERED | fresh validated data and no halted stages | WATCHING | require fresh full evaluation; never restore prior recommendation | same recovered snapshot is no-op |
| DATA_DEGRADED | DATA_RECOVERED | remaining stages halted | WAITING_CONFIRMATION | record fresh data availability; require explicit resume | same recovered snapshot is no-op |
| DATA_DEGRADED | EXECUTION_CONFIRMED | late valid confirmation of a stale notified decision | WAITING_CONFIRMATION | record execution; do not revive recommendation or stages | confirmation ID unique |
| WATCHING | NEAR_SIGNAL | rules meet configured near guard | NEAR_TRIGGER | persist evaluation | same run returns prior outcome |
| NEAR_TRIGGER | ACTION_SIGNAL | action guard and valid candidate exist | ACTION_PENDING | persist score and sizing | same run returns prior outcome |
| ACTION_PENDING | DECISION_VALIDATED | sizing and safety invariants pass | ACTION_NOTIFIED | emit notification intent only | fingerprint deduplicates intent |
| ACTION_PENDING | VALIDATION_FAILED | any invariant fails | WATCHING | record failure reason | no notification |
| ACTION_PENDING | SIGNAL_INVALIDATED | invalidation rule passes before execution | INVALIDATED | discard pending action; halt stages | same run no-op |
| ACTION_NOTIFIED | SIGNAL_INVALIDATED | invalidation rule passes before confirmation | INVALIDATED | expire notified recommendation; halt stages | same fingerprint is not resent |
| ACTION_NOTIFIED | EXECUTION_CONFIRMED | user confirms valid transaction | PARTIALLY_EXECUTED or STAGE_COMPLETED | future M5 updates positions | confirmation ID unique |
| PARTIALLY_EXECUTED | SIGNAL_INVALIDATED | invalidation rule or safety event passes | WAITING_CONFIRMATION | halt remaining stages; preserve confirmed execution | same run no-op |
| PARTIALLY_EXECUTED | NEXT_STAGE_ELIGIBLE | no halt and fresh rule evaluation passes | ACTION_PENDING | request next deterministic evaluation | stage/run key unique |
| WAITING_CONFIRMATION | EXECUTION_CONFIRMED | late valid user confirmation | WAITING_CONFIRMATION | record actual execution; do not resume stages | confirmation ID unique |
| WAITING_CONFIRMATION | RESUME_REMAINING_STAGES | authorized explicit resume plus fresh valid evaluation | WATCHING | clear halt and require new scan | repeated resume no-op |
| STAGE_COMPLETED | ALL_STAGES_DONE | final configured stage completed | ROTATION_COMPLETED | close recommendation lifecycle | terminal no-op |
| WATCHING | SIGNAL_INVALIDATED | configured invalidation rule passes | INVALIDATED | persist evidence | same run no-op |
| NEAR_TRIGGER | SIGNAL_INVALIDATED | configured invalidation rule passes | INVALIDATED | persist evidence | same run no-op |
| INVALIDATED | EXECUTION_CONFIRMED | late valid confirmation of an already executed transaction | WAITING_CONFIRMATION | record execution; do not revive recommendation | confirmation ID unique |
| any nonterminal | PLAN_PAUSED | authorized user action | PAUSED | audit pause | repeated pause no-op |
| PAUSED | PLAN_RESUMED | authorized user action and fresh scan required | WATCHING | audit resume | repeated resume no-op |

Invalid transitions are rejected with a typed TransitionNotAllowed error. Transition guards are events and validated rule/sizing outputs; the finite state set and allowable edges are code-level safety invariants.

## Invalidation and recovery semantics

ACTION_PENDING and ACTION_NOTIFIED may be invalidated before a user confirms execution. Their recommendations expire; they cannot initiate a later stage or be re-notified. DATA_DEGRADED may occur in every active recommendation state. Recovery always requires a new full evaluation and never restores the old action merely because freshness returns.

If execution is partial, invalidation or degraded data sends the recommendation to WAITING_CONFIRMATION with remaining_stages_halted true. Existing execution status and actual positions stay unchanged. Only an authorized explicit resume event combined with fresh validated data can clear the halt; it starts from WATCHING rather than the former action.

## Protected positions and notifications

Protected positions are excluded before rules run, rejected by SizingEngine input validation, and denied as sale sources by every transition guard. ACTION_NOTIFIED means only that a notification intent was recorded. It never implies delivery, user agreement, or holdings changed. A notification retry affects delivery state, not recommendation state.
