"""SQLAlchemy implementations of the scoped repository ports."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, insert, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.application.configuration import ConfigurationLayer
from app.application.configuration_snapshots import PersistedConfigurationSnapshot
from app.domain.access import AccessContext
from app.domain.entities import LogicalScanRun, Position, RotationPlan, ScanAttempt, StrategyRun
from app.domain.enums import LogicalScanStatus, Scope
from app.domain.errors import (
    IdempotencyConflictError,
    LogicalScanAlreadyCompletedError,
    NotFoundForActor,
    PlanReferenceUnauthorizedError,
    PositionAlreadyOpenError,
)
from app.domain.values import IdempotencyKey, Ownership
from app.persistence.mappers import (
    configuration_layer_from_model,
    configuration_layer_to_model,
    configuration_snapshot_from_model,
    configuration_snapshot_to_model,
    logical_scan_from_model,
    logical_scan_to_model,
    position_from_model,
    position_to_model,
    rotation_plan_from_model,
    rotation_plan_to_model,
    scan_attempt_from_model,
    scan_attempt_to_model,
    strategy_run_from_model,
    strategy_run_to_model,
)
from app.persistence.models import (
    CandidateGroupModel,
    ConfigurationLayerModel,
    ConfigurationSnapshotModel,
    LogicalScanRunModel,
    PortfolioModel,
    PositionModel,
    RotationPlanModel,
    ScanAttemptModel,
    StrategyRunModel,
    rotation_plan_candidate_groups,
    rotation_plan_protected_positions,
    rotation_plan_source_positions,
)
from app.repositories.configuration_layers import (
    ConfigurationLayerSelection,
    ConfigurationLayerSelectionEntry,
)
from app.repositories.locks import ScanLockRequest
from app.repositories.logical_scans import validate_scan_attempt_append
from app.repositories.strategy_runs import require_exact_strategy_run_retry
from app.schemas.common import ConfigurationLayerScope


class _SqlAlchemyScopedRepository:
    """Shared database ownership lookup and enumeration-safe access enforcement."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _portfolio_owner(self, portfolio_id: UUID) -> UUID:
        owner_id = self._session.scalar(
            select(PortfolioModel.user_id).where(PortfolioModel.portfolio_id == portfolio_id)
        )
        if owner_id is None:
            raise NotFoundForActor("portfolio was not found for actor")
        return owner_id

    def _require_portfolio_read(self, portfolio_id: UUID, access_context: AccessContext) -> UUID:
        owner_id = self._portfolio_owner(portfolio_id)
        access_context.require_read(
            Ownership(Scope.PORTFOLIO, portfolio_id), portfolio_owner_id=owner_id
        )
        return owner_id

    def _require_portfolio_mutation(
        self, portfolio_id: UUID, access_context: AccessContext
    ) -> UUID:
        owner_id = self._portfolio_owner(portfolio_id)
        access_context.require_mutation(
            Ownership(Scope.PORTFOLIO, portfolio_id), portfolio_owner_id=owner_id
        )
        return owner_id


class SqlAlchemyPositionRepository(_SqlAlchemyScopedRepository):
    """Position repository that relies on the database partial index for final integrity."""

    def add(self, position: Position, *, access_context: AccessContext) -> None:
        self._require_portfolio_mutation(position.portfolio_id, access_context)
        try:
            with self._session.begin_nested():
                self._session.add(position_to_model(position))
                self._session.flush()
        except IntegrityError as error:
            if _is_open_position_unique_conflict(error):
                raise PositionAlreadyOpenError(
                    "only one OPEN position is allowed for a portfolio/instrument pair"
                ) from error
            raise

    def get(self, position_id: UUID, *, access_context: AccessContext) -> Position:
        model = self._session.get(PositionModel, position_id)
        if model is None:
            raise NotFoundForActor("position was not found for actor")
        self._require_portfolio_read(model.portfolio_id, access_context)
        return position_from_model(model)

    def list_for_portfolio(
        self, portfolio_id: UUID, *, access_context: AccessContext
    ) -> Sequence[Position]:
        self._require_portfolio_read(portfolio_id, access_context)
        models = self._session.scalars(
            select(PositionModel).where(PositionModel.portfolio_id == portfolio_id)
        ).all()
        return tuple(position_from_model(model) for model in models)


