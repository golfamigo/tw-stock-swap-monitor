"""ASGI application composition root."""

from datetime import UTC, datetime
from typing import Never
from uuid import UUID

from fastapi import FastAPI, HTTPException, status

from app.api import admin, health
from app.api.body_size import ADMIN_RUN_ONCE_BODY_MAX_BYTES, AdminRequestBodyLimitMiddleware
from app.api.dependencies import ApiDependencies, RunRequestBuilder
from app.application.run_coordinator import RunCoordinator, RunCoordinatorRequest
from app.data_sources.models import MarketDataRequest
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
from app.services.rotation_run import RotationEvaluation


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


def _unconfigured_request_builder(*_: object, **__: object) -> RunCoordinatorRequest:
    """Require deployers to inject a validated request builder with plan inputs."""

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="dry-run execution context is not configured",
    )


def _create_safe_default_coordinator() -> RunCoordinator:
    """Compose only in-memory, non-network adapters for the importable ASGI application."""

    portfolio_owners: dict[UUID, UUID] = {}
    rotation_plans = InMemoryRotationPlanRepository(
        portfolio_owners,
        candidate_group_owners={},
        position_portfolios={},
    )
    logical_scans = InMemoryLogicalScanRepository(portfolio_owners)
    child_intents = InMemoryChildIntentRepository(portfolio_owners)
    return RunCoordinator(
        rotation_plans=rotation_plans,
        configuration_snapshots=InMemoryConfigurationSnapshotRepository(portfolio_owners),
        positions=InMemoryPositionRepository(portfolio_owners),
        recommendation_states=InMemoryRecommendationStateRepository(portfolio_owners),
        child_intents=child_intents,
        logical_scans=logical_scans,
        strategy_runs=InMemoryStrategyRunRepository(
            portfolio_owners,
            logical_scan_repository=logical_scans,
            child_intent_repository=child_intents,
        ),
        locks=InMemoryLockProvider(portfolio_owners),
        market_data=_FailClosedMarketDataProvider(),
        evaluator=_unreachable_evaluator,
        now=lambda: datetime.now(UTC),
    )


def create_application(
    *,
    coordinator: RunCoordinator | None = None,
    request_builder: RunRequestBuilder | None = None,
) -> FastAPI:
    """Create an HTTP-only application with optional guarded dry-run wiring."""

    application = FastAPI()
    application.add_middleware(
        AdminRequestBodyLimitMiddleware,
        maximum_body_bytes=ADMIN_RUN_ONCE_BODY_MAX_BYTES,
    )
    configured_coordinator = (
        _create_safe_default_coordinator() if coordinator is None else coordinator
    )
    configured_request_builder = (
        _unconfigured_request_builder if request_builder is None else request_builder
    )
    application.state.api_dependencies = ApiDependencies.from_environment(
        coordinator=configured_coordinator,
        request_builder=configured_request_builder,
    )
    application.include_router(health.router)
    application.include_router(admin.router)
    return application


app = create_application()
