"""Tests for TokenAuthMiddleware and token-based protection for HTTP/SSE transports."""
import pytest
import httpx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from server import TokenAuthMiddleware
from config import Settings, load_settings


def dummy_endpoint(request):
    return PlainTextResponse("mcp-ok")


@pytest.fixture
def protected_app():
    app = Starlette(routes=[Route("/mcp", dummy_endpoint)])
    app.add_middleware(TokenAuthMiddleware, secret_token="my-super-secret-token")
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
        res = await client.get("/mcp?token=wrong-secret")
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_request_with_correct_query_token_succeeds(protected_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=protected_app), base_url="http://test") as client:
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
