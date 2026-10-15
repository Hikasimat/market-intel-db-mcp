"""Search -> validate -> confirm -> create orchestration. Tool bodies in server.py are thin wrappers."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from api_client import ApiError, ApiTimeout, AuthError, EmailResult, Identity
from confirmation_store import ConfirmationError, ConfirmationStore
from config import Settings
from schemas import ProspectInput, parse_prospect
from validation import (EmailTarget, business_rule_errors, collect_email_targets, find_duplicates,
                        referenced_company_ids)

log = logging.getLogger("prospect_mcp.workflow")

MAX_SEARCH_LIMIT = 25
DEFAULT_SEARCH_LIMIT = 10
PAGE_SIZE = 100
NO_PROOF_NOTE = ("Search only covers prospects visible to the configured PRE account. No match is NOT proof "
                 "that the company does not exist; the backend's global duplicate check decides at creation.")


class ProspectApi(Protocol):  # the subset of MarketIntelClient the workflow uses
    identity: Identity | None
    async def authenticate(self) -> Identity: ...
    async def list_prospects(self, params: dict[str, Any]) -> Any: ...
    async def instant_verify(self, email: str) -> EmailResult: ...
    async def create_prospect(self, payload: dict[str, Any]) -> Any: ...
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

    # -------------------------------------------------------------- validate
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
        token, ttl = self.store.issue(ident.key, payload)
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
                "next_step": "No confirmation token was issued and no prospect was created. "
                             "Ask the user to correct the listed problems, then call validate_prospect_data again."}

    # ---------------------------------------------------------------- create
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

        prospect, errs = parse_prospect(op.payload)
        if prospect is None:  # should be impossible; refuse rather than send something unreviewed
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
            if exc.status is None:  # timeout / network: the backend may have processed it
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
        return {"created": True, "prospect_id": pid, "company_name": created.get("company_name") or p.company_name,
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


def preview_text(p: ProspectInput) -> str:
    L = [f"Company: {p.company_name}", f"Country (head office): {p.country_head_office}",
         f"Address: {p.complete_address}", f"Primary industries: {p.primary_industries}",
         f"Ownership sector: {p.ownership_sector}", f"Structure: {p.company_structure}",
         f"Operational status: {p.operational_status}", f"Primary offering type: {p.primary_offering_type}"]
    for label, val in (("Phone", p.official_phone_number), ("Email", p.official_email_address),
                       ("Website", p.official_website_url), ("LinkedIn", p.linkedin_company_page)):
        if val:
            L.append(f"{label}: {val}")
    if p.parent_companies:
        L.append("Parent company IDs: " + ", ".join(map(str, p.parent_companies)))
    for o in p.offerings_data:
        L.append(f"Offering: {o.offering_type} - {o.name}")
    for c in p.key_contacts:
        bits = [f"{c.prefix + ' ' if c.prefix else ''}{c.contact_name}"]
        if c.designation:
            bits.append(c.designation)
        bits += [x for x in (c.official_email, c.phone_number, c.linkedin_profile) if x]
        L.append("Key contact: " + " | ".join(bits))
    for s in p.shareholdings:
        L.append(f"Shareholding: {s.holder_type} {s.share_percentage:g}%")
    return "\n".join(L)
