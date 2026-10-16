import inspect
import json
import logging

import httpx
import pytest
import respx

import api_client
from api_client import (ALLOWED_ENDPOINTS, ApiTimeout, AuthError, EndpointNotAllowed, MarketIntelClient)

BASE = "https://api.test/api"


def mk(settings):
    return MarketIntelClient(settings)


def mock_auth(role="PRE", me_status=200):
    respx.post(f"{BASE}/auth/login/").mock(return_value=httpx.Response(200, json={"access": "tok-123"}))
    respx.get(f"{BASE}/auth/me/").mock(return_value=httpx.Response(
        me_status, json={"id": 7, "username": "pre_user", "role": role}))


def test_allowlist_is_exactly_the_approved_set():
    assert ALLOWED_ENDPOINTS == {
        ("POST", "/auth/login/"), ("POST", "/auth/refresh/"), ("GET", "/auth/me/"),
        ("POST", "/email-verifications/instant-verify/"), ("POST", "/email-verifications/verify/"),
        ("GET", "/prospects/"), ("POST", "/prospects/"),
        ("GET", "/government-entities/"), ("POST", "/government-entities/"),
        ("GET", "/market-events/"), ("POST", "/market-events/"),
    }
    assert not {m for m, _ in ALLOWED_ENDPOINTS} & {"PUT", "PATCH", "DELETE"}


@respx.mock
@pytest.mark.parametrize("method,path", [
    ("DELETE", "/prospects/"), ("PATCH", "/prospects/"), ("PUT", "/prospects/"),
    ("DELETE", "/prospects/123/"), ("GET", "/prospects/123/"), ("GET", "/users/"),
    ("DELETE", "/prospects/11111111-1111-1111-1111-111111111111/"),
    ("DELETE", "/government-entities/11111111-1111-1111-1111-111111111111/"),
    ("DELETE", "/market-events/11111111-1111-1111-1111-111111111111/"),
    ("PUT", "/prospects/11111111-1111-1111-1111-111111111111/"),
    ("POST", "/prospects/../users/"), ("GET", "https://evil.example/x"), ("POST", "/auth/me/"),
    ("GET", "/email-verifications/verify/"),
])
async def test_rejects_arbitrary_endpoints_and_methods(settings, method, path):
    c = mk(settings)
    c._token = "t"
    with pytest.raises(EndpointNotAllowed):
        await c._send(method, path)
    assert len(respx.calls) == 0  # nothing ever left the process


def test_no_public_method_exposes_raw_requests():
    public = [n for n, _ in inspect.getmembers(MarketIntelClient, inspect.isfunction) if not n.startswith("_")]
    assert set(public) == {
        "aclose", "authenticate", "list_prospects", "get_prospect", "create_prospect", "update_prospect",
        "list_government_entities", "get_government_entity", "create_government_entity", "update_government_entity",
        "list_market_events", "get_market_event", "create_market_event", "update_market_event",
        "get_record", "update_record", "instant_verify", "persist_verification"
    }


@respx.mock
async def test_pre_authenticates_and_sends_bearer(settings):
    mock_auth()
    route = respx.get(f"{BASE}/prospects/").mock(return_value=httpx.Response(200, json={"results": []}))
    c = mk(settings)
    ident = await c.authenticate()
    assert ident.role == "PRE" and ident.key == "7:pre_user"
    await c.list_prospects({"search": "x"})
    assert route.calls[0].request.headers["authorization"] == "Bearer tok-123"


@respx.mock
@pytest.mark.parametrize("role", ["LQ", "MANAGER", "", "ADMIN"])
async def test_non_pre_rejected(settings, role):
    mock_auth(role)
    with pytest.raises(AuthError, match="only PRE"):
        await mk(settings).authenticate()


@respx.mock
async def test_bad_credentials_rejected(settings):
    respx.post(f"{BASE}/auth/login/").mock(return_value=httpx.Response(401, json={"detail": "bad"}))
    with pytest.raises(AuthError):
        await mk(settings).authenticate()


