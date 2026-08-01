"""Frozen value objects shared by aggregates and application ports."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.domain.enums import Scope
from app.domain.errors import (
    NaiveDatetimeError,
    NegativeQuantityError,
    NonDecimalValueError,
    NonFiniteDecimalError,
)


def require_timezone_aware(value: datetime, *, field_name: str) -> datetime:
    """Return an aware datetime or reject a naive timestamp at the domain boundary."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise NaiveDatetimeError(f"{field_name} must be timezone-aware")
    return value


def require_finite_decimal(value: Decimal, *, field_name: str) -> Decimal:
    """Reject non-Decimal and non-finite numeric values without coercion."""

    if not isinstance(value, Decimal):
        raise NonDecimalValueError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise NonFiniteDecimalError(f"{field_name} must be finite")
    return value


@dataclass(frozen=True, slots=True)
class Ownership:
    """A scope and, when required by that scope, the owning aggregate identifier."""

    scope: Scope
    owner_id: UUID | None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope):
            raise ValueError("scope must be a Scope")
        if self.scope is Scope.SYSTEM and self.owner_id is not None:
            raise ValueError("SYSTEM ownership must not have an owner_id")
        if self.scope is not Scope.SYSTEM and self.owner_id is None:
            raise ValueError(f"{self.scope.value} ownership requires an owner_id")


@dataclass(frozen=True, slots=True)
class InstrumentRef:
    """A typed reference to a globally defined instrument."""

    instrument_id: UUID


@dataclass(frozen=True, slots=True)
class Money:
    """A finite Decimal monetary value in an explicit currency."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        require_finite_decimal(self.amount, field_name="amount")
        if not self.currency.strip():
            raise ValueError("currency must not be blank")


@dataclass(frozen=True, slots=True)
class Quantity:
    """A finite, non-negative Decimal quantity."""

    value: Decimal

    def __post_init__(self) -> None:
        require_finite_decimal(self.value, field_name="quantity")
        if self.value < Decimal("0"):
            raise NegativeQuantityError("quantity must not be negative")


@dataclass(frozen=True, slots=True)
class Percentage:
    """A finite Decimal proportion in the inclusive range zero through one."""

    value: Decimal

    def __post_init__(self) -> None:
        require_finite_decimal(self.value, field_name="percentage")
        if not Decimal("0") <= self.value <= Decimal("1"):
            raise ValueError("percentage must be between zero and one")


@dataclass(frozen=True, slots=True)
class MarketSession:
    """A timezone-aware market open/close interval."""

    opens_at: datetime
    closes_at: datetime

    def __post_init__(self) -> None:
        require_timezone_aware(self.opens_at, field_name="opens_at")
        require_timezone_aware(self.closes_at, field_name="closes_at")
        if self.closes_at <= self.opens_at:
            raise ValueError("closes_at must be after opens_at")


@dataclass(frozen=True, slots=True)
class ConfigurationSnapshotRef:
    """The immutable identity and creation time of a resolved configuration snapshot."""

    snapshot_id: UUID
    content_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        require_timezone_aware(self.created_at, field_name="created_at")
        if len(self.content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.content_hash.lower()
        ):
            raise ValueError("content_hash must be a SHA-256 hexadecimal digest")


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    """A non-empty opaque key for deduplicating a command or scan."""

    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("idempotency key must not be blank")


@dataclass(frozen=True, slots=True)
class PriceZone:
    """An inclusive price interval whose bounds share one currency."""

    lower: Money
    upper: Money

    def __post_init__(self) -> None:
        if self.lower.currency != self.upper.currency:
            raise ValueError("price zone bounds must use the same currency")
        if self.lower.amount > self.upper.amount:
            raise ValueError("price zone lower bound must not exceed upper bound")
