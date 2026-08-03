"""Construct immutable strategy-run evidence from one deterministic evaluation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from uuid import UUID

from app.data_sources.models import MarketDataSnapshot
from app.domain.entities import RotationPlan, StrategyRun
from app.domain.values import ConfigurationSnapshotRef, require_timezone_aware
from app.state_machine.machine import StateMachine, TransitionGuards, TransitionRequest
from app.state_machine.states import RecommendationEvent, RecommendationState


@dataclass(frozen=True, slots=True)
class RotationEvaluation:
    """Typed deterministic recommendation evaluation with no position mutation capability."""

    current_state: RecommendationState
    event: RecommendationEvent
    guards: TransitionGuards
    outputs: Mapping[str, object]
    sale_position_id: UUID | None = None
    remaining_stages_halted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.current_state, RecommendationState):
            raise TypeError("current_state must be a RecommendationState")
        if not isinstance(self.event, RecommendationEvent):
            raise TypeError("event must be a RecommendationEvent")
        if not isinstance(self.guards, TransitionGuards):
            raise TypeError("guards must be TransitionGuards")
        if not isinstance(self.outputs, Mapping):
            raise TypeError("outputs must be a mapping")
        if self.sale_position_id is not None and not isinstance(self.sale_position_id, UUID):
            raise TypeError("sale_position_id must be a UUID or None")
        if not isinstance(self.remaining_stages_halted, bool):
            raise TypeError("remaining_stages_halted must be a Boolean")
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))


RotationEvaluator = Callable[
    [RotationPlan, MarketDataSnapshot, ConfigurationSnapshotRef], RotationEvaluation
]


class RotationRunService:
    """Build final audit evidence after market data has already passed safety gating."""

    def __init__(
        self, evaluator: RotationEvaluator, *, machine: StateMachine | None = None
    ) -> None:
        self._evaluator = evaluator
        self._machine = machine or StateMachine()

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
    ) -> StrategyRun:
        """Evaluate a safe snapshot and retain immutable output and identity evidence."""

        require_timezone_aware(occurred_at, field_name="occurred_at")
        if not snapshot.is_actionable:
            raise ValueError("non-actionable market data cannot build a strategy run")
        evaluation = self._evaluator(plan, snapshot, configuration_snapshot)
        if not isinstance(evaluation, RotationEvaluation):
            raise TypeError("evaluator must return RotationEvaluation")
        transition = self._machine.transition(
            TransitionRequest(
                current_state=evaluation.current_state,
                event=evaluation.event,
                guards=evaluation.guards,
                protected_position_ids=plan.protected_position_ids,
                sale_position_id=evaluation.sale_position_id,
                remaining_stages_halted=evaluation.remaining_stages_halted,
            )
        )
        return StrategyRun(
            strategy_run_id=strategy_run_id,
            rotation_plan_id=plan.rotation_plan_id,
            portfolio_id=plan.portfolio_id,
            configuration_snapshot=configuration_snapshot,
            market_data_snapshot_id=snapshot.snapshot_id,
            state_transition=f"{evaluation.current_state.value}->{transition.next_state.value}",
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
                    "next_state": transition.next_state.value,
                    "notification_intent_recorded": transition.notification_intent_recorded,
                    "remaining_stages_halted": transition.remaining_stages_halted,
                },
            },
            occurred_at=occurred_at,
        )
