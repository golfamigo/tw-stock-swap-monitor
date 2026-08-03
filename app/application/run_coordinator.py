"""Lock-first coordinator for one recoverable, non-trading rotation evaluation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.application.child_intents import ChildIntentPurpose, build_child_intent
from app.application.configuration_snapshots import PersistedConfigurationSnapshot
from app.application.idempotency import FinalStrategyIdentity, build_scan_lock_key
from app.data_sources.base import MarketDataProvider
from app.data_sources.models import MarketDataRequest, MarketDataSnapshot
from app.domain.access import AccessContext
from app.domain.entities import LogicalScanRun, Position, RotationPlan, ScanAttempt, StrategyRun
from app.domain.enums import FinalizationDisposition, LogicalScanStatus, ScanAttemptStatus
from app.domain.errors import DomainError
from app.domain.values import ConfigurationSnapshotRef, require_timezone_aware
from app.repositories.child_intents import ChildIntentRepository
from app.repositories.configuration_snapshots import ConfigurationSnapshotRepository
from app.repositories.locks import LockProvider, ScanLockRequest
from app.repositories.logical_scans import LogicalScanRepository
from app.repositories.positions import PositionRepository
from app.repositories.recommendation_states import RecommendationStateRepository
from app.repositories.rotation_plans import RotationPlanRepository
from app.repositories.strategy_runs import StrategyRunRepository
from app.services.rotation_run import RotationEvaluation, RotationEvaluator, RotationRunService
from app.state_machine.states import RecommendationState


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
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


class RunCoordinatorInvariantError(DomainError):
    """Persisted scan evidence did not support a safe coordinator action."""


class _FinalizationSuperseded(RunCoordinatorInvariantError):
    """A persisted candidate lost its state transition to durable evidence."""


class ConfigurationSnapshotValidationError(DomainError):
    """A request cited configuration evidence that is missing, mismatched, or unauthorized."""


@dataclass(frozen=True, slots=True)
class _FinalizationEvidence:
    """Persisted transition effects that a retry can complete without re-evaluation."""

    previous_state: RecommendationState
    previous_remaining_stages_halted: bool
    next_state: RecommendationState
    remaining_stages_halted: bool
    notification_intent_recorded: bool


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
    """Coordinate lock-first provider work, durable recommendation state, and final evidence."""

    def __init__(
        self,
        *,
        rotation_plans: RotationPlanRepository,
        configuration_snapshots: ConfigurationSnapshotRepository,
        positions: PositionRepository,
        recommendation_states: RecommendationStateRepository,
        child_intents: ChildIntentRepository,
        logical_scans: LogicalScanRepository,
        strategy_runs: StrategyRunRepository,
        locks: LockProvider,
        market_data: MarketDataProvider,
        evaluator: RotationEvaluator,
        now: Callable[[], datetime],
        new_uuid: Callable[[], UUID] = uuid4,
        max_provider_attempts: int = 2,
    ) -> None:
        if isinstance(max_provider_attempts, bool) or max_provider_attempts < 1:
            raise ValueError("max_provider_attempts must be a positive integer")
        self._rotation_plans = rotation_plans
        self._configuration_snapshots = configuration_snapshots
        self._positions = positions
        self._recommendation_states = recommendation_states
        self._child_intents = child_intents
        self._logical_scans = logical_scans
        self._strategy_runs = strategy_runs
        self._locks = locks
        self._market_data = market_data
        self._run_service = RotationRunService(evaluator)
        self._now = now
        self._new_uuid = new_uuid
        self._max_provider_attempts = max_provider_attempts

    def run(self, request: RunCoordinatorRequest) -> RunCoordinatorResult:
        """Coordinate one recommendation evaluation without fetching before acquiring its lock."""

        if not isinstance(request, RunCoordinatorRequest):
            raise TypeError("request must be a RunCoordinatorRequest")
        plan = self._rotation_plans.get(
            request.plan.rotation_plan_id, access_context=request.access_context
        )
        if plan != request.plan:
            raise RunCoordinatorInvariantError("request plan does not match the authorized plan")
        self._validate_configuration_snapshot(request)
        scan_request = request.scan_lock_request()
        scan_lock_key = build_scan_lock_key(scan_request)
        lease = self._locks.acquire_scan_lock(scan_request, access_context=request.access_context)
        if lease is None:
            return self._duplicate_result(request=request, scan_lock_key=scan_lock_key)

        try:
            scan = self._logical_scans.get_by_lock_key(
                request=scan_request, access_context=request.access_context
            )
            if scan is not None and scan.status is LogicalScanStatus.COMPLETED:
                return self._completed_result(
                    request=request, scan=scan, scan_lock_key=scan_lock_key
                )
            scan = scan or self._logical_scans.create_or_recover(
                request=scan_request,
                created_at=self._current_time(),
                access_context=request.access_context,
            )
            pending_run = self._strategy_runs.get_for_logical_scan(
                logical_scan_run_id=scan.logical_scan_run_id,
                access_context=request.access_context,
            )
            if pending_run is not None:
                return self._recover_pending_finalization(
                    request=request,
                    plan=plan,
                    scan=scan,
                    scan_lock_key=scan_lock_key,
                    strategy_run=pending_run,
                )
            if (
                self._provider_attempt_count(request=request, scan=scan)
                >= self._max_provider_attempts
            ):
                return self._retry_exhausted_result(
                    request=request, scan=scan, scan_lock_key=scan_lock_key
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
                try:
                    recommendation_state = self._recommendation_states.get_or_create(
                        plan=plan,
                        created_at=self._current_time(),
                        access_context=request.access_context,
                    )
                    if recommendation_state.state is not RecommendationState.IDLE:
                        transition = self._run_service.transition_degraded_data(
                            plan=plan,
                            recommendation_state=recommendation_state,
                        )
                        self._recommendation_states.record_transition(
                            plan=plan,
                            previous=recommendation_state,
                            next_state=transition.next_state,
                            remaining_stages_halted=transition.remaining_stages_halted,
                            changed_at=self._current_time(),
                            access_context=request.access_context,
                        )
                except Exception:
                    return self._failed_result(
                        request=request,
                        scan=scan,
                        running_attempt=running_attempt,
                        scan_lock_key=scan_lock_key,
                        failure_code="DATA_DEGRADATION_STATE_ERROR",
                        failure_detail="durable degraded-data transition rejected evidence",
                        snapshot=snapshot,
                        final_identity=final_identity,
                    )
                terminal = self._record_terminal_attempt(
                    request=request,
                    scan=scan,
                    previous_attempt=running_attempt,
                    status=ScanAttemptStatus.DEGRADED,
                    snapshot=snapshot,
                    final_identity=final_identity,
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
                recommendation_state = self._recommendation_states.get_or_create(
                    plan=plan,
                    created_at=self._current_time(),
                    access_context=request.access_context,
                )
                evaluation = self._run_service.evaluate(
                    plan=plan,
                    snapshot=snapshot,
                    configuration_snapshot=request.configuration_snapshot,
                )
                sale_source = self._authoritative_sale_source(
                    evaluation=evaluation, plan=plan, access_context=request.access_context
                )
                prepared = self._run_service.build_strategy_run(
                    plan=plan,
                    snapshot=snapshot,
                    configuration_snapshot=request.configuration_snapshot,
                    scan_lock_key=scan_lock_key,
                    final_strategy_key=final_identity.key,
                    occurred_at=self._current_time(),
                    strategy_run_id=self._new_uuid(),
                    recommendation_state=recommendation_state,
                    evaluation=evaluation,
                    sale_source_position=sale_source,
                )
                stored_run = self._strategy_runs.record_or_get(
                    plan=plan,
                    run=prepared.strategy_run,
                    idempotency_key=final_identity.as_idempotency_key(),
                    logical_scan_run_id=scan.logical_scan_run_id,
                    access_context=request.access_context,
                )
                try:
                    notification_intent_key = self._ensure_finalization_effects(
                        request=request,
                        plan=plan,
                        scan=scan,
                        strategy_run=stored_run,
                        final_identity=final_identity,
                    )
                except _FinalizationSuperseded:
                    return self._finalize_superseded_candidate(
                        request=request,
                        plan=plan,
                        scan=scan,
                        scan_lock_key=scan_lock_key,
                        strategy_run=stored_run,
                        final_identity=final_identity,
                    )
                terminal = self._record_recovered_final_attempt(
                    request=request,
                    scan=scan,
                    strategy_run=stored_run,
                    final_identity=final_identity,
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
                    final_identity=final_identity,
                )
            try:
                self._logical_scans.attach_final_strategy_run(
                    logical_scan_run_id=scan.logical_scan_run_id,
                    plan=plan,
                    strategy_run_id=stored_run.strategy_run_id,
                    completed_at=self._current_time(),
                    access_context=request.access_context,
                )
            except Exception:
                return self._pending_finalization_result(
                    request=request,
                    scan=scan,
                    scan_lock_key=scan_lock_key,
                    strategy_run=stored_run,
                    final_identity=final_identity,
                )
            audit = dict(
                self._audit(
                    request=request,
                    scan=scan,
                    final_identity=final_identity,
                    terminal_attempt=terminal,
                    market_data_actionable=True,
                )
            )
            if notification_intent_key is not None:
                audit["notification_intent_key"] = notification_intent_key
            return RunCoordinatorResult(
                disposition=RunDisposition.COMPLETED,
                strategy_run=stored_run,
                scan_lock_key=scan_lock_key,
                logical_scan_run_id=scan.logical_scan_run_id,
                final_strategy_key=final_identity.key,
                audit=audit,
            )
        finally:
            self._locks.release(lease, access_context=request.access_context)

    def _validate_configuration_snapshot(self, request: RunCoordinatorRequest) -> None:
        """Fail closed before locking on an invalid immutable configuration reference."""

        try:
            persisted = self._configuration_snapshots.get(
                request.configuration_snapshot.snapshot_id, access_context=request.access_context
            )
        except Exception as error:
            raise ConfigurationSnapshotValidationError(
                "configuration snapshot was unavailable for this actor"
            ) from error
        actual = _configuration_reference(persisted)
        if actual != request.configuration_snapshot:
            raise ConfigurationSnapshotValidationError(
                "configuration snapshot reference does not match immutable persisted evidence"
            )
        if persisted.target_reference_id != request.plan.rotation_plan_id:
            raise ConfigurationSnapshotValidationError(
                "configuration snapshot is not bound to the requested rotation plan"
            )

    def _authoritative_sale_source(
        self,
        *,
        evaluation: RotationEvaluation,
        plan: RotationPlan,
        access_context: AccessContext,
    ) -> Position | None:
        if evaluation.sale_source_position_id is None:
            return None
        position = self._positions.get(
            evaluation.sale_source_position_id, access_context=access_context
        )
        if position.portfolio_id != plan.portfolio_id:
            raise RunCoordinatorInvariantError("sale source position does not belong to the plan")
        return position

    def _recover_pending_finalization(
        self,
        *,
        request: RunCoordinatorRequest,
        plan: RotationPlan,
        scan: LogicalScanRun,
        scan_lock_key: str,
        strategy_run: StrategyRun,
    ) -> RunCoordinatorResult:
        """Complete persisted finalization effects without re-reading a provider or evaluator."""

        try:
            final_identity = _final_identity_from_run(strategy_run)
            applied = self._existing_applied_finalization(
                request=request,
                plan=plan,
                scan=scan,
                strategy_run=strategy_run,
                final_identity=final_identity,
            )
            if applied is None:
                try:
                    notification_intent_key = self._ensure_finalization_effects(
                        request=request,
                        plan=plan,
                        scan=scan,
                        strategy_run=strategy_run,
                        final_identity=final_identity,
                    )
                except _FinalizationSuperseded:
                    return self._finalize_superseded_candidate(
                        request=request,
                        plan=plan,
                        scan=scan,
                        scan_lock_key=scan_lock_key,
                        strategy_run=strategy_run,
                        final_identity=final_identity,
                    )
                terminal = self._record_recovered_final_attempt(
                    request=request,
                    scan=scan,
                    strategy_run=strategy_run,
                    final_identity=final_identity,
                )
            else:
                terminal, notification_intent_key = applied
            self._logical_scans.attach_final_strategy_run(
                logical_scan_run_id=scan.logical_scan_run_id,
                plan=plan,
                strategy_run_id=strategy_run.strategy_run_id,
                completed_at=self._current_time(),
                access_context=request.access_context,
            )
        except Exception as error:
            return self._pending_finalization_result(
                request=request,
                scan=scan,
                scan_lock_key=scan_lock_key,
                strategy_run=strategy_run,
                final_identity=_try_final_identity_from_run(strategy_run),
                failure_detail=type(error).__name__,
            )
        audit = dict(
            self._audit(
                request=request,
                scan=scan,
                final_identity=final_identity,
                terminal_attempt=terminal,
                market_data_actionable=True,
            )
        )
        if notification_intent_key is not None:
            audit["notification_intent_key"] = notification_intent_key
        return RunCoordinatorResult(
            disposition=RunDisposition.COMPLETED,
            strategy_run=strategy_run,
            scan_lock_key=scan_lock_key,
            logical_scan_run_id=scan.logical_scan_run_id,
            final_strategy_key=final_identity.key,
            audit=audit,
        )

    def _ensure_finalization_effects(
        self,
        *,
        request: RunCoordinatorRequest,
        plan: RotationPlan,
        scan: LogicalScanRun,
        strategy_run: StrategyRun,
        final_identity: FinalStrategyIdentity,
    ) -> str | None:
        """Persist or verify the transition and child evidence carried by one final candidate."""

        effects = _finalization_evidence(strategy_run)
        expected_run_id = strategy_run.strategy_run_id
        expected_key = final_identity.key
        current = self._recommendation_states.get_or_create(
            plan=plan,
            created_at=self._current_time(),
            access_context=request.access_context,
        )
        if (
            current.state is effects.next_state
            and current.remaining_stages_halted == effects.remaining_stages_halted
        ):
            if (
                current.finalization_strategy_run_id == expected_run_id
                and current.finalization_strategy_key == expected_key
            ):
                pass
            elif (
                current.finalization_strategy_run_id is None
                and current.finalization_strategy_key is None
                and self._is_legacy_pending_finalization(
                    request=request,
                    effects=effects,
                    scan=scan,
                    strategy_run=strategy_run,
                    final_identity=final_identity,
                )
            ):
                notification_intent_key = self._existing_notification_intent_key(
                    request=request,
                    plan=plan,
                    strategy_run=strategy_run,
                    final_identity=final_identity,
                    notification_intent_recorded=effects.notification_intent_recorded,
                )
                self._recommendation_states.record_transition(
                    plan=plan,
                    previous=current,
                    next_state=effects.next_state,
                    remaining_stages_halted=effects.remaining_stages_halted,
                    changed_at=self._current_time(),
                    access_context=request.access_context,
                    finalization_strategy_run_id=expected_run_id,
                    finalization_strategy_key=expected_key,
                )
                return notification_intent_key
            else:
                raise _FinalizationSuperseded(
                    "pending finalization lost to different durable state evidence"
                )
        elif (
            current.state is effects.previous_state
            and current.remaining_stages_halted == effects.previous_remaining_stages_halted
        ):
            self._recommendation_states.record_transition(
                plan=plan,
                previous=current,
                next_state=effects.next_state,
                remaining_stages_halted=effects.remaining_stages_halted,
                changed_at=self._current_time(),
                access_context=request.access_context,
                finalization_strategy_run_id=expected_run_id,
                finalization_strategy_key=expected_key,
            )
        else:
            raise _FinalizationSuperseded(
                "pending finalization no longer matches the durable recommendation state"
            )
        return self._record_notification_intent(
            request=request,
            plan=plan,
            strategy_run=strategy_run,
            final_identity=final_identity,
            notification_intent_recorded=effects.notification_intent_recorded,
        )

    def _is_legacy_pending_finalization(
        self,
        *,
        request: RunCoordinatorRequest,
        effects: _FinalizationEvidence,
        scan: LogicalScanRun,
        strategy_run: StrategyRun,
        final_identity: FinalStrategyIdentity,
    ) -> bool:
        """Allow only the historic no-terminal-attempt 0002 shape to claim a fence."""

        if (
            scan.status is not LogicalScanStatus.RUNNING
            or final_identity.scan_lock_key != scan.scan_lock_key
            or strategy_run.configuration_snapshot.content_hash != scan.configuration_snapshot_hash
            or (
                effects.previous_state is effects.next_state
                and (effects.previous_remaining_stages_halted == effects.remaining_stages_halted)
            )
        ):
            return False
        return not any(
            attempt.status is ScanAttemptStatus.SUCCEEDED
            and attempt.final_strategy_run_id == strategy_run.strategy_run_id
            for attempt in self._logical_scans.list_attempts(
                logical_scan_run_id=scan.logical_scan_run_id,
                access_context=request.access_context,
            )
        )

    def _existing_applied_finalization(
        self,
        *,
        request: RunCoordinatorRequest,
        plan: RotationPlan,
        scan: LogicalScanRun,
        strategy_run: StrategyRun,
        final_identity: FinalStrategyIdentity,
    ) -> tuple[ScanAttempt, str | None] | None:
        """Return immutable applied evidence that can finish attachment without re-adjudicating."""

        market_data_snapshot_id, market_data_content_hash = _market_evidence_from_run(strategy_run)
        for attempt in reversed(
            self._logical_scans.list_attempts(
                logical_scan_run_id=scan.logical_scan_run_id,
                access_context=request.access_context,
            )
        ):
            if (
                attempt.status is not ScanAttemptStatus.SUCCEEDED
                or attempt.final_strategy_run_id != strategy_run.strategy_run_id
                or attempt.finalization_disposition is FinalizationDisposition.SUPERSEDED
            ):
                continue
            if (
                attempt.final_strategy_key != final_identity.key
                or attempt.final_strategy_identity_format_version != final_identity.format_version
                or attempt.configuration_snapshot != strategy_run.configuration_snapshot
                or attempt.market_data_snapshot_id != market_data_snapshot_id
                or attempt.market_data_content_hash != market_data_content_hash
            ):
                raise RunCoordinatorInvariantError(
                    "applied final attempt does not match immutable strategy run evidence"
                )
            effects = _finalization_evidence(strategy_run)
            return (
                attempt,
                self._existing_notification_intent_key(
                    request=request,
                    plan=plan,
                    strategy_run=strategy_run,
                    final_identity=final_identity,
                    notification_intent_recorded=effects.notification_intent_recorded,
                ),
            )
        return None

    def _existing_notification_intent_key(
        self,
        *,
        request: RunCoordinatorRequest,
        plan: RotationPlan,
        strategy_run: StrategyRun,
        final_identity: FinalStrategyIdentity,
        notification_intent_recorded: bool,
    ) -> str | None:
        """Verify notification evidence already exists without emitting or rewriting an intent."""

        if not notification_intent_recorded:
            return None
        expected = build_child_intent(
            child_intent_id=uuid5(
                NAMESPACE_URL,
                f"rotation-child-intent:v1:{final_identity.key}:{strategy_run.strategy_run_id}",
            ),
            rotation_plan_id=plan.rotation_plan_id,
            portfolio_id=plan.portfolio_id,
            strategy_run_id=strategy_run.strategy_run_id,
            final_strategy_key=final_identity.key,
            purpose=ChildIntentPurpose.NOTIFICATION,
            created_at=strategy_run.occurred_at,
        )
        matches = [
            intent
            for intent in self._child_intents.list_for_strategy_run(
                strategy_run_id=strategy_run.strategy_run_id,
                access_context=request.access_context,
            )
            if (
                intent.rotation_plan_id == expected.rotation_plan_id
                and intent.portfolio_id == expected.portfolio_id
                and intent.strategy_run_id == expected.strategy_run_id
                and intent.final_strategy_key == expected.final_strategy_key
                and (
                    intent.final_strategy_identity_format_version
                    == expected.final_strategy_identity_format_version
                )
                and intent.purpose is expected.purpose
                and intent.intent_key == expected.intent_key
            )
        ]
        if len(matches) != 1:
            raise RunCoordinatorInvariantError(
                "applied finalization is missing its durable notification intent"
            )
        return matches[0].intent_key

    def _record_notification_intent(
        self,
        *,
        request: RunCoordinatorRequest,
        plan: RotationPlan,
        strategy_run: StrategyRun,
        final_identity: FinalStrategyIdentity,
        notification_intent_recorded: bool,
    ) -> str | None:
        """Record one stable notification fingerprint; this coordinator never sends it."""

        if not notification_intent_recorded:
            return None
        effects = _finalization_evidence(strategy_run)
        if (
            effects.previous_state is not RecommendationState.ACTION_PENDING
            or effects.next_state is not RecommendationState.ACTION_NOTIFIED
        ):
            raise RunCoordinatorInvariantError("notification intent requires a validated decision")
        intent = build_child_intent(
            child_intent_id=uuid5(
                NAMESPACE_URL,
                f"rotation-child-intent:v1:{final_identity.key}:{strategy_run.strategy_run_id}",
            ),
            rotation_plan_id=plan.rotation_plan_id,
            portfolio_id=plan.portfolio_id,
            strategy_run_id=strategy_run.strategy_run_id,
            final_strategy_key=final_identity.key,
            purpose=ChildIntentPurpose.NOTIFICATION,
            created_at=strategy_run.occurred_at,
        )
        stored = self._child_intents.record_or_get(
            intent=intent, access_context=request.access_context
        )
        if stored.purpose is not ChildIntentPurpose.NOTIFICATION:
            raise RunCoordinatorInvariantError(
                "notification transition recorded a different child intent"
            )
        return stored.intent_key

    def _record_recovered_final_attempt(
        self,
        *,
        request: RunCoordinatorRequest,
        scan: LogicalScanRun,
        strategy_run: StrategyRun,
        final_identity: FinalStrategyIdentity,
        finalization_disposition: FinalizationDisposition = FinalizationDisposition.APPLIED,
    ) -> ScanAttempt:
        """Append exactly one final attempt for a stored candidate that survived a failed finish."""

        attempts = self._logical_scans.list_attempts(
            logical_scan_run_id=scan.logical_scan_run_id, access_context=request.access_context
        )
        existing = next(
            (
                attempt
                for attempt in reversed(attempts)
                if (
                    attempt.status is ScanAttemptStatus.SUCCEEDED
                    and attempt.final_strategy_run_id == strategy_run.strategy_run_id
                )
            ),
            None,
        )
        if existing is not None and (
            existing.finalization_disposition is finalization_disposition
            or (
                finalization_disposition is FinalizationDisposition.APPLIED
                and existing.finalization_disposition is None
            )
        ):
            return existing
        if not attempts:
            raise RunCoordinatorInvariantError("pending finalization has no provider attempt")
        previous = attempts[-1]
        market_data_snapshot_id, market_data_content_hash = _market_evidence_from_run(strategy_run)
        terminal = ScanAttempt(
            scan_attempt_id=self._new_uuid(),
            logical_scan_run_id=scan.logical_scan_run_id,
            attempt_number=len(attempts) + 1,
            status=ScanAttemptStatus.SUCCEEDED,
            configuration_snapshot_hash=strategy_run.configuration_snapshot.content_hash,
            configuration_snapshot_id=strategy_run.configuration_snapshot.snapshot_id,
            configuration_snapshot_created_at=strategy_run.configuration_snapshot.created_at,
            market_data_snapshot_id=market_data_snapshot_id,
            market_data_content_hash=market_data_content_hash,
            final_strategy_key=final_identity.key,
            final_strategy_identity_format_version=final_identity.format_version,
            trigger_correlation_id=request.correlation_id,
            actor_correlation_id=f"actor:{request.access_context.actor_user_id}",
            started_at=previous.started_at,
            completed_at=self._current_time(),
            recovery_of_attempt_id=previous.scan_attempt_id,
            final_strategy_run_id=strategy_run.strategy_run_id,
            finalization_disposition=finalization_disposition,
        )
        self._logical_scans.record_attempt(attempt=terminal, access_context=request.access_context)
        return terminal

    def _finalize_superseded_candidate(
        self,
        *,
        request: RunCoordinatorRequest,
        plan: RotationPlan,
        scan: LogicalScanRun,
        scan_lock_key: str,
        strategy_run: StrategyRun,
        final_identity: FinalStrategyIdentity,
    ) -> RunCoordinatorResult:
        """Terminally audit a candidate that lost its finalization fence without side effects."""

        try:
            terminal = self._record_recovered_final_attempt(
                request=request,
                scan=scan,
                strategy_run=strategy_run,
                final_identity=final_identity,
                finalization_disposition=FinalizationDisposition.SUPERSEDED,
            )
            self._logical_scans.attach_final_strategy_run(
                logical_scan_run_id=scan.logical_scan_run_id,
                plan=plan,
                strategy_run_id=strategy_run.strategy_run_id,
                completed_at=self._current_time(),
                access_context=request.access_context,
            )
        except Exception as error:
            return self._pending_finalization_result(
                request=request,
                scan=scan,
                scan_lock_key=scan_lock_key,
                strategy_run=strategy_run,
                final_identity=final_identity,
                failure_detail=type(error).__name__,
            )
        return self._superseded_result(
            request=request,
            scan=scan,
            scan_lock_key=scan_lock_key,
            strategy_run=strategy_run,
            final_identity=final_identity,
            terminal_attempt=terminal,
        )

    def _superseded_result(
        self,
        *,
        request: RunCoordinatorRequest,
        scan: LogicalScanRun,
        scan_lock_key: str,
        strategy_run: StrategyRun,
        final_identity: FinalStrategyIdentity,
        terminal_attempt: ScanAttempt,
    ) -> RunCoordinatorResult:
        """Render the durable no-op outcome from a superseded final attempt."""

        audit = dict(
            self._audit(
                request=request,
                scan=scan,
                final_identity=final_identity,
                terminal_attempt=terminal_attempt,
                market_data_actionable=True,
            )
        )
        audit["attempt_disposition"] = RunDisposition.SUPERSEDED.value
        audit["failure_code"] = "FINALIZATION_SUPERSEDED"
        return RunCoordinatorResult(
            disposition=RunDisposition.SUPERSEDED,
            strategy_run=strategy_run,
            scan_lock_key=scan_lock_key,
            logical_scan_run_id=scan.logical_scan_run_id,
            final_strategy_key=final_identity.key,
            audit=audit,
        )

    def _pending_finalization_result(
        self,
        *,
        request: RunCoordinatorRequest,
        scan: LogicalScanRun,
        scan_lock_key: str,
        strategy_run: StrategyRun,
        final_identity: FinalStrategyIdentity | None,
        failure_detail: str | None = None,
    ) -> RunCoordinatorResult:
        """Report a recoverable finalization gap without appending conflicting evidence."""

        audit = dict(
            self._audit(
                request=request,
                scan=scan,
                final_identity=final_identity,
                terminal_attempt=None,
                market_data_actionable=True,
            )
        )
        audit["attempt_disposition"] = RunDisposition.FAILED.value
        audit["failure_code"] = "FINALIZATION_PENDING"
        if failure_detail is not None:
            audit["failure_detail"] = failure_detail
        return RunCoordinatorResult(
            disposition=RunDisposition.FAILED,
            strategy_run=None,
            scan_lock_key=scan_lock_key,
            logical_scan_run_id=scan.logical_scan_run_id,
            final_strategy_key=(
                _final_key_from_run(strategy_run) if final_identity is None else final_identity.key
            ),
            audit=audit,
        )

    def _completed_result(
        self,
        *,
        request: RunCoordinatorRequest,
        scan: LogicalScanRun,
        scan_lock_key: str,
    ) -> RunCoordinatorResult:
        stored_run = self._strategy_runs.get_for_logical_scan(
            logical_scan_run_id=scan.logical_scan_run_id,
            access_context=request.access_context,
        )
        if stored_run is None:
            raise RunCoordinatorInvariantError("completed scan is missing its final strategy run")
        final_identity = _final_identity_from_run(stored_run)
        terminal = next(
            (
                attempt
                for attempt in reversed(
                    self._logical_scans.list_attempts(
                        logical_scan_run_id=scan.logical_scan_run_id,
                        access_context=request.access_context,
                    )
                )
                if (
                    attempt.status is ScanAttemptStatus.SUCCEEDED
                    and attempt.final_strategy_run_id == stored_run.strategy_run_id
                )
            ),
            None,
        )
        if terminal is None:
            terminal = self._record_recovered_final_attempt(
                request=request,
                scan=scan,
                strategy_run=stored_run,
                final_identity=final_identity,
            )
        if terminal.finalization_disposition is FinalizationDisposition.SUPERSEDED:
            return self._superseded_result(
                request=request,
                scan=scan,
                scan_lock_key=scan_lock_key,
                strategy_run=stored_run,
                final_identity=final_identity,
                terminal_attempt=terminal,
            )
        return RunCoordinatorResult(
            disposition=RunDisposition.COMPLETED,
            strategy_run=stored_run,
            scan_lock_key=scan_lock_key,
            logical_scan_run_id=scan.logical_scan_run_id,
            final_strategy_key=_final_key_from_run(stored_run),
            audit=self._audit(
                request=request,
                scan=scan,
                final_identity=None,
                terminal_attempt=terminal,
                market_data_actionable=True,
            ),
        )

    def _duplicate_result(
        self, *, request: RunCoordinatorRequest, scan_lock_key: str
    ) -> RunCoordinatorResult:
        disposition = (
            RunDisposition.API_CONFLICT
            if request.trigger_source is TriggerSource.API
            else RunDisposition.SCHEDULER_SKIPPED
        )
        reference = ExistingResultReference(scan_lock_key=scan_lock_key, logical_scan_run_id=None)
        return RunCoordinatorResult(
            disposition=disposition,
            strategy_run=None,
            scan_lock_key=scan_lock_key,
            logical_scan_run_id=None,
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
            configuration_snapshot_id=request.configuration_snapshot.snapshot_id,
            configuration_snapshot_created_at=request.configuration_snapshot.created_at,
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
        final_identity: FinalStrategyIdentity,
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
            configuration_snapshot_id=request.configuration_snapshot.snapshot_id,
            configuration_snapshot_created_at=request.configuration_snapshot.created_at,
            market_data_snapshot_id=snapshot.snapshot_id,
            market_data_content_hash=snapshot.content_hash,
            final_strategy_key=final_identity.key,
            final_strategy_identity_format_version=final_identity.format_version,
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
        final_identity: FinalStrategyIdentity | None = None,
    ) -> RunCoordinatorResult:
        terminal = self._failed_terminal_attempt(
            request=request,
            scan=scan,
            running_attempt=running_attempt,
            snapshot=snapshot,
            final_identity=final_identity,
            failure_code=failure_code,
            failure_detail=failure_detail,
        )
        return RunCoordinatorResult(
            disposition=RunDisposition.FAILED,
            strategy_run=None,
            scan_lock_key=scan_lock_key,
            logical_scan_run_id=scan.logical_scan_run_id,
            final_strategy_key=None if final_identity is None else final_identity.key,
            audit=self._audit(
                request=request,
                scan=scan,
                final_identity=final_identity,
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
        final_identity: FinalStrategyIdentity | None,
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
            configuration_snapshot_id=request.configuration_snapshot.snapshot_id,
            configuration_snapshot_created_at=request.configuration_snapshot.created_at,
            market_data_snapshot_id=None if snapshot is None else snapshot.snapshot_id,
            market_data_content_hash=None if snapshot is None else snapshot.content_hash,
            final_strategy_key=None if final_identity is None else final_identity.key,
            final_strategy_identity_format_version=(
                None if final_identity is None else final_identity.format_version
            ),
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
            "configuration_snapshot_id": str(request.configuration_snapshot.snapshot_id),
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


def _configuration_reference(snapshot: PersistedConfigurationSnapshot) -> ConfigurationSnapshotRef:
    resolved = snapshot.resolved_snapshot
    return ConfigurationSnapshotRef(
        snapshot.snapshot_id,
        resolved.content_hash,
        resolved.created_at,
    )


def _final_key_from_run(run: StrategyRun) -> str | None:
    idempotency = run.outputs.get("idempotency")
    if not isinstance(idempotency, Mapping):
        return None
    final_strategy_key = idempotency.get("final_strategy_key")
    if not isinstance(final_strategy_key, str) or len(final_strategy_key) != 64:
        return None
    return final_strategy_key


def _finalization_evidence(run: StrategyRun) -> _FinalizationEvidence:
    """Decode the complete transition facts embedded in a coordinator-built strategy run."""

    recommendation = run.outputs.get("recommendation")
    if not isinstance(recommendation, Mapping):
        raise RunCoordinatorInvariantError(
            "strategy run is missing recommendation finalization evidence"
        )
    previous_state = recommendation.get("previous_state")
    next_state = recommendation.get("next_state")
    previous_halted = recommendation.get("previous_remaining_stages_halted")
    remaining_halted = recommendation.get("remaining_stages_halted")
    notification_recorded = recommendation.get("notification_intent_recorded")
    if not isinstance(previous_state, str) or not isinstance(next_state, str):
        raise RunCoordinatorInvariantError(
            "strategy run recommendation state evidence is malformed"
        )
    if (
        type(previous_halted) is not bool
        or type(remaining_halted) is not bool
        or type(notification_recorded) is not bool
    ):
        raise RunCoordinatorInvariantError(
            "strategy run recommendation guard evidence is malformed"
        )
    try:
        effects = _FinalizationEvidence(
            previous_state=RecommendationState(previous_state),
            previous_remaining_stages_halted=previous_halted,
            next_state=RecommendationState(next_state),
            remaining_stages_halted=remaining_halted,
            notification_intent_recorded=notification_recorded,
        )
    except ValueError as error:
        raise RunCoordinatorInvariantError(
            "strategy run recommendation state evidence is malformed"
        ) from error
    if run.state_transition != f"{effects.previous_state.value}->{effects.next_state.value}":
        raise RunCoordinatorInvariantError(
            "strategy run state transition does not match its evidence"
        )
    return effects


def _market_evidence_from_run(run: StrategyRun) -> tuple[str, str]:
    """Restore the immutable market identity needed for a finalization-only retry."""

    market_data = run.outputs.get("market_data")
    if not isinstance(market_data, Mapping):
        raise RunCoordinatorInvariantError(
            "strategy run is missing market-data finalization evidence"
        )
    snapshot_id = market_data.get("snapshot_id")
    content_hash = market_data.get("content_hash")
    if not isinstance(snapshot_id, str) or not isinstance(content_hash, str):
        raise RunCoordinatorInvariantError("strategy run market-data evidence is malformed")
    if snapshot_id != run.market_data_snapshot_id:
        raise RunCoordinatorInvariantError(
            "strategy run market-data identity does not match its evidence"
        )
    return snapshot_id, content_hash


def _final_identity_from_run(run: StrategyRun) -> FinalStrategyIdentity:
    """Rebuild and verify the two-phase final identity retained by a stored candidate."""

    idempotency = run.outputs.get("idempotency")
    if not isinstance(idempotency, Mapping):
        raise RunCoordinatorInvariantError("strategy run is missing idempotency evidence")
    scan_lock_key = idempotency.get("scan_lock_key")
    final_strategy_key = idempotency.get("final_strategy_key")
    if not isinstance(scan_lock_key, str) or not isinstance(final_strategy_key, str):
        raise RunCoordinatorInvariantError("strategy run idempotency evidence is malformed")
    market_data_snapshot_id, market_data_content_hash = _market_evidence_from_run(run)
    identity = FinalStrategyIdentity(
        scan_lock_key=scan_lock_key,
        market_data_snapshot_id=market_data_snapshot_id,
        market_data_content_hash=market_data_content_hash,
    )
    if identity.key != final_strategy_key:
        raise RunCoordinatorInvariantError(
            "strategy run final identity does not match its evidence"
        )
    return identity


def _try_final_identity_from_run(run: StrategyRun) -> FinalStrategyIdentity | None:
    """Keep a malformed pending candidate auditable without claiming a reconstructed identity."""

    try:
        return _final_identity_from_run(run)
    except RunCoordinatorInvariantError:
        return None
