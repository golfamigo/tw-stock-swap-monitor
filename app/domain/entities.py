"""Frozen aggregate roots and mutation-controlled position history."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID

from app.domain.enums import PositionRole, PositionStatus, Scope
from app.domain.errors import (
    InvalidStrategyRunEvidenceError,
    PositionHistoryError,
    PositionNotOpenError,
)
from app.domain.values import (
    ConfigurationSnapshotRef,
    InstrumentRef,
    Ownership,
    Quantity,
    require_finite_decimal,
    require_timezone_aware,
)


def _freeze_evidence(value: object, *, active_container_ids: set[int]) -> object:
    """Recursively convert builtin mutable containers into immutable evidence values."""

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
    if value is None or isinstance(value, str | int | UUID | bytes):
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
    symbol: str
    created_at: datetime

    def __post_init__(self) -> None:
        require_timezone_aware(self.created_at, field_name="created_at")
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
    """A portfolio-scoped plan that names its candidate and position references."""

    rotation_plan_id: UUID
    portfolio_id: UUID
    candidate_group_id: UUID
    source_position_ids: tuple[UUID, ...]
    protected_position_ids: tuple[UUID, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        require_timezone_aware(self.created_at, field_name="created_at")
        object.__setattr__(self, "source_position_ids", tuple(self.source_position_ids))
        object.__setattr__(self, "protected_position_ids", tuple(self.protected_position_ids))
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