class SqlAlchemyConfigurationLayerRepository(_SqlAlchemyScopedRepository):
    """Configuration loader that binds every portfolio-owned layer to one exact plan."""

    def add(self, layer: ConfigurationLayer, *, access_context: AccessContext) -> None:
        if layer.ownership.scope is Scope.PORTFOLIO:
            portfolio_id = layer.ownership.owner_id
            if portfolio_id is None:
                raise ValueError("portfolio configuration layer owner is required")
            owner_id = self._require_portfolio_mutation(portfolio_id, access_context)
            if layer.portfolio_owner_id != owner_id:
                raise ValueError("configuration layer portfolio owner does not match portfolio")
        else:
            access_context.require_mutation(
                layer.ownership, portfolio_owner_id=layer.portfolio_owner_id
            )
        self._session.add(configuration_layer_to_model(layer))
        self._session.flush()

    def load_for_plan(
        self,
        *,
        plan: RotationPlan,
        selection: ConfigurationLayerSelection,
        access_context: AccessContext,
    ) -> Sequence[ConfigurationLayer]:
        plan_owner_id = self._require_portfolio_read(plan.portfolio_id, access_context)
        if not isinstance(selection, ConfigurationLayerSelection):
            raise TypeError("selection must be a ConfigurationLayerSelection")
        selection.validate_for_plan(plan)
        predicates = [
            self._selection_predicate(entry=entry, plan=plan, plan_owner_id=plan_owner_id)
            for entry in selection.ordered_entries
        ]
        if not predicates:
            return ()
        models = self._session.scalars(
            select(ConfigurationLayerModel).where(or_(*predicates))
        ).all()
        selected: list[ConfigurationLayer] = []
        for entry in selection.ordered_entries:
            matches = [
                model
                for model in models
                if (
                    model.scope == entry.scope.value
                    and model.reference_id == entry.reference_id
                    and model.version == entry.version
                    and model.content_hash == entry.content_hash
                )
            ]
            if not matches:
                raise ValueError("selected configuration layer is missing")
            if len(matches) > 1:
                raise ValueError("duplicate selected configuration layer identity")
            selected.append(configuration_layer_from_model(matches[0]))
        return tuple(selected)

    @staticmethod
    def _selection_predicate(
        *,
        entry: ConfigurationLayerSelectionEntry,
        plan: RotationPlan,
        plan_owner_id: UUID,
    ) -> ColumnElement[bool]:
        identity = and_(
            ConfigurationLayerModel.scope == entry.scope.value,
            ConfigurationLayerModel.reference_id == entry.reference_id,
            ConfigurationLayerModel.version == entry.version,
            ConfigurationLayerModel.content_hash == entry.content_hash,
        )
        if entry.scope in {
            ConfigurationLayerScope.SYSTEM,
            ConfigurationLayerScope.MARKET,
            ConfigurationLayerScope.STRATEGY,
        }:
            ownership = and_(
                ConfigurationLayerModel.ownership_scope == Scope.SYSTEM.value,
                ConfigurationLayerModel.owner_id.is_(None),
                ConfigurationLayerModel.portfolio_owner_id.is_(None),
            )
        elif entry.scope is ConfigurationLayerScope.USER:
            ownership = and_(
                ConfigurationLayerModel.ownership_scope == Scope.USER.value,
                ConfigurationLayerModel.owner_id == plan_owner_id,
                ConfigurationLayerModel.portfolio_owner_id.is_(None),
            )
        else:
            ownership = and_(
                ConfigurationLayerModel.ownership_scope == Scope.PORTFOLIO.value,
                ConfigurationLayerModel.owner_id == plan.portfolio_id,
                ConfigurationLayerModel.portfolio_owner_id == plan_owner_id,
            )
        return and_(identity, ownership)


