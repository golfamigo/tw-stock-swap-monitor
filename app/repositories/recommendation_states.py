"""Typed persistence port for the durable recommendation lifecycle only."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.access import AccessContext
from app.domain.entities import RotationPlan
from app.state_machine.states import RecommendationState, RecommendationStateRecord


class RecommendationStateRepository(Protocol):
    """Load and atomically advance recommendation state under the plan's portfolio scope."""

    def get_or_create(
        self,
        *,
        plan: RotationPlan,
        created_at: datetime,
        access_context: AccessContext,
    ) -> RecommendationStateRecord:
        """Return a plan's existing state or persist the initial IDLE record."""

    def get(
        self, rotation_plan_id: UUID, *, access_context: AccessContext
    ) -> RecommendationStateRecord:
        """Return one authorized durable recommendation-state record."""

    def record_transition(
        self,
        *,
        plan: RotationPlan,
        previous: RecommendationStateRecord,
        next_state: RecommendationState,
        remaining_stages_halted: bool,
        changed_at: datetime,
        access_context: AccessContext,
    ) -> RecommendationStateRecord:
        """Compare against the stored record and persist exactly one coordinator transition."""
