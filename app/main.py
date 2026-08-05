"""ASGI application composition root."""

from fastapi import FastAPI

from app.api import admin, health
from app.api.dependencies import ApiDependencies, RunRequestBuilder
from app.application.run_coordinator import RunCoordinator


def create_application(
    *,
    coordinator: RunCoordinator | None = None,
    request_builder: RunRequestBuilder | None = None,
) -> FastAPI:
    """Create an HTTP-only application with optional guarded dry-run wiring."""

    application = FastAPI()
    application.state.api_dependencies = ApiDependencies.from_environment(
        coordinator=coordinator,
        request_builder=request_builder,
    )
    application.include_router(health.router)
    application.include_router(admin.router)
    return application


app = create_application()
