"""Tests for TokenAuthMiddleware, constant-time checks, query-token defaults, and remote transport guards."""
import pytest
import httpx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from config import ConfigError
from server import TokenAuthMiddleware, validate_remote_transport_auth


def dummy_endpoint(request):
    return PlainTextResponse("mcp-ok")


@pytest.fixture
def protected_app():
    app = Starlette(routes=[Route("/mcp", dummy_endpoint)])
    app.add_middleware(TokenAuthMiddleware, secret_token="my-super-secret-token")
    return app


@pytest.fixture
def query_opt_in_app():
    app = Starlette(routes=[Route("/mcp", dummy_endpoint)])
    app.add_middleware(TokenAuthMiddleware, secret_token="my-super-secret-token", allow_query_token=True)
    return app


@pytest.mark.asyncio
async def test_request_without_token_is_rejected(protected_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=protected_app), base_url="http://test") as client:
        res = await client.get("/mcp")
        assert res.status_code == 401
        data = res.json()
        assert "Unauthorized" in data["error"]


@pytest.mark.asyncio
async def test_request_with_wrong_token_is_rejected(protected_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=protected_app), base_url="http://test") as client:
        res = await client.get("/mcp", headers={"Authorization": "Bearer wrong-secret"})
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_query_token_rejected_by_default(protected_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=protected_app), base_url="http://test") as client:
        res = await client.get("/mcp?token=my-super-secret-token")
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_query_token_works_only_when_explicitly_enabled(query_opt_in_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=query_opt_in_app), base_url="http://test") as client:
        res = await client.get("/mcp?token=my-super-secret-token")
        assert res.status_code == 200
        assert res.text == "mcp-ok"


@pytest.mark.asyncio
async def test_request_with_bearer_token_succeeds(protected_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=protected_app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer my-super-secret-token"}
        res = await client.get("/mcp", headers=headers)
        assert res.status_code == 200
        assert res.text == "mcp-ok"


def test_remote_transport_refuses_to_start_without_auth():
    with pytest.raises(ConfigError, match="requires authentication"):
        validate_remote_transport_auth("streamable-http", enable_oauth=False, auth_token=None)
    with pytest.raises(ConfigError, match="requires authentication"):
        validate_remote_transport_auth("sse", enable_oauth=False, auth_token=None)
    # Local stdio works without HTTP auth, and authenticated remote transports pass:
    validate_remote_transport_auth("stdio", enable_oauth=False, auth_token=None)
    validate_remote_transport_auth("streamable-http", enable_oauth=True, auth_token=None)
    validate_remote_transport_auth("sse", enable_oauth=False, auth_token="secret-123")
