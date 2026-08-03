"""State-transition safety contracts for recommendation lifecycle only."""

from __future__ import annotations

from uuid import UUID

import pytest
from app.domain.errors import ProtectedPositionSaleError
from app.state_machine.machine import (
    StateMachine,
    TransitionGuards,
    TransitionRequest,
    TransitionResult,
)
from app.state_machine.states import (
    DataOutcome,
    RecommendationEvent,
    RecommendationState,
    RuleOutcome,
    SizingOutcome,
    TransitionNotAllowed,
)

PROTECTED_POSITION_ID = UUID("00000000-0000-0000-0000-000000000701")


def _transition(
    state: RecommendationState,
    event: RecommendationEvent,
    *,
    guards: TransitionGuards | None = None,
    sale_position_id: UUID | None = None,
    remaining_stages_halted: bool = False,
) -> TransitionResult:
    return StateMachine().transition(
        TransitionRequest(
            current_state=state,
            event=event,
            guards=guards or TransitionGuards(),
            protected_position_ids=(PROTECTED_POSITION_ID,),
            sale_position_id=sale_position_id,
            remaining_stages_halted=remaining_stages_halted,
        )
    )


@pytest.mark.parametrize(
    "state", [RecommendationState.ACTION_PENDING, RecommendationState.ACTION_NOTIFIED]
)
def test_pending_and_notified_actions_are_invalidated_before_confirmation(
    state: RecommendationState,
) -> None:
    result = _transition(
        state,
        RecommendationEvent.SIGNAL_INVALIDATED,
        guards=TransitionGuards(rule_outcome=RuleOutcome.PASSED),
    )

    assert result.next_state is RecommendationState.INVALIDATED
    assert result.remaining_stages_halted
    assert not result.execution_status_changed
    assert not result.positions_changed


@pytest.mark.parametrize(
    ("state", "expected_state", "expected_halted"),
    [
        (RecommendationState.WATCHING, RecommendationState.DATA_DEGRADED, False),
        (RecommendationState.NEAR_TRIGGER, RecommendationState.DATA_DEGRADED, False),
        (RecommendationState.ACTION_PENDING, RecommendationState.DATA_DEGRADED, False),
        (RecommendationState.ACTION_NOTIFIED, RecommendationState.DATA_DEGRADED, False),
        (RecommendationState.STAGE_COMPLETED, RecommendationState.DATA_DEGRADED, False),
        (RecommendationState.PARTIALLY_EXECUTED, RecommendationState.WAITING_CONFIRMATION, True),
    ],
)
def test_data_degraded_from_every_active_recommendation_state(
    state: RecommendationState,
    expected_state: RecommendationState,
    expected_halted: bool,
) -> None:
    result = _transition(
        state,
        RecommendationEvent.DATA_DEGRADED,
        guards=TransitionGuards(data_outcome=DataOutcome.DEGRADED),
    )

    assert result.next_state is expected_state
    assert result.remaining_stages_halted is expected_halted
    assert not result.positions_changed


def test_data_recovery_requires_a_fresh_full_evaluation_and_never_restores_action() -> None:
    with pytest.raises(TransitionNotAllowed, match="fresh full evaluation"):
        _transition(
            RecommendationState.DATA_DEGRADED,
            RecommendationEvent.DATA_RECOVERED,
            guards=TransitionGuards(data_outcome=DataOutcome.FRESH_COMPLETE),
        )

    recovered = _transition(
        RecommendationState.DATA_DEGRADED,
        RecommendationEvent.DATA_RECOVERED,
        guards=TransitionGuards(data_outcome=DataOutcome.FRESH_FULL_EVALUATION),
    )

    assert recovered.next_state is RecommendationState.WATCHING
    assert recovered.requires_fresh_full_evaluation


@pytest.mark.parametrize(
    "event",
    [RecommendationEvent.SIGNAL_INVALIDATED, RecommendationEvent.DATA_DEGRADED],
)
def test_partial_execution_halts_remaining_stages_when_invalidated_or_degraded(
    event: RecommendationEvent,
) -> None:
    guards = (
        TransitionGuards(rule_outcome=RuleOutcome.PASSED)
        if event is RecommendationEvent.SIGNAL_INVALIDATED
        else TransitionGuards(data_outcome=DataOutcome.DEGRADED)
    )

    result = _transition(RecommendationState.PARTIALLY_EXECUTED, event, guards=guards)

    assert result.next_state is RecommendationState.WAITING_CONFIRMATION
    assert result.remaining_stages_halted
    assert not result.positions_changed


