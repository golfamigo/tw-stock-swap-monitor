"""ASGI application composition root."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Never
from uuid import UUID

from fastapi import FastAPI, HTTPException, status

from app.api import admin, health
from app.api.body_size import ADMIN_RUN_ONCE_BODY_MAX_BYTES, AdminRequestBodyLimitMiddleware
from app.api.dependencies import ApiDependencies, RunRequestBuilder
from app.application.run_coordinator import RunCoordinator, RunCoordinatorRequest, TriggerSource
from app.data_sources.base import MarketDataProvider
from app.data_sources.models import MarketDataRequest
from app.domain.errors import NotFoundForActor
from app.persistence.in_memory import (
    InMemoryChildIntentRepository,
    InMemoryConfigurationSnapshotRepository,
    InMemoryLockProvider,
    InMemoryLogicalScanRepository,
    InMemoryPositionRepository,
    InMemoryRecommendationStateRepository,
    InMemoryRotationPlanRepository,
    InMemoryStrategyRunRepository,
)
from app.repositories.locks import LockProvider
from app.services.rotation_run import RotationEvaluation, RotationEvaluator


class _FailClosedMarketDataProvider:
    """Non-network provider that prevents an unconfigured process from fabricating evidence."""

    provider_name = "unconfigured-in-memory"

    def get_market_snapshot(self, request: MarketDataRequest) -> Never:
        del request
        raise RuntimeError("market-data execution context is not configured")

    def get_quotes(self, request: MarketDataRequest) -> Never:
        del request
        raise AssertionError("the coordinator requires one atomic market-data snapshot")

    def get_intraday_bars(self, request: MarketDataRequest) -> Never:
        del request
        raise AssertionError("the coordinator requires one atomic market-data snapshot")

    def get_daily_bars(self, request: MarketDataRequest) -> Never:
        del request
        raise AssertionError("the coordinator requires one atomic market-data snapshot")


def _unreachable_evaluator(*_: object) -> RotationEvaluation:
    """Make missing execution context explicit if a caller bypasses the request builder."""

    raise AssertionError("run-once evaluator requires an explicit execution context")


class _InMemoryRunRequestBuilder:
    """Resolve pre-approved request templates against the coordinator's plan repository."""

    def __init__(
        self,
        *,
        rotation_plans: InMemoryRotationPlanRepository,
        request_templates: Mapping[UUID, RunCoordinatorRequest],
    ) -> None:
        templates = dict(request_templates)
        if any(
            rotation_plan_id != request.plan.rotation_plan_id
            for rotation_plan_id, request in templates.items()
        ):
            raise ValueError("request templates must be keyed by their rotation plan identifier")
        self._rotation_plans = rotation_plans
        self._request_templates = MappingProxyType(templates)

    def __call__(
        self, rotation_plan_id: UUID, *, trigger_source: TriggerSource
    ) -> RunCoordinatorRequest:
        try:
            template = self._request_templates[rotation_plan_id]
        except KeyError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="rotation plan was not found",
            ) from error
        try:
            plan = self._rotation_plans.get(
                rotation_plan_id,
                access_context=template.access_context,
            )
        except NotFoundForActor as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="rotation plan was not found",
            ) from error
        return replace(template, plan=plan, trigger_source=trigger_source)


@dataclass(frozen=True, slots=True)
class InMemoryRunOnceComposition:
    """Public pairing of a coordinator with its plan-resolving in-memory request builder."""

    coordinator: RunCoordinator
    request_builder: RunRequestBuilder


def create_in_memory_run_once_composition(
    *,
    rotation_plans: InMemoryRotationPlanRepository,
    configuration_snapshots: InMemoryConfigurationSnapshotRepository,
    positions: InMemoryPositionRepository,
    recommendation_states: InMemoryRecommendationStateRepository,
    child_intents: InMemoryChildIntentRepository,
    logical_scans: InMemoryLogicalScanRepository,
    strategy_runs: InMemoryStrategyRunRepository,
    locks: LockProvider,
    market_data: MarketDataProvider,
    evaluator: RotationEvaluator,
    now: Callable[[], datetime],
    request_templates: Mapping[UUID, RunCoordinatorRequest],
) -> InMemoryRunOnceComposition:
    """Build a shared coordinator and request builder from one in-memory adapter set."""

    coordinator = RunCoordinator(
        rotation_plans=rotation_plans,
        configuration_snapshots=configuration_snapshots,
        positions=positions,
        recommendation_states=recommendation_states,
        child_intents=child_intents,
        logical_scans=logical_scans,
        strategy_runs=strategy_runs,
        locks=locks,
        market_data=market_data,
        evaluator=evaluator,
        now=now,
    )
    return InMemoryRunOnceComposition(
        coordinator=coordinator,
        request_builder=_InMemoryRunRequestBuilder(
            rotation_plans=rotation_plans,
            request_templates=request_templates,
        ),
    )


def _create_safe_default_composition() -> InMemoryRunOnceComposition:
    """Compose only in-memory, non-network adapters for the importable ASGI application."""

    portfolio_owners: dict[UUID, UUID] = {}
    rotation_plans = InMemoryRotationPlanRepository(
        portfolio_owners,
        candidate_group_owners={},
        position_portfolios={},
    )
    logical_scans = InMemoryLogicalScanRepository(portfolio_owners)
    child_intents = InMemoryChildIntentRepository(portfolio_owners)
    positions = InMemoryPositionRepository(portfolio_owners)
    recommendation_states = InMemoryRecommendationStateRepository(portfolio_owners)
    strategy_runs = InMemoryStrategyRunRepository(
        portfolio_owners,
        logical_scan_repository=logical_scans,
        child_intent_repository=child_intents,
    )
    return create_in_memory_run_once_composition(
        rotation_plans=rotation_plans,
        configuration_snapshots=InMemoryConfigurationSnapshotRepository(portfolio_owners),
        positions=positions,
        recommendation_states=recommendation_states,
        child_intents=child_intents,
        logical_scans=logical_scans,
        strategy_runs=strategy_runs,
        locks=InMemoryLockProvider(portfolio_owners),
        market_data=_FailClosedMarketDataProvider(),
        evaluator=_unreachable_evaluator,
        now=lambda: datetime.now(UTC),
        request_templates={},
    )


def create_application(
    *,
    composition: InMemoryRunOnceComposition | None = None,
    coordinator: RunCoordinator | None = None,
    request_builder: RunRequestBuilder | None = None,
) -> FastAPI:
    """Create an HTTP-only application with optional guarded dry-run wiring."""

    if composition is not None:
        if coordinator is not None or request_builder is not None:
            raise ValueError("composition cannot be combined with direct coordinator dependencies")
        configured_coordinator = composition.coordinator
        configured_request_builder = composition.request_builder
    elif coordinator is None and request_builder is None:
        default_composition = _create_safe_default_composition()
        configured_coordinator = default_composition.coordinator
        configured_request_builder = default_composition.request_builder
    elif coordinator is None or request_builder is None:
        raise ValueError("coordinator and request_builder must be configured together")
    else:
        configured_coordinator = coordinator
        configured_request_builder = request_builder

    application = FastAPI()
    application.add_middleware(
        AdminRequestBodyLimitMiddleware,
        maximum_body_bytes=ADMIN_RUN_ONCE_BODY_MAX_BYTES,
    )
    application.state.api_dependencies = ApiDependencies.from_environment(
        coordinator=configured_coordinator,
        request_builder=configured_request_builder,
    )
    application.include_router(health.router)
    application.include_router(admin.router)
    return application


app = create_application()
