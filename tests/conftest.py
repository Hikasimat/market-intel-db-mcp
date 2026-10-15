"""Shared fixtures: a fake API client (no network) and sample payloads. ALL tests here are MOCKED."""
from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest

from api_client import ApiError, ApiTimeout, EmailResult, Identity
from config import Settings
from confirmation_store import ConfirmationStore
from workflow import Workflow

PARENT_ID = str(uuid.uuid4())


def valid_prospect(**over: Any) -> dict[str, Any]:
    base = {
        "company_name": "ABC Technologies",
        "country_head_office": "India",
        "complete_address": "12 MG Road, Bengaluru",
        "primary_industries": "Information Technology",
        "ownership_sector": "Private Company",
        "company_structure": "Parent",
        "operational_status": "Active",
        "primary_offering_type": "Services",
        "official_email_address": "info@abc.example",
        "official_website_url": "https://abc.example",
        "offerings_data": [{"offering_type": "Service", "name": "Consulting"}],
        "key_contacts": [{
            "prefix": "Mr.", "contact_name": "Arun Kumar", "designation": "Operations Manager",
            "official_email": "arun@abc.example", "phone_number": "+91 9876543210",
            "linkedin_profile": "https://www.linkedin.com/in/example",
        }],
    }
    base.update(over)
    return base


class FakeClient:
    def __init__(self):
        self.identity: Identity | None = None
        self.role = "PRE"
        self.visible: list[dict] = []
        self.email_status: dict[str, str] = {}          # email -> status (default VALID)
        self.email_exc: dict[str, Exception] = {}
        self.create_exc: Exception | None = None
        self.persist_exc: Exception | None = None
        self.persist_status = "VALID"
        self.calls: list[tuple] = []

    async def authenticate(self) -> Identity:
        self.calls.append(("authenticate",))
        if self.role != "PRE":
            from api_client import AuthError
            raise AuthError(f"Account role is {self.role!r}; only PRE accounts are permitted.")
        self.identity = Identity("1", "pre_user", "PRE")
        return self.identity

    async def list_prospects(self, params):
        self.calls.append(("list", dict(params)))
        recs = self.visible
        if params.get("search"):
            s = params["search"].casefold()
            recs = [r for r in recs if s in r["company_name"].casefold()]
        return {"results": recs, "next": None}

    async def instant_verify(self, email):
        self.calls.append(("verify", email))
        if email in self.email_exc:
            exc = self.email_exc[email]
            return EmailResult(email, "ERROR", str(exc))
        st = self.email_status.get(email, "VALID")
        return EmailResult(email, st, "mock reason")

    async def create_prospect(self, payload):
        self.calls.append(("create", copy.deepcopy(payload)))
        if self.create_exc:
            raise self.create_exc
        return {"id": str(uuid.uuid4()), "company_name": payload["company_name"],
                "key_contacts": [{"id": f"c{i}", "contact_name": c["contact_name"],
                                  "official_email": c.get("official_email")}
                                 for i, c in enumerate(payload.get("key_contacts", []))]}

    async def persist_verification(self, prospect_id, email, contact_id=None):
        self.calls.append(("persist", prospect_id, email, contact_id))
        if self.persist_exc:
            raise self.persist_exc
        return {"status": self.persist_status}

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


@pytest.fixture
def clock(): return Clock()

@pytest.fixture
def client(): return FakeClient()

@pytest.fixture
def settings():
    return Settings(api_base_url="https://api.test/api", username="pre_user", password="secret-pw")

@pytest.fixture
def store(clock): return ConfirmationStore(600, clock)

@pytest.fixture
def wf(client, store, settings): return Workflow(client, store, settings)
