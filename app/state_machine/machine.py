"""Pure transition table for recommendations, deliberately separate from execution."""

from dataclasses import dataclass

from app.domain.entities import Position, RotationPlan
from app.domain.invariants import (
    ensure_authorized_rotation_sale,
    ensure_authorized_rotation_source,
)
from app.domain.values import Quantity
from app.state_machine.states import (
    ConfirmationOutcome,
    DataOutcome,
    RecommendationEvent,
    RecommendationState,
    RuleOutcome,
    SizingOutcome,
    TransitionNotAllowed,
)


@dataclass(frozen=True, slots=True)
class TransitionGuards:
    """Only validated rule, sizing, data, authorization, and confirmation outcomes."""

    plan_is_valid: bool = False
    data_outcome: DataOutcome = DataOutcome.UNKNOWN
    rule_outcome: RuleOutcome = RuleOutcome.NOT_EVALUATED
    sizing_outcome: SizingOutcome = SizingOutcome.NOT_VALIDATED
    authorization_is_valid: bool = False
    confirmation_is_valid: bool = False
    confirmation_outcome: ConfirmationOutcome = ConfirmationOutcome.PARTIAL


@dataclass(frozen=True, slots=True)
class TransitionRequest:
    """All immutable evidence required to request one recommendation transition."""

    current_state: RecommendationState
    event: RecommendationEvent
    guards: TransitionGuards
    plan: RotationPlan
    sale_source_position: Position | None = None
    sale_quantity: Quantity | None = None
    remaining_stages_halted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.current_state, RecommendationState):
            raise TypeError("current_state must be a RecommendationState")
        if not isinstance(self.event, RecommendationEvent):
            raise TypeError("event must be a RecommendationEvent")
        if not isinstance(self.guards, TransitionGuards):
            raise TypeError("guards must be TransitionGuards")
        if not isinstance(self.plan, RotationPlan):
            raise TypeError("plan must be a RotationPlan")
        if self.sale_source_position is not None and not isinstance(
            self.sale_source_position, Position
        ):
            raise TypeError("sale_source_position must be a Position or None")
        if self.sale_quantity is not None and not isinstance(self.sale_quantity, Quantity):
            raise TypeError("sale_quantity must be a Quantity or None")
        if self.sale_quantity is not None and (
            self.event is not RecommendationEvent.DECISION_VALIDATED
            or self.guards.sizing_outcome is not SizingOutcome.PASSED
        ):
            raise ValueError("sale_quantity only applies to passed sizing validation")
        if not isinstance(self.remaining_stages_halted, bool):
            raise TypeError("remaining_stages_halted must be a Boolean")


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """Recommendation-only outcome, with explicit negative execution/position effects."""

    previous_state: RecommendationState
    next_state: RecommendationState
    remaining_stages_halted: bool
    is_noop: bool = False
    notification_intent_recorded: bool = False
    requires_fresh_full_evaluation: bool = False
    execution_status_changed: bool = False
    positions_changed: bool = False