@pytest.mark.parametrize(
    "state",
    [RecommendationState.DATA_DEGRADED, RecommendationState.INVALIDATED],
)
def test_late_confirmation_cannot_revive_a_stale_recommendation(state: RecommendationState) -> None:
    result = _transition(
        state,
        RecommendationEvent.EXECUTION_CONFIRMED,
        guards=TransitionGuards(confirmation_is_valid=True),
    )

    assert result.next_state is RecommendationState.WAITING_CONFIRMATION
    assert result.remaining_stages_halted
    assert not result.notification_intent_recorded
    assert not result.positions_changed


def test_authorized_resume_requires_fresh_evaluation_and_returns_to_watching() -> None:
    with pytest.raises(TransitionNotAllowed, match="authorized"):
        _transition(
            RecommendationState.WAITING_CONFIRMATION,
            RecommendationEvent.RESUME_REMAINING_STAGES,
            guards=TransitionGuards(rule_outcome=RuleOutcome.FRESH_FULL_EVALUATION),
            remaining_stages_halted=True,
        )

    resumed = _transition(
        RecommendationState.WAITING_CONFIRMATION,
        RecommendationEvent.RESUME_REMAINING_STAGES,
        guards=TransitionGuards(
            rule_outcome=RuleOutcome.FRESH_FULL_EVALUATION,
            authorization_is_valid=True,
        ),
        remaining_stages_halted=True,
    )

    assert resumed.next_state is RecommendationState.WATCHING
    assert not resumed.remaining_stages_halted
    assert resumed.requires_fresh_full_evaluation


def test_action_notified_only_records_notification_intent() -> None:
    result = _transition(
        RecommendationState.ACTION_PENDING,
        RecommendationEvent.DECISION_VALIDATED,
        guards=TransitionGuards(sizing_outcome=SizingOutcome.PASSED),
    )

    assert result.next_state is RecommendationState.ACTION_NOTIFIED
    assert result.notification_intent_recorded
    assert not result.execution_status_changed
    assert not result.positions_changed


@pytest.mark.parametrize(
    ("state", "event", "guards"),
    [
        (
            RecommendationState.WATCHING,
            RecommendationEvent.PLAN_ACTIVATED,
            TransitionGuards(plan_is_valid=True),
        ),
        (
            RecommendationState.NEAR_TRIGGER,
            RecommendationEvent.NEAR_SIGNAL,
            TransitionGuards(rule_outcome=RuleOutcome.PASSED),
        ),
        (
            RecommendationState.ACTION_PENDING,
            RecommendationEvent.ACTION_SIGNAL,
            TransitionGuards(rule_outcome=RuleOutcome.PASSED),
        ),
        (
            RecommendationState.ACTION_NOTIFIED,
            RecommendationEvent.DECISION_VALIDATED,
            TransitionGuards(sizing_outcome=SizingOutcome.PASSED),
        ),
        (
            RecommendationState.INVALIDATED,
            RecommendationEvent.SIGNAL_INVALIDATED,
            TransitionGuards(rule_outcome=RuleOutcome.PASSED),
        ),
        (
            RecommendationState.PAUSED,
            RecommendationEvent.PLAN_PAUSED,
            TransitionGuards(authorization_is_valid=True),
        ),
    ],
)
def test_duplicate_events_are_deterministic_noops(
    state: RecommendationState,
    event: RecommendationEvent,
    guards: TransitionGuards,
) -> None:
    result = _transition(state, event, guards=guards)

    assert result.next_state is state
    assert result.is_noop
    assert not result.positions_changed


@pytest.mark.parametrize(
    ("state", "event", "guards"),
    [
        (
            RecommendationState.NEAR_TRIGGER,
            RecommendationEvent.ACTION_SIGNAL,
            TransitionGuards(rule_outcome=RuleOutcome.FAILED),
        ),
        (
            RecommendationState.ACTION_PENDING,
            RecommendationEvent.DECISION_VALIDATED,
            TransitionGuards(sizing_outcome=SizingOutcome.FAILED),
        ),
        (
            RecommendationState.PARTIALLY_EXECUTED,
            RecommendationEvent.NEXT_STAGE_ELIGIBLE,
            TransitionGuards(rule_outcome=RuleOutcome.FAILED),
        ),
    ],
)
def test_protected_position_is_denied_by_every_sale_or_action_transition_guard(
    state: RecommendationState,
    event: RecommendationEvent,
    guards: TransitionGuards,
) -> None:
    with pytest.raises(ProtectedPositionSaleError):
        _transition(
            state,
            event,
            guards=guards,
            sale_position_id=PROTECTED_POSITION_ID,
        )


def test_invalid_edges_are_rejected_with_a_typed_error() -> None:
    with pytest.raises(TransitionNotAllowed):
        _transition(RecommendationState.IDLE, RecommendationEvent.ACTION_SIGNAL)
