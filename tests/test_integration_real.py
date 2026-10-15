"""REAL integration tests. Skipped unless MI_INTEGRATION=1 and a *test* backend is configured in .env.
They are NOT mocked. Never point them at production.

  MI_INTEGRATION=1                 -> auth + role + instant-verify (read-only/no records)
  MI_TEST_EMAIL=someone@domain     -> email used for the instant-verify check
  MI_TEST_ALLOW_CREATE=1           -> additionally creates ONE clearly named test prospect
"""
import os
import time
import uuid

import pytest

from api_client import MarketIntelClient
from config import load_settings
from confirmation_store import ConfirmationStore
from workflow import Workflow

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("MI_INTEGRATION") != "1", reason="set MI_INTEGRATION=1 (test backend only)")]


@pytest.fixture
async def real():
    s = load_settings()
    c = MarketIntelClient(s)
    yield c, Workflow(c, ConfirmationStore(s.confirmation_ttl_seconds), s)
    await c.aclose()


async def test_real_auth_and_role(real):
    c, _ = real
    assert (await c.authenticate()).role == "PRE"


async def test_real_instant_verify_shape(real):
    c, _ = real
    await c.authenticate()
    res = await c.instant_verify(os.environ.get("MI_TEST_EMAIL", "test@example.com"))
    assert res.status in {"VALID", "INVALID", "UNKNOWN"}, res  # ERROR => response shape differs from assumption


@pytest.mark.skipif(os.environ.get("MI_TEST_ALLOW_CREATE") != "1", reason="set MI_TEST_ALLOW_CREATE=1 on a test backend")
async def test_real_create_prospect_and_persist_verification(real):
    _, wf = real
    name = f"MCP Integration Test {uuid.uuid4().hex[:8]}"
    email = os.environ["MI_TEST_EMAIL"]
    v = await wf.validate({
        "company_name": name, "country_head_office": "India", "complete_address": "Test address",
        "primary_industries": "Testing", "ownership_sector": "Private Company",
        "company_structure": "Parent", "operational_status": "Active", "primary_offering_type": "Services",
        "official_email_address": email,
        "key_contacts": [{"contact_name": "Test Contact", "designation": "QA", "official_email": email}]})
    assert v["valid"], v["errors"]
    r = await wf.create(v["confirmation_token"], True)
    assert r["created"] and r["prospect_id"] and r["key_contacts"][0]["id"], r
