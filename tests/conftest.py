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


def valid_government_entity(**over: Any) -> dict[str, Any]:
    base = {
        "name": "Department of Commerce",
        "country": "India",
        "is_state_government": False,
        "official_website": "https://commerce.gov.in",
        "official_email": "contact@commerce.gov.in",
    }
    base.update(over)
    return base


def valid_market_event(**over: Any) -> dict[str, Any]:
    base = {
        "event_title": "Global Tech Summit 2026",
        "host_country": "India",
        "start_date": "2026-11-10",
        "end_date": "2026-11-12",
        "website_url": "https://techsummit.example.com",
    }
    base.update(over)
    return base


class FakeClient:
    def __init__(self):
        self.identity: Identity | None = None
        self.role = "PRE"
        self.visible: list[dict] = []
        self.visible_gov_entities: list[dict] = []
        self.visible_market_events: list[dict] = []
        self.email_status: dict[str, str] = {}          # email -> status (default VALID)
        self.email_exc: dict[str, Exception] = {}
        self.create_exc: Exception | None = None
        self.update_exc: Exception | None = None
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
            recs = [r for r in recs if s in r.get("company_name", "").casefold()]
        return {"results": recs, "next": None}

    async def get_prospect(self, prospect_id: str):
        self.calls.append(("get_prospect", prospect_id))
        for r in self.visible:
            if str(r.get("id")) == str(prospect_id):
                return copy.deepcopy(r)
        raise ApiError(f"Prospect {prospect_id} not found", status=404)

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
        new_id = str(uuid.uuid4())
        rec = {
            "id": new_id,
            **copy.deepcopy(payload),
            "key_contacts": [{"id": f"c{i}", "contact_name": c["contact_name"],
                              "official_email": c.get("official_email")}
                             for i, c in enumerate(payload.get("key_contacts", []))]
        }
        self.visible.append(rec)
        return rec

    async def update_prospect(self, prospect_id: str, payload: dict):
        self.calls.append(("update_prospect", prospect_id, copy.deepcopy(payload)))
        if self.update_exc:
            raise self.update_exc
        for r in self.visible:
            if str(r.get("id")) == str(prospect_id):
                r.update(copy.deepcopy(payload))
                return copy.deepcopy(r)
        raise ApiError(f"Prospect {prospect_id} not found", status=404)

    async def list_government_entities(self, params):
        self.calls.append(("list_gov", dict(params)))
        recs = self.visible_gov_entities
        if params.get("search"):
            s = params["search"].casefold()
            recs = [r for r in recs if s in r.get("name", "").casefold() or any(w in r.get("name", "").casefold() for w in s.split() if len(w) > 3)]
        return {"results": recs, "next": None}

    async def get_government_entity(self, entity_id: str):
        self.calls.append(("get_gov", entity_id))
        for r in self.visible_gov_entities:
            if str(r.get("id")) == str(entity_id):
                return copy.deepcopy(r)
        raise ApiError(f"Government entity {entity_id} not found", status=404)

    async def create_government_entity(self, payload: dict):
        self.calls.append(("create_gov", copy.deepcopy(payload)))
        if self.create_exc:
            raise self.create_exc
        rec = {"id": str(uuid.uuid4()), **copy.deepcopy(payload)}
        self.visible_gov_entities.append(rec)
        return rec

    async def update_government_entity(self, entity_id: str, payload: dict):
        self.calls.append(("update_gov", entity_id, copy.deepcopy(payload)))
        if self.update_exc:
            raise self.update_exc
        for r in self.visible_gov_entities:
            if str(r.get("id")) == str(entity_id):
                r.update(copy.deepcopy(payload))
                return copy.deepcopy(r)
        raise ApiError(f"Government entity {entity_id} not found", status=404)

    async def list_market_events(self, params):
        self.calls.append(("list_event", dict(params)))
        recs = self.visible_market_events
        if params.get("search"):
            s = params["search"].casefold()
            recs = [r for r in recs if s in r.get("event_title", "").casefold() or any(w in r.get("event_title", "").casefold() for w in s.split() if len(w) > 3)]
        return {"results": recs, "next": None}

    async def get_market_event(self, event_id: str):
        self.calls.append(("get_event", event_id))
        for r in self.visible_market_events:
            if str(r.get("id")) == str(event_id):
                return copy.deepcopy(r)
        raise ApiError(f"Market event {event_id} not found", status=404)

    async def create_market_event(self, payload: dict):
        self.calls.append(("create_event", copy.deepcopy(payload)))
        if self.create_exc:
            raise self.create_exc
        rec = {"id": str(uuid.uuid4()), **copy.deepcopy(payload)}
        self.visible_market_events.append(rec)
        return rec

    async def update_market_event(self, event_id: str, payload: dict):
        self.calls.append(("update_event", event_id, copy.deepcopy(payload)))
        if self.update_exc:
            raise self.update_exc
        for r in self.visible_market_events:
            if str(r.get("id")) == str(event_id):
                r.update(copy.deepcopy(payload))
                return copy.deepcopy(r)
        raise ApiError(f"Market event {event_id} not found", status=404)

    async def get_record(self, module: str, record_id: str):
        if module == "prospect":
            return await self.get_prospect(record_id)
        elif module == "government_entity":
            return await self.get_government_entity(record_id)
        elif module == "market_event":
            return await self.get_market_event(record_id)
        raise ApiError(f"Unsupported module {module}", status=400)

    async def update_record(self, module: str, record_id: str, payload: dict):
        if module == "prospect":
            return await self.update_prospect(record_id, payload)
        elif module == "government_entity":
            return await self.update_government_entity(record_id, payload)
        elif module == "market_event":
            return await self.update_market_event(record_id, payload)
        raise ApiError(f"Unsupported module {module}", status=400)

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