class StateMachine:
    """Apply the finite transition table while prohibiting position or execution mutation."""

    _ACTION_OR_SALE_EVENTS = frozenset(
        {
            RecommendationEvent.ACTION_SIGNAL,
            RecommendationEvent.DECISION_VALIDATED,
            RecommendationEvent.NEXT_STAGE_ELIGIBLE,
        }
    )

    def transition(self, request: TransitionRequest) -> TransitionResult:
        """Return the next recommendation state or raise a typed safety error."""

        self._reject_unauthorized_sale_source(request)
        state = request.current_state
        event = request.event
        guards = request.guards

        if event is RecommendationEvent.PLAN_PAUSED:
            self._require(guards.authorization_is_valid, "authorized pause")
            if state is RecommendationState.PAUSED:
                return self._noop(request)
            if state is RecommendationState.ROTATION_COMPLETED:
                raise TransitionNotAllowed("terminal recommendation cannot be paused")
            return self._result(request, RecommendationState.PAUSED)
        if event is RecommendationEvent.PLAN_RESUMED:
            self._require(guards.authorization_is_valid, "authorized resume")
            self._require(
                guards.rule_outcome is RuleOutcome.FRESH_FULL_EVALUATION,
                "fresh full evaluation",
            )
            if state is RecommendationState.WATCHING:
                return self._noop(request)
            self._require(state is RecommendationState.PAUSED, "paused recommendation")
            return self._result(
                request,
                RecommendationState.WATCHING,
                requires_fresh_full_evaluation=True,
            )

        if event is RecommendationEvent.DATA_DEGRADED:
            self._require(guards.data_outcome is DataOutcome.DEGRADED, "degraded data evidence")
            if state is RecommendationState.DATA_DEGRADED:
                return self._noop(request)
            if state is RecommendationState.PARTIALLY_EXECUTED:
                return self._result(
                    request, RecommendationState.WAITING_CONFIRMATION, remaining_stages_halted=True
                )
            if state in {
                RecommendationState.WATCHING,
                RecommendationState.NEAR_TRIGGER,
                RecommendationState.ACTION_PENDING,
                RecommendationState.ACTION_NOTIFIED,
                RecommendationState.STAGE_COMPLETED,
            }:
                return self._result(request, RecommendationState.DATA_DEGRADED)
            if state is RecommendationState.WAITING_CONFIRMATION:
                return self._noop(request)
            raise TransitionNotAllowed(
                "data degradation is not valid for this recommendation state"
            )

        if event is RecommendationEvent.DATA_RECOVERED:
            self._require(
                guards.data_outcome is DataOutcome.FRESH_FULL_EVALUATION,
                "fresh full evaluation",
            )
            if state is RecommendationState.WATCHING:
                return self._noop(request)
            self._require(state is RecommendationState.DATA_DEGRADED, "degraded recommendation")
            if request.remaining_stages_halted:
                return self._result(
                    request,
                    RecommendationState.WAITING_CONFIRMATION,
                    remaining_stages_halted=True,
                    requires_fresh_full_evaluation=True,
                )
            return self._result(
                request,
                RecommendationState.WATCHING,
                requires_fresh_full_evaluation=True,
            )

        if event is RecommendationEvent.PLAN_ACTIVATED:
            self._require(guards.plan_is_valid, "valid plan")
            if state is RecommendationState.WATCHING:
                return self._noop(request)
            self._require(state is RecommendationState.IDLE, "idle recommendation")
            return self._result(request, RecommendationState.WATCHING)

        if event is RecommendationEvent.NEAR_SIGNAL:
            self._require(guards.rule_outcome is RuleOutcome.PASSED, "validated near rule")
            if state is RecommendationState.NEAR_TRIGGER:
                return self._noop(request)
            self._require(state is RecommendationState.WATCHING, "watching recommendation")
            return self._result(request, RecommendationState.NEAR_TRIGGER)

        if event is RecommendationEvent.ACTION_SIGNAL:
            self._require(guards.rule_outcome is RuleOutcome.PASSED, "validated action rule")
            if state is RecommendationState.ACTION_PENDING:
                return self._noop(request)
            self._require(state is RecommendationState.NEAR_TRIGGER, "near-trigger recommendation")
            return self._result(request, RecommendationState.ACTION_PENDING)

        if event is RecommendationEvent.DECISION_VALIDATED:
            self._require(guards.sizing_outcome is SizingOutcome.PASSED, "validated sizing")
            if state is RecommendationState.ACTION_NOTIFIED:
                return self._noop(request)
            self._require(state is RecommendationState.ACTION_PENDING, "pending recommendation")
            return self._result(
                request, RecommendationState.ACTION_NOTIFIED, notification_intent_recorded=True
            )

        if event is RecommendationEvent.VALIDATION_FAILED:
            self._require(state is RecommendationState.ACTION_PENDING, "pending recommendation")
            return self._result(request, RecommendationState.WATCHING)

        if event is RecommendationEvent.SIGNAL_INVALIDATED:
            self._require(guards.rule_outcome is RuleOutcome.PASSED, "validated invalidation rule")
            if state is RecommendationState.INVALIDATED:
                return self._noop(request)
            if state is RecommendationState.PARTIALLY_EXECUTED:
                return self._result(
                    request, RecommendationState.WAITING_CONFIRMATION, remaining_stages_halted=True
                )
            self._require(
                state
                in {
                    RecommendationState.WATCHING,
                    RecommendationState.NEAR_TRIGGER,
                    RecommendationState.ACTION_PENDING,
                    RecommendationState.ACTION_NOTIFIED,
                },
                "active recommendation",
            )
            return self._result(
                request, RecommendationState.INVALIDATED, remaining_stages_halted=True
            )

        if event is RecommendationEvent.EXECUTION_CONFIRMED:
            self._require(guards.confirmation_is_valid, "valid execution confirmation")
            if state in {
                RecommendationState.DATA_DEGRADED,
                RecommendationState.INVALIDATED,
                RecommendationState.WAITING_CONFIRMATION,
            }:
                return self._result(
                    request, RecommendationState.WAITING_CONFIRMATION, remaining_stages_halted=True
                )
            self._require(state is RecommendationState.ACTION_NOTIFIED, "notified recommendation")
            next_state = (
                RecommendationState.PARTIALLY_EXECUTED
                if guards.confirmation_outcome is ConfirmationOutcome.PARTIAL
                else RecommendationState.STAGE_COMPLETED
            )
            return self._result(request, next_state)

        if event is RecommendationEvent.NEXT_STAGE_ELIGIBLE:
            self._require(not request.remaining_stages_halted, "remaining stages are not halted")
            self._require(guards.rule_outcome is RuleOutcome.FRESH_FULL_EVALUATION, "fresh rule")
            if state is RecommendationState.ACTION_PENDING:
                return self._noop(request)
            self._require(state is RecommendationState.PARTIALLY_EXECUTED, "partial recommendation")
            return self._result(request, RecommendationState.ACTION_PENDING)

        if event is RecommendationEvent.RESUME_REMAINING_STAGES:
            self._require(guards.authorization_is_valid, "authorized resume")
            self._require(
                guards.rule_outcome is RuleOutcome.FRESH_FULL_EVALUATION,
                "fresh full evaluation",
            )
            if state is RecommendationState.WATCHING:
                return self._noop(request)
            self._require(
                state is RecommendationState.WAITING_CONFIRMATION
                and request.remaining_stages_halted,
                "halted recommendation awaiting confirmation",
            )
            return self._result(
                request,
                RecommendationState.WATCHING,
                remaining_stages_halted=False,
                requires_fresh_full_evaluation=True,
            )

        if event is RecommendationEvent.ALL_STAGES_DONE:
            if state is RecommendationState.ROTATION_COMPLETED:
                return self._noop(request)
            self._require(state is RecommendationState.STAGE_COMPLETED, "completed stage")
            return self._result(request, RecommendationState.ROTATION_COMPLETED)

        raise TransitionNotAllowed("unsupported recommendation event")

    @classmethod
    def _reject_unauthorized_sale_source(cls, request: TransitionRequest) -> None:
        if request.event not in cls._ACTION_OR_SALE_EVENTS:
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
                raise TransitionNotAllowed(
                    "validated sizing requires an authoritative sale quantity"
                )
            ensure_authorized_rotation_sale(request.plan, position, request.sale_quantity)

    @staticmethod
    def _require(condition: bool, description: str) -> None:
        if not condition:
            raise TransitionNotAllowed(f"transition requires {description}")

    @staticmethod
    def _result(
        request: TransitionRequest,
        next_state: RecommendationState,
        *,
        remaining_stages_halted: bool | None = None,
        is_noop: bool = False,
        notification_intent_recorded: bool = False,
        requires_fresh_full_evaluation: bool = False,
    ) -> TransitionResult:
        return TransitionResult(
            previous_state=request.current_state,
            next_state=next_state,
            remaining_stages_halted=(
                request.remaining_stages_halted
                if remaining_stages_halted is None
                else remaining_stages_halted
            ),
            is_noop=is_noop,
            notification_intent_recorded=notification_intent_recorded,
            requires_fresh_full_evaluation=requires_fresh_full_evaluation,
        )

    @classmethod
    def _noop(cls, request: TransitionRequest) -> TransitionResult:
        return cls._result(request, request.current_state, is_noop=True)