@respx.mock
async def test_unauthenticated_request_is_rejected_after_failed_refresh_and_login(settings):
    respx.post(f"{BASE}/auth/login/").mock(return_value=httpx.Response(200, json={"access": "t"}))
    respx.post(f"{BASE}/auth/refresh/").mock(return_value=httpx.Response(401, json={}))
    respx.get(f"{BASE}/prospects/").mock(return_value=httpx.Response(401, json={"detail": "nope"}))
    with pytest.raises(AuthError):
        await mk(settings).list_prospects({})


@respx.mock
async def test_expired_token_refreshes_and_retries_once(settings):
    mock_auth()
    respx.post(f"{BASE}/auth/refresh/").mock(return_value=httpx.Response(200, json={"access": "tok-new"}))
    route = respx.get(f"{BASE}/prospects/").mock(side_effect=[
        httpx.Response(401, json={}), httpx.Response(200, json={"results": []})])
    c = mk(settings)
    await c.authenticate()
    await c.list_prospects({})
    assert route.call_count == 2 and route.calls[1].request.headers["authorization"] == "Bearer tok-new"


@respx.mock
async def test_create_never_retried_on_timeout(settings):
    mock_auth()
    route = respx.post(f"{BASE}/prospects/").mock(side_effect=httpx.ReadTimeout("t"))
    c = mk(settings)
    await c.authenticate()
    with pytest.raises(ApiTimeout):
        await c.create_prospect({"company_name": "x"})
    assert route.call_count == 1


@respx.mock
async def test_get_retried_once_on_network_error(settings):
    mock_auth()
    route = respx.get(f"{BASE}/prospects/").mock(side_effect=[httpx.ConnectError("x"), httpx.Response(200, json=[])])
    c = mk(settings)
    await c.authenticate()
    await c.list_prospects({})
    assert route.call_count == 2


@respx.mock
async def test_only_post_used_for_creation_and_no_destructive_verbs(settings):
    mock_auth()
    respx.post(f"{BASE}/prospects/").mock(return_value=httpx.Response(201, json={"id": "p1"}))
    respx.post(f"{BASE}/email-verifications/verify/").mock(return_value=httpx.Response(200, json={"status": "VALID"}))
    c = mk(settings)
    await c.authenticate()
    await c.create_prospect({"company_name": "x"})
    await c.persist_verification("p1", "a@b.co", "c1")
    assert {call.request.method for call in respx.calls} <= {"GET", "POST"}
    persist_call = [call for call in respx.calls if call.request.url.path.endswith("/email-verifications/verify/")][0]
    import json
    assert json.loads(persist_call.request.content) == {
        "prospect_id": "p1", "email_address": "a@b.co", "prospect_contact_id": "c1"
    }


@respx.mock
async def test_api_error_formatting_and_size_limit(settings, monkeypatch):
    mock_auth()
    respx.post(f"{BASE}/prospects/").mock(return_value=httpx.Response(400, json={"company_name": ["already exists"]}))
    c = mk(settings)
    await c.authenticate()
    with pytest.raises(api_client.ApiError, match="company_name: already exists"):
        await c.create_prospect({})
    monkeypatch.setattr(api_client, "MAX_RESPONSE_BYTES", 5)
    respx.get(f"{BASE}/prospects/").mock(return_value=httpx.Response(200, json={"results": ["x" * 50]}))
    with pytest.raises(api_client.ApiError, match="size limit"):
        await c.list_prospects({})


@respx.mock
async def test_credentials_and_tokens_never_logged_or_in_errors(settings, caplog):
    caplog.set_level(logging.DEBUG)
    mock_auth()
    respx.post(f"{BASE}/prospects/").mock(return_value=httpx.Response(500, text="boom"))
    c = mk(settings)
    await c.authenticate()
    with pytest.raises(api_client.ApiError) as ei:
        await c.create_prospect({})
    blob = caplog.text + str(ei.value) + repr(settings)
    assert "secret-pw" not in blob and "tok-123" not in blob


def test_settings_repr_hides_password(settings):
    assert "secret-pw" not in repr(settings)
