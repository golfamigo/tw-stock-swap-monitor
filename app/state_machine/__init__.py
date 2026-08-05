"""Finite recommendation lifecycle transitions with no execution side effects."""

from app.state_machine.machine import (
    StateMachine,
    TransitionGuards,
    TransitionRequest,
    TransitionResult,
)
from app.state_machine.states import RecommendationEvent, RecommendationState, TransitionNotAllowed

__all__ = [
    "RecommendationEvent",
    "RecommendationState",
    "StateMachine",
    "TransitionGuards",
    "TransitionNotAllowed",
    "TransitionRequest",
    "TransitionResult",
]
