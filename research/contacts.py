"""Strict Key Contacts & official company email verification module."""
from __future__ import annotations

import re
from typing import Any

from api_client import ApiError
from research.models import CandidateContact, FetchedPage, FieldEvidence, VerificationStatus
from research.prompts import contacts_prompt
from research.provider_router import ProviderRouter
from research.security import format_sources_for_prompt
from research.verifier import build_evidence, verify_contact_candidate
from schemas import EMAIL_RE, PHONE_RE

CONTACT_PATH_HINTS = ("contact", "leadership", "team", "management", "executive", "board", "about", "people")


def _prioritize_contact_pages(pages: list[FetchedPage]) -> list[FetchedPage]:
    return sorted(
        pages,
        key=lambda p: (
            sum(1 for h in CONTACT_PATH_HINTS if h in p.final_url.lower()),
            len(p.emails_found),
        ),
        reverse=True,
    )


def _deterministic_contact_candidates(pages: list[FetchedPage]) -> list[dict[str, Any]]:
    """Extract candidate contacts from JSON-LD Person/ContactPoint records when explicit."""
    out: list[dict[str, Any]] = []
    for p in pages:
        for obj in p.json_ld:
            t = str(obj.get("@type") or "").casefold()
            if "person" in t and obj.get("name"):
                out.append({
                    "contact_name": str(obj.get("name")).strip(),
                    "designation": str(obj.get("jobTitle") or "").strip() or None,
                    "official_email": str(obj.get("email") or "").strip().removeprefix("mailto:") or None,
                    "phone_number": str(obj.get("telephone") or "").strip() or None,
                    "linkedin_profile": None,
                    "source_url": p.final_url,
                    "evidence_quote": f"{obj.get('name')} {obj.get('jobTitle', '')}".strip(),
                })
            # Check employees / founder / member arrays inside Organization JSON-LD
            for key in ("founder", "founders", "employee", "employees", "member"):
                sub = obj.get(key)
                if isinstance(sub, dict):
                    sub = [sub]
                if isinstance(sub, list):
                    for person in sub:
                        if isinstance(person, dict) and person.get("name"):
                            out.append({
                                "contact_name": str(person.get("name")).strip(),
                                "designation": str(person.get("jobTitle") or key).strip() or None,
                                "official_email": str(person.get("email") or "").strip().removeprefix("mailto:") or None,
                                "phone_number": str(person.get("telephone") or "").strip() or None,
                                "linkedin_profile": None,
                                "source_url": p.final_url,
                                "evidence_quote": f"{person.get('name')} {person.get('jobTitle', '')}".strip(),
                            })
    return out


async def research_key_contacts(
    company_name: str,
    pages: list[FetchedPage],
    *,
    official_domain: str | None,
    api_client: Any,
    router: ProviderRouter | None,
    budget: Any,
    max_context_chars: int = 12_000,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Extract and strictly verify key contacts (name + designation + grounded email + VALID instant_verify)."""
    ordered = _prioritize_contact_pages(pages)
    pages_by_url = {p.final_url: p for p in ordered}
    for p in ordered:
        pages_by_url.setdefault(p.final_url.rstrip("/"), p)

    raw_candidates = _deterministic_contact_candidates(ordered)

    if router is not None and ordered:
        sources_block = format_sources_for_prompt(ordered[:4], max_total_chars=min(max_context_chars, 8000))
        sys_p, usr_p = contacts_prompt(company_name, sources_block)
        llm_res = await router.complete_json(
            task_type="key_contacts",
            system_prompt=sys_p,
            user_prompt=usr_p,
            stage="primary",
            budget=budget,
            max_tokens=900,
            force_refresh=force_refresh,
        )
        if isinstance(llm_res, dict) and isinstance(llm_res.get("contacts"), list):
            for c in llm_res["contacts"]:
                if isinstance(c, dict) and c.get("contact_name"):
                    raw_candidates.append(c)

    # Deduplicate raw candidates by (name, email)
    seen_raw: set[tuple[str, str]] = set()
    deduped_raw: list[dict[str, Any]] = []
    for c in raw_candidates:
        nm = " ".join(str(c.get("contact_name") or "").split()).casefold()
        em = str(c.get("official_email") or "").strip().casefold()
        if not nm:
            continue
        key = (nm, em)
        if key not in seen_raw:
            seen_raw.add(key)
            deduped_raw.append(c)

    all_candidates: list[CandidateContact] = []
    verified_contacts: list[dict[str, Any]] = []
    evidences: list[FieldEvidence] = []

    for cand in deduped_raw[:15]:
        checked = await verify_contact_candidate(
            cand,
            company_name=company_name,
            official_domain=official_domain,
            pages_by_url=pages_by_url,
            api_client=api_client,
        )
        all_candidates.append(checked)
        if checked.included_in_payload:
            contact_payload: dict[str, Any] = {
                "contact_name": checked.contact_name,
                "designation": checked.designation,
                "official_email": checked.official_email,
            }
            if checked.prefix:
                contact_payload["prefix"] = checked.prefix
            if checked.phone_number and PHONE_RE.match(checked.phone_number):
                contact_payload["phone_number"] = checked.phone_number
            if checked.linkedin_profile and "linkedin.com" in checked.linkedin_profile.lower():
                contact_payload["linkedin_profile"] = checked.linkedin_profile
            verified_contacts.append(contact_payload)
            evidences.append(
                build_evidence(
                    "key_contacts",
                    contact_payload,
                    checked.source_url,
                    pages_by_url,
                    excerpt=checked.excerpt or f"{checked.contact_name} - {checked.designation}",
                    official_domain=official_domain,
                )
            )

    return {
        "key_contacts": verified_contacts,
        "all_candidates": all_candidates,
        "excluded_count": sum(1 for c in all_candidates if not c.included_in_payload),
        "evidence": {"key_contacts": evidences} if evidences else {},
    }


async def verify_company_email(
    candidate_email: str | None,
    pages: list[FetchedPage],
    api_client: Any,
) -> tuple[str | None, str, str]:
    """Verify corporate official_email_address via Market Intel instant_verify; require VALID."""
    if not candidate_email:
        return None, "UNAVAILABLE", "No official company email found in sources."
    email = candidate_email.strip()
    if not EMAIL_RE.match(email):
        return None, "INVALID_SYNTAX", "Candidate email failed format validation."

    if pages:
        grounded = any(
            any(e.casefold() == email.casefold() for e in p.emails_found)
            or email.casefold() in p.text.casefold()
            for p in pages
        )
        if not grounded:
            return None, "UNGROUNDED", "Candidate company email did not appear in fetched sources."

    try:
        res = await api_client.instant_verify(email)
    except ApiError as exc:
        return None, "ERROR", str(exc)

    if res.ok:
        return email, "VALID", res.reason
    return None, res.status, res.reason
