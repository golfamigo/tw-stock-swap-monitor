"""Typed port for portfolio-scoped rotation plans."""

from typing import Protocol
from uuid import UUID

from app.domain.access import AccessContext
from app.domain.entities import RotationPlan


class RotationPlanRepository(Protocol):
    """Persist rotation plans without unscoped caller access."""

    def add(self, plan: RotationPlan, *, access_context: AccessContext) -> None:
        """Store a plan after authorizing the plan portfolio."""

    def get(self, rotation_plan_id: UUID, *, access_context: AccessContext) -> RotationPlan:
        """Read a plan or hide it from unauthorized callers."""
