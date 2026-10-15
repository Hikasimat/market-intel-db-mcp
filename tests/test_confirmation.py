import asyncio

import pytest

from api_client import ApiError, ApiTimeout
from confirmation_store import ConfirmationError, ConfirmationStore
from conftest import valid_prospect


# ---------------- store unit tests ----------------
def test_token_single_use(store):
    tok, _ = store.issue("1:u", {"a": 1})
    assert store.consume(tok, "1:u").payload == {"a": 1}
    with pytest.raises(ConfirmationError, match="already used"):
        store.consume(tok, "1:u")


def test_missing_unknown_and_wrong_identity(store):
    for bad in ("", None, "nope"):
        with pytest.raises(ConfirmationError):
            store.consume(bad, "1:u")
    tok, _ = store.issue("1:u", {})
    with pytest.raises(ConfirmationError, match="different"):
        store.consume(tok, "2:other")


def test_expiry(store, clock):
    tok, _ = store.issue("1:u", {})
    clock.t += 601
    with pytest.raises(ConfirmationError, match="expired"):
        store.consume(tok, "1:u")


def test_tamper_detected(store):
    tok, _ = store.issue("1:u", {"company_name": "A"})
    next(iter(store._ops.values())).payload_json = '{"company_name":"Evil"}'
    with pytest.raises(ConfirmationError, match="integrity"):
        store.consume(tok, "1:u")


async def test_concurrent_consume_only_one_wins():
    s = ConfirmationStore(600)
    tok, _ = s.issue("1:u", {})
    outcomes = []
    def attempt():
        try: s.consume(tok, "1:u"); outcomes.append("ok")
        except ConfirmationError: outcomes.append("no")
    await asyncio.gather(*(asyncio.to_thread(attempt) for _ in range(20)))
    assert outcomes.count("ok") == 1


# ---------------- workflow creation behaviour ----------------
async def test_validation_never_creates(wf, client):
    await wf.validate(valid_prospect())
    assert client.count("create") == 0


async def test_missing_token_blocks(wf, client):
    r = await wf.create("", True)
    assert not r["created"] and client.count("create") == 0


async def test_explicit_confirmation_required_and_token_preserved(wf, client):
    tok = (await wf.validate(valid_prospect()))["confirmation_token"]
    r = await wf.create(tok, False)
    assert not r["created"] and "Explicit user confirmation" in r["errors"][0]
    assert (await wf.create(tok, True))["created"]  # token was NOT burned by the refusal
    assert client.count("create") == 1


async def test_expired_token_blocks(wf, client, clock):
    tok = (await wf.validate(valid_prospect()))["confirmation_token"]
    clock.t += 700
    r = await wf.create(tok, True)
    assert not r["created"] and "expired" in r["errors"][0] and client.count("create") == 0


async def test_reused_token_creates_only_once(wf, client):
    tok = (await wf.validate(valid_prospect()))["confirmation_token"]
    first, second = await asyncio.gather(wf.create(tok, True), wf.create(tok, True))
    assert [first["created"], second["created"]].count(True) == 1
    assert client.count("create") == 1


async def test_identical_payload_not_created_twice_via_new_token(wf, client):
    t1 = (await wf.validate(valid_prospect()))["confirmation_token"]
    t2 = (await wf.validate(valid_prospect()))["confirmation_token"]
    assert (await wf.create(t1, True))["created"]
    r = await wf.create(t2, True)
    assert not r["created"] and client.count("create") == 1


async def test_tampered_payload_rejected(wf, client, store):
    tok = (await wf.validate(valid_prospect()))["confirmation_token"]
    next(iter(store._ops.values())).payload_json = '{"company_name":"Evil Corp"}'
    r = await wf.create(tok, True)
    assert not r["created"] and client.count("create") == 0


async def test_success_returns_actual_ids_and_exact_payload(wf, client):
    v = await wf.validate(valid_prospect())
    r = await wf.create(v["confirmation_token"], True)
    assert r["created"] and r["company_name"] == "ABC Technologies"
    assert r["key_contacts"] == [{"id": "c0", "name": "Arun Kumar"}]
    sent = [c for c in client.calls if c[0] == "create"][0][1]
    assert sent == v["payload_to_be_submitted"]


async def test_backend_duplicate_rejection_reported_no_retry(wf, client):
    tok = (await wf.validate(valid_prospect()))["confirmation_token"]
    client.create_exc = ApiError("company_name: prospect with this name already exists.", 400)
    r = await wf.create(tok, True)
    assert not r["created"] and "already exists" in r["errors"][0]
    assert client.count("create") == 1
    assert not (await wf.create(tok, True))["created"] and client.count("create") == 1


async def test_duplicate_found_again_before_creation(wf, client):
    tok = (await wf.validate(valid_prospect()))["confirmation_token"]
    client.visible = [{"id": "z", "company_name": "ABC Technologies"}]
    r = await wf.create(tok, True)
    assert not r["created"] and client.count("create") == 0


async def test_timeout_is_uncertain_and_never_retried(wf, client):
    tok = (await wf.validate(valid_prospect()))["confirmation_token"]
    client.create_exc = ApiTimeout("timed out")
    r = await wf.create(tok, True)
    assert r["outcome"] == "UNCERTAIN" and "Do NOT retry" in r["errors"][0]
    assert client.count("create") == 1
    client.create_exc = None
    t2 = (await wf.validate(valid_prospect()))["confirmation_token"]
    assert not (await wf.create(t2, True))["created"]  # blind identical retry blocked
    assert client.count("create") == 1


async def test_non_pre_cannot_create(wf, client):
    tok = (await wf.validate(valid_prospect()))["confirmation_token"]
    client.identity = None; client.role = "MANAGER"
    r = await wf.create(tok, True)
    assert not r["created"] and client.count("create") == 0