class SqlAlchemyConfigurationSnapshotRepository(_SqlAlchemyScopedRepository):
    """Scoped canonical snapshot adapter with exact immutable retry behavior."""

    def record_or_get(
        self,
        *,
        snapshot: PersistedConfigurationSnapshot,
        access_context: AccessContext,
    ) -> PersistedConfigurationSnapshot:
        model = configuration_snapshot_to_model(snapshot)
        canonical_candidate = configuration_snapshot_from_model(model)
        self._require_snapshot_mutation(canonical_candidate, access_context)
        existing_model = self._session.get(
            ConfigurationSnapshotModel, canonical_candidate.snapshot_id
        )
        if existing_model is not None:
            return self._exact_snapshot_retry(
                existing_model=existing_model,
                candidate=canonical_candidate,
                access_context=access_context,
            )
        existing_model = self._target_version_model(canonical_candidate)
        if existing_model is not None:
            return self._target_version_retry(
                existing_model=existing_model,
                candidate=canonical_candidate,
                access_context=access_context,
            )
        try:
            with self._session.begin_nested():
                self._session.add(model)
                self._session.flush()
        except IntegrityError as error:
            existing_model = self._session.get(
                ConfigurationSnapshotModel, canonical_candidate.snapshot_id
            )
            if existing_model is None:
                existing_model = self._target_version_model(canonical_candidate)
                if existing_model is None:
                    raise error
                return self._target_version_retry(
                    existing_model=existing_model,
                    candidate=canonical_candidate,
                    access_context=access_context,
                )
            return self._exact_snapshot_retry(
                existing_model=existing_model,
                candidate=canonical_candidate,
                access_context=access_context,
            )
        return canonical_candidate

    def get(
        self,
        snapshot_id: UUID,
        *,
        access_context: AccessContext,
    ) -> PersistedConfigurationSnapshot:
        model = self._session.get(ConfigurationSnapshotModel, snapshot_id)
        if model is None:
            raise NotFoundForActor("configuration snapshot was not found for actor")
        self._require_model_snapshot_read(model, access_context)
        return configuration_snapshot_from_model(model)

    def _exact_snapshot_retry(
        self,
        *,
        existing_model: ConfigurationSnapshotModel,
        candidate: PersistedConfigurationSnapshot,
        access_context: AccessContext,
    ) -> PersistedConfigurationSnapshot:
        self._require_model_snapshot_read(existing_model, access_context)
        existing = configuration_snapshot_from_model(existing_model)
        if existing != candidate:
            raise IdempotencyConflictError(
                "configuration snapshot id already records different immutable evidence"
            )
        return existing

    def _target_version_retry(
        self,
        *,
        existing_model: ConfigurationSnapshotModel,
        candidate: PersistedConfigurationSnapshot,
        access_context: AccessContext,
    ) -> PersistedConfigurationSnapshot:
        self._require_model_snapshot_read(existing_model, access_context)
        existing = configuration_snapshot_from_model(existing_model)
        if existing != candidate:
            raise IdempotencyConflictError(
                "configuration snapshot target and version already record different evidence"
            )
        return existing

    def _require_snapshot_mutation(
        self, snapshot: PersistedConfigurationSnapshot, access_context: AccessContext
    ) -> None:
        ownership = snapshot.target_ownership
        if ownership.scope is Scope.PORTFOLIO:
            assert ownership.owner_id is not None
            self._require_portfolio_mutation(ownership.owner_id, access_context)
            return
        access_context.require_mutation(ownership)

    def _target_version_model(
        self, snapshot: PersistedConfigurationSnapshot
    ) -> ConfigurationSnapshotModel | None:
        ownership = snapshot.target_ownership
        statement = select(ConfigurationSnapshotModel).where(
            ConfigurationSnapshotModel.target_scope == ownership.scope.value,
            ConfigurationSnapshotModel.target_reference_id == snapshot.target_reference_id,
            ConfigurationSnapshotModel.config_version == snapshot.config_version,
        )
        if ownership.owner_id is None:
            statement = statement.where(ConfigurationSnapshotModel.target_owner_id.is_(None))
        else:
            statement = statement.where(
                ConfigurationSnapshotModel.target_owner_id == ownership.owner_id
            )
        return self._session.scalar(statement)

    def _require_model_snapshot_read(
        self, model: ConfigurationSnapshotModel, access_context: AccessContext
    ) -> None:
        try:
            ownership = Ownership(Scope(model.target_scope), model.target_owner_id)
        except ValueError as error:
            raise ValueError(
                "stored configuration snapshot target ownership is malformed"
            ) from error
        if ownership.scope is Scope.PORTFOLIO:
            assert ownership.owner_id is not None
            self._require_portfolio_read(ownership.owner_id, access_context)
            return
        access_context.require_read(ownership)


