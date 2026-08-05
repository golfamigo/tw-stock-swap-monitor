"""FastAPI dependencies for guarded, shared dry-run coordination."""

from __future__ import annotations

import os
from dataclasses import dataclass
from hmac import compare_digest
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from app.application.run_coordinator import (
    RunCoordinator,
    RunCoordinatorRequest,
    RunCoordinatorResult,
    TriggerSource,
)


class RunRequestBuilder(Protocol):
    """Build a fully validated coordinator request for one explicit trigger source."""

    def __call__(
        self, rotation_plan_id: UUID, *, trigger_source: TriggerSource
    ) -> RunCoordinatorRequest: ...


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    """Explicit composition-root dependencies; no provider or broker is constructed here."""

    coordinator: RunCoordinator | None
    request_builder: RunRequestBuilder | None
    admin_api_token: str | None

    @classmethod
    def from_environment(
        cls,
        *,
        coordinator: RunCoordinator | None = None,
        request_builder: RunRequestBuilder | None = None,
    ) -> ApiDependencies:
        """Read the sole administrative credential from the deployment environment."""

        admin_api_token = os.environ.get("ADMIN_API_TOKEN")
        if admin_api_token is not None and (
            not admin_api_token.strip() or not admin_api_token.isascii()
        ):
            admin_api_token = None
        return cls(
            coordinator=coordinator,
            request_builder=request_builder,
            admin_api_token=admin_api_token,
        )

    @property
    def run_once_enabled(self) -> bool:
        """Require both an explicit token and the shared coordinator workflow."""

        return (
            self.admin_api_token is not None
            and self.coordinator is not None
            and self.request_builder is not None
        )

    def run_once(
        self, *, rotation_plan_id: UUID, trigger_source: TriggerSource
    ) -> RunCoordinatorResult:
        """Use the sole coordinator path for API and future scheduler invocations."""

        if self.coordinator is None or self.request_builder is None:
            raise RuntimeError("run-once coordinator dependencies are not configured")
        request = self.request_builder(rotation_plan_id, trigger_source=trigger_source)
        return self.coordinator.run(request)


def get_api_dependencies(request: Request) -> ApiDependencies:
    """Resolve the immutable factory wiring from application state."""

    dependencies = request.app.state.api_dependencies
    if not isinstance(dependencies, ApiDependencies):
        raise RuntimeError("application API dependencies are invalid")
    return dependencies


def require_admin_token(
    dependencies: Annotated[ApiDependencies, Depends(get_api_dependencies)],
    x_admin_token: Annotated[str | None, Header()] = None,
) -> None:
    """Fail closed when the token is absent, invalid, or intentionally unconfigured."""

    expected_token = dependencies.admin_api_token
    if expected_token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin run-once is disabled",
        )
    if (
        x_admin_token is None
        or not x_admin_token.isascii()
        or not compare_digest(x_admin_token.encode("ascii"), expected_token.encode("ascii"))
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid administrative credentials",
            headers={"WWW-Authenticate": "AdminToken"},
        )
    if not dependencies.run_once_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin run-once is unavailable",
        )
