"""Typed persistence contract for recoverable logical scan identities and attempts."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.access import AccessContext
from app.domain.entities import LogicalScanRun, RotationPlan, ScanAttempt
from app.domain.enums import (
    LogicalScanStatus as LogicalScanStatus,
)
from app.domain.enums import (
    ScanAttemptRecoveryDecision,
    ScanAttemptStatus,
)
from app.repositories.locks import ScanLockRequest

__all__ = ["LogicalScanRepository", "LogicalScanRun", "LogicalScanStatus", "ScanAttempt"]


def validate_scan_attempt_append(
    *, existing_attempts: Sequence[ScanAttempt], candidate: ScanAttempt
) -> None:
    """Enforce an immutable, sequential recovery chain for one logical scan."""

    if not existing_attempts:
        if candidate.attempt_number != 1:
            raise ValueError("the first scan attempt must be numbered one")
        if candidate.recovery_of_attempt_id is not None:
            raise ValueError("the first scan attempt cannot recover another attempt")
        return
    previous = max(existing_attempts, key=lambda attempt: attempt.attempt_number)
    if candidate.attempt_number != previous.attempt_number + 1:
        raise ValueError("scan attempt numbers must be sequential")
    if any(attempt.status is ScanAttemptStatus.SUCCEEDED for attempt in existing_attempts):
        if candidate.final_strategy_run_id is not None or (
            candidate.duplicate_of_attempt_id is not None
            and candidate.recovery_of_attempt_id is None
        ):
            return
        raise ValueError("succeeded scan attempts cannot accept new work")
    if candidate.recovery_of_attempt_id != previous.scan_attempt_id:
        raise ValueError("scan attempt recovery must reference the previous attempt")
    if previous.recovery_decision not in {
        ScanAttemptRecoveryDecision.RESUME,
        ScanAttemptRecoveryDecision.RETRY,
    }:
        raise ValueError("previous scan attempt is not recoverable")


class LogicalScanRepository(Protocol):
    """Retain retry evidence for one lock-keyed scan before its final strategy result."""

    def create_or_recover(
        self,
        *,
        request: ScanLockRequest,
        created_at: datetime,
        access_context: AccessContext,
    ) -> LogicalScanRun:
        """Create the RUNNING scan or recover the pre-existing record by lock key."""

    def get_by_lock_key(
        self, *, request: ScanLockRequest, access_context: AccessContext
    ) -> LogicalScanRun | None:
        """Return the authorized logical scan for one deterministic preliminary lock key."""

    def get(self, logical_scan_run_id: UUID, *, access_context: AccessContext) -> LogicalScanRun:
        """Return one authorized logical scan by immutable identity."""

    def record_attempt(self, *, attempt: ScanAttempt, access_context: AccessContext) -> None:
        """Append one uniquely numbered provider attempt while the scan remains running."""

    def list_attempts(
        self, *, logical_scan_run_id: UUID, access_context: AccessContext
    ) -> Sequence[ScanAttempt]:
        """Return every authorized attempt in deterministic attempt-number order."""

    def attach_final_strategy_run(
        self,
        *,
        logical_scan_run_id: UUID,
        plan: RotationPlan,
        strategy_run_id: UUID,
        completed_at: datetime,
        access_context: AccessContext,
    ) -> LogicalScanRun:
        """Complete a scan exactly once after its final strategy result is persisted."""
