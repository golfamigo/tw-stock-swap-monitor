"""Frozen aggregate roots and mutation-controlled position history."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from uuid import UUID

from app.domain.enums import (
    LogicalScanStatus,
    PositionRole,
    PositionStatus,
    ScanAttemptRecoveryDecision,
    ScanAttemptStatus,
    Scope,
)
from app.domain.errors import (
    InvalidStrategyRunEvidenceError,
    NonPositivePositionQuantityError,
    PositionHistoryError,
    PositionNotOpenError,
)
from app.domain.values import (
    ConfigurationSnapshotRef,
    InstrumentRef,
    Ownership,
    Quantity,
    ScanIdentity,
    require_finite_decimal,
    require_timezone_aware,
)


def _freeze_evidence(value: object, *, active_container_ids: set[int]) -> object:
    """Recursively convert builtin mutable containers into immutable evidence values."""

    if isinstance(value, Enum):
        raise InvalidStrategyRunEvidenceError("evidence Enum values are not supported")
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_container_ids:
            raise InvalidStrategyRunEvidenceError(
                "evidence must not contain active-container cycles"
            )
        active_container_ids.add(container_id)
        try:
            frozen_mapping: dict[str, object] = {}
            for key, nested_value in value.items():
                if not isinstance(key, str):
                    raise InvalidStrategyRunEvidenceError("evidence mapping keys must be strings")
                frozen_mapping[key] = _freeze_evidence(
                    nested_value, active_container_ids=active_container_ids
                )
            return MappingProxyType(frozen_mapping)
        finally:
            active_container_ids.remove(container_id)
    if isinstance(value, list | tuple):
        container_id = id(value)
        if container_id in active_container_ids:
            raise InvalidStrategyRunEvidenceError(
                "evidence must not contain active-container cycles"
            )
        active_container_ids.add(container_id)
        try:
            return tuple(
                _freeze_evidence(item, active_container_ids=active_container_ids) for item in value
            )
        finally:
            active_container_ids.remove(container_id)
    if isinstance(value, set | frozenset):
        raise InvalidStrategyRunEvidenceError("evidence must not contain unordered set values")
    if isinstance(value, Decimal):
        return require_finite_decimal(value, field_name="evidence Decimal")
    if isinstance(value, datetime):
        return require_timezone_aware(value, field_name="evidence datetime")
    if isinstance(value, bytes):
        raise InvalidStrategyRunEvidenceError("evidence bytes are not supported")
    if value is None or isinstance(value, str | int | UUID):
        return value
    raise InvalidStrategyRunEvidenceError("unsupported mutable or non-deterministic evidence value")


@dataclass(frozen=True, slots=True)
class User:
    """The root owner of private and portfolio-scoped resources."""

    user_id: UUID
    created_at: datetime

    def __post_init__(self) -> None:
        require_timezone_aware(self.created_at, field_name="created_at")

    @property
    def ownership(self) -> Ownership:
        return Ownership(Scope.USER, self.user_id)


@dataclass(frozen=True, slots=True)
class Portfolio:
    """A portfolio owned by exactly one user."""

    portfolio_id: UUID
    user_id: UUID
    created_at: datetime

    def __post_init__(self) -> None:
        require_timezone_aware(self.created_at, field_name="created_at")

    @property
    def ownership(self) -> Ownership:
        return Ownership(Scope.USER, self.user_id)


@dataclass(frozen=True, slots=True)
class Instrument:
    """A global instrument definition, mutable only by an administrator."""

    instrument_id: UUID
    market: str
    symbol: str
    created_at: datetime

    def __post_init__(self) -> None:
        require_timezone_aware(self.created_at, field_name="created_at")
        if not self.market.strip():
            raise ValueError("market must not be blank")
        if not self.symbol.strip():
            raise ValueError("symbol must not be blank")

    @property
    def ownership(self) -> Ownership:
        return Ownership(Scope.SYSTEM, None)


@dataclass(frozen=True, slots=True)
class Position:
    """A portfolio holding with explicit open/closed historical state."""

    position_id: UUID
    portfolio_id: UUID
    instrument: InstrumentRef
    quantity: Quantity
    role: PositionRole
    status: PositionStatus
    opened_at: datetime
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        require_timezone_aware(self.opened_at, field_name="opened_at")
        if self.quantity.value <= Decimal("0"):
            raise NonPositivePositionQuantityError("position quantity must be positive")
        if not isinstance(self.role, PositionRole):
            raise ValueError("role must be a PositionRole")
        if not isinstance(self.status, PositionStatus):
            raise ValueError("status must be a PositionStatus")
        if self.status is PositionStatus.OPEN and self.closed_at is not None:
            raise PositionHistoryError("an OPEN position cannot have closed_at")
        if self.status is PositionStatus.CLOSED and self.closed_at is None:
            raise PositionHistoryError("a CLOSED position requires closed_at")
        if self.closed_at is not None:
            require_timezone_aware(self.closed_at, field_name="closed_at")
            if self.closed_at < self.opened_at:
                raise PositionHistoryError("closed_at cannot be before opened_at")

    @property
    def ownership(self) -> Ownership:
        return Ownership(Scope.PORTFOLIO, self.portfolio_id)

    def close(self, closed_at: datetime) -> "Position":
        """Return a closed historical record without mutating the original holding."""

        if self.status is not PositionStatus.OPEN:
            raise PositionNotOpenError("only OPEN positions can be closed")
        return replace(self, status=PositionStatus.CLOSED, closed_at=closed_at)


@dataclass(frozen=True, slots=True)
class CandidateGroup:
    """A user's allowed candidate instruments for one or more rotation plans."""

    candidate_group_id: UUID
    user_id: UUID
    instruments: tuple[InstrumentRef, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        require_timezone_aware(self.created_at, field_name="created_at")
        object.__setattr__(self, "instruments", tuple(self.instruments))
        instrument_ids = {instrument.instrument_id for instrument in self.instruments}
        if len(instrument_ids) != len(self.instruments):
            raise ValueError("candidate group instruments must be unique")

    @property
    def ownership(self) -> Ownership:
        return Ownership(Scope.USER, self.user_id)


@dataclass(frozen=True, slots=True)
class RotationPlan:
    """A portfolio-scoped plan that names candidate groups and position references."""

    rotation_plan_id: UUID
    portfolio_id: UUID
    candidate_group_ids: tuple[UUID, ...]
    source_position_ids: tuple[UUID, ...]
    protected_position_ids: tuple[UUID, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        require_timezone_aware(self.created_at, field_name="created_at")
        object.__setattr__(self, "candidate_group_ids", tuple(self.candidate_group_ids))
        object.__setattr__(self, "source_position_ids", tuple(self.source_position_ids))
        object.__setattr__(self, "protected_position_ids", tuple(self.protected_position_ids))
        if not self.candidate_group_ids:
            raise ValueError("candidate_group_ids must not be empty")
        if len(set(self.candidate_group_ids)) != len(self.candidate_group_ids):
            raise ValueError("candidate_group_ids must be unique")
        if len(set(self.source_position_ids)) != len(self.source_position_ids):
            raise ValueError("source_position_ids must be unique")
        if len(set(self.protected_position_ids)) != len(self.protected_position_ids):
            raise ValueError("protected_position_ids must be unique")
        if set(self.source_position_ids) & set(self.protected_position_ids):
            raise ValueError("a position cannot be both source and protected")

    @property
    def ownership(self) -> Ownership:
        return Ownership(Scope.PORTFOLIO, self.portfolio_id)


@dataclass(frozen=True, slots=True)
class StrategyRun:
    """Immutable evidence of one deterministic evaluation of a rotation plan."""

    strategy_run_id: UUID
    rotation_plan_id: UUID
    portfolio_id: UUID
    configuration_snapshot: ConfigurationSnapshotRef
    market_data_snapshot_id: str
    state_transition: str
    outputs: Mapping[str, object]
    occurred_at: datetime

    def __post_init__(self) -> None:
        require_timezone_aware(self.occurred_at, field_name="occurred_at")
        if not self.market_data_snapshot_id.strip():
            raise ValueError("market_data_snapshot_id must not be blank")
        if not self.state_transition.strip():
            raise ValueError("state_transition must not be blank")
        if not isinstance(self.outputs, Mapping):
            raise InvalidStrategyRunEvidenceError("outputs must be a mapping")
        object.__setattr__(
            self,
            "outputs",
            _freeze_evidence(self.outputs, active_container_ids=set()),
        )

    @property
    def ownership(self) -> Ownership:
        return Ownership(Scope.PORTFOLIO, self.portfolio_id)


@dataclass(frozen=True, slots=True)
class LogicalScanRun:
    """One recoverable scan identity before a market-data snapshot is available."""

    logical_scan_run_id: UUID
    rotation_plan_id: UUID
    portfolio_id: UUID
    market_session_date: str
    scan_window_start: datetime
    scan_interval: str
    market_timezone: str
    configuration_snapshot_hash: str
    scan_identity_format_version: str
    scan_lock_key: str
    status: LogicalScanStatus
    created_at: datetime
    completed_at: datetime | None = None
    final_strategy_run_id: UUID | None = None

    def __post_init__(self) -> None:
        require_timezone_aware(self.scan_window_start, field_name="scan_window_start")
        require_timezone_aware(self.created_at, field_name="created_at")
        if not self.market_session_date.strip() or not self.scan_interval.strip():
            raise ValueError("logical scan identity fields must not be blank")
        if not self.scan_lock_key.strip():
            raise ValueError("scan_lock_key must not be blank")
        if self.scan_lock_key != self.identity.key:
            raise ValueError("scan_lock_key must match the scan identity")
        if not isinstance(self.status, LogicalScanStatus):
            raise TypeError("status must be a LogicalScanStatus")
        if self.status is LogicalScanStatus.RUNNING:
            if self.completed_at is not None or self.final_strategy_run_id is not None:
                raise ValueError("RUNNING logical scans cannot have final result evidence")
        else:
            if self.completed_at is None or self.final_strategy_run_id is None:
                raise ValueError("COMPLETED logical scans require final result evidence")
            require_timezone_aware(self.completed_at, field_name="completed_at")
            if self.completed_at < self.created_at:
                raise ValueError("logical scan completed_at cannot be before created_at")

    @property
    def identity(self) -> ScanIdentity:
        """Reconstruct the exact versioned lock identity retained for recovery and audit."""

        try:
            market_session_date = date.fromisoformat(self.market_session_date)
        except ValueError as error:
            raise ValueError("market_session_date must be an ISO date") from error
        return ScanIdentity(
            rotation_plan_id=self.rotation_plan_id,
            market_session_date=market_session_date,
            scan_window_start=self.scan_window_start,
            scan_interval=self.scan_interval,
            configuration_snapshot_hash=self.configuration_snapshot_hash,
            market_timezone=self.market_timezone,
            format_version=self.scan_identity_format_version,
        )


@dataclass(frozen=True, slots=True)
class ScanAttempt:
    """Auditable provider attempt retained for a recoverable logical scan."""

    scan_attempt_id: UUID
    logical_scan_run_id: UUID
    attempt_number: int
    status: ScanAttemptStatus
    configuration_snapshot_hash: str
    market_data_snapshot_id: str | None
    market_data_content_hash: str | None
    trigger_correlation_id: str
    actor_correlation_id: str
    started_at: datetime
    completed_at: datetime | None
    failure_code: str | None = None
    failure_detail: str | None = None
    recovery_of_attempt_id: UUID | None = None
    duplicate_of_attempt_id: UUID | None = None
    final_strategy_run_id: UUID | None = None
    configuration_snapshot_id: UUID | None = None
    configuration_snapshot_created_at: datetime | None = None
    final_strategy_key: str | None = None
    final_strategy_identity_format_version: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.attempt_number, bool) or self.attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        if not isinstance(self.status, ScanAttemptStatus):
            raise TypeError("status must be a ScanAttemptStatus")
        _require_sha256_hash(
            self.configuration_snapshot_hash, field_name="configuration_snapshot_hash"
        )
        if (self.configuration_snapshot_id is None) != (
            self.configuration_snapshot_created_at is None
        ):
            raise ValueError(
                "configuration_snapshot_id and "
                "configuration_snapshot_created_at must be supplied together"
            )
        if self.configuration_snapshot_created_at is not None:
            require_timezone_aware(
                self.configuration_snapshot_created_at,
                field_name="configuration_snapshot_created_at",
            )
        if self.market_data_snapshot_id is not None and not self.market_data_snapshot_id.strip():
            raise ValueError("market_data_snapshot_id must not be blank when supplied")
        if (self.market_data_snapshot_id is None) != (self.market_data_content_hash is None):
            raise ValueError(
                "market_data_snapshot_id and market_data_content_hash must be supplied together"
            )
        if self.market_data_content_hash is not None:
            _require_sha256_hash(
                self.market_data_content_hash, field_name="market_data_content_hash"
            )
        if (self.final_strategy_key is None) != (
            self.final_strategy_identity_format_version is None
        ):
            raise ValueError(
                "final_strategy_key and "
                "final_strategy_identity_format_version must be supplied together"
            )
        if self.final_strategy_key is not None:
            _require_sha256_hash(self.final_strategy_key, field_name="final_strategy_key")
            if (
                not self.final_strategy_identity_format_version
                or len(self.final_strategy_identity_format_version) > 16
            ):
                raise ValueError("final_strategy_identity_format_version must be non-blank")
        _require_bounded_text(
            self.trigger_correlation_id, field_name="trigger_correlation_id", maximum_length=256
        )
        _require_bounded_text(
            self.actor_correlation_id, field_name="actor_correlation_id", maximum_length=256
        )
        require_timezone_aware(self.started_at, field_name="started_at")
        if self.completed_at is not None:
            require_timezone_aware(self.completed_at, field_name="completed_at")
            if self.completed_at < self.started_at:
                raise ValueError("attempt completed_at cannot be before started_at")
        if self.failure_code is not None:
            _require_bounded_text(self.failure_code, field_name="failure_code", maximum_length=64)
        if self.failure_detail is not None:
            _require_bounded_text(
                self.failure_detail, field_name="failure_detail", maximum_length=1024
            )
        if self.recovery_of_attempt_id == self.scan_attempt_id:
            raise ValueError("recovery_of_attempt_id cannot reference this attempt")
        if self.duplicate_of_attempt_id == self.scan_attempt_id:
            raise ValueError("duplicate_of_attempt_id cannot reference this attempt")
        self._validate_status_evidence()

    def _validate_status_evidence(self) -> None:
        has_market_data = self.market_data_snapshot_id is not None
        has_failure = self.failure_code is not None or self.failure_detail is not None
        if self.status is ScanAttemptStatus.RUNNING:
            if self.completed_at is not None or has_market_data or has_failure:
                raise ValueError("RUNNING attempts cannot have completed outcome evidence")
            if self.duplicate_of_attempt_id is not None or self.final_strategy_run_id is not None:
                raise ValueError("RUNNING attempts cannot have final or duplicate references")
            return
        if self.completed_at is None:
            raise ValueError("terminal scan attempts require completed_at")
        if self.status in {ScanAttemptStatus.FAILED, ScanAttemptStatus.DEGRADED}:
            if self.failure_code is None or self.failure_detail is None:
                raise ValueError("failure_code and failure_detail are required for failed evidence")
            if self.final_strategy_run_id is not None:
                raise ValueError("failed or degraded attempts cannot have a final strategy run")
            if self.duplicate_of_attempt_id is not None:
                raise ValueError("failed or degraded attempts cannot be duplicate results")
            if self.status is ScanAttemptStatus.DEGRADED and not has_market_data:
                raise ValueError("DEGRADED attempts require market_data evidence")
            return
        if has_failure:
            raise ValueError("SUCCEEDED attempts cannot have failure evidence")
        if self.duplicate_of_attempt_id is not None:
            if has_market_data:
                raise ValueError("duplicate successes cannot repeat market_data evidence")
            if self.final_strategy_run_id is not None:
                raise ValueError("duplicate successes cannot create a final strategy run")
            return
        if not has_market_data:
            raise ValueError("SUCCEEDED attempts require market_data evidence")

    @property
    def recovery_decision(self) -> ScanAttemptRecoveryDecision:
        """Return the only safe process action implied by immutable attempt evidence."""

        if self.status is ScanAttemptStatus.RUNNING:
            return ScanAttemptRecoveryDecision.RESUME
        if self.status in {ScanAttemptStatus.FAILED, ScanAttemptStatus.DEGRADED}:
            return ScanAttemptRecoveryDecision.RETRY
        return ScanAttemptRecoveryDecision.FINALIZE

    @property
    def configuration_snapshot(self) -> ConfigurationSnapshotRef | None:
        """Reconstruct the immutable configuration reference retained with this attempt."""

        if self.configuration_snapshot_id is None:
            return None
        assert self.configuration_snapshot_created_at is not None
        return ConfigurationSnapshotRef(
            self.configuration_snapshot_id,
            self.configuration_snapshot_hash,
            self.configuration_snapshot_created_at,
        )


def _require_sha256_hash(value: str, *, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise ValueError(f"{field_name} must be a SHA-256 hexadecimal digest")


def _require_bounded_text(value: str, *, field_name: str, maximum_length: int) -> None:
    if not value.strip() or len(value) > maximum_length:
        raise ValueError(f"{field_name} must be non-blank and at most {maximum_length} characters")
