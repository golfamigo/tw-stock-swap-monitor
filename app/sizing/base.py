"""Pure, typed sizing request and audit-result value objects."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.domain.entities import CandidateGroup, Position, RotationPlan
from app.domain.values import InstrumentRef, Quantity, require_finite_decimal
from app.sizing.costs import DecimalRoundingPolicy, FeeTaxProfile, SlippageProfile


class SizingError(ValueError):
    """Raised for invalid pure sizing configuration or result structure."""


class OddLotPolicy(StrEnum):
    """Whether a stage can use the configured minimum quantity or only full lots."""

    ALLOWED = "allowed"
    PROHIBITED = "prohibited"


class SizingStatus(StrEnum):
    """Safe coordinator-facing status for a sizing recommendation."""

    ACTIONABLE = "actionable"
    INSUFFICIENT_CASH = "insufficient_cash"
    NO_PURCHASE_QUANTITY = "no_purchase_quantity"


@dataclass(frozen=True, slots=True)
class SizingStage:
    """One explicitly ordered positive allocation stage."""

    stage_id: str
    allocation_weight: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.stage_id, str) or not self.stage_id.strip():
            raise SizingError("stage_id must not be blank")
        require_finite_decimal(self.allocation_weight, field_name="allocation_weight")
        if self.allocation_weight <= Decimal("0"):
            raise SizingError("allocation_weight must be positive")
        object.__setattr__(self, "stage_id", self.stage_id.strip())


@dataclass(frozen=True, slots=True)
class SizingConfiguration:
    """All market-rule values needed for deterministic staged sizing are injected here."""

    stages: tuple[SizingStage, ...]
    fee_tax_profile: FeeTaxProfile
    slippage_profile: SlippageProfile
    reserve: Decimal
    minimum_quantity: Decimal
    lot_quantity: Decimal
    odd_lot_policy: OddLotPolicy
    rounding: DecimalRoundingPolicy

    def __post_init__(self) -> None:
        stages = tuple(self.stages)
        if not stages:
            raise SizingError("sizing configuration must contain at least one stage")
        if any(not isinstance(stage, SizingStage) for stage in stages):
            raise TypeError("stages must contain SizingStage values")
        stage_ids = tuple(stage.stage_id for stage in stages)
        if len(set(stage_ids)) != len(stage_ids):
            raise SizingError("stage ids must be unique")
        if sum((stage.allocation_weight for stage in stages), Decimal("0")) != Decimal("1"):
            raise SizingError("stage allocation weights must total exactly one")
        if not isinstance(self.fee_tax_profile, FeeTaxProfile):
            raise TypeError("fee_tax_profile must be a FeeTaxProfile")
        if not isinstance(self.slippage_profile, SlippageProfile):
            raise TypeError("slippage_profile must be a SlippageProfile")
        require_finite_decimal(self.reserve, field_name="reserve")
        if self.reserve < Decimal("0"):
            raise SizingError("reserve must not be negative")
        for value, field_name in (
            (self.minimum_quantity, "minimum_quantity"),
            (self.lot_quantity, "lot_quantity"),
        ):
            require_finite_decimal(value, field_name=field_name)
            if value <= Decimal("0"):
                raise SizingError(f"{field_name} must be positive")
        if self.minimum_quantity > self.lot_quantity:
            raise SizingError("minimum_quantity must not exceed lot_quantity")
        if not isinstance(self.odd_lot_policy, OddLotPolicy):
            raise TypeError("odd_lot_policy must be an OddLotPolicy")
        if not isinstance(self.rounding, DecimalRoundingPolicy):
            raise TypeError("rounding must be a DecimalRoundingPolicy")
        object.__setattr__(self, "stages", stages)


@dataclass(frozen=True, slots=True)
class SizingRequest:
    """All authorization, source sale, candidate purchase, and cash inputs for sizing."""

    plan: RotationPlan
    candidate_group: CandidateGroup
    portfolio_owner_id: UUID
    source_position: Position
    source_sale_quantity: Quantity
    source_sale_price: Decimal
    candidate: InstrumentRef
    candidate_price: Decimal
    available_cash: Decimal
    configuration: SizingConfiguration

    def __post_init__(self) -> None:
        if not isinstance(self.plan, RotationPlan):
            raise TypeError("plan must be a RotationPlan")
        if not isinstance(self.candidate_group, CandidateGroup):
            raise TypeError("candidate_group must be a CandidateGroup")
        if not isinstance(self.portfolio_owner_id, UUID):
            raise TypeError("portfolio_owner_id must be a UUID")
        if not isinstance(self.source_position, Position):
            raise TypeError("source_position must be a Position")
        if not isinstance(self.source_sale_quantity, Quantity):
            raise TypeError("source_sale_quantity must be a Quantity")
        if not isinstance(self.candidate, InstrumentRef):
            raise TypeError("candidate must be an InstrumentRef")
        for value, field_name in (
            (self.source_sale_price, "source_sale_price"),
            (self.candidate_price, "candidate_price"),
            (self.available_cash, "available_cash"),
        ):
            require_finite_decimal(value, field_name=field_name)
        if self.source_sale_price <= Decimal("0") or self.candidate_price <= Decimal("0"):
            raise SizingError("source_sale_price and candidate_price must be positive")
        if self.available_cash < Decimal("0"):
            raise SizingError("available_cash must not be negative")
        if not isinstance(self.configuration, SizingConfiguration):
            raise TypeError("configuration must be a SizingConfiguration")


@dataclass(frozen=True, slots=True)
class SourceSaleAudit:
    """Exact source sale quantity and proceeds, without mutating the Position record."""

    quantity: Quantity
    gross_value: Decimal
    slippage_cost: Decimal
    fee: Decimal
    tax: Decimal
    net_proceeds: Decimal


@dataclass(frozen=True, slots=True)
class StageSizing:
    """Exact target, quantity, and cost evidence for one configured stage."""

    stage_id: str
    allocation_weight: Decimal
    target_value: Decimal
    purchase_quantity: Quantity
    required_purchase_value: Decimal
    slippage_cost: Decimal
    fee: Decimal
    total_cash: Decimal


@dataclass(frozen=True, slots=True)
class SizingResult:
    """The non-mutating safe recommendation and complete Decimal funding evidence."""

    status: SizingStatus
    actionable: bool
    source_sale: SourceSaleAudit
    stages: tuple[StageSizing, ...]
    available_cash: Decimal
    net_sale_proceeds: Decimal
    reserve: Decimal
    required_purchase_value: Decimal
    configured_costs: Decimal
    total_required_cash: Decimal
    remaining_cash: Decimal
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, SizingStatus):
            raise TypeError("status must be a SizingStatus")
        if not isinstance(self.actionable, bool):
            raise TypeError("actionable must be a Boolean")
        if self.actionable is not (self.status is SizingStatus.ACTIONABLE):
            raise SizingError("actionable must match an ACTIONABLE sizing status")
        stages = tuple(self.stages)
        if not stages:
            raise SizingError("sizing result must retain configured stage evidence")
        for value, field_name in (
            (self.available_cash, "available_cash"),
            (self.net_sale_proceeds, "net_sale_proceeds"),
            (self.reserve, "reserve"),
            (self.required_purchase_value, "required_purchase_value"),
            (self.configured_costs, "configured_costs"),
            (self.total_required_cash, "total_required_cash"),
            (self.remaining_cash, "remaining_cash"),
        ):
            require_finite_decimal(value, field_name=field_name)
            if value < Decimal("0"):
                raise SizingError(f"{field_name} must not be negative")
        if (
            self.actionable
            and self.total_required_cash > self.available_cash + self.net_sale_proceeds
        ):
            raise SizingError("actionable sizing result violates funding inequality")
        object.__setattr__(self, "stages", stages)


@runtime_checkable
class SizingEngine(Protocol):
    """Pure port for deterministic, non-mutating staged sizing."""

    def size(self, request: SizingRequest) -> SizingResult:
        """Return a safe sizing recommendation or explicit non-actionable outcome."""
