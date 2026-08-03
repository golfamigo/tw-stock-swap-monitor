"""Code-defined states, events, and validated guard outcomes for recommendations."""

from enum import StrEnum

from app.domain.errors import DomainError


class RecommendationState(StrEnum):
    """Finite recommendation states; none represents brokerage holdings or orders."""

    IDLE = "IDLE"
    WATCHING = "WATCHING"
    NEAR_TRIGGER = "NEAR_TRIGGER"
    ACTION_PENDING = "ACTION_PENDING"
    ACTION_NOTIFIED = "ACTION_NOTIFIED"
    DATA_DEGRADED = "DATA_DEGRADED"
    INVALIDATED = "INVALIDATED"
    PARTIALLY_EXECUTED = "PARTIALLY_EXECUTED"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    ROTATION_COMPLETED = "ROTATION_COMPLETED"
    PAUSED = "PAUSED"


class RecommendationEvent(StrEnum):
    """The only lifecycle inputs accepted by the recommendation state machine."""

    PLAN_ACTIVATED = "PLAN_ACTIVATED"
    DATA_DEGRADED = "DATA_DEGRADED"
    DATA_RECOVERED = "DATA_RECOVERED"
    NEAR_SIGNAL = "NEAR_SIGNAL"
    ACTION_SIGNAL = "ACTION_SIGNAL"
    DECISION_VALIDATED = "DECISION_VALIDATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    SIGNAL_INVALIDATED = "SIGNAL_INVALIDATED"
    EXECUTION_CONFIRMED = "EXECUTION_CONFIRMED"
    NEXT_STAGE_ELIGIBLE = "NEXT_STAGE_ELIGIBLE"
    RESUME_REMAINING_STAGES = "RESUME_REMAINING_STAGES"
    ALL_STAGES_DONE = "ALL_STAGES_DONE"
    PLAN_PAUSED = "PLAN_PAUSED"
    PLAN_RESUMED = "PLAN_RESUMED"


class DataOutcome(StrEnum):
    """Validated market-data quality relevant to a recommendation transition."""

    UNKNOWN = "UNKNOWN"
    DEGRADED = "DEGRADED"
    FRESH_COMPLETE = "FRESH_COMPLETE"
    FRESH_FULL_EVALUATION = "FRESH_FULL_EVALUATION"


class RuleOutcome(StrEnum):
    """Validated rule evidence, never raw provider or LLM output."""

    NOT_EVALUATED = "NOT_EVALUATED"
    FAILED = "FAILED"
    PASSED = "PASSED"
    FRESH_FULL_EVALUATION = "FRESH_FULL_EVALUATION"


class SizingOutcome(StrEnum):
    """Validated deterministic sizing outcome required before notification intent."""

    NOT_VALIDATED = "NOT_VALIDATED"
    FAILED = "FAILED"
    PASSED = "PASSED"


class ConfirmationOutcome(StrEnum):
    """Recorded confirmation state without mutating execution or position entities."""

    PARTIAL = "PARTIAL"
    STAGE_COMPLETED = "STAGE_COMPLETED"


class TransitionNotAllowed(DomainError):
    """The requested recommendation edge or its validated guard was not allowed."""
