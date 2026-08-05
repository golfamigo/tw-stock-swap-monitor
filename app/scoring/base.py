"""Pure, typed inputs and audit outputs for candidate scoring."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.domain.entities import CandidateGroup, RotationPlan
from app.domain.evidence_payload import MAX_IDENTIFIER_UTF8_BYTES
from app.domain.values import InstrumentRef, require_finite_decimal

_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


class ScoringError(ValueError):
    """Raised for invalid deterministic scoring configuration or request structure."""


class FactorDirection(StrEnum):
    """Whether larger or smaller raw values represent a better outcome."""

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class TieBreakPolicy(StrEnum):
    """An auditable policy for equal aggregate scores."""

    INSTRUMENT_ID_ASCENDING = "instrument_id_ascending"


class FactorScoreStatus(StrEnum):
    """The data state for one configured factor on one candidate."""

    SCORED = "scored"
    MISSING = "missing"
    INVALID = "invalid"


class CandidateScoreStatus(StrEnum):
    """Whether a candidate was safely rankable from all configured factor inputs."""

    RANKED = "ranked"
    MISSING_METRIC = "missing_metric"
    INVALID_METRIC = "invalid_metric"


@dataclass(frozen=True, slots=True)
class LinearNormalization:
    """A configured inclusive range normalized linearly and clamped to zero through one."""

    lower_bound: Decimal
    upper_bound: Decimal

    def __post_init__(self) -> None:
        require_finite_decimal(self.lower_bound, field_name="normalization lower_bound")
        require_finite_decimal(self.upper_bound, field_name="normalization upper_bound")
        if self.lower_bound >= self.upper_bound:
            raise ScoringError("normalization lower_bound must be smaller than upper_bound")

    def normalize(self, value: Decimal) -> Decimal:
        """Return the configured range-normalized Decimal without coercing input values."""

        require_finite_decimal(value, field_name="factor metric")
        with localcontext(_DECIMAL_CONTEXT):
            normalized = (value - self.lower_bound) / (self.upper_bound - self.lower_bound)
        if normalized < Decimal("0"):
            return Decimal("0")
        if normalized > Decimal("1"):
            return Decimal("1")
        return normalized


@dataclass(frozen=True, slots=True)
class FactorConfiguration:
    """One explicit factor identifier, weight, direction, and normalization rule."""

    factor_id: str
    weight: Decimal
    direction: FactorDirection
    normalization: LinearNormalization

    def __post_init__(self) -> None:
        _require_factor_identifier(self.factor_id, field_name="factor_id")
        require_finite_decimal(self.weight, field_name="factor weight")
        if self.weight <= Decimal("0"):
            raise ScoringError("factor weight must be positive")
        if not isinstance(self.direction, FactorDirection):
            raise TypeError("direction must be a FactorDirection")
        if not isinstance(self.normalization, LinearNormalization):
            raise TypeError("normalization must be a LinearNormalization")


@dataclass(frozen=True, slots=True)
class ScoringConfiguration:
    """Injected scoring policy with no implicit factors, weights, or tie behavior."""

    factors: tuple[FactorConfiguration, ...]
    tie_breaker: TieBreakPolicy

    def __post_init__(self) -> None:
        factors = tuple(self.factors)
        if not factors:
            raise ScoringError("scoring configuration must contain at least one factor")
        if any(not isinstance(factor, FactorConfiguration) for factor in factors):
            raise TypeError("factors must contain FactorConfiguration values")
        factor_ids = tuple(factor.factor_id for factor in factors)
        if len(set(factor_ids)) != len(factor_ids):
            raise ScoringError("factor ids must be unique")
        if not isinstance(self.tie_breaker, TieBreakPolicy):
            raise TypeError("tie_breaker must be a TieBreakPolicy")
        object.__setattr__(self, "factors", factors)


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    """A candidate and its supplied flat metrics, preserved without numeric coercion."""

    instrument: InstrumentRef
    metrics: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, InstrumentRef):
            raise TypeError("instrument must be an InstrumentRef")
        if not isinstance(self.metrics, Mapping):
            raise TypeError("metrics must be a mapping")
        metrics = dict(self.metrics)
        for key in metrics:
            _require_factor_identifier(key, field_name="metric key")
        object.__setattr__(self, "metrics", MappingProxyType(metrics))


@dataclass(frozen=True, slots=True)
class ScoringRequest:
    """All authorization context, candidates, and scoring policy required for one evaluation."""

    plan: RotationPlan
    candidate_group: CandidateGroup
    portfolio_owner_id: UUID
    configuration: ScoringConfiguration
    candidates: tuple[CandidateMetrics, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.plan, RotationPlan):
            raise TypeError("plan must be a RotationPlan")
        if not isinstance(self.candidate_group, CandidateGroup):
            raise TypeError("candidate_group must be a CandidateGroup")
        if not isinstance(self.portfolio_owner_id, UUID):
            raise TypeError("portfolio_owner_id must be a UUID")
        if not isinstance(self.configuration, ScoringConfiguration):
            raise TypeError("configuration must be a ScoringConfiguration")
        candidates = tuple(self.candidates)
        if not candidates:
            raise ScoringError("scoring request must contain at least one candidate")
        if any(not isinstance(candidate, CandidateMetrics) for candidate in candidates):
            raise TypeError("candidates must contain CandidateMetrics values")
        instrument_ids = tuple(candidate.instrument.instrument_id for candidate in candidates)
        if len(set(instrument_ids)) != len(instrument_ids):
            raise ScoringError("candidates must be unique by instrument")
        object.__setattr__(self, "candidates", candidates)


@dataclass(frozen=True, slots=True)
class FactorContribution:
    """Exact per-factor audit evidence, including an explicit unavailable state."""

    factor_id: str
    status: FactorScoreStatus
    weight: Decimal
    metric_value: Decimal | None
    normalized_value: Decimal | None
    contribution: Decimal | None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_factor_identifier(self.factor_id, field_name="factor contribution factor_id")
        if not isinstance(self.status, FactorScoreStatus):
            raise TypeError("status must be a FactorScoreStatus")
        require_finite_decimal(self.weight, field_name="factor contribution weight")
        if self.status is FactorScoreStatus.SCORED:
            for value, field_name in (
                (self.metric_value, "metric_value"),
                (self.normalized_value, "normalized_value"),
                (self.contribution, "contribution"),
            ):
                if value is None:
                    raise ScoringError(f"scored factor requires {field_name}")
                require_finite_decimal(value, field_name=field_name)
            if self.reason is not None:
                raise ScoringError("scored factor cannot have a reason")
        else:
            if any(value is not None for value in (self.normalized_value, self.contribution)):
                raise ScoringError(
                    "unavailable factor cannot have a normalized value or contribution"
                )
            if self.reason is None or not self.reason.strip():
                raise ScoringError("unavailable factor requires an audit reason")
            if self.metric_value is not None:
                require_finite_decimal(self.metric_value, field_name="metric_value")


@dataclass(frozen=True, slots=True)
class CandidateScore:
    """The final outcome for one authorized candidate."""

    instrument: InstrumentRef
    status: CandidateScoreStatus
    score: Decimal | None
    rank: int | None
    factor_contributions: tuple[FactorContribution, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, InstrumentRef):
            raise TypeError("instrument must be an InstrumentRef")
        if not isinstance(self.status, CandidateScoreStatus):
            raise TypeError("status must be a CandidateScoreStatus")
        contributions = tuple(self.factor_contributions)
        if not contributions:
            raise ScoringError("candidate score must retain factor contributions")
        if self.status is CandidateScoreStatus.RANKED:
            if self.score is None or self.rank is None or self.rank < 1:
                raise ScoringError("ranked candidate requires a score and positive rank")
            require_finite_decimal(self.score, field_name="candidate score")
        elif self.score is not None or self.rank is not None:
            raise ScoringError("non-rankable candidate cannot have a score or rank")
        object.__setattr__(self, "factor_contributions", contributions)


@dataclass(frozen=True, slots=True)
class ScoringResult:
    """A deterministic complete ranking with its explicit tie-break policy."""

    candidates: tuple[CandidateScore, ...]
    tie_breaker: TieBreakPolicy
    actionable: bool

    def __post_init__(self) -> None:
        candidates = tuple(self.candidates)
        if not candidates:
            raise ScoringError("scoring result must contain candidate outcomes")
        if not isinstance(self.tie_breaker, TieBreakPolicy):
            raise TypeError("tie_breaker must be a TieBreakPolicy")
        if not isinstance(self.actionable, bool):
            raise TypeError("actionable must be a Boolean")
        if self.actionable != any(
            candidate.status is CandidateScoreStatus.RANKED for candidate in candidates
        ):
            raise ScoringError("actionable must match whether a candidate was ranked")
        object.__setattr__(self, "candidates", candidates)


@runtime_checkable
class ScoringEngine(Protocol):
    """Pure port for deterministic candidate ranking."""

    def score(self, request: ScoringRequest) -> ScoringResult:
        """Rank only candidates authorized by the supplied plan and candidate group."""


def _require_factor_identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.isidentifier():
        raise ScoringError(f"{field_name} must be a Python-identifier-like configured key")
    if len(value.encode("utf-8")) > MAX_IDENTIFIER_UTF8_BYTES:
        raise ScoringError(f"{field_name} exceeds the {MAX_IDENTIFIER_UTF8_BYTES}-byte UTF-8 limit")
    return value
