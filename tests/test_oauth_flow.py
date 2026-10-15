"""Tests for OAuth 2.0 PKCE flow, SQLite persistence, and interactive login."""
import secrets
import pytest
import respx
import httpx
from pydantic import AnyUrl
from starlette.requests import Request

from config import Settings
from mcp.server.auth.provider import (
    AuthorizationParams,
    OAuthClientInformationFull,
)
from oauth_store import OAuthStore
from oauth_provider import SQLiteOAuthProvider
from login_views import create_login_handler


@pytest.fixture
def tmp_store(tmp_path):
    db_file = tmp_path / "test_oauth.db"
    return OAuthStore(db_file)


@pytest.fixture
def provider(tmp_store):
    return SQLiteOAuthProvider(tmp_store, "https://mcp.example.com")


@pytest.fixture
def dummy_settings():
    return Settings(
        api_base_url="https://api.marketintel.example.com/api",
        username="dummy_pre",
        password="dummy_password",
        oauth_enabled=True,
        oauth_issuer_url="https://mcp.example.com",
    )


@pytest.mark.asyncio
async def test_oauth_store_client_lifecycle(tmp_store):
    client = OAuthClientInformationFull(
        client_id="test-client-123",
        client_name="Claude Web",
        client_secret="secret-456",
        redirect_uris=[AnyUrl("https://claude.ai/callback")],
        grant_types=["authorization_code", "refresh_token"],
        scope="prospects:entry",
    )
    tmp_store.save_client(client)

    loaded = tmp_store.get_client("test-client-123")
    assert loaded is not None
    assert loaded.client_id == "test-client-123"
    assert loaded.client_name == "Claude Web"
    assert loaded.client_secret == "secret-456"
    assert str(loaded.redirect_uris[0]) == "https://claude.ai/callback"


@pytest.mark.asyncio
async def test_oauth_provider_authorize_and_exchange(provider, tmp_store):
    client = OAuthClientInformationFull(
        client_id="client-abc",
        redirect_uris=[AnyUrl("https://claude.ai/oauth/callback")],
        grant_types=["authorization_code", "refresh_token"],
    )
    tmp_store.save_client(client)

    params = AuthorizationParams(
        redirect_uri=AnyUrl("https://claude.ai/oauth/callback"),
        redirect_uri_provided_explicitly=True,
        state="xyz123",
        scopes=["prospects:entry"],
        code_challenge="dummy_challenge",
    )

    login_url = await provider.authorize(client, params)
    assert login_url.startswith("https://mcp.example.com/login?session_id=")
    session_id = login_url.split("session_id=")[1]

    session = tmp_store.get_auth_session(session_id)
    assert session is not None
    assert session.client_id == "client-abc"
    assert session.state == "xyz123"

    # Simulate login completion saving an auth code
    code = "mcp_code_12345"
    tmp_store.save_auth_code(
        code=code,
        client_id="client-abc",
        redirect_uri="https://claude.ai/oauth/callback",
        code_challenge="dummy_challenge",
        scopes=["prospects:entry"],
        subject="u-001:alice_pre",
        django_access_token="django_at_999",
        django_refresh_token="django_rt_999",
    )

    auth_code_obj = await provider.load_authorization_code(client, code)
    assert auth_code_obj is not None
    assert auth_code_obj.subject == "u-001:alice_pre"

    token_resp = await provider.exchange_authorization_code(client, auth_code_obj)
    assert token_resp.access_token.startswith("mcp_at_")
    assert token_resp.refresh_token.startswith("mcp_rt_")

    # Verify code cannot be reused
    reused = await provider.load_authorization_code(client, code)
    assert reused is None

    # Verify access token loads
    loaded_at = await provider.load_access_token(token_resp.access_token)
    assert loaded_at is not None
    assert loaded_at.subject == "u-001:alice_pre"


@pytest.mark.asyncio
async def test_login_view_get_and_post_success(tmp_store, dummy_settings):
    # Save a pending session
    session_id = "sess_test_123"
    tmp_store.save_auth_session(
        session_id=session_id,
        client_id="c_claude",
        redirect_uri="https://claude.ai/callback",
        state="my_state",
        code_challenge="chal",
        scopes=["prospects:entry"],
        resource=None,
    )

    handler = create_login_handler(tmp_store, dummy_settings)

    # 1. GET /login?session_id=...
    scope_get = {
        "type": "http",
        "method": "GET",
        "path": "/login",
        "headers": [],
        "query_string": b"session_id=sess_test_123",
    }
    req_get = Request(scope_get)
    resp_get = await handler(req_get)
    assert resp_get.status_code == 200
    assert "Sign in to Connect" in resp_get.body.decode()

    # 2. POST /login with valid PRE credentials
    with respx.mock(base_url="https://api.marketintel.example.com/api") as respx_mock:
        respx_mock.post("/auth/login/").respond(
            200,
            json={"access": "mock_django_access", "refresh": "mock_django_refresh"},
        )
        respx_mock.get("/auth/me/").respond(
            200,
            json={"user": {"id": "uuid-1", "username": "sarah_pre", "role": "PRE"}},
        )

        body_bytes = b"session_id=sess_test_123&username=sarah_pre&password=valid_password"
        scope_post = {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "query_string": b"",
        }
        async def receive_post():
            return {"type": "http.request", "body": body_bytes}

        req_post = Request(scope_post, receive=receive_post)
        resp_post = await handler(req_post)

        assert resp_post.status_code == 302
        redirect_location = resp_post.headers["location"]
        assert redirect_location.startswith("https://claude.ai/callback?code=mcp_code_")
        assert "state=my_state" in redirect_location

        # Session should now be consumed
        assert tmp_store.get_auth_session("sess_test_123") is None