class SqlAlchemyLogicalScanRepository(_SqlAlchemyScopedRepository):
    """Recover logical scans by deterministic lock key and retain provider attempts."""

    def create_or_recover(
        self,
        *,
        request: ScanLockRequest,
        created_at: datetime,
        access_context: AccessContext,
    ) -> LogicalScanRun:
        self._require_portfolio_mutation(request.plan.portfolio_id, access_context)
        existing = self._session.scalar(
            select(LogicalScanRunModel).where(LogicalScanRunModel.scan_lock_key == request.key)
        )
        if existing is not None:
            return self._for_request(existing, request, access_context)
        scan = LogicalScanRun(
            logical_scan_run_id=uuid4(),
            rotation_plan_id=request.plan.rotation_plan_id,
            portfolio_id=request.plan.portfolio_id,
            market_session_date=request.market_session_date,
            scan_window_start=request.scan_window_start,
            scan_interval=request.scan_interval,
            scan_lock_key=request.key,
            status=LogicalScanStatus.RUNNING,
            created_at=created_at,
        )
        try:
            with self._session.begin_nested():
                self._session.add(logical_scan_to_model(scan))
                self._session.flush()
        except IntegrityError as error:
            if not _is_scan_lock_conflict(error):
                raise
            existing = self._session.scalar(
                select(LogicalScanRunModel).where(LogicalScanRunModel.scan_lock_key == request.key)
            )
            if existing is None:
                raise error
            return self._for_request(existing, request, access_context)
        return scan

    def get_by_lock_key(
        self, *, request: ScanLockRequest, access_context: AccessContext
    ) -> LogicalScanRun | None:
        self._require_portfolio_read(request.plan.portfolio_id, access_context)
        model = self._session.scalar(
            select(LogicalScanRunModel).where(LogicalScanRunModel.scan_lock_key == request.key)
        )
        if model is None:
            return None
        return self._for_request(model, request, access_context)

    def get(self, logical_scan_run_id: UUID, *, access_context: AccessContext) -> LogicalScanRun:
        model = self._session.get(LogicalScanRunModel, logical_scan_run_id)
        if model is None:
            raise NotFoundForActor("logical scan was not found for actor")
        portfolio_id = self._plan_portfolio_id(model.rotation_plan_id)
        self._require_portfolio_read(portfolio_id, access_context)
        return self._to_domain(model, portfolio_id=portfolio_id)

    def record_attempt(self, *, attempt: ScanAttempt, access_context: AccessContext) -> None:
        scan = self.get(attempt.logical_scan_run_id, access_context=access_context)
        self._require_portfolio_mutation(scan.portfolio_id, access_context)
        existing_attempts = self.list_attempts(
            logical_scan_run_id=attempt.logical_scan_run_id,
            access_context=access_context,
        )
        self._validate_attempt_references(attempt, scan=scan)
        if scan.status is LogicalScanStatus.COMPLETED and attempt.final_strategy_run_id is None:
            raise LogicalScanAlreadyCompletedError(
                "completed logical scans can record only final strategy run evidence"
            )
        validate_scan_attempt_append(existing_attempts=existing_attempts, candidate=attempt)
        with self._session.begin_nested():
            self._session.add(scan_attempt_to_model(attempt))
            self._session.flush()

    def _validate_attempt_references(self, attempt: ScanAttempt, *, scan: LogicalScanRun) -> None:
        if attempt.recovery_of_attempt_id is not None:
            recovered = self._session.scalar(
                select(ScanAttemptModel.scan_attempt_id).where(
                    ScanAttemptModel.logical_scan_run_id == attempt.logical_scan_run_id,
                    ScanAttemptModel.scan_attempt_id == attempt.recovery_of_attempt_id,
                )
            )
            if recovered != attempt.recovery_of_attempt_id:
                raise ValueError(
                    "scan attempt recovery reference must exist in the same logical scan"
                )
        if attempt.duplicate_of_attempt_id is not None:
            duplicate = self._session.scalar(
                select(ScanAttemptModel).where(
                    ScanAttemptModel.logical_scan_run_id == attempt.logical_scan_run_id,
                    ScanAttemptModel.scan_attempt_id == attempt.duplicate_of_attempt_id,
                )
            )
            if duplicate is None:
                raise ValueError(
                    "scan attempt duplicate reference must exist in the same logical scan"
                )
            if duplicate.status != "SUCCEEDED":
                raise ValueError("scan attempt duplicate reference must target a succeeded attempt")
        if attempt.final_strategy_run_id is not None:
            if scan.status is not LogicalScanStatus.COMPLETED:
                raise ValueError("final strategy run reference requires a completed final scan")
            if scan.final_strategy_run_id != attempt.final_strategy_run_id:
                raise ValueError("final strategy run reference must be the scan's actual final run")
            final_run_id = self._session.scalar(
                select(StrategyRunModel.strategy_run_id).where(
                    StrategyRunModel.logical_scan_run_id == attempt.logical_scan_run_id,
                    StrategyRunModel.strategy_run_id == attempt.final_strategy_run_id,
                )
            )
            if final_run_id is None:
                raise ValueError("final strategy run reference must be the scan's actual final run")

    def list_attempts(
        self, *, logical_scan_run_id: UUID, access_context: AccessContext
    ) -> Sequence[ScanAttempt]:
        self.get(logical_scan_run_id, access_context=access_context)
        models = self._session.scalars(
            select(ScanAttemptModel)
            .where(ScanAttemptModel.logical_scan_run_id == logical_scan_run_id)
            .order_by(ScanAttemptModel.attempt_number)
        ).all()
        return tuple(scan_attempt_from_model(model) for model in models)

    def attach_final_strategy_run(
        self,
        *,
        logical_scan_run_id: UUID,
        plan: RotationPlan,
        strategy_run_id: UUID,
        completed_at: datetime,
        access_context: AccessContext,
    ) -> LogicalScanRun:
        model = self._session.get(LogicalScanRunModel, logical_scan_run_id)
        if model is None:
            raise NotFoundForActor("logical scan was not found for actor")
        portfolio_id = self._plan_portfolio_id(model.rotation_plan_id)
        self._require_portfolio_mutation(portfolio_id, access_context)
        if model.rotation_plan_id != plan.rotation_plan_id or portfolio_id != plan.portfolio_id:
            raise ValueError("logical scan must belong to the supplied rotation plan")
        if model.status == LogicalScanStatus.COMPLETED.value:
            raise LogicalScanAlreadyCompletedError("logical scan already has a final strategy run")
        stored_final = self._session.scalar(
            select(StrategyRunModel.strategy_run_id).where(
                StrategyRunModel.strategy_run_id == strategy_run_id,
                StrategyRunModel.logical_scan_run_id == logical_scan_run_id,
            )
        )
        if stored_final != strategy_run_id:
            raise ValueError("logical scan final strategy run was not persisted")
        model.status = LogicalScanStatus.COMPLETED.value
        model.completed_at = completed_at
        self._session.flush()
        return self._to_domain(model, portfolio_id=portfolio_id)

    def _for_request(
        self,
        model: LogicalScanRunModel,
        request: ScanLockRequest,
        access_context: AccessContext,
    ) -> LogicalScanRun:
        portfolio_id = self._plan_portfolio_id(model.rotation_plan_id)
        if (
            model.rotation_plan_id != request.plan.rotation_plan_id
            or portfolio_id != request.plan.portfolio_id
        ):
            raise ValueError("scan lock key resolves to a different rotation plan")
        self._require_portfolio_read(portfolio_id, access_context)
        return self._to_domain(model, portfolio_id=portfolio_id)

    def _plan_portfolio_id(self, rotation_plan_id: UUID) -> UUID:
        portfolio_id = self._session.scalar(
            select(RotationPlanModel.portfolio_id).where(
                RotationPlanModel.rotation_plan_id == rotation_plan_id
            )
        )
        if portfolio_id is None:
            raise ValueError("logical scan references a missing rotation plan")
        return portfolio_id

    def _to_domain(self, model: LogicalScanRunModel, *, portfolio_id: UUID) -> LogicalScanRun:
        final_strategy_run_id = None
        if model.status == LogicalScanStatus.COMPLETED.value:
            final_strategy_run_id = self._session.scalar(
                select(StrategyRunModel.strategy_run_id).where(
                    StrategyRunModel.logical_scan_run_id == model.logical_scan_run_id
                )
            )
        return logical_scan_from_model(
            model,
            portfolio_id=portfolio_id,
            final_strategy_run_id=final_strategy_run_id,
        )


