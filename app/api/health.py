"""Health and non-sensitive operational status endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.api.dependencies import ApiDependencies, get_api_dependencies

router = APIRouter(tags=["operations"])


class HealthResponse(BaseModel):
    """Liveness response with no runtime configuration details."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    """Readiness response for the ASGI application itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready"] = "ready"


class StatusResponse(BaseModel):
    """Public status that reveals capability state but never credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready"] = "ready"
    run_once_enabled: bool


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Report that the process can serve requests."""

    return HealthResponse()


@router.get("/ready", response_model=ReadinessResponse)
def readiness() -> ReadinessResponse:
    """Report readiness without enabling administrative execution."""

    return ReadinessResponse()


@router.get("/status", response_model=StatusResponse)
def operational_status(
    dependencies: Annotated[ApiDependencies, Depends(get_api_dependencies)],
) -> StatusResponse:
    """Report whether guarded dry-run coordination is configured."""

    return StatusResponse(run_once_enabled=dependencies.run_once_enabled)
