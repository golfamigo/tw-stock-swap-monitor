"""Behavior tests for deterministic, authorized Decimal candidate scoring."""

import importlib
import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from app.domain.entities import CandidateGroup, RotationPlan
from app.domain.errors import CandidateInstrumentUnauthorizedError, NonDecimalValueError
from app.domain.values import InstrumentRef
from app.scoring.base import ScoringError


def _api() -> Any:
    assert importlib.util.find_spec("app.scoring") is not None
    return importlib.import_module("app.scoring")


def _created_at() -> datetime:
    return datetime(2026, 1, 5, 9, tzinfo=UTC)


def _authorized_context() -> (
    tuple[RotationPlan, CandidateGroup, UUID, InstrumentRef, InstrumentRef]
):
    owner_id = uuid4()
    first_candidate = InstrumentRef(UUID(int=1))
    second_candidate = InstrumentRef(UUID(int=2))
    candidate_group = CandidateGroup(
        candidate_group_id=uuid4(),
        user_id=owner_id,
        instruments=(first_candidate, second_candidate),
        created_at=_created_at(),
    )
    plan = RotationPlan(
        rotation_plan_id=uuid4(),
        portfolio_id=uuid4(),
        candidate_group_ids=(candidate_group.candidate_group_id,),
        source_position_ids=(uuid4(),),
        protected_position_ids=(),
        created_at=_created_at(),
    )
    return plan, candidate_group, owner_id, first_candidate, second_candidate


def _configuration(api: Any) -> Any:
    return api.ScoringConfiguration(
        factors=(
            api.FactorConfiguration(
                factor_id="strength",
                weight=Decimal("2"),
                direction=api.FactorDirection.HIGHER_IS_BETTER,
                normalization=api.LinearNormalization(
                    lower_bound=Decimal("0"), upper_bound=Decimal("10")
                ),
            ),
            api.FactorConfiguration(
                factor_id="cost",
                weight=Decimal("3"),
                direction=api.FactorDirection.LOWER_IS_BETTER,
                normalization=api.LinearNormalization(
                    lower_bound=Decimal("0"), upper_bound=Decimal("10")
                ),
            ),
        ),
        tie_breaker=api.TieBreakPolicy.INSTRUMENT_ID_ASCENDING,
    )


def test_scoring_uses_configured_weights_directions_and_linear_normalization() -> None:
    api = _api()
    plan, candidate_group, owner_id, first_candidate, _ = _authorized_context()
    request = api.ScoringRequest(
        plan=plan,
        candidate_group=candidate_group,
        portfolio_owner_id=owner_id,
        configuration=_configuration(api),
        candidates=(
            api.CandidateMetrics(
                instrument=first_candidate,
                metrics={"strength": Decimal("7"), "cost": Decimal("2")},
            ),
        ),
    )

    result = api.DeterministicScoringEngine().score(request)

    candidate = result.candidates[0]
    assert result.actionable is True
    assert candidate.status is api.CandidateScoreStatus.RANKED
    assert candidate.rank == 1
    assert candidate.score == Decimal("3.8")
    assert candidate.factor_contributions[0].normalized_value == Decimal("0.7")
    assert candidate.factor_contributions[0].contribution == Decimal("1.4")
    assert candidate.factor_contributions[1].normalized_value == Decimal("0.8")
    assert candidate.factor_contributions[1].contribution == Decimal("2.4")


def test_scoring_breaks_equal_scores_by_explicit_instrument_identifier_policy() -> None:
    api = _api()
    plan, candidate_group, owner_id, first_candidate, second_candidate = _authorized_context()
    request = api.ScoringRequest(
        plan=plan,
        candidate_group=candidate_group,
        portfolio_owner_id=owner_id,
        configuration=_configuration(api),
        candidates=(
            api.CandidateMetrics(
                instrument=second_candidate,
                metrics={"strength": Decimal("5"), "cost": Decimal("5")},
            ),
            api.CandidateMetrics(
                instrument=first_candidate,
                metrics={"strength": Decimal("5"), "cost": Decimal("5")},
            ),
        ),
    )

    result = api.DeterministicScoringEngine().score(request)

    assert result.tie_breaker is api.TieBreakPolicy.INSTRUMENT_ID_ASCENDING
    assert tuple(candidate.instrument for candidate in result.candidates) == (
        first_candidate,
        second_candidate,
    )
    assert tuple(candidate.rank for candidate in result.candidates) == (1, 2)