class SqlAlchemyStrategyRunRepository(_SqlAlchemyScopedRepository):
    """Idempotent final strategy-run persistence implementation."""

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self._logical_scans = SqlAlchemyLogicalScanRepository(session)

    def record_or_get(
        self,
        *,
        plan: RotationPlan,
        run: StrategyRun,
        idempotency_key: IdempotencyKey,
        logical_scan_run_id: UUID | None = None,
        access_context: AccessContext,
    ) -> StrategyRun:
        plan_owner_id = self._require_portfolio_mutation(plan.portfolio_id, access_context)
        if run.rotation_plan_id != plan.rotation_plan_id or run.portfolio_id != plan.portfolio_id:
            raise ValueError("strategy run must belong to the supplied rotation plan")
        self._validate_configuration_snapshot_reference(run, plan=plan, plan_owner_id=plan_owner_id)
        stored = self._session.scalar(
            select(StrategyRunModel).where(
                StrategyRunModel.rotation_plan_id == plan.rotation_plan_id,
                StrategyRunModel.idempotency_key == idempotency_key.value,
            )
        )
        if stored is not None:
            return _exact_strategy_run_retry(
                stored=stored,
                candidate=run,
                logical_scan_run_id=logical_scan_run_id,
            )
        if logical_scan_run_id is not None:
            scan = self._logical_scans.get(logical_scan_run_id, access_context=access_context)
            if (
                scan.rotation_plan_id != plan.rotation_plan_id
                or scan.portfolio_id != plan.portfolio_id
            ):
                raise ValueError("logical scan must belong to the supplied rotation plan")
            if scan.status is LogicalScanStatus.COMPLETED:
                raise LogicalScanAlreadyCompletedError(
                    "logical scan already has a final strategy run"
                )
        try:
            with self._session.begin_nested():
                self._session.add(
                    strategy_run_to_model(
                        run,
                        idempotency_key=idempotency_key.value,
                        logical_scan_run_id=logical_scan_run_id,
                    )
                )
                self._session.flush()
                if logical_scan_run_id is not None:
                    self._logical_scans.attach_final_strategy_run(
                        logical_scan_run_id=logical_scan_run_id,
                        plan=plan,
                        strategy_run_id=run.strategy_run_id,
                        completed_at=run.occurred_at,
                        access_context=access_context,
                    )
        except IntegrityError as error:
            if not _is_strategy_run_idempotency_conflict(error):
                if _is_logical_scan_final_conflict(error):
                    raise LogicalScanAlreadyCompletedError(
                        "logical scan already has a final strategy run"
                    ) from error
                raise
            existing = self._session.scalar(
                select(StrategyRunModel).where(
                    StrategyRunModel.rotation_plan_id == plan.rotation_plan_id,
                    StrategyRunModel.idempotency_key == idempotency_key.value,
                )
            )
            if existing is None:
                raise error
            return _exact_strategy_run_retry(
                stored=existing,
                candidate=run,
                logical_scan_run_id=logical_scan_run_id,
            )
        return run

    def _validate_configuration_snapshot_reference(
        self, run: StrategyRun, *, plan: RotationPlan, plan_owner_id: UUID
    ) -> None:
        """Require a run to cite the exact validated snapshot persisted for its decision."""

        declared_reference = run.configuration_snapshot
        model = self._session.get(ConfigurationSnapshotModel, declared_reference.snapshot_id)
        if model is None:
            raise ValueError("configuration snapshot was not found")
        persisted_snapshot = configuration_snapshot_from_model(model)
        persisted_reference = persisted_snapshot.resolved_snapshot
        if (
            persisted_snapshot.snapshot_id != declared_reference.snapshot_id
            or persisted_reference.content_hash != declared_reference.content_hash
            or persisted_reference.created_at != declared_reference.created_at
        ):
            raise ValueError(
                "strategy run configuration snapshot does not match persisted canonical snapshot"
            )
        target_ownership = persisted_snapshot.target_ownership
        if target_ownership.scope is Scope.SYSTEM:
            return
        if target_ownership.scope is Scope.USER and target_ownership.owner_id == plan_owner_id:
            return
        if (
            target_ownership.scope is Scope.PORTFOLIO
            and target_ownership.owner_id == plan.portfolio_id
        ):
            return
        raise ValueError("configuration snapshot target ownership does not authorize plan")


