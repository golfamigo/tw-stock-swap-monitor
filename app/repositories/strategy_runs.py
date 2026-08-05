"""Typed port for immutable, idempotent strategy run evidence."""

from typing import Protocol
from uuid import UUID

from app.domain.access import AccessContext
from app.domain.entities import RotationPlan, StrategyRun
from app.domain.errors import IdempotencyConflictError
from app.domain.values import IdempotencyKey


def require_exact_strategy_run_retry(
    *,
    existing: StrategyRun,
    candidate: StrategyRun,
    stored_logical_scan_run_id: UUID | None,
    requested_logical_scan_run_id: UUID | None,
) -> None:
    """Reject reuse of an idempotency key unless the immutable candidate is identical."""

    if existing != candidate or stored_logical_scan_run_id != requested_logical_scan_run_id:
        raise IdempotencyConflictError("idempotency key already records a different strategy run")


class StrategyRunRepository(Protocol):
    """Store exactly one final result for a plan-local idempotency key."""

    def record_or_get(
        self,
        *,
        plan: RotationPlan,
        run: StrategyRun,
        idempotency_key: IdempotencyKey,
        logical_scan_run_id: UUID | None = None,
        access_context: AccessContext,
    ) -> StrategyRun:
        """Return the stored final run when the key has already completed."""

    def get_for_logical_scan(
        self, *, logical_scan_run_id: UUID, access_context: AccessContext
    ) -> StrategyRun | None:
        """Return the final result associated with a completed logical scan, if any."""
