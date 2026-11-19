"""统一 HTTP 状态、错误分类、校验信息和请求编号的接口契约。"""

from types import SimpleNamespace

import httpx
import pytest
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.http_api_client.basic_client import BasicClient
from web import main
from web.api import app as app_api
from web.utils import auth


@pytest.fixture
def contract_app(monkeypatch):
    async def identity(authorization=None, **kwargs):
        if authorization != "Bearer valid":
            raise HTTPException(
                401, "请重新登录", headers={"WWW-Authenticate": "Bearer"}
            )
        return SimpleNamespace(username="operator"), "test"

    monkeypatch.setattr(auth, "get_current_user", identity)
    app = FastAPI()
    app.middleware("http")(main.request_log_context)
    for cls, handler in [
        (HTTPException, main.business_exception_handler),
        (StarletteHTTPException, main.http_exception_handler),
        (RequestValidationError, main.validation_exception_handler),
        (Exception, main.general_exception_handler),
        (
            requests.exceptions.RequestException,
            main.upstream_http_exception_handler,
        ),
    ]:
        app.add_exception_handler(cls, handler)

    @app.get("/code/{code}")
    @auth.auth_with_renew()
    def business(request: Request, code: int):
        return code, "operation result", {"value": "kept"}

    @app.get("/envelope")
    @auth.auth_with_renew()
    def envelope(request: Request):
        return {"code": 409, "message": "conflict", "data": None}

    @app.get("/binary")
    @auth.auth_with_renew()
    def binary(request: Request):
        return Response(b"\x00\xff", media_type="application/octet-stream")

    @app.get("/exception/{code}")
    def exception(code: int):
        raise HTTPException(
            code, "operation refused", headers={"Retry-After": "7"}
        )

    @app.get("/failure/{mode}")
    def failure(mode: str):
        if mode == "network":
            raise requests.exceptions.ConnectionError(
                "private upstream credentials"
            )
        if mode == "timeout":
            raise requests.exceptions.Timeout("private upstream credentials")
        raise ValueError("private internal data")

    @app.get("/structured-detail")
    def structured():
        raise HTTPException(
            503,
            detail={"status": "not_ready", "failures": ["worker unavailable"]},
        )

    class Body(BaseModel):
        count: int
        password: str

    @app.post("/validation")
    def validation(body: Body):
        return body.model_dump()

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["get/999", "cluster/999", "clusterInfo/999"])
async def test_missing_app_or_cluster_is_404(contract_app, monkeypatch, path):
    monkeypatch.setattr(app_api, "find_application_by_id", lambda _: None)
    monkeypatch.setattr(app_api, "find_cluster_by_id", lambda _: None)
    contract_app.include_router(app_api.router, prefix="/api/v1/app")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=contract_app), base_url="http://test"
    ) as http:
        response = await http.get(
            f"/api/v1/app/{path}", headers={"Authorization": "Bearer valid"}
        )
    assert response.status_code == 404
    assert response.json()["error_code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_helm_read_failure_is_not_an_empty_success(
    contract_app, monkeypatch
):
    monkeypatch.setattr(
        app_api,
        "find_cluster_by_id",
        lambda _: SimpleNamespace(helm_name="demo"),
    )
    monkeypatch.setattr(
        app_api,
        "_helm_command",
        lambda _: SimpleNamespace(is_success=lambda: False),
    )
    contract_app.include_router(app_api.router, prefix="/api/v1/app")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=contract_app), base_url="http://test"
    ) as http:
        response = await http.get(
            "/api/v1/app/clusterInfo/999",
            headers={"Authorization": "Bearer valid"},
        )
    assert response.status_code == 502
    assert response.json()["error_code"] == "UPSTREAM_ERROR"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        200,
        201,
        202,
        204,
        400,
        403,
        404,
        409,
        413,
        422,
        429,
        500,
        502,
        503,
        504,
    ],
)
async def test_business_status_matches_http_with_envelope_and_trace(
    contract_app, code
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=contract_app), base_url="http://test"
    ) as http:
        response = await http.get(
            f"/code/{code}",
            headers={
                "Authorization": "Bearer valid",
                "X-Request-ID": "contract-test",
            },
        )
    assert response.status_code == (200 if code == 204 else code)
    body = response.json()
    assert body["code"] == code and body["data"] == {"value": "kept"}
    assert (
        body["request_id"]
        == response.headers["X-Request-ID"]
        == "contract-test"
    )
    assert bool(body.get("error_code")) == (code >= 400)


