"""Lock-first coordinator for one recoverable, non-trading rotation evaluation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID, uuid4

from app.application.idempotency import FinalStrategyIdentity, build_scan_lock_key
from app.data_sources.base import MarketDataProvider
from app.data_sources.models import MarketDataRequest, MarketDataSnapshot
from app.domain.access import AccessContext
from app.domain.entities import LogicalScanRun, RotationPlan, ScanAttempt, StrategyRun
from app.domain.enums import LogicalScanStatus, ScanAttemptStatus
from app.domain.errors import DomainError
from app.domain.values import ConfigurationSnapshotRef, require_timezone_aware
from app.repositories.locks import LockProvider, ScanLockRequest
from app.repositories.logical_scans import LogicalScanRepository
from app.repositories.rotation_plans import RotationPlanRepository
from app.repositories.strategy_runs import StrategyRunRepository
from app.services.rotation_run import RotationEvaluator, RotationRunService


class TriggerSource(StrEnum):
    """The two callers that share one scan identity and coordination behavior."""

    API = "API"
    SCHEDULER = "SCHEDULER"


class RunDisposition(StrEnum):
    """Safe completed, duplicate, or non-actionable outcomes for one coordinator request."""

    COMPLETED = "COMPLETED"
    API_CONFLICT = "API_CONFLICT"
    SCHEDULER_SKIPPED = "SCHEDULER_SKIPPED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


class RunCoordinatorInvariantError(DomainError):
    """Persisted scan evidence did not support a safe coordinator action."""


@dataclass(frozen=True, slots=True)
class RunCoordinatorRequest:
    """Validated concrete inputs for an API or scheduler request; no implicit configuration."""

    plan: RotationPlan
    configuration_snapshot: ConfigurationSnapshotRef
    market_session_date: str
    scan_window_start: datetime
    scan_interval: str
    market_timezone: str
    market_data_request: MarketDataRequest
    access_context: AccessContext
    trigger_source: TriggerSource
    correlation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.plan, RotationPlan):
            raise TypeError("plan must be a RotationPlan")
        if not isinstance(self.configuration_snapshot, ConfigurationSnapshotRef):
            raise TypeError("configuration_snapshot must be a ConfigurationSnapshotRef")
        require_timezone_aware(self.scan_window_start, field_name="scan_window_start")
        if not self.market_session_date.strip() or not self.scan_interval.strip():
            raise ValueError("scan identity values must not be blank")
        if not self.market_timezone.strip() or not self.correlation_id.strip():
            raise ValueError("market_timezone and correlation_id must not be blank")
        if not isinstance(self.market_data_request, MarketDataRequest):
            raise TypeError("market_data_request must be a MarketDataRequest")
        if not isinstance(self.access_context, AccessContext):
            raise TypeError("access_context must be an AccessContext")
        if not isinstance(self.trigger_source, TriggerSource):
            raise TypeError("trigger_source must be a TriggerSource")

    def scan_lock_request(self) -> ScanLockRequest:
        """Use Task 4's single canonical scan-lock identity implementation."""

        return ScanLockRequest(
            plan=self.plan,
            market_session_date=self.market_session_date,
            scan_window_start=self.scan_window_start,
            scan_interval=self.scan_interval,
            market_timezone=self.market_timezone,
            configuration_snapshot_hash=self.configuration_snapshot.content_hash,
        )


@dataclass(frozen=True, slots=True)
class ExistingResultReference:
    """Typed duplicate reference for an in-progress scan without exposing credentials or data."""

    scan_lock_key: str
    logical_scan_run_id: UUID | None


@dataclass(frozen=True, slots=True)
class RunCoordinatorResult:
    """Auditable coordinator output that never includes position or brokerage mutations."""

    disposition: RunDisposition
    strategy_run: StrategyRun | None
    scan_lock_key: str
    logical_scan_run_id: UUID | None
    final_strategy_key: str | None
    audit: Mapping[str, object]
    existing_result: ExistingResultReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, RunDisposition):
            raise TypeError("disposition must be a RunDisposition")
        if len(self.scan_lock_key) != 64:
            raise ValueError("scan_lock_key must be a SHA-256 digest")
        if self.final_strategy_key is not None and len(self.final_strategy_key) != 64:
            raise ValueError("final_strategy_key must be a SHA-256 digest when supplied")
        object.__setattr__(self, "audit", MappingProxyType(dict(self.audit)))


