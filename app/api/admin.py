"""Guarded administrative dry-run API adapter."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict

from app.api.body_size import RequestBodyTooLargeResponse
from app.api.dependencies import ApiDependencies, get_api_dependencies, require_admin_token
from app.application.run_coordinator import (
    ExistingResultReference,
    RunCoordinatorResult,
    RunDisposition,
    TriggerSource,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_literal_json_true(value: object) -> Literal[True]:
    """Reject look-alike JSON values before Literal validation can coerce them."""

    if type(value) is not bool or value is not True:
        raise ValueError("dry_run must be the JSON boolean true")
    return True


class RunOnceRequest(BaseModel):
    """External command that admits only an explicit dry run for one rotation plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rotation_plan_id: UUID
    dry_run: Annotated[Literal[True], BeforeValidator(_require_literal_json_true)]


class ExistingResultResponse(BaseModel):
    """Non-sensitive duplicate reference for an already-running logical scan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scan_lock_key: str
    logical_scan_run_id: UUID | None

    @classmethod
    def from_domain(cls, result: ExistingResultReference) -> ExistingResultResponse:
        return cls(
            scan_lock_key=result.scan_lock_key,
            logical_scan_run_id=result.logical_scan_run_id,
        )


class RunOnceResponse(BaseModel):
    """Typed coordinator summary that deliberately excludes audit payloads and secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: RunDisposition
    scan_lock_key: str
    logical_scan_run_id: UUID | None
    final_strategy_key: str | None
    existing_result: ExistingResultResponse | None

    @classmethod
    def from_domain(cls, result: RunCoordinatorResult) -> RunOnceResponse:
        return cls(
            disposition=result.disposition,
            scan_lock_key=result.scan_lock_key,
            logical_scan_run_id=result.logical_scan_run_id,
            final_strategy_key=result.final_strategy_key,
            existing_result=(
                None
                if result.existing_result is None
                else ExistingResultResponse.from_domain(result.existing_result)
            ),
        )


@router.post(
    "/run-once",
    response_model=RunOnceResponse,
    dependencies=[Depends(require_admin_token)],
    responses={
        409: {"model": RunOnceResponse},
        413: {"model": RequestBodyTooLargeResponse},
    },
)
def run_once(
    payload: RunOnceRequest,
    dependencies: Annotated[ApiDependencies, Depends(get_api_dependencies)],
) -> RunOnceResponse | JSONResponse:
    """Coordinate a protected dry run; this adapter has no order or position mutation path."""

    result = dependencies.run_once(
        rotation_plan_id=payload.rotation_plan_id,
        trigger_source=TriggerSource.API,
    )
    response = RunOnceResponse.from_domain(result)
    if result.disposition is RunDisposition.API_CONFLICT:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=response.model_dump(mode="json"),
        )
    return response
