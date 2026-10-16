"""Search -> validate -> confirm -> create/update orchestration across Prospects, Government Entities, and Market Events."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol
from uuid import UUID

from api_client import ApiError, ApiTimeout, AuthError, EmailResult, Identity
from confirmation_store import ConfirmationError, ConfirmationStore
from config import Settings
from schemas import (
    GovernmentEntityInput,
    MarketEventInput,
    PROSPECT_BLUEPRINT,
    ProspectInput,
    parse_government_entity,
    parse_market_event,
    parse_prospect,
)
from validation import (
    EDITABLE_FIELDS_BY_MODULE,
    EmailTarget,
    business_rule_errors,
    collect_email_targets,
    compute_record_diff,
    find_duplicates,
    find_government_entity_duplicates,
    find_market_event_duplicates,
    referenced_company_ids,
    referenced_government_entity_ids,
    referenced_market_event_ids,
)

log = logging.getLogger("prospect_mcp.workflow")

MAX_SEARCH_LIMIT = 25
DEFAULT_SEARCH_LIMIT = 10
PAGE_SIZE = 100
NO_PROOF_NOTE = (
    "Search only covers records visible to the configured PRE account. No match is NOT proof "
    "that the record does not exist; the backend's duplicate checks decide at creation."
)


class ProspectApi(Protocol):
    identity: Identity | None
    async def authenticate(self) -> Identity: ...
    # Prospects
    async def list_prospects(self, params: dict[str, Any] | None = None) -> Any: ...
    async def get_prospect(self, prospect_id: str) -> Any: ...
    async def create_prospect(self, payload: dict[str, Any]) -> Any: ...
    async def update_prospect(self, prospect_id: str, payload: dict[str, Any]) -> Any: ...
    # Government Entities
    async def list_government_entities(self, params: dict[str, Any] | None = None) -> Any: ...
    async def get_government_entity(self, entity_id: str) -> Any: ...
    async def create_government_entity(self, payload: dict[str, Any]) -> Any: ...
    async def update_government_entity(self, entity_id: str, payload: dict[str, Any]) -> Any: ...
    # Market Events
    async def list_market_events(self, params: dict[str, Any] | None = None) -> Any: ...
    async def get_market_event(self, event_id: str) -> Any: ...
    async def create_market_event(self, payload: dict[str, Any]) -> Any: ...
    async def update_market_event(self, event_id: str, payload: dict[str, Any]) -> Any: ...
    # Generic record helpers
    async def get_record(self, module: str, record_id: str) -> Any: ...
    async def update_record(self, module: str, record_id: str, payload: dict[str, Any]) -> Any: ...
    # Verification
    async def instant_verify(self, email: str) -> EmailResult: ...
    async def persist_verification(self, prospect_id: str, email: str, contact_id: str | None = None) -> Any: ...


def _records(data: Any) -> list[dict[str, Any]]:
    """List endpoint may be paginated ({results: [...]}) or a bare list."""
    if isinstance(data, dict):
        data = data.get("results", [])
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _minimal(r: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": r.get("id"),
        "company_name": r.get("company_name"),
        "country_head_office": r.get("country_head_office"),
        "company_structure": r.get("company_structure"),
        "operational_status": r.get("operational_status"),
        "ownership_sector": r.get("ownership_sector"),
    }
    parents = r.get("parent_companies")
    if parents:
        out["parent_companies"] = parents
    return out


def _minimal_gov(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "country": r.get("country"),
        "is_state_government": r.get("is_state_government"),
        "state_name": r.get("state_name"),
        "central_government": r.get("central_government"),
        "official_website": r.get("official_website"),
    }


def _minimal_event(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r.get("id"),
        "event_title": r.get("event_title"),
        "host_country": r.get("host_country"),
        "start_date": r.get("start_date"),
        "end_date": r.get("end_date"),
        "website_url": r.get("website_url"),
    }


def _normalize_entity_type(raw: str) -> str | None:
    norm = (raw or "").strip().lower()
    mapping = {
        "prospect": "prospect",
        "prospects": "prospect",
        "government_entity": "government_entity",
        "government_entities": "government_entity",
        "government-entity": "government_entity",
        "government-entities": "government_entity",
        "market_event": "market_event",
        "market_events": "market_event",
        "market-event": "market_event",
        "market-events": "market_event",
    }
    return mapping.get(norm)


def email_failure_message(t: EmailTarget, res: EmailResult) -> str:
    if res.status == "ERROR":
        return (f"{t.label}: email verification could not be completed ({res.reason}). "
                "No prospect has been created.")
    if t.kind == "company":
        return f"Company email verification failed: {res.status}. Reason: {res.reason or 'none given'}"
    return (f"{t.label}: email status {res.status}. Reason: {res.reason or 'none given'}. "
            "Prospect creation is blocked until a valid email is provided.")


class Workflow:
    def __init__(self, client: ProspectApi, store: ConfirmationStore, settings: Settings):
        self.client, self.store, self.settings = client, store, settings

    async def _identity(self) -> Identity:
        if self.client.identity is None:
            await self.client.authenticate()
        assert self.client.identity is not None
        return self.client.identity

    # ------------------------------------------------------------- blueprint
    def get_prospect_blueprint(self) -> dict[str, Any]:
        """Return the complete, structured prospect blueprint."""
        return {"status": "success", "blueprint": PROSPECT_BLUEPRINT, **PROSPECT_BLUEPRINT}

    async def get_blueprint(self) -> dict[str, Any]:
        return self.get_prospect_blueprint()

    # ---------------------------------------------------------------- search
    async def search(self, query: str, country: str | None = None, limit: int = DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
        query = (query or "").strip()
        if not query or len(query) > 200:
            return {"ok": False, "errors": ["query must be 1-200 characters."]}
        try:
            limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_SEARCH_LIMIT
        try:
            await self._identity()
            data = await self.client.list_prospects({"search": query, "page_size": limit})
        except ApiError as exc:
            return {"ok": False, "errors": [f"Search failed: {exc}"]}
        recs = _records(data)
        if country and country.strip():
            c = country.strip().casefold()
            recs = [r for r in recs if str(r.get("country_head_office", "")).casefold() == c]
        matches = [_minimal(r) for r in recs[:limit]]
        return {"ok": True, "count": len(matches), "matches": matches, "note": NO_PROOF_NOTE}

    async def search_government_entities(
        self, query: str, country: str | None = None, is_state_government: bool | None = None, limit: int = DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query or len(query) > 200:
            return {"ok": False, "errors": ["query must be 1-200 characters."]}
        try:
            limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_SEARCH_LIMIT
        try:
            await self._identity()
            data = await self.client.list_government_entities({"search": query})
        except ApiError as exc:
            return {"ok": False, "errors": [f"Search failed: {exc}"]}
        recs = _records(data)
        if country and country.strip():
            c = country.strip().casefold()
            recs = [r for r in recs if str(r.get("country", "")).casefold() == c]
        if is_state_government is not None:
            recs = [r for r in recs if bool(r.get("is_state_government")) == is_state_government]
        matches = [_minimal_gov(r) for r in recs[:limit]]
        return {"ok": True, "count": len(matches), "matches": matches, "note": NO_PROOF_NOTE}

    async def search_market_events(
        self, query: str, host_country: str | None = None, limit: int = DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query or len(query) > 200:
            return {"ok": False, "errors": ["query must be 1-200 characters."]}
        try:
            limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_SEARCH_LIMIT
        try:
            await self._identity()
            data = await self.client.list_market_events({"search": query})
        except ApiError as exc:
            return {"ok": False, "errors": [f"Search failed: {exc}"]}
        recs = _records(data)
        if host_country and host_country.strip():
            c = host_country.strip().casefold()
            recs = [r for r in recs if str(r.get("host_country", "")).casefold() == c]
        matches = [_minimal_event(r) for r in recs[:limit]]
        return {"ok": True, "count": len(matches), "matches": matches, "note": NO_PROOF_NOTE}

    # -------------------------------------------------------- record details
    async def get_record_details(self, entity_type: str, record_id: str) -> dict[str, Any]:
        norm = _normalize_entity_type(entity_type)
        if not norm:
            return {"ok": False, "errors": [f"Unsupported entity_type: '{entity_type}'. Allowed: 'prospect', 'government_entity', 'market_event'."]}
        record_id = (record_id or "").strip()
        if not record_id:
            return {"ok": False, "errors": ["record_id is required."]}
        try:
            UUID(record_id)
        except ValueError:
            return {"ok": False, "errors": [f"Invalid UUID: '{record_id}'."]}
        try:
            await self._identity()
            data = await self.client.get_record(norm, record_id)
            return {"ok": True, "entity_type": norm, "record": data}
        except ApiError as exc:
            return {"ok": False, "errors": [f"Failed to retrieve {norm} {record_id}: {exc}"]}

    # ------------------------------------------------------ missing lookups
    async def _missing_company_ids(self, ids: set[str]) -> set[str]:
        """Paginate the visible list until every id is found (bounded). Returns ids not found."""
        remaining = set(ids)
        for page in range(1, self.settings.max_parent_lookup_pages + 1):
            if not remaining:
                break
            data = await self.client.list_prospects({"page": page, "page_size": PAGE_SIZE})
            recs = _records(data)
            remaining -= {str(r.get("id")) for r in recs}
            if not recs or not (isinstance(data, dict) and data.get("next")):
                break
        return remaining

    async def _missing_government_entity_ids(self, ids: set[str]) -> set[str]:
        missing = set()
        for gid in ids:
            try:
                res = await self.client.get_government_entity(gid)
                if not isinstance(res, dict) or not res.get("id"):
                    missing.add(gid)
            except ApiError:
                missing.add(gid)
        return missing

    async def _missing_market_event_ids(self, ids: set[str]) -> set[str]:
        missing = set()
        for eid in ids:
            try:
                res = await self.client.get_market_event(eid)
                if not isinstance(res, dict) or not res.get("id"):
                    missing.add(eid)
            except ApiError:
                missing.add(eid)
        return missing

    # ---------------------------------------------------- validate prospects
    async def validate(self, raw: Any) -> dict[str, Any]:
        warnings: list[str] = []
        prospect, errors = parse_prospect(raw)
        if prospect is not None:
            errors += business_rule_errors(prospect)
        if prospect is None:
            return self._rejected(errors, warnings)

        try:
            ident = await self._identity()
            dup_info = await self._duplicate_check(prospect, errors, warnings)
            ids = referenced_company_ids(prospect)
            if ids:
                for missing in sorted(await self._missing_company_ids(ids)):
                    errors.append(f"Referenced company {missing} was not found among prospects visible to this account. "
                                  "Ask the user for a valid existing company, or create that company separately first.")
            # Verify referenced Government Entities
            gov_ids = referenced_government_entity_ids(prospect)
            if gov_ids:
                for missing in sorted(await self._missing_government_entity_ids(gov_ids)):
                    errors.append(
                        f"Referenced Government Entity {missing} was not found among records visible to this account. "
                        "Search existing entities via search_government_entities or create it first."
                    )
            # Verify referenced Market Events
            event_ids = referenced_market_event_ids(prospect)
            if event_ids:
                for missing in sorted(await self._missing_market_event_ids(event_ids)):
                    errors.append(
                        f"Referenced Market Event {missing} was not found among records visible to this account. "
                        "Search existing events via search_market_events or create it first."
                    )
            targets = collect_email_targets(prospect)
            results = await asyncio.gather(*(self.client.instant_verify(t.email) for t in targets))
        except AuthError as exc:
            return self._rejected([f"Authentication/role check failed: {exc}"], warnings)
        except ApiError as exc:
            return self._rejected(errors + [f"Validation could not complete: {exc}. No prospect has been created."], warnings)

        email_report = []
        for t, res in zip(targets, results):
            email_report.append({"field": t.label, "email": t.email, "status": res.status, "reason": res.reason})
            if not res.ok:
                errors.append(email_failure_message(t, res))
        if errors:
            return self._rejected(errors, warnings, dup_info, email_report)

        payload = prospect.to_api_payload()
        token, ttl = self.store.issue(ident.key, payload, op_type="create", entity_type="prospect")
        return {
            "valid": True, "errors": [], "warnings": warnings, "duplicate_check": dup_info,
            "email_verification": email_report, "summary": preview_text(prospect),
            "payload_to_be_submitted": payload,
            "confirmation_token": token, "token_expires_in_seconds": ttl,
            "next_step": ("NO PROSPECT HAS BEEN CREATED. Show this summary to the user and ask for explicit approval. "
                          "Only after the user clearly approves, call create_confirmed_prospect with this token and "
                          "user_has_explicitly_confirmed=true."),
        }

    async def _duplicate_check(self, p: ProspectInput, errors: list[str], warnings: list[str]) -> dict[str, Any]:
        data = await self.client.list_prospects({"search": p.company_name, "page_size": MAX_SEARCH_LIMIT})
        exact, similar = find_duplicates(p.company_name, _records(data))
        if exact:
            errors.append(f"Duplicate: a visible prospect named '{p.company_name}' already exists "
                          f"(ID {exact[0].get('id')}). The backend would reject this creation.")
        for r in similar:
            warnings.append(f"Possible duplicate: '{r.get('company_name')}' (ID {r.get('id')}, "
                            f"{r.get('country_head_office')}). Confirm with the user it is a different company.")
        warnings.append(NO_PROOF_NOTE)
        return {"exact_matches": [_minimal(r) for r in exact], "similar_matches": [_minimal(r) for r in similar]}

    @staticmethod
    def _rejected(errors, warnings, dup=None, emails=None) -> dict[str, Any]:
        return {"valid": False, "errors": errors, "warnings": warnings, "duplicate_check": dup,
                "email_verification": emails,
                "next_step": "No confirmation token was issued and no record was created. "
                             "Ask the user to correct the listed problems, then validate again."}

    # ------------------------------------------------------ create prospects
    async def create(self, token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        if user_has_explicitly_confirmed is not True:
            return self._create_fail(["Explicit user confirmation is required (user_has_explicitly_confirmed=true). "
                                      "Show the summary to the user and ask. The token has not been used."])
        try:
            ident = await self._identity()
            op = self.store.consume(token, ident.key)
        except ConfirmationError as exc:
            return self._create_fail([str(exc)])
        except ApiError as exc:
            return self._create_fail([f"Authentication/role check failed: {exc}"])

        if op.op_type != "create" or op.entity_type != "prospect":
            return self._create_fail([f"Operation mismatch: confirmation token is for {op.op_type} of {op.entity_type}, not create of prospect."])

        prospect, errs = parse_prospect(op.payload)
        if prospect is None:
            return self._create_fail(["Staged payload could not be re-validated: " + "; ".join(errs)])
        try:
            dup_errors: list[str] = []
            await self._duplicate_check(prospect, dup_errors, [])
            if dup_errors:
                return self._create_fail(dup_errors + ["Token consumed; nothing was created."])
            targets = collect_email_targets(prospect)
            results = await asyncio.gather(*(self.client.instant_verify(t.email) for t in targets))
        except ApiError as exc:
            return self._create_fail([f"Pre-creation checks failed: {exc}. Nothing was created."])
        bad = [email_failure_message(t, r) for t, r in zip(targets, results) if not r.ok]
        if bad:
            return self._create_fail(["Email re-verification failed immediately before creation:"] + bad
                                     + ["Nothing was created. Token consumed; re-run validate_prospect_data."])

        # Single, non-retried creation attempt.
        try:
            created = await self.client.create_prospect(op.payload)
        except ApiError as exc:
            if exc.status is None:
                self.store.mark(op, "UNCERTAIN")
                return {"created": False, "outcome": "UNCERTAIN", "errors": [
                    f"{exc} The prospect MAY OR MAY NOT have been created. Do NOT retry creation. "
                    f"Call search_existing_prospects for '{prospect.company_name}' to check first."]}
            return self._create_fail([f"The Market Intel API rejected the creation (HTTP {exc.status}): {exc}",
                                      "Nothing was created. The existing API error is authoritative; do not retry "
                                      "with altered spelling."])
        pid = created.get("id") if isinstance(created, dict) else None
        if not pid:
            self.store.mark(op, "UNCERTAIN")
            return {"created": False, "outcome": "UNCERTAIN", "errors": [
                "The API returned success but no prospect ID. Search for the company before doing anything else."]}
        self.store.mark(op, "CREATED")
        return await self._after_create(prospect, created, str(pid), targets, results)

    async def _after_create(self, p: ProspectInput, created: dict, pid: str,
                            targets: list[EmailTarget], pre: list[EmailResult]) -> dict[str, Any]:
        contacts_resp = [c for c in (created.get("key_contacts") or []) if isinstance(c, dict)]
        contacts = []
        for i, c in enumerate(contacts_resp):
            contacts.append({"id": c.get("id"), "name": c.get("contact_name") or
                             (p.key_contacts[i].contact_name if i < len(p.key_contacts) else None)})
        warnings: list[str] = []
        if len(contacts_resp) != len(p.key_contacts):
            warnings.append("Number of returned key contacts differs from the number submitted; check the record.")

        report = []
        for t, pre_res in zip(targets, pre):
            cid = None
            if t.kind == "contact":
                cid = self._contact_id(t, contacts_resp)
                if cid is None:
                    warnings.append(f"{t.label}: no matching contact ID in the response; verification record not persisted.")
                    report.append({"field": t.label, "email": t.email, "preflight": pre_res.status,
                                   "persisted": "NOT_PERSISTED"})
                    continue
            try:
                data = await self.client.persist_verification(pid, t.email, cid)
                status = self._persisted_status(data)
            except ApiError as exc:
                status = "NOT_PERSISTED"
                warnings.append(f"{t.label}: post-creation verification could not be saved ({exc}). "
                                "The prospect was created and has NOT been deleted.")
            else:
                if status != pre_res.status:
                    warnings.append(f"{t.label}: persisted verification is {status} but preflight was "
                                    f"{pre_res.status}. The prospect was kept; the backend workflow handles this status.")
            report.append({"field": t.label, "email": t.email, "preflight": pre_res.status, "persisted": status})
        return {"created": True, "id": pid, "prospect_id": pid, "company_name": created.get("company_name") or p.company_name,
                "key_contacts": contacts, "email_verification": report, "warnings": warnings}

    @staticmethod
    def _contact_id(t: EmailTarget, resp: list[dict]) -> str | None:
        for c in resp:
            if str(c.get("official_email") or "").strip().casefold() == t.email.casefold() and c.get("id"):
                return str(c["id"])
        if t.contact_index is not None and t.contact_index < len(resp) and resp[t.contact_index].get("id"):
            return str(resp[t.contact_index]["id"])
        return None

    @staticmethod
    def _persisted_status(data: Any) -> str:
        if isinstance(data, dict):
            for k in ("status", "verification_status"):
                if isinstance(data.get(k), str):
                    return data[k].strip().upper()
            inner = data.get("verification")
            if isinstance(inner, dict) and isinstance(inner.get("status"), str):
                return inner["status"].strip().upper()
        return "UNKNOWN_RESPONSE"

    @staticmethod
    def _create_fail(errors: list[str]) -> dict[str, Any]:
        return {"created": False, "outcome": "NOT_CREATED", "errors": errors}

    # --------------------------------------------- validate government entities
    async def validate_government_entity(self, raw: Any) -> dict[str, Any]:
        warnings: list[str] = []
        entity, errors = parse_government_entity(raw)
        if entity is None:
            return self._rejected(errors, warnings)

        try:
            ident = await self._identity()
            # Duplicate check
            data = await self.client.list_government_entities({"search": entity.name})
            exact, similar = find_government_entity_duplicates(entity.name, entity.country, _records(data))
            if exact:
                errors.append(f"Duplicate: a visible government entity named '{entity.name}' in '{entity.country}' "
                              f"already exists (ID {exact[0].get('id')}). Use the existing record instead of creating a duplicate.")
            for r in similar:
                warnings.append(f"Possible duplicate: '{r.get('name')}' in '{r.get('country')}' (ID {r.get('id')}). "
                                "Confirm with the user that this is a distinct entity.")
            warnings.append(NO_PROOF_NOTE)
            # Check central government if referenced
            if entity.central_government:
                try:
                    c_gov = await self.client.get_government_entity(str(entity.central_government))
                    if not isinstance(c_gov, dict) or not c_gov.get("id"):
                        errors.append(f"Referenced central_government {entity.central_government} was not found.")
                    elif c_gov.get("is_state_government"):
                        errors.append(f"Referenced central_government {entity.central_government} is marked as a State Government.")
                except ApiError as exc:
                    errors.append(f"Could not verify central_government {entity.central_government}: {exc}")
        except AuthError as exc:
            return self._rejected([f"Authentication/role check failed: {exc}"], warnings)
        except ApiError as exc:
            return self._rejected(errors + [f"Validation failed: {exc}"], warnings)

        dup_info = {"exact_matches": [_minimal_gov(r) for r in exact], "similar_matches": [_minimal_gov(r) for r in similar]}
        if errors:
            return self._rejected(errors, warnings, dup_info)

        payload = entity.to_api_payload()
        token, ttl = self.store.issue(ident.key, payload, op_type="create", entity_type="government_entity")
        return {
            "valid": True,
            "errors": [],
            "warnings": warnings,
            "duplicate_check": dup_info,
            "summary": government_entity_preview_text(entity),
            "payload_to_be_submitted": payload,
            "confirmation_token": token,
            "token_expires_in_seconds": ttl,
            "next_step": (
                "NO GOVERNMENT ENTITY HAS BEEN CREATED. Show this summary to the user and obtain explicit approval. "
                "Only after the user clearly approves, call create_confirmed_government_entity with this token and "
                "user_has_explicitly_confirmed=true."
            ),
        }

    async def create_government_entity(self, token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        if user_has_explicitly_confirmed is not True:
            return self._create_fail(["Explicit user confirmation is required (user_has_explicitly_confirmed=true). "
                                      "Show the summary to the user and ask. The token has not been used."])
        try:
            ident = await self._identity()
            op = self.store.consume(token, ident.key)
        except ConfirmationError as exc:
            return self._create_fail([str(exc)])
        except ApiError as exc:
            return self._create_fail([f"Authentication/role check failed: {exc}"])

        if op.op_type != "create" or op.entity_type != "government_entity":
            return self._create_fail([f"Operation mismatch: confirmation token is for {op.op_type} of {op.entity_type}, not create of government_entity."])

        entity, errs = parse_government_entity(op.payload)
        if entity is None:
            return self._create_fail(["Staged payload could not be re-validated: " + "; ".join(errs)])

        try:
            created = await self.client.create_government_entity(op.payload)
        except ApiError as exc:
            if exc.status is None:
                self.store.mark(op, "UNCERTAIN")
                return {"created": False, "outcome": "UNCERTAIN", "errors": [
                    f"{exc} The entity MAY OR MAY NOT have been created. Search first before retrying."]}
            return self._create_fail([f"The Market Intel API rejected government entity creation (HTTP {exc.status}): {exc}"])

        gid = created.get("id") if isinstance(created, dict) else None
        if not gid:
            self.store.mark(op, "UNCERTAIN")
            return {"created": False, "outcome": "UNCERTAIN", "errors": ["API returned success but no record ID."]}
        self.store.mark(op, "CREATED")
        return {
            "created": True,
            "outcome": "CONFIRMED_SUCCESS",
            "id": str(gid),
            "government_entity_id": str(gid),
            "name": created.get("name") or entity.name,
            "country": created.get("country") or entity.country,
            "record": created,
        }

    # ---------------------------------------------------- validate market events
    async def validate_market_event(self, raw: Any) -> dict[str, Any]:
        warnings: list[str] = []
        event, errors = parse_market_event(raw)
        if event is None:
            return self._rejected(errors, warnings)

        try:
            ident = await self._identity()
            data = await self.client.list_market_events({"search": event.event_title})
            exact, similar = find_market_event_duplicates(event.event_title, _records(data))
            if exact:
                errors.append(f"Duplicate: a visible market event named '{event.event_title}' already exists "
                              f"(ID {exact[0].get('id')}). Link to the existing record instead of creating a duplicate.")
            for r in similar:
                warnings.append(f"Possible duplicate: '{r.get('event_title')}' in '{r.get('host_country')}' (ID {r.get('id')}). "
                                "Confirm with the user that this is a separate event.")
            warnings.append(NO_PROOF_NOTE)
        except AuthError as exc:
            return self._rejected([f"Authentication/role check failed: {exc}"], warnings)
        except ApiError as exc:
            return self._rejected(errors + [f"Validation failed: {exc}"], warnings)

        dup_info = {"exact_matches": [_minimal_event(r) for r in exact], "similar_matches": [_minimal_event(r) for r in similar]}
        if errors:
            return self._rejected(errors, warnings, dup_info)

        payload = event.to_api_payload()
        token, ttl = self.store.issue(ident.key, payload, op_type="create", entity_type="market_event")
        return {
            "valid": True,
            "errors": [],
            "warnings": warnings,
            "duplicate_check": dup_info,
            "summary": market_event_preview_text(event),
            "payload_to_be_submitted": payload,
            "confirmation_token": token,
            "token_expires_in_seconds": ttl,
            "next_step": (
                "NO MARKET EVENT HAS BEEN CREATED. Show this summary to the user and obtain explicit approval. "
                "Only after the user clearly approves, call create_confirmed_market_event with this token and "
                "user_has_explicitly_confirmed=true."
            ),
        }

    async def create_market_event(self, token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        if user_has_explicitly_confirmed is not True:
            return self._create_fail(["Explicit user confirmation is required (user_has_explicitly_confirmed=true). "
                                      "Show the summary to the user and ask. The token has not been used."])
        try:
            ident = await self._identity()
            op = self.store.consume(token, ident.key)
        except ConfirmationError as exc:
            return self._create_fail([str(exc)])
        except ApiError as exc:
            return self._create_fail([f"Authentication/role check failed: {exc}"])

        if op.op_type != "create" or op.entity_type != "market_event":
            return self._create_fail([f"Operation mismatch: confirmation token is for {op.op_type} of {op.entity_type}, not create of market_event."])

        event, errs = parse_market_event(op.payload)
        if event is None:
            return self._create_fail(["Staged payload could not be re-validated: " + "; ".join(errs)])

        try:
            created = await self.client.create_market_event(op.payload)
        except ApiError as exc:
            if exc.status is None:
                self.store.mark(op, "UNCERTAIN")
                return {"created": False, "outcome": "UNCERTAIN", "errors": [
                    f"{exc} The event MAY OR MAY NOT have been created. Search first before retrying."]}
            return self._create_fail([f"The Market Intel API rejected market event creation (HTTP {exc.status}): {exc}"])

        eid = created.get("id") if isinstance(created, dict) else None
        if not eid:
            self.store.mark(op, "UNCERTAIN")
            return {"created": False, "outcome": "UNCERTAIN", "errors": ["API returned success but no record ID."]}
        self.store.mark(op, "CREATED")
        return {
            "created": True,
            "outcome": "CONFIRMED_SUCCESS",
            "id": str(eid),
            "market_event_id": str(eid),
            "event_title": created.get("event_title") or event.event_title,
            "host_country": created.get("host_country") or event.host_country,
            "record": created,
        }

    # ----------------------------------------------------------- aliases
    get_record = get_record_details
    search_gov_entities = search_government_entities
    search_events = search_market_events

    # ----------------------------------------------------------- edit workflow
    async def validate_record_update(
        self,
        entity_type: str = "",
        record_id: str = "",
        changes: dict[str, Any] | None = None,
        module: str = "",
    ) -> dict[str, Any]:
        """Validate proposed updates to an existing Prospect, Government Entity, or Market Event."""
        entity_type = entity_type or module
        changes = changes or {}
        warnings: list[str] = []
        norm = _normalize_entity_type(entity_type)
        if not norm:
            return {"valid": False, "errors": [f"Unsupported entity_type: '{entity_type}'. Allowed: 'prospect', 'government_entity', 'market_event'."]}
        record_id = (record_id or "").strip()
        if not record_id:
            return {"valid": False, "errors": ["record_id is required."]}
        try:
            UUID(record_id)
        except ValueError:
            return {"valid": False, "errors": [f"Invalid UUID: '{record_id}'."]}
        if not isinstance(changes, dict) or not changes:
            return {"valid": False, "errors": ["changes must be a non-empty JSON object containing fields to update."]}

        allowed_fields = EDITABLE_FIELDS_BY_MODULE[norm]
        forbidden = set(changes.keys()) - allowed_fields
        if forbidden:
            return {"valid": False, "errors": [f"Field '{f}' is not editable or not supported on {norm}." for f in sorted(forbidden)]}

        try:
            ident = await self._identity()
            orig = await self.client.get_record(norm, record_id)
            if not isinstance(orig, dict) or not orig.get("id"):
                return {"valid": False, "errors": [f"{norm} with ID {record_id} was not found."]}
        except AuthError as exc:
            return {"valid": False, "errors": [f"Authentication check failed: {exc}"]}
        except ApiError as exc:
            return {"valid": False, "errors": [f"Failed to retrieve {norm} {record_id}: {exc}"]}

        # Construct merged candidate filtered to allowed fields
        merged = dict(orig)
        merged.update(changes)
        candidate = {k: v for k, v in merged.items() if k in allowed_fields}

        # Validate according to entity type
        errors: list[str] = []
        if norm == "government_entity":
            g, errs = parse_government_entity(candidate)
            errors += errs
            if g is not None:
                # If name or country changed, check duplicate
                if g.name != orig.get("name") or g.country != orig.get("country"):
                    data = await self.client.list_government_entities({"search": g.name})
                    exact, similar = find_government_entity_duplicates(g.name, g.country, _records(data))
                    for ex in exact:
                        if str(ex.get("id")) != record_id:
                            errors.append(f"Duplicate: an existing entity named '{g.name}' in '{g.country}' already exists (ID {ex.get('id')}).")
                if g.central_government:
                    try:
                        c_gov = await self.client.get_government_entity(str(g.central_government))
                        if not isinstance(c_gov, dict) or not c_gov.get("id"):
                            errors.append(f"Referenced central_government {g.central_government} was not found.")
                        elif c_gov.get("is_state_government"):
                            errors.append(f"Referenced central_government {g.central_government} is marked as State Government.")
                    except ApiError as exc:
                        errors.append(f"Could not verify central_government {g.central_government}: {exc}")
        elif norm == "market_event":
            m, errs = parse_market_event(candidate)
            errors += errs
            if m is not None:
                if m.event_title != orig.get("event_title"):
                    data = await self.client.list_market_events({"search": m.event_title})
                    exact, similar = find_market_event_duplicates(m.event_title, _records(data))
                    for ex in exact:
                        if str(ex.get("id")) != record_id:
                            errors.append(f"Duplicate: an existing market event named '{m.event_title}' already exists (ID {ex.get('id')}).")
        elif norm == "prospect":
            p, errs = parse_prospect(candidate)
            errors += errs
            if p is not None:
                errors += business_rule_errors(p)
                # Verify parent companies
                ids = referenced_company_ids(p)
                if ids:
                    for missing in sorted(await self._missing_company_ids(ids)):
                        errors.append(f"Referenced company {missing} not found among visible prospects.")
                # Verify government entity lookups
                gov_ids = referenced_government_entity_ids(p)
                if gov_ids:
                    for missing in sorted(await self._missing_government_entity_ids(gov_ids)):
                        errors.append(f"Referenced Government Entity {missing} not found.")
                # Verify market event lookups
                event_ids = referenced_market_event_ids(p)
                if event_ids:
                    for missing in sorted(await self._missing_market_event_ids(event_ids)):
                        errors.append(f"Referenced Market Event {missing} not found.")
                # Check email verification for CHANGED emails
                orig_email = orig.get("official_email_address")
                if p.official_email_address and p.official_email_address != orig_email:
                    res = await self.client.instant_verify(p.official_email_address)
                    if not res.ok:
                        errors.append(f"Changed company email failed verification: {res.status} ({res.reason})")
                orig_contact_emails = {c.get("official_email") for c in (orig.get("key_contacts") or []) if isinstance(c, dict)}
                for c in p.key_contacts:
                    if c.official_email and c.official_email not in orig_contact_emails:
                        res = await self.client.instant_verify(c.official_email)
                        if not res.ok:
                            errors.append(f"Changed contact email for '{c.contact_name}' failed verification: {res.status} ({res.reason})")

        diff = compute_record_diff(orig, merged, allowed_fields)
        if not diff:
            return {"valid": False, "errors": ["No changes detected between existing record and submitted values."]}

        if errors:
            return {"valid": False, "errors": errors, "warnings": warnings, "diff": diff}

        token, ttl = self.store.issue(
            ident.key,
            payload=changes,
            op_type="update",
            entity_type=norm,
            record_id=record_id,
            original_snapshot=orig,
        )
        return {
            "valid": True,
            "errors": [],
            "warnings": warnings,
            "entity_type": norm,
            "record_id": record_id,
            "diff": diff,
            "summary": update_diff_preview_text(norm, record_id, diff),
            "confirmation_token": token,
            "token_expires_in_seconds": ttl,
            "next_step": (
                f"NO RECORD HAS BEEN MODIFIED. Show the before-and-after diff to the user and obtain explicit approval. "
                f"Only after the user clearly approves, call update_confirmed_record with this confirmation_token and "
                f"user_has_explicitly_confirmed=true."
            ),
        }

    async def update_confirmed_record(
        self,
        token: str = "",
        user_has_explicitly_confirmed: bool = False,
        confirmation_token: str = "",
    ) -> dict[str, Any]:
        """Apply an approved update to an existing record after checking for concurrent modifications."""
        token = token or confirmation_token
        if user_has_explicitly_confirmed is not True:
            return {"updated": False, "outcome": "NOT_UPDATED", "errors": [
                "Explicit user confirmation is required (user_has_explicitly_confirmed=true). "
                "Show the changes diff to the user and ask. The token has not been used."
            ]}
        try:
            ident = await self._identity()
            op = self.store.consume(token, ident.key)
        except ConfirmationError as exc:
            return {"updated": False, "outcome": "NOT_UPDATED", "errors": [str(exc)]}
        except ApiError as exc:
            return {"updated": False, "outcome": "NOT_UPDATED", "errors": [f"Authentication/role check failed: {exc}"]}

        if op.op_type != "update" or not op.record_id:
            return {"updated": False, "outcome": "MISMATCH_ABORTED", "errors": [
                f"Operation mismatch: confirmation token is for op_type='{op.op_type}', not 'update'."
            ]}

        # Check for concurrent modifications (stale check)
        try:
            current = await self.client.get_record(op.entity_type, op.record_id)
        except ApiError as exc:
            return {"updated": False, "outcome": "NOT_UPDATED", "errors": [
                f"Could not verify current record status before saving: {exc}"
            ]}

        snap = op.original_snapshot or {}
        allowed_fields = EDITABLE_FIELDS_BY_MODULE[op.entity_type]
        stale_diff = compute_record_diff(snap, current, allowed_fields)
        if stale_diff:
            return {
                "updated": False,
                "outcome": "STALE_ABORTED",
                "errors": [
                    "Concurrent modification detected: The record was modified in the database after your review. "
                    "The staged update has been invalidated to avoid overwriting recent changes. "
                    "Please retrieve the current record and validate your update again."
                ],
                "conflicting_fields": list(stale_diff.keys()),
            }

        # Apply update
        try:
            updated = await self.client.update_record(op.entity_type, op.record_id, op.payload)
        except ApiError as exc:
            if exc.status is None:
                self.store.mark(op, "UNCERTAIN")
                return {"updated": False, "outcome": "UNCERTAIN", "errors": [
                    f"{exc} The record update MAY OR MAY NOT have been applied. Inspect the record before retrying."
                ]}
            return {"updated": False, "outcome": "NOT_UPDATED", "errors": [
                f"The Market Intel API rejected the update (HTTP {exc.status}): {exc}"
            ]}

        self.store.mark(op, "UPDATED")
        return {
            "updated": True,
            "outcome": "CONFIRMED_SUCCESS",
            "entity_type": op.entity_type,
            "record_id": op.record_id,
            "record": updated,
            "updated_record": updated,
        }

    # Generic creation/commit dispatchers
    async def validate_record_creation(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        norm = _normalize_entity_type(entity_type)
        if norm == "government_entity":
            return await self.validate_government_entity(data)
        elif norm == "market_event":
            return await self.validate_market_event(data)
        elif norm == "prospect":
            return await self.validate(data)
        return {"valid": False, "errors": [f"Unsupported entity_type: '{entity_type}'. Allowed: 'prospect', 'government_entity', 'market_event'."]}

    async def create_confirmed_record(self, confirmation_token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        """Dispatch creation or update based on token op_type and entity_type."""
        # Peek at staged operation to dispatch to the correct handler
        slot = self.store._slot(confirmation_token.strip()) if confirmation_token else None
        op = self.store._ops.get(slot) if slot else None
        if op and op.op_type == "update":
            return await self.update_confirmed_record(confirmation_token, user_has_explicitly_confirmed)
        if op and op.entity_type == "government_entity":
            return await self.create_government_entity(confirmation_token, user_has_explicitly_confirmed)
        if op and op.entity_type == "market_event":
            return await self.create_market_event(confirmation_token, user_has_explicitly_confirmed)
        return await self.create(confirmation_token, user_has_explicitly_confirmed)


def preview_text(p: ProspectInput) -> str:
    L = [
        f"Company: {p.company_name}",
        f"Country (head office): {p.country_head_office}",
        f"Address: {p.complete_address}",
        f"Primary industries: {p.primary_industries}",
        f"Ownership sector: {p.ownership_sector}",
        f"Structure: {p.company_structure}",
        f"Operational status: {p.operational_status}",
        f"Primary offering type: {p.primary_offering_type}",
    ]
    for label, val in (
        ("Phone", p.official_phone_number),
        ("Email", p.official_email_address),
        ("Website", p.official_website_url),
        ("LinkedIn", p.linkedin_company_page),
    ):
        if val:
            L.append(f"{label}: {val}")
    if p.parent_companies:
        L.append("Parent company IDs: " + ", ".join(map(str, p.parent_companies)))
    if p.merging_companies:
        L.append("Merging company IDs: " + ", ".join(map(str, p.merging_companies)))
    if p.dissolved_companies:
        L.append("Dissolved company IDs: " + ", ".join(map(str, p.dissolved_companies)))
    if p.acquiring_companies_data:
        L.append("Acquiring companies: " + ", ".join(f"{a.acquiring_company} ({a.acquired_shares}%)" for a in p.acquiring_companies_data))
    for o in p.offerings_data:
        L.append(f"Offering: {o.offering_type} - {o.name}")
    for c in p.key_contacts:
        bits = [f"{c.prefix + ' ' if c.prefix else ''}{c.contact_name}"]
        if c.designation:
            bits.append(c.designation)
        bits += [x for x in (c.official_email, c.phone_number, c.linkedin_profile) if x]
        L.append("Key contact: " + " | ".join(bits))
    for s in p.shareholdings:
        extra = []
        if s.company:
            extra.append(f"Company: {s.company}")
        elif s.is_self:
            extra.append("Self / Treasury")
        elif s.contact:
            extra.append(f"Contact: {s.contact}")
        elif s.contact_index is not None:
            extra.append(f"Contact Index: {s.contact_index}")
        elif s.government_entity:
            extra.append(f"Government Entity: {s.government_entity}")
        elif s.other_name:
            extra.append(f"Other: {s.other_name}")
        L.append(f"Shareholding: {s.holder_type} {s.share_percentage:g}%" + (f" ({', '.join(extra)})" if extra else ""))
    if p.market_event_ids:
        L.append("Market Event IDs: " + ", ".join(map(str, p.market_event_ids)))
    return "\n".join(L)


def government_entity_preview_text(g: GovernmentEntityInput) -> str:
    L = [
        f"Government Entity: {g.name}",
        f"Country: {g.country}",
        f"Is State Government: {g.is_state_government}",
    ]
    if g.is_state_government:
        if g.state_name:
            L.append(f"State Name: {g.state_name}")
        if g.central_government:
            L.append(f"Central Government ID: {g.central_government}")
    for label, val in (
        ("Official Website", g.official_website),
        ("Official Email", g.official_email),
        ("Phone Number", g.phone_number),
        ("Complete Address", g.complete_address),
    ):
        if val:
            L.append(f"{label}: {val}")
    return "\n".join(L)


def market_event_preview_text(e: MarketEventInput) -> str:
    L = [
        f"Event Title: {e.event_title}",
        f"Host Country: {e.host_country}",
        f"Start Date: {e.start_date}",
        f"End Date: {e.end_date}",
    ]
    if e.website_url:
        L.append(f"Website: {e.website_url}")
    return "\n".join(L)


def update_diff_preview_text(entity_type: str, record_id: str, diff: dict[str, dict[str, Any]]) -> str:
    L = [f"Proposed Update for {entity_type} (ID: {record_id}):"]
    for field, change in diff.items():
        L.append(f"  - {field}:")
        L.append(f"      Before: {change.get('before')}")
        L.append(f"      After:  {change.get('after')}")
    return "\n".join(L)
