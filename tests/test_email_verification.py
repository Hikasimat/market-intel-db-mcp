import httpx
import respx

from api_client import MarketIntelClient
from conftest import valid_prospect

BASE = "https://api.test/api"


async def test_all_emails_valid_and_each_verified(wf, client):
    r = await wf.validate(valid_prospect())
    assert r["valid"]
    assert {e["email"] for e in r["email_verification"]} == {"info@abc.example", "arun@abc.example"}
    assert client.count("verify") == 2


async def test_company_email_invalid_blocks(wf, client):
    client.email_status["info@abc.example"] = "INVALID"
    r = await wf.validate(valid_prospect())
    assert not r["valid"] and "confirmation_token" not in r
    assert r["errors"][0].startswith("Company email verification failed: INVALID. Reason: mock reason")


async def test_contact_email_invalid_blocks_with_field_message(wf, client):
    client.email_status["arun@abc.example"] = "UNKNOWN"
    r = await wf.validate(valid_prospect())
    assert not r["valid"]
    assert 'Key contact "Arun Kumar": email status UNKNOWN' in r["errors"][0]


async def test_one_of_several_contacts_invalid(wf, client):
    raw = valid_prospect()
    raw["key_contacts"].append({"contact_name": "Bela Rao", "designation": "CTO", "official_email": "bela@abc.example"})
    client.email_status["bela@abc.example"] = "INVALID"
    r = await wf.validate(raw)
    assert not r["valid"] and len(r["errors"]) == 1 and "Bela Rao" in r["errors"][0]


async def test_verification_error_blocks(wf, client):
    client.email_exc["info@abc.example"] = RuntimeError("timeout")
    r = await wf.validate(valid_prospect())
    assert not r["valid"] and "could not be completed" in r["errors"][0] and "No prospect has been created" in r["errors"][0]


async def test_optional_emails_omitted_is_fine(wf, client):
    raw = valid_prospect(); raw.pop("official_email_address"); raw["key_contacts"][0].pop("official_email")
    r = await wf.validate(raw)
    assert r["valid"] and client.count("verify") == 0


async def test_email_whitespace_trimmed_before_verification(wf, client):
    r = await wf.validate(valid_prospect(official_email_address="  info@abc.example "))
    assert r["valid"] and ("verify", "info@abc.example") in client.calls


async def test_status_changes_between_validation_and_creation(wf, client):
    token = (await wf.validate(valid_prospect()))["confirmation_token"]
    client.email_status["arun@abc.example"] = "INVALID"
    r = await wf.create(token, True)
    assert not r["created"] and client.count("create") == 0
    assert "re-verification failed" in r["errors"][0]


async def test_post_creation_persistence_failure_keeps_prospect(wf, client):
    from api_client import ApiError
    token = (await wf.validate(valid_prospect()))["confirmation_token"]
    client.persist_exc = ApiError("boom", 500)
    r = await wf.create(token, True)
    assert r["created"] and r["prospect_id"]
    assert any("NOT been deleted" in w for w in r["warnings"])
    assert all(e["persisted"] == "NOT_PERSISTED" for e in r["email_verification"])


async def test_post_creation_discrepancy_reported(wf, client):
    token = (await wf.validate(valid_prospect()))["confirmation_token"]
    client.persist_status = "INVALID"
    r = await wf.create(token, True)
    assert r["created"] and any("persisted verification is INVALID" in w for w in r["warnings"])


async def test_persist_uses_ids_from_response(wf, client):
    token = (await wf.validate(valid_prospect()))["confirmation_token"]
    r = await wf.create(token, True)
    persist = [c for c in client.calls if c[0] == "persist"]
    assert (persist[0][1], persist[0][2], persist[0][3]) == (r["prospect_id"], "info@abc.example", None)
    assert persist[1][3] == r["key_contacts"][0]["id"] == "c0"


# ---- HTTP-level instant-verify behaviour (respx-mocked transport) ----
def _client(settings):
    c = MarketIntelClient(settings)
    c._token = "tok"
    return c


@respx.mock
async def test_instant_verify_valid_and_request_shape(settings):
    route = respx.post(f"{BASE}/email-verifications/instant-verify/").mock(
        return_value=httpx.Response(200, json={"verification_status": "VALID", "reason": "MX ok"}))
    res = await _client(settings).instant_verify("a@b.co")
    assert res.ok and res.reason == "MX ok"
    import json
    assert json.loads(route.calls[0].request.content) == {"email_address": "a@b.co"}


@respx.mock
async def test_instant_verify_timeout_is_not_valid(settings):
    respx.post(f"{BASE}/email-verifications/instant-verify/").mock(side_effect=httpx.ReadTimeout("t"))
    res = await _client(settings).instant_verify("a@b.co")
    assert res.status == "ERROR" and not res.ok


@respx.mock
async def test_instant_verify_malformed_or_missing_status(settings):
    url = f"{BASE}/email-verifications/instant-verify/"
    for resp in (httpx.Response(200, text="<html>"), httpx.Response(200, json={"foo": 1}),
                 httpx.Response(200, json={"status": "MAYBE"}), httpx.Response(500, json={"detail": "x"})):
        respx.post(url).mock(return_value=resp)
        assert (await _client(settings).instant_verify("a@b.co")).status == "ERROR"
