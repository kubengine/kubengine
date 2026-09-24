import asyncio
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from core.logger import get_log_context
from web.main import request_log_context


def make_request(request_id: str) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": [(b"x-request-id", request_id.encode())],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    return Request(scope)


def test_request_middleware_binds_and_returns_request_id() -> None:
    observed_context: dict[str, str] = {}

    async def call_next(request: Request) -> Response:
        observed_context.update(get_log_context())
        assert request.state.request_id == "req-web-1"
        return Response("ok")

    response = asyncio.run(
        request_log_context(make_request("req-web-1"), call_next)
    )

    assert observed_context == {"request_id": "req-web-1"}
    assert response.headers["X-Request-ID"] == "req-web-1"
    assert get_log_context() == {}


def test_request_middleware_replaces_invalid_request_id() -> None:
    async def call_next(request: Request) -> Response:
        assert request.state.request_id != "invalid\nvalue"
        return Response("ok")

    response = asyncio.run(
        request_log_context(make_request("invalid\nvalue"), call_next)
    )

    assert len(response.headers["X-Request-ID"]) == 32
