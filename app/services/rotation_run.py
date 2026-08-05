"""Construct immutable strategy-run evidence from deterministic, persisted-state transitions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from uuid import UUID

from app.data_sources.models import MarketDataSnapshot
from app.domain.entities import Position, RotationPlan, StrategyRun
from app.domain.values import ConfigurationSnapshotRef, Quantity, require_timezone_aware
from app.sizing.base import SizingResult
from app.state_machine.machine import (
    StateMachine,
    TransitionGuards,
    TransitionRequest,
    TransitionResult,
)
from app.state_machine.states import (
    DataOutcome,
    RecommendationEvent,
    RecommendationStateRecord,
    SizingOutcome,
)


@dataclass(frozen=True, slots=True)
class RotationEvaluation:
    """Typed evaluator output that cannot supply recommendation state or a trusted position."""

    event: RecommendationEvent
    guards: TransitionGuards
    outputs: Mapping[str, object]
    sale_source_position_id: UUID | None = None
    sizing_result: SizingResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event, RecommendationEvent):
            raise TypeError("event must be a RecommendationEvent")
        if not isinstance(self.guards, TransitionGuards):
            raise TypeError("guards must be TransitionGuards")
        if not isinstance(self.outputs, Mapping):
            raise TypeError("outputs must be a mapping")
        if self.sale_source_position_id is not None and not isinstance(
            self.sale_source_position_id, UUID
        ):
            raise TypeError("sale_source_position_id must be a UUID or None")
        if self.sizing_result is not None and not isinstance(self.sizing_result, SizingResult):
            raise TypeError("sizing_result must be a SizingResult or None")
        requires_sizing_result = (
            self.event is RecommendationEvent.DECISION_VALIDATED
            and self.guards.sizing_outcome is SizingOutcome.PASSED
        )
        if requires_sizing_result and self.sizing_result is None:
            raise ValueError("passed sizing validation requires a SizingResult")
        if not requires_sizing_result and self.sizing_result is not None:
            raise ValueError("sizing_result only applies to passed sizing validation")
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))


RotationEvaluator = Callable[
    [RotationPlan, MarketDataSnapshot, ConfigurationSnapshotRef], RotationEvaluation
]


@dataclass(frozen=True, slots=True)
class PreparedRotationRun:
    """A final-run candidate paired with its pure, durable-state transition result."""

    strategy_run: StrategyRun
    transition: TransitionResult


class RotationRunService:
    """Build final audit evidence after market data and authoritative guards are available."""

    def __init__(
        self, evaluator: RotationEvaluator, *, machine: StateMachine | None = None
    ) -> None:
        self._evaluator = evaluator
        self._machine = machine or StateMachine()

    def evaluate(
        self,
        *,
        plan: RotationPlan,
        snapshot: MarketDataSnapshot,
        configuration_snapshot: ConfigurationSnapshotRef,
    ) -> RotationEvaluation:
        """Return an event proposal; current recommendation state remains repository-owned."""

        evaluation = self._evaluator(plan, snapshot, configuration_snapshot)
        if not isinstance(evaluation, RotationEvaluation):
            raise TypeError("evaluator must return RotationEvaluation")
        return evaluation

    def transition_degraded_data(
        self,
        *,
        plan: RotationPlan,
        recommendation_state: RecommendationStateRecord,
    ) -> TransitionResult:
        """Apply the fixed degraded-data event from durable state without evaluating a signal."""

        if recommendation_state.rotation_plan_id != plan.rotation_plan_id:
            raise ValueError("recommendation state must belong to the supplied plan")
        return self._machine.transition(
            TransitionRequest(
                current_state=recommendation_state.state,
                event=RecommendationEvent.DATA_DEGRADED,
                guards=TransitionGuards(data_outcome=DataOutcome.DEGRADED),
                plan=plan,
                remaining_stages_halted=recommendation_state.remaining_stages_halted,
            )
        )

    def build_strategy_run(
        self,
        *,
        plan: RotationPlan,
        snapshot: MarketDataSnapshot,
        configuration_snapshot: ConfigurationSnapshotRef,
        scan_lock_key: str,
        final_strategy_key: str,
        occurred_at: datetime,
        strategy_run_id: UUID,
        recommendation_state: RecommendationStateRecord,
        evaluation: RotationEvaluation,
        sale_source_position: Position | None,
        sale_quantity: Quantity | None,
    ) -> PreparedRotationRun:
        """Use durable state and a repository-sourced position to build immutable run evidence."""

        require_timezone_aware(occurred_at, field_name="occurred_at")
        if not snapshot.is_actionable:
            raise ValueError("non-actionable market data cannot build a strategy run")
        if not isinstance(recommendation_state, RecommendationStateRecord):
            raise TypeError("recommendation_state must be a RecommendationStateRecord")
        if recommendation_state.rotation_plan_id != plan.rotation_plan_id:
            raise ValueError("recommendation state must belong to the supplied plan")
        transition = self._machine.transition(
            TransitionRequest(
                current_state=recommendation_state.state,
                event=evaluation.event,
                guards=evaluation.guards,
                plan=plan,
                sale_source_position=sale_source_position,
                sale_quantity=sale_quantity,
                remaining_stages_halted=recommendation_state.remaining_stages_halted,
            )
        )
        strategy_run = StrategyRun(
            strategy_run_id=strategy_run_id,
            rotation_plan_id=plan.rotation_plan_id,
            portfolio_id=plan.portfolio_id,
            configuration_snapshot=configuration_snapshot,
            market_data_snapshot_id=snapshot.snapshot_id,
            state_transition=f"{recommendation_state.state.value}->{transition.next_state.value}",
            outputs={
                "configuration_snapshot": {
                    "content_hash": configuration_snapshot.content_hash,
                    "created_at": configuration_snapshot.created_at.isoformat(),
                    "snapshot_id": str(configuration_snapshot.snapshot_id),
                },
                "evaluation": dict(evaluation.outputs),
                "idempotency": {
                    "final_strategy_key": final_strategy_key,
                    "scan_lock_key": scan_lock_key,
                },
                "market_data": {
                    "content_hash": snapshot.content_hash,
                    "snapshot_id": snapshot.snapshot_id,
                },
                "recommendation": {
                    "previous_remaining_stages_halted": (
                        recommendation_state.remaining_stages_halted
                    ),
                    "previous_state": recommendation_state.state.value,
                    "next_state": transition.next_state.value,
                    "notification_intent_recorded": transition.notification_intent_recorded,
                    "remaining_stages_halted": transition.remaining_stages_halted,
                },
            },
            occurred_at=occurred_at,
        )
        return PreparedRotationRun(strategy_run=strategy_run, transition=transition)