class RunCoordinator:
    """Coordinate a pre-provider lock, immutable attempts, and at most one final run."""

    def __init__(
        self,
        *,
        rotation_plans: RotationPlanRepository,
        logical_scans: LogicalScanRepository,
        strategy_runs: StrategyRunRepository,
        locks: LockProvider,
        market_data: MarketDataProvider,
        evaluator: RotationEvaluator,
        now: Callable[[], datetime],
        new_uuid: Callable[[], UUID] = uuid4,
        max_provider_attempts: int = 2,
    ) -> None:
        self._rotation_plans = rotation_plans
        self._logical_scans = logical_scans
        self._strategy_runs = strategy_runs
        self._locks = locks
        self._market_data = market_data
        self._run_service = RotationRunService(evaluator)
        self._now = now
        self._new_uuid = new_uuid
        if isinstance(max_provider_attempts, bool) or max_provider_attempts < 1:
            raise ValueError("max_provider_attempts must be a positive integer")
        self._max_provider_attempts = max_provider_attempts

    def run(self, request: RunCoordinatorRequest) -> RunCoordinatorResult:
        """Perform one evaluation, acquiring the scan lock before any provider operation."""

        if not isinstance(request, RunCoordinatorRequest):
            raise TypeError("request must be a RunCoordinatorRequest")
        plan = self._rotation_plans.get(
            request.plan.rotation_plan_id, access_context=request.access_context
        )
        if plan != request.plan:
            raise RunCoordinatorInvariantError("request plan does not match the authorized plan")
        scan_request = request.scan_lock_request()
        scan_lock_key = build_scan_lock_key(scan_request)
        existing_scan = self._logical_scans.get_by_lock_key(
            request=scan_request, access_context=request.access_context
        )
        if existing_scan is not None and existing_scan.status is LogicalScanStatus.COMPLETED:
            return self._completed_result(
                request=request,
                scan_request=scan_request,
                scan=existing_scan,
                scan_lock_key=scan_lock_key,
            )

        lease = self._locks.acquire_scan_lock(scan_request, access_context=request.access_context)
        if lease is None:
            duplicate_scan = self._logical_scans.get_by_lock_key(
                request=scan_request, access_context=request.access_context
            )
            if duplicate_scan is not None and duplicate_scan.status is LogicalScanStatus.COMPLETED:
                return self._completed_result(
                    request=request,
                    scan_request=scan_request,
                    scan=duplicate_scan,
                    scan_lock_key=scan_lock_key,
                )
            return self._duplicate_result(
                request=request,
                scan_lock_key=scan_lock_key,
                scan=duplicate_scan,
            )

        try:
            recovered_scan = self._logical_scans.get_by_lock_key(
                request=scan_request, access_context=request.access_context
            )
            if recovered_scan is not None and recovered_scan.status is LogicalScanStatus.COMPLETED:
                return self._completed_result(
                    request=request,
                    scan_request=scan_request,
                    scan=recovered_scan,
                    scan_lock_key=scan_lock_key,
                )
            scan = recovered_scan or self._logical_scans.create_or_recover(
                request=scan_request,
                created_at=self._current_time(),
                access_context=request.access_context,
            )
            if (
                self._provider_attempt_count(request=request, scan=scan)
                >= self._max_provider_attempts
            ):
                return self._retry_exhausted_result(
                    request=request,
                    scan=scan,
                    scan_lock_key=scan_lock_key,
                )
            running_attempt = self._record_running_attempt(request=request, scan=scan)
            try:
                snapshot = self._market_data.get_market_snapshot(request.market_data_request)
            except Exception:
                return self._failed_result(
                    request=request,
                    scan=scan,
                    running_attempt=running_attempt,
                    scan_lock_key=scan_lock_key,
                    failure_code="MARKET_DATA_PROVIDER_ERROR",
                    failure_detail="market-data provider failed before immutable snapshot evidence",
                )
            if not isinstance(snapshot, MarketDataSnapshot):
                return self._failed_result(
                    request=request,
                    scan=scan,
                    running_attempt=running_attempt,
                    scan_lock_key=scan_lock_key,
                    failure_code="INVALID_MARKET_DATA_SNAPSHOT",
                    failure_detail=(
                        "market-data provider did not return validated snapshot evidence"
                    ),
                )
            final_identity = FinalStrategyIdentity.from_snapshot(scan_lock_key, snapshot)
            if not snapshot.is_actionable:
                terminal = self._record_terminal_attempt(
                    request=request,
                    scan=scan,
                    previous_attempt=running_attempt,
                    status=ScanAttemptStatus.DEGRADED,
                    snapshot=snapshot,
                    failure_code="MARKET_DATA_DEGRADED",
                    failure_detail="market-data evidence was stale, incomplete, or non-actionable",
                )
                return RunCoordinatorResult(
                    disposition=RunDisposition.DEGRADED,
                    strategy_run=None,
                    scan_lock_key=scan_lock_key,
                    logical_scan_run_id=scan.logical_scan_run_id,
                    final_strategy_key=final_identity.key,
                    audit=self._audit(
                        request=request,
                        scan=scan,
                        final_identity=final_identity,
                        terminal_attempt=terminal,
                        market_data_actionable=False,
                    ),
                )
            try:
                strategy_run = self._run_service.build_strategy_run(
                    plan=plan,
                    snapshot=snapshot,
                    configuration_snapshot=request.configuration_snapshot,
                    scan_lock_key=scan_lock_key,
                    final_strategy_key=final_identity.key,
                    occurred_at=self._current_time(),
                    strategy_run_id=self._new_uuid(),
                )
                stored_run = self._strategy_runs.record_or_get(
                    plan=plan,
                    run=strategy_run,
                    idempotency_key=final_identity.as_idempotency_key(),
                    logical_scan_run_id=scan.logical_scan_run_id,
                    access_context=request.access_context,
                )
            except Exception:
                return self._failed_result(
                    request=request,
                    scan=scan,
                    running_attempt=running_attempt,
                    scan_lock_key=scan_lock_key,
                    failure_code="EVALUATION_OR_PERSISTENCE_ERROR",
                    failure_detail=(
                        "deterministic evaluation or final-run persistence rejected evidence"
                    ),
                    snapshot=snapshot,
                    final_strategy_key=final_identity.key,
                )
            terminal = self._record_terminal_attempt(
                request=request,
                scan=scan,
                previous_attempt=running_attempt,
                status=ScanAttemptStatus.SUCCEEDED,
                snapshot=snapshot,
                final_strategy_run_id=stored_run.strategy_run_id,
            )
            return RunCoordinatorResult(
                disposition=RunDisposition.COMPLETED,
                strategy_run=stored_run,
                scan_lock_key=scan_lock_key,
                logical_scan_run_id=scan.logical_scan_run_id,
                final_strategy_key=final_identity.key,
                audit=self._audit(
                    request=request,
                    scan=scan,
                    final_identity=final_identity,
                    terminal_attempt=terminal,
                    market_data_actionable=True,
                ),
            )
        finally:
            self._locks.release(lease, access_context=request.access_context)

    def _completed_result(
        self,
        *,
        request: RunCoordinatorRequest,
        scan_request: ScanLockRequest,
        scan: LogicalScanRun,
        scan_lock_key: str,
    ) -> RunCoordinatorResult:
        stored_run = self._strategy_runs.get_for_logical_scan(
            logical_scan_run_id=scan.logical_scan_run_id,
            access_context=request.access_context,
        )
        if stored_run is None:
            raise RunCoordinatorInvariantError("completed scan is missing its final strategy run")
        final_strategy_key = _final_key_from_run(stored_run)
        return RunCoordinatorResult(
            disposition=RunDisposition.COMPLETED,
            strategy_run=stored_run,
            scan_lock_key=scan_lock_key,
            logical_scan_run_id=scan.logical_scan_run_id,
            final_strategy_key=final_strategy_key,
            audit=self._audit(
                request=request,
                scan=scan,
                final_identity=None,
                terminal_attempt=None,
                market_data_actionable=True,
            ),
        )

    def _duplicate_result(
        self,
        *,
        request: RunCoordinatorRequest,
        scan_lock_key: str,
        scan: LogicalScanRun | None,
    ) -> RunCoordinatorResult:
        disposition = (
            RunDisposition.API_CONFLICT
            if request.trigger_source is TriggerSource.API
            else RunDisposition.SCHEDULER_SKIPPED
        )
        reference = ExistingResultReference(
            scan_lock_key=scan_lock_key,
            logical_scan_run_id=None if scan is None else scan.logical_scan_run_id,
        )
        return RunCoordinatorResult(
            disposition=disposition,
            strategy_run=None,
            scan_lock_key=scan_lock_key,
            logical_scan_run_id=reference.logical_scan_run_id,
            final_strategy_key=None,
            audit={
                "actor_id": str(request.access_context.actor_user_id),
                "attempt_disposition": disposition.value,
                "correlation_id": request.correlation_id,
                "scan_lock_key": scan_lock_key,
                "trigger_source": request.trigger_source.value,
            },
            existing_result=reference,
        )

    def _record_running_attempt(
        self, *, request: RunCoordinatorRequest, scan: LogicalScanRun
    ) -> ScanAttempt:
        attempts = self._logical_scans.list_attempts(
            logical_scan_run_id=scan.logical_scan_run_id, access_context=request.access_context
        )
        previous = attempts[-1] if attempts else None
        if previous is not None and previous.status is ScanAttemptStatus.SUCCEEDED:
            raise RunCoordinatorInvariantError("uncompleted scan retains a succeeded attempt")
        attempt = ScanAttempt(
            scan_attempt_id=self._new_uuid(),
            logical_scan_run_id=scan.logical_scan_run_id,
            attempt_number=len(attempts) + 1,
            status=ScanAttemptStatus.RUNNING,
            configuration_snapshot_hash=request.configuration_snapshot.content_hash,
            market_data_snapshot_id=None,
            market_data_content_hash=None,
            trigger_correlation_id=request.correlation_id,
            actor_correlation_id=f"actor:{request.access_context.actor_user_id}",
            started_at=self._current_time(),
            completed_at=None,
            recovery_of_attempt_id=None if previous is None else previous.scan_attempt_id,
        )
        self._logical_scans.record_attempt(attempt=attempt, access_context=request.access_context)
        return attempt

    def _provider_attempt_count(
        self, *, request: RunCoordinatorRequest, scan: LogicalScanRun
    ) -> int:
        """Count persisted pre-provider attempt markers before allowing a bounded retry."""

        attempts = self._logical_scans.list_attempts(
            logical_scan_run_id=scan.logical_scan_run_id, access_context=request.access_context
        )
        return sum(attempt.status is ScanAttemptStatus.RUNNING for attempt in attempts)

    def _retry_exhausted_result(
        self,
        *,
        request: RunCoordinatorRequest,
        scan: LogicalScanRun,
        scan_lock_key: str,
    ) -> RunCoordinatorResult:
        """Stop incomplete scans safely without fetching more market data after the retry bound."""

        audit = dict(
            self._audit(
                request=request,
                scan=scan,
                final_identity=None,
                terminal_attempt=None,
                market_data_actionable=False,
            )
        )
        audit["attempt_disposition"] = RunDisposition.RETRY_EXHAUSTED.value
        audit["failure_code"] = "RETRY_LIMIT_REACHED"
        audit["max_provider_attempts"] = self._max_provider_attempts
        return RunCoordinatorResult(
            disposition=RunDisposition.RETRY_EXHAUSTED,
            strategy_run=None,
            scan_lock_key=scan_lock_key,
            logical_scan_run_id=scan.logical_scan_run_id,
            final_strategy_key=None,
            audit=audit,
        )

    def _record_terminal_attempt(
        self,
        *,
        request: RunCoordinatorRequest,
        scan: LogicalScanRun,
        previous_attempt: ScanAttempt,
        status: ScanAttemptStatus,
        snapshot: MarketDataSnapshot,
        final_strategy_run_id: UUID | None = None,
        failure_code: str | None = None,
        failure_detail: str | None = None,
    ) -> ScanAttempt:
        attempts = self._logical_scans.list_attempts(
            logical_scan_run_id=scan.logical_scan_run_id, access_context=request.access_context
        )
        terminal = ScanAttempt(
            scan_attempt_id=self._new_uuid(),
            logical_scan_run_id=scan.logical_scan_run_id,
            attempt_number=len(attempts) + 1,
            status=status,
            configuration_snapshot_hash=request.configuration_snapshot.content_hash,
            market_data_snapshot_id=snapshot.snapshot_id,
            market_data_content_hash=snapshot.content_hash,
            trigger_correlation_id=request.correlation_id,
            actor_correlation_id=f"actor:{request.access_context.actor_user_id}",
            started_at=previous_attempt.started_at,
            completed_at=self._current_time(),
            failure_code=failure_code,
            failure_detail=failure_detail,
            recovery_of_attempt_id=previous_attempt.scan_attempt_id,
            final_strategy_run_id=final_strategy_run_id,
        )
        self._logical_scans.record_attempt(attempt=terminal, access_context=request.access_context)
        return terminal

    def _failed_result(
        self,
        *,
        request: RunCoordinatorRequest,
        scan: LogicalScanRun,
        running_attempt: ScanAttempt,
        scan_lock_key: str,
        failure_code: str,
        failure_detail: str,
        snapshot: MarketDataSnapshot | None = None,
        final_strategy_key: str | None = None,
    ) -> RunCoordinatorResult:
        terminal = self._failed_terminal_attempt(
            request=request,
            scan=scan,
            running_attempt=running_attempt,
            snapshot=snapshot,
            failure_code=failure_code,
            failure_detail=failure_detail,
        )
        return RunCoordinatorResult(
            disposition=RunDisposition.FAILED,
            strategy_run=None,
            scan_lock_key=scan_lock_key,
            logical_scan_run_id=scan.logical_scan_run_id,
            final_strategy_key=final_strategy_key,
            audit=self._audit(
                request=request,
                scan=scan,
                final_identity=None,
                terminal_attempt=terminal,
                market_data_actionable=False,
            ),
        )

    def _failed_terminal_attempt(
        self,
        *,
        request: RunCoordinatorRequest,
        scan: LogicalScanRun,
        running_attempt: ScanAttempt,
        snapshot: MarketDataSnapshot | None,
        failure_code: str,
        failure_detail: str,
    ) -> ScanAttempt:
        attempts = self._logical_scans.list_attempts(
            logical_scan_run_id=scan.logical_scan_run_id, access_context=request.access_context
        )
        terminal = ScanAttempt(
            scan_attempt_id=self._new_uuid(),
            logical_scan_run_id=scan.logical_scan_run_id,
            attempt_number=len(attempts) + 1,
            status=ScanAttemptStatus.FAILED,
            configuration_snapshot_hash=request.configuration_snapshot.content_hash,
            market_data_snapshot_id=None if snapshot is None else snapshot.snapshot_id,
            market_data_content_hash=None if snapshot is None else snapshot.content_hash,
            trigger_correlation_id=request.correlation_id,
            actor_correlation_id=f"actor:{request.access_context.actor_user_id}",
            started_at=running_attempt.started_at,
            completed_at=self._current_time(),
            failure_code=failure_code,
            failure_detail=failure_detail,
            recovery_of_attempt_id=running_attempt.scan_attempt_id,
        )
        self._logical_scans.record_attempt(attempt=terminal, access_context=request.access_context)
        return terminal

    def _audit(
        self,
        *,
        request: RunCoordinatorRequest,
        scan: LogicalScanRun,
        final_identity: FinalStrategyIdentity | None,
        terminal_attempt: ScanAttempt | None,
        market_data_actionable: bool,
    ) -> Mapping[str, object]:
        attempts = self._logical_scans.list_attempts(
            logical_scan_run_id=scan.logical_scan_run_id, access_context=request.access_context
        )
        audit: dict[str, object] = {
            "actor_id": str(request.access_context.actor_user_id),
            "attempt_disposition": None
            if terminal_attempt is None
            else terminal_attempt.status.value,
            "attempt_statuses": [attempt.status.value for attempt in attempts],
            "configuration_snapshot_hash": request.configuration_snapshot.content_hash,
            "correlation_id": request.correlation_id,
            "market_data_actionable": market_data_actionable,
            "scan_lock_key": scan.scan_lock_key,
            "scan_lock_key_components": scan.identity.canonical_payload,
            "trigger_source": request.trigger_source.value,
        }
        if terminal_attempt is not None:
            audit["failure_code"] = terminal_attempt.failure_code
            audit["market_data_content_hash"] = terminal_attempt.market_data_content_hash
            audit["market_data_snapshot_id"] = terminal_attempt.market_data_snapshot_id
        if final_identity is not None:
            audit["final_strategy_key"] = final_identity.key
            audit["final_strategy_key_components"] = final_identity.canonical_payload
        return audit

    def _current_time(self) -> datetime:
        value = self._now()
        require_timezone_aware(value, field_name="coordinator clock")
        return value


def _final_key_from_run(run: StrategyRun) -> str | None:
    idempotency = run.outputs.get("idempotency")
    if not isinstance(idempotency, Mapping):
        return None
    final_strategy_key = idempotency.get("final_strategy_key")
    if not isinstance(final_strategy_key, str) or len(final_strategy_key) != 64:
        return None
    return final_strategy_key
