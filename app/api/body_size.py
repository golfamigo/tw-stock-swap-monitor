"""ASGI request-body limit for the protected administrative command."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ADMIN_RUN_ONCE_PATH = "/admin/run-once"
ADMIN_RUN_ONCE_BODY_MAX_BYTES = 16_384
REQUEST_BODY_TOO_LARGE_DETAIL: Literal["request body exceeds configured limit"] = (
    "request body exceeds configured limit"
)


class RequestBodyTooLargeResponse(BaseModel):
    """Typed error body for a rejected request that was never parsed as JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: Literal["request body exceeds configured limit"] = REQUEST_BODY_TOO_LARGE_DETAIL


class _RequestBodyTooLarge(Exception):
    """Abort downstream parsing after the streaming byte ceiling is exceeded."""


class AdminRequestBodyLimitMiddleware:
    """Reject declared and observed oversize admin bodies before FastAPI parses JSON."""

    def __init__(self, app: ASGIApp, *, maximum_body_bytes: int) -> None:
        if type(maximum_body_bytes) is not int:
            raise TypeError("maximum_body_bytes must be an integer")
        if maximum_body_bytes <= 0:
            raise ValueError("maximum_body_bytes must be a positive integer")
        self.app = app
        self.maximum_body_bytes = maximum_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_admin_run_once_scope(scope):
            await self.app(scope, receive, send)
            return
        if _declared_body_exceeds_limit(scope, maximum_body_bytes=self.maximum_body_bytes):
            await _send_body_too_large(send)
            return

        observed_body_bytes = 0
        body_limit_exceeded = False

        async def limited_receive() -> Message:
            nonlocal body_limit_exceeded, observed_body_bytes
            message = await receive()
            if message["type"] == "http.request":
                observed_body_bytes += len(message.get("body", b""))
                if observed_body_bytes > self.maximum_body_bytes:
                    body_limit_exceeded = True
                    raise _RequestBodyTooLarge
            return message

        async def limited_send(message: Message) -> None:
            if not body_limit_exceeded:
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except _RequestBodyTooLarge:
            body_limit_exceeded = True
        if body_limit_exceeded:
            await _send_body_too_large(send)


def _declared_body_exceeds_limit(scope: Scope, *, maximum_body_bytes: int) -> bool:
    """Use every valid Content-Length value as an early, non-parsing rejection signal."""

    for header_name, header_value in scope["headers"]:
        if header_name.lower() != b"content-length":
            continue
        try:
            declared_body_bytes = int(header_value)
        except ValueError:
            continue
        if declared_body_bytes > maximum_body_bytes:
            return True
    return False


def _is_admin_run_once_scope(scope: Scope) -> bool:
    """Match the route whether ASGI path includes the deployment root path or not."""

    path = scope.get("path")
    root_path = scope.get("root_path", "")
    if not isinstance(path, str) or not isinstance(root_path, str):
        return False
    if path == ADMIN_RUN_ONCE_PATH:
        return True
    normalized_root_path = root_path.rstrip("/")
    if not normalized_root_path:
        return False
    root_path_prefix = f"{normalized_root_path}/"
    if not path.startswith(root_path_prefix):
        return False
    return path[len(normalized_root_path) :] == ADMIN_RUN_ONCE_PATH


async def _send_body_too_large(send: Send) -> None:
    """Send a static, secret-free 413 response without passing a partial body onward."""

    body = RequestBodyTooLargeResponse().model_dump_json().encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": (
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ),
        }
    )
    await send({"type": "http.response.body", "body": body})