def _exact_strategy_run_retry(
    *,
    stored: StrategyRunModel,
    candidate: StrategyRun,
    logical_scan_run_id: UUID | None,
) -> StrategyRun:
    """Decode one stored final result and permit only an exact idempotent replay."""

    existing = strategy_run_from_model(stored)
    require_exact_strategy_run_retry(
        existing=existing,
        candidate=candidate,
        stored_logical_scan_run_id=stored.logical_scan_run_id,
        requested_logical_scan_run_id=logical_scan_run_id,
    )
    return existing


def _is_open_position_unique_conflict(error: IntegrityError) -> bool:
    """Recognize only the partial OPEN position uniqueness violation across dialects."""

    message = str(error.orig).lower()
    return (
        "uq_positions_open_portfolio_instrument" in message
        or "positions.portfolio_id, positions.instrument_id" in message
    )


def _is_strategy_run_idempotency_conflict(error: IntegrityError) -> bool:
    """Recognize only the final plan-local strategy idempotency key violation."""

    message = str(error.orig).lower()
    return (
        "uq_strategy_run_key" in message
        or "strategy_runs.rotation_plan_id, strategy_runs.idempotency_key" in message
    )


def _is_logical_scan_final_conflict(error: IntegrityError) -> bool:
    """Recognize the one-final-strategy-run relation for a logical scan."""

    message = str(error.orig).lower()
    return (
        "strategy_runs.logical_scan_run_id" in message or "uq_strategy_run_logical_scan" in message
    )


