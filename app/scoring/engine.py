"""Deterministic, Decimal-only scoring implementation."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from app.domain.invariants import ensure_candidate_instrument_is_authorized
from app.scoring.base import (
    CandidateMetrics,
    CandidateScore,
    CandidateScoreStatus,
    FactorConfiguration,
    FactorContribution,
    FactorDirection,
    FactorScoreStatus,
    ScoringRequest,
    ScoringResult,
    TieBreakPolicy,
)

_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


class DeterministicScoringEngine:
    """Rank authorized candidates without implicit metrics, defaults, or tie behavior."""

    def score(self, request: ScoringRequest) -> ScoringResult:
        """Evaluate every configured factor and return complete audit contributions."""

        if not isinstance(request, ScoringRequest):
            raise TypeError("request must be a ScoringRequest")
        outcomes = tuple(
            self._score_candidate(request, candidate) for candidate in request.candidates
        )
        ranked = [outcome for outcome in outcomes if outcome.status is CandidateScoreStatus.RANKED]
        non_ranked = [
            outcome for outcome in outcomes if outcome.status is not CandidateScoreStatus.RANKED
        ]
        ordered_ranked = self._rank(ranked, request.configuration.tie_breaker)
        return ScoringResult(
            candidates=tuple((*ordered_ranked, *non_ranked)),
            tie_breaker=request.configuration.tie_breaker,
            actionable=bool(ordered_ranked),
        )

    def _score_candidate(
        self, request: ScoringRequest, candidate: CandidateMetrics
    ) -> CandidateScore:
        ensure_candidate_instrument_is_authorized(
            request.plan,
            request.candidate_group,
            candidate.instrument,
            portfolio_owner_id=request.portfolio_owner_id,
        )
        contributions = tuple(
            self._contribution(factor, candidate) for factor in request.configuration.factors
        )
        if any(item.status is FactorScoreStatus.INVALID for item in contributions):
            return CandidateScore(
                instrument=candidate.instrument,
                status=CandidateScoreStatus.INVALID_METRIC,
                score=None,
                rank=None,
                factor_contributions=contributions,
            )
        if any(item.status is FactorScoreStatus.MISSING for item in contributions):
            return CandidateScore(
                instrument=candidate.instrument,
                status=CandidateScoreStatus.MISSING_METRIC,
                score=None,
                rank=None,
                factor_contributions=contributions,
            )
        with localcontext(_DECIMAL_CONTEXT):
            total = sum(
                (item.contribution for item in contributions if item.contribution is not None),
                Decimal("0"),
            )
        return CandidateScore(
            instrument=candidate.instrument,
            status=CandidateScoreStatus.RANKED,
            score=total,
            rank=1,
            factor_contributions=contributions,
        )

    def _contribution(
        self, factor: FactorConfiguration, candidate: CandidateMetrics
    ) -> FactorContribution:
        if factor.factor_id not in candidate.metrics:
            return FactorContribution(
                factor_id=factor.factor_id,
                status=FactorScoreStatus.MISSING,
                weight=factor.weight,
                metric_value=None,
                normalized_value=None,
                contribution=None,
                reason="metric_not_supplied",
            )
        raw_value = candidate.metrics[factor.factor_id]
        if not isinstance(raw_value, Decimal) or not raw_value.is_finite():
            return FactorContribution(
                factor_id=factor.factor_id,
                status=FactorScoreStatus.INVALID,
                weight=factor.weight,
                metric_value=None,
                normalized_value=None,
                contribution=None,
                reason="metric_must_be_finite_decimal",
            )
        normalized = factor.normalization.normalize(raw_value)
        if factor.direction is FactorDirection.LOWER_IS_BETTER:
            normalized = Decimal("1") - normalized
        with localcontext(_DECIMAL_CONTEXT):
            contribution = normalized * factor.weight
        return FactorContribution(
            factor_id=factor.factor_id,
            status=FactorScoreStatus.SCORED,
            weight=factor.weight,
            metric_value=raw_value,
            normalized_value=normalized,
            contribution=contribution,
        )

    def _rank(
        self, candidates: list[CandidateScore], tie_breaker: TieBreakPolicy
    ) -> tuple[CandidateScore, ...]:
        if tie_breaker is not TieBreakPolicy.INSTRUMENT_ID_ASCENDING:
            raise ValueError("unsupported explicit scoring tie-break policy")
        ordered = sorted(
            candidates,
            key=lambda candidate: (
                -(candidate.score if candidate.score is not None else Decimal("0")),
                str(candidate.instrument.instrument_id),
            ),
        )
        return tuple(
            CandidateScore(
                instrument=candidate.instrument,
                status=candidate.status,
                score=candidate.score,
                rank=index,
                factor_contributions=candidate.factor_contributions,
            )
            for index, candidate in enumerate(ordered, start=1)
        )