def test_scoring_records_missing_and_invalid_metrics_without_zero_defaulting() -> None:
    api = _api()
    plan, candidate_group, owner_id, first_candidate, second_candidate = _authorized_context()
    request = api.ScoringRequest(
        plan=plan,
        candidate_group=candidate_group,
        portfolio_owner_id=owner_id,
        configuration=_configuration(api),
        candidates=(
            api.CandidateMetrics(instrument=first_candidate, metrics={}),
            api.CandidateMetrics(
                instrument=second_candidate,
                metrics={"strength": 1.0, "cost": Decimal("2")},
            ),
        ),
    )

    result = api.DeterministicScoringEngine().score(request)

    assert result.actionable is False
    missing, invalid = result.candidates
    assert missing.status is api.CandidateScoreStatus.MISSING_METRIC
    assert missing.score is None
    assert missing.factor_contributions[0].status is api.FactorScoreStatus.MISSING
    assert missing.factor_contributions[0].contribution is None
    assert invalid.status is api.CandidateScoreStatus.INVALID_METRIC
    assert invalid.score is None
    assert invalid.factor_contributions[0].status is api.FactorScoreStatus.INVALID
    assert invalid.factor_contributions[0].contribution is None


def test_scoring_rejects_an_unauthorized_candidate_instead_of_ranking_it() -> None:
    api = _api()
    plan, candidate_group, owner_id, _, _ = _authorized_context()
    request = api.ScoringRequest(
        plan=plan,
        candidate_group=candidate_group,
        portfolio_owner_id=owner_id,
        configuration=_configuration(api),
        candidates=(
            api.CandidateMetrics(
                instrument=InstrumentRef(uuid4()),
                metrics={"strength": Decimal("7"), "cost": Decimal("2")},
            ),
        ),
    )

    with pytest.raises(CandidateInstrumentUnauthorizedError):
        api.DeterministicScoringEngine().score(request)


def test_scoring_rejects_float_factor_configuration_without_coercion() -> None:
    api = _api()

    with pytest.raises(NonDecimalValueError):
        api.FactorConfiguration(
            factor_id="strength",
            weight=cast(Decimal, 1.0),
            direction=api.FactorDirection.HIGHER_IS_BETTER,
            normalization=api.LinearNormalization(
                lower_bound=Decimal("0"), upper_bound=Decimal("10")
            ),
        )


def test_scoring_factor_identifiers_have_a_fixed_utf8_byte_limit() -> None:
    api = _api()
    _, _, _, first_candidate, _ = _authorized_context()
    exact_identifier = "f" * 256
    over_limit_identifier = "f" * 257
    normalization = api.LinearNormalization(lower_bound=Decimal("0"), upper_bound=Decimal("10"))

    assert (
        api.FactorConfiguration(
            factor_id=exact_identifier,
            weight=Decimal("1"),
            direction=api.FactorDirection.HIGHER_IS_BETTER,
            normalization=normalization,
        ).factor_id
        == exact_identifier
    )
    assert api.CandidateMetrics(
        instrument=first_candidate,
        metrics={exact_identifier: Decimal("1")},
    ).metrics[exact_identifier] == Decimal("1")
    assert (
        api.FactorContribution(
            factor_id=exact_identifier,
            status=api.FactorScoreStatus.MISSING,
            weight=Decimal("1"),
            metric_value=None,
            normalized_value=None,
            contribution=None,
            reason="unavailable",
        ).factor_id
        == exact_identifier
    )

    with pytest.raises(ScoringError, match="256"):
        api.FactorConfiguration(
            factor_id=over_limit_identifier,
            weight=Decimal("1"),
            direction=api.FactorDirection.HIGHER_IS_BETTER,
            normalization=normalization,
        )
    with pytest.raises(ScoringError, match="256"):
        api.CandidateMetrics(
            instrument=first_candidate,
            metrics={over_limit_identifier: Decimal("1")},
        )
    with pytest.raises(ScoringError, match="256"):
        api.FactorContribution(
            factor_id=over_limit_identifier,
            status=api.FactorScoreStatus.MISSING,
            weight=Decimal("1"),
            metric_value=None,
            normalized_value=None,
            contribution=None,
            reason="unavailable",
        )