def _is_scan_lock_conflict(error: IntegrityError) -> bool:
    """Recognize only the unique preliminary logical scan lock-key conflict."""

    message = str(error.orig).lower()
    return "logical_scan_runs.scan_lock_key" in message or "scan_lock_key" in message


class SqlAlchemyRotationPlanRepository(_SqlAlchemyScopedRepository):
    """Plan projection adapter with explicit many-to-many candidate-group membership."""

    def add(self, plan: RotationPlan, *, access_context: AccessContext) -> None:
        plan_owner_id = self._require_portfolio_mutation(plan.portfolio_id, access_context)
        self._validate_references(plan, plan_owner_id)
        self._session.add(rotation_plan_to_model(plan))
        self._session.flush()
        if plan.candidate_group_ids:
            self._session.execute(
                insert(rotation_plan_candidate_groups),
                [
                    {
                        "rotation_plan_id": plan.rotation_plan_id,
                        "candidate_group_id": candidate_group_id,
                        "ordinal": ordinal,
                    }
                    for ordinal, candidate_group_id in enumerate(plan.candidate_group_ids)
                ],
            )
        if plan.source_position_ids:
            self._session.execute(
                insert(rotation_plan_source_positions),
                [
                    {
                        "rotation_plan_id": plan.rotation_plan_id,
                        "position_id": position_id,
                        "ordinal": ordinal,
                    }
                    for ordinal, position_id in enumerate(plan.source_position_ids)
                ],
            )
        if plan.protected_position_ids:
            self._session.execute(
                insert(rotation_plan_protected_positions),
                [
                    {
                        "rotation_plan_id": plan.rotation_plan_id,
                        "position_id": position_id,
                        "ordinal": ordinal,
                    }
                    for ordinal, position_id in enumerate(plan.protected_position_ids)
                ],
            )

    def _validate_references(self, plan: RotationPlan, plan_owner_id: UUID) -> None:
        for candidate_group_id in plan.candidate_group_ids:
            candidate_owner_id = self._session.scalar(
                select(CandidateGroupModel.user_id).where(
                    CandidateGroupModel.candidate_group_id == candidate_group_id
                )
            )
            if candidate_owner_id != plan_owner_id:
                raise PlanReferenceUnauthorizedError(
                    "candidate group belongs to another user or is unavailable"
                )
        for position_id in (*plan.source_position_ids, *plan.protected_position_ids):
            position_portfolio_id = self._session.scalar(
                select(PositionModel.portfolio_id).where(PositionModel.position_id == position_id)
            )
            if position_portfolio_id != plan.portfolio_id:
                raise PlanReferenceUnauthorizedError(
                    "position belongs to another portfolio or is unavailable"
                )

    def get(self, rotation_plan_id: UUID, *, access_context: AccessContext) -> RotationPlan:
        model = self._session.get(RotationPlanModel, rotation_plan_id)
        if model is None:
            raise NotFoundForActor("rotation plan was not found for actor")
        self._require_portfolio_read(model.portfolio_id, access_context)
        candidate_group_ids = tuple(
            self._session.scalars(
                select(rotation_plan_candidate_groups.c.candidate_group_id)
                .where(rotation_plan_candidate_groups.c.rotation_plan_id == rotation_plan_id)
                .order_by(rotation_plan_candidate_groups.c.ordinal)
            ).all()
        )
        source_position_ids = tuple(
            self._session.scalars(
                select(rotation_plan_source_positions.c.position_id)
                .where(rotation_plan_source_positions.c.rotation_plan_id == rotation_plan_id)
                .order_by(rotation_plan_source_positions.c.ordinal)
            ).all()
        )
        protected_position_ids = tuple(
            self._session.scalars(
                select(rotation_plan_protected_positions.c.position_id)
                .where(rotation_plan_protected_positions.c.rotation_plan_id == rotation_plan_id)
                .order_by(rotation_plan_protected_positions.c.ordinal)
            ).all()
        )
        return rotation_plan_from_model(
            model,
            candidate_group_ids=candidate_group_ids,
            source_position_ids=source_position_ids,
            protected_position_ids=protected_position_ids,
        )