@pytest.mark.asyncio
async def test_upstream_401_is_not_an_app_authentication_failure(contract_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=contract_app), base_url="http://test"
    ) as http:
        upstream = await http.get(
            "/code/401", headers={"Authorization": "Bearer valid"}
        )
        expired = await http.get("/code/200")
    assert upstream.status_code == 502
    assert upstream.json()["error_code"] == "UPSTREAM_AUTH_ERROR"
    assert upstream.json()["details"]["upstream_status"] == 401
    assert (
        expired.status_code == 401
        and expired.json()["error_code"] == "AUTH_REQUIRED"
    )
    assert expired.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_direct_envelopes_and_binary_responses_are_not_double_wrapped(
    contract_app,
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=contract_app),
        base_url="http://test",
        headers={"Authorization": "Bearer valid"},
    ) as http:
        response = await http.get("/envelope")
        binary = await http.get("/binary")
    assert response.status_code == 409 and response.json()["data"] is None
    assert (
        binary.content == b"\x00\xff"
        and binary.headers["content-type"] == "application/octet-stream"
    )


@pytest.mark.asyncio
async def test_validation_is_structured_without_raw_secret_input(contract_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=contract_app), base_url="http://test"
    ) as http:
        response = await http.post(
            "/validation",
            json={"count": "not-an-integer", "password": "private-password"},
        )
    assert (
        response.status_code == 422
        and response.json()["error_code"] == "VALIDATION_ERROR"
    )
    field = response.json()["details"][0]
    assert field["loc"] == ["body", "count"] and "input" not in field
    assert (
        "private-password" not in response.text
        and "not-an-integer" not in response.text
    )


@pytest.mark.asyncio
async def test_http_headers_and_structured_http_details_are_preserved(
    contract_app,
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=contract_app), base_url="http://test"
    ) as http:
        limited = await http.get("/exception/429")
        structured = await http.get("/structured-detail")
    assert (
        limited.headers["Retry-After"] == "7"
        and limited.json()["error_code"] == "RATE_LIMITED"
    )
    assert (
        structured.status_code == 503
        and structured.json()["details"]["status"] == "not_ready"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,code", [("network", 502), ("timeout", 504), ("internal", 500)]
)
async def test_failures_have_trace_without_sensitive_response(
    contract_app, mode, code
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=contract_app, raise_app_exceptions=False
        ),
        base_url="http://test",
    ) as http:
        response = await http.get(
            "/failure/" + mode, headers={"X-Request-ID": "error-trace"}
        )
    assert response.status_code == code
    assert (
        response.json()["request_id"]
        == response.headers["X-Request-ID"]
        == "error-trace"
    )
    assert "private" not in response.text


def test_upstream_adapter_handles_created_empty_success_and_malformed_json():
    client = BasicClient.__new__(BasicClient)
    response = SimpleNamespace(
        status_code=201,
        text='{"created":true}',
        json=lambda: {"created": True},
    )
    assert client.api_result(response) == (201, "success", {"created": True})

    def invalid():
        raise ValueError("invalid json")

    response = SimpleNamespace(status_code=204, text="", json=invalid)
    assert client.api_result(response) == (204, "success", {})
    response.status_code, response.text = 200, "<html>proxy failure</html>"
    assert client.api_result(response)[0] == 502
    response.status_code = 502
    assert "<html>" not in client.api_result(response)[1]
