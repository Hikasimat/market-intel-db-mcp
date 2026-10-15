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
async def test_oauth_provider_cimd_resolution(provider, tmp_store):
    metadata_url = "https://claude.ai/oauth/metadata.json"
    with respx.mock:
        respx.get(metadata_url).respond(
            200,
            json={
                "client_name": "Claude",
                "redirect_uris": ["https://claude.ai/api/connectors/oauth/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_method": "none",
            },
        )

        client = await provider.get_client(metadata_url)
        assert client is not None
        assert client.client_id == metadata_url
        assert client.client_name == "Claude"
        assert str(client.redirect_uris[0]) == "https://claude.ai/api/connectors/oauth/callback"

        # Should now be cached in store
        cached = tmp_store.get_client(metadata_url)
        assert cached is not None
        assert cached.client_name == "Claude"



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


@pytest.mark.asyncio
async def test_oauth_store_auth_method_persistence(tmp_store):
    """Verify that token_endpoint_auth_method is persisted and defaulted correctly."""
    # 1. Explicit none (public client)
    c_none = OAuthClientInformationFull(
        client_id="client-none",
        redirect_uris=[AnyUrl("https://claude.ai/callback")],
        token_endpoint_auth_method="none",
    )
    tmp_store.save_client(c_none)
    loaded_none = tmp_store.get_client("client-none")
    assert loaded_none is not None
    assert loaded_none.token_endpoint_auth_method == "none"
    assert loaded_none.client_secret is None

    # 2. Explicit client_secret_post (confidential client)
    c_secret = OAuthClientInformationFull(
        client_id="client-secret",
        client_secret="sec123",
        redirect_uris=[AnyUrl("https://example.com/callback")],
        token_endpoint_auth_method="client_secret_post",
    )
    tmp_store.save_client(c_secret)
    loaded_secret = tmp_store.get_client("client-secret")
    assert loaded_secret is not None
    assert loaded_secret.token_endpoint_auth_method == "client_secret_post"
    assert loaded_secret.client_secret == "sec123"

    # 3. Omitted token_endpoint_auth_method with no secret -> defaults to none
    c_omitted = OAuthClientInformationFull(
        client_id="client-omitted",
        redirect_uris=[AnyUrl("https://claude.ai/callback")],
    )
    tmp_store.save_client(c_omitted)
    loaded_omitted = tmp_store.get_client("client-omitted")
    assert loaded_omitted is not None
    assert loaded_omitted.token_endpoint_auth_method == "none"


@pytest.mark.asyncio
async def test_client_authenticator_allows_public_client(tmp_store, provider):
    """Verify ClientAuthenticator accepts public clients without client secret."""
    from mcp.server.auth.middleware.client_auth import ClientAuthenticator, AuthenticationError

    c_pub = OAuthClientInformationFull(
        client_id="pub-client-1",
        redirect_uris=[AnyUrl("https://claude.ai/callback")],
        token_endpoint_auth_method="none",
    )
    tmp_store.save_client(c_pub)

    auth = ClientAuthenticator(provider)

    class FakePublicRequest:
        async def form(self):
            return {"client_id": "pub-client-1"}
        headers = {}

    authenticated_client = await auth.authenticate_request(FakePublicRequest())
    assert authenticated_client.client_id == "pub-client-1"
    assert authenticated_client.token_endpoint_auth_method == "none"


@pytest.mark.asyncio
async def test_oauth_metadata_and_protected_resource_discovery(dummy_settings, tmp_store):
    """Verify public OAuth authorization-server discovery and RFC 9728 protected resource metadata."""
    from confirmation_store import ConfirmationStore
    from workflow import Workflow
    from api_client import MarketIntelClient
    from server import create_mcp, configure_oauth_endpoints
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

    client_mock = MarketIntelClient(dummy_settings)
    wf = Workflow(client_mock, ConfirmationStore(300), dummy_settings)
    issuer = "https://marketintel-mcp.hikasimat.com"
    provider = SQLiteOAuthProvider(tmp_store, issuer)
    auth_settings = AuthSettings(
        issuer_url=issuer,
        resource_server_url=f"{issuer}/mcp",
        validate_token_resource=False,
        client_registration_options=ClientRegistrationOptions(enabled=True),
    )
    mcp = create_mcp(lambda: wf, auth_settings=auth_settings, auth_server_provider=provider)
    app = mcp.streamable_http_app()
    configure_oauth_endpoints(app, issuer)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http_client:
        # 1. Authorization server metadata
        res = await http_client.get("/.well-known/oauth-authorization-server")
        assert res.status_code == 200
        data = res.json()
        assert data["issuer"] == "https://marketintel-mcp.hikasimat.com"
        assert data["authorization_endpoint"] == "https://marketintel-mcp.hikasimat.com/authorize"
        assert data["token_endpoint"] == "https://marketintel-mcp.hikasimat.com/token"
        assert data["registration_endpoint"] == "https://marketintel-mcp.hikasimat.com/register"
        assert "none" in data["token_endpoint_auth_methods_supported"]
        assert "client_secret_post" in data["token_endpoint_auth_methods_supported"]
        assert data["response_types_supported"] == ["code"]
        assert "authorization_code" in data["grant_types_supported"]
        assert "S256" in data["code_challenge_methods_supported"]
        assert data["client_id_metadata_document_supported"] is True

        # 2. Protected resource metadata (root path)
        res_pr1 = await http_client.get("/.well-known/oauth-protected-resource")
        assert res_pr1.status_code == 200
        data_pr1 = res_pr1.json()
        assert data_pr1["resource"] == "https://marketintel-mcp.hikasimat.com/mcp"
        assert "https://marketintel-mcp.hikasimat.com" in data_pr1["authorization_servers"]

        # 3. Protected resource metadata (/mcp path)
        res_pr2 = await http_client.get("/.well-known/oauth-protected-resource/mcp")
        assert res_pr2.status_code == 200
        data_pr2 = res_pr2.json()
        assert data_pr2["resource"] == "https://marketintel-mcp.hikasimat.com/mcp"
        assert "https://marketintel-mcp.hikasimat.com" in data_pr2["authorization_servers"]


@pytest.mark.asyncio
async def test_dynamic_client_registration_and_pkce_exchange_flow(dummy_settings, tmp_store):
    """Verify full OAuth PKCE authorization-code exchange without client secret."""
    import base64
    import hashlib
    from urllib.parse import urlparse, parse_qs
    from confirmation_store import ConfirmationStore
    from workflow import Workflow
    from api_client import MarketIntelClient
    from server import create_mcp, configure_oauth_endpoints, DefaultPublicClientAuthMethodMiddleware
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

    client_mock = MarketIntelClient(dummy_settings)
    wf = Workflow(client_mock, ConfirmationStore(300), dummy_settings)
    issuer = "https://marketintel-mcp.hikasimat.com"
    provider = SQLiteOAuthProvider(tmp_store, issuer)
    auth_settings = AuthSettings(
        issuer_url=issuer,
        resource_server_url=f"{issuer}/mcp",
        validate_token_resource=False,
        client_registration_options=ClientRegistrationOptions(enabled=True),
    )
    mcp = create_mcp(lambda: wf, auth_settings=auth_settings, auth_server_provider=provider)
    mcp.custom_route("/login", methods=["GET", "POST"])(create_login_handler(tmp_store, dummy_settings))
    app = mcp.streamable_http_app()
    configure_oauth_endpoints(app, issuer)
    app.add_middleware(DefaultPublicClientAuthMethodMiddleware)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http_client:
        # 1. Dynamic Client Registration (Claude omits token_endpoint_auth_method)
        reg_resp = await http_client.post(
            "/register",
            json={
                "client_name": "Claude Test Connector",
                "redirect_uris": ["https://claude.ai/api/oauth/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert reg_resp.status_code == 201
        reg_data = reg_resp.json()
        client_id = reg_data["client_id"]
        assert reg_data["token_endpoint_auth_method"] == "none"
        assert reg_data.get("client_secret") is None

        # 2. PKCE code verifier and challenge
        verifier = secrets.token_urlsafe(32)
        sha = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(sha).decode("ascii").rstrip("=")

        # 3. GET /authorize
        auth_url = f"/authorize?response_type=code&client_id={client_id}&redirect_uri=https://claude.ai/api/oauth/callback&code_challenge={challenge}&code_challenge_method=S256&state=state_abc"
        auth_resp = await http_client.get(auth_url)
        assert auth_resp.status_code == 302
        login_url = auth_resp.headers["location"]
        session_id = login_url.split("session_id=")[1]

        # 4. POST /login with PRE user
        with respx.mock(base_url=dummy_settings.api_base_url) as respx_mock:
            respx_mock.post("/auth/login/").respond(200, json={"access": "django_acc", "refresh": "django_ref"})
            respx_mock.get("/auth/me/").respond(200, json={"user": {"id": "u1", "username": "pre_user", "role": "PRE"}})

            login_resp = await http_client.post(
                "/login",
                data={"session_id": session_id, "username": "pre_user", "password": "password"},
            )
            assert login_resp.status_code == 302
            cb_url = login_resp.headers["location"]
            parsed_cb = parse_qs(urlparse(cb_url).query)
            code = parsed_cb["code"][0]
            assert parsed_cb["state"][0] == "state_abc"

        # 5. POST /token - Token exchange without secret
        token_resp = await http_client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": "https://claude.ai/api/oauth/callback",
            },
        )
        assert token_resp.status_code == 200
        token_json = token_resp.json()
        assert "access_token" in token_json
        assert "refresh_token" in token_json
        access_tok = token_json["access_token"]
        refresh_tok = token_json["refresh_token"]

        # 6. Reusing code must be rejected (single-use code)
        reuse_resp = await http_client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": "https://claude.ai/api/oauth/callback",
            },
        )
        assert reuse_resp.status_code == 400
        assert reuse_resp.json()["error"] == "invalid_grant"

        # 7. Refresh token rotation
        ref_resp = await http_client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_tok,
            },
        )
        assert ref_resp.status_code == 200
        new_ref_json = ref_resp.json()
        assert new_ref_json["access_token"] != access_tok
        assert new_ref_json["refresh_token"] != refresh_tok

        # 8. Reusing old refresh token must be rejected
        old_ref_reuse = await http_client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_tok,
            },
        )
        assert old_ref_reuse.status_code == 400
        assert old_ref_reuse.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_login_rejects_non_pre_role(dummy_settings, tmp_store):
    """Verify accounts without the PRE role cannot complete OAuth login."""
    tmp_store.save_auth_session(
        session_id="sess_analyst",
        client_id="cid",
        redirect_uri="https://claude.ai/callback",
        state="st",
        code_challenge="chal",
        scopes=["prospects:entry"],
        resource=None,
    )
    handler = create_login_handler(tmp_store, dummy_settings)

    with respx.mock(base_url=dummy_settings.api_base_url) as respx_mock:
        respx_mock.post("/auth/login/").respond(200, json={"access": "acc", "refresh": "ref"})
        respx_mock.get("/auth/me/").respond(200, json={"user": {"id": "u2", "username": "analyst_bob", "role": "ANALYST"}})

        scope_post = {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "query_string": b"",
        }
        async def receive():
            return {"type": "http.request", "body": b"session_id=sess_analyst&username=analyst_bob&password=pass"}

        req = Request(scope_post, receive=receive)
        resp = await handler(req)
        assert resp.status_code == 200  # Re-renders login with error
        body_text = resp.body.decode()
        assert "Only accounts with the PRE role are permitted" in body_text


@pytest.mark.asyncio
async def test_cors_preflight_on_mcp_and_token(dummy_settings, tmp_store):
    """Verify CORS preflight OPTIONS requests return 200 OK with allowed origins and headers."""
    from confirmation_store import ConfirmationStore
    from workflow import Workflow
    from api_client import MarketIntelClient
    from server import create_mcp
    from starlette.middleware.cors import CORSMiddleware

    client_mock = MarketIntelClient(dummy_settings)
    wf = Workflow(client_mock, ConfirmationStore(300), dummy_settings)
    mcp = create_mcp(lambda: wf)
    app = mcp.streamable_http_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
        expose_headers=["*"],
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http_client:
        res = await http_client.options(
            "/mcp",
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization,Content-Type,mcp-protocol-version",
            },
        )
        assert res.status_code == 200
        assert res.headers.get("access-control-allow-origin") == "https://claude.ai" or res.headers.get("access-control-allow-origin") == "*"

