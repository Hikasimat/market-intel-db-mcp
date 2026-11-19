"""Shareholding & ownership research with strict separation from directors and zero percentage fabrication."""
from __future__ import annotations

from typing import Any

from api_client import ApiError
from research.corporate_structure import _match_prospect_record
from research.models import FetchedPage, FieldEvidence, VerificationStatus
from research.verifier import build_evidence, is_quote_grounded
from validation import find_government_entity_duplicates

VALID_HOLDER_TYPES = frozenset({"Company", "Individual", "Government", "Other"})


async def _lookup_government_entity(name: str, country: str | None, api_client: Any) -> dict[str, Any] | None:
    try:
        resp = await api_client.list_government_entities({"search": name})
    except ApiError:
        return None
    recs = resp.get("results", []) if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])
    recs = [r for r in recs if isinstance(r, dict)]
    exact, similar = find_government_entity_duplicates(name, country or "", recs)
    if exact:
        return exact[0]
    norm_name = " ".join(name.split()).casefold()
    for r in recs:
        if " ".join(str(r.get("name") or "").split()).casefold() == norm_name:
            return r
    return None


async def _lookup_company_shareholder(name: str, country: str | None, api_client: Any) -> dict[str, Any] | None:
    try:
        resp = await api_client.list_prospects({"search": name, "page_size": 25})
    except ApiError:
        return None
    recs = resp.get("results", []) if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])
    recs = [r for r in recs if isinstance(r, dict)]
    return _match_prospect_record(name, country, None, recs)


async def resolve_ownership(
    company_name: str,
    company_country: str | None,
    extracted_corp: dict[str, Any] | None,
    pages: list[FetchedPage],
    *,
    official_domain: str | None,
    api_client: Any,
    verified_contacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build verified schema-valid shareholdings while separating directors and incomplete holders."""
    pages_by_url = {p.final_url: p for p in pages}
    for p in pages:
        pages_by_url.setdefault(p.final_url.rstrip("/"), p)

    raw = extracted_corp or {}
    raw_holders = raw.get("shareholders") or []
    directors_only = [
        d for d in (raw.get("directors_only") or [])
        if isinstance(d, dict) and d.get("name")
    ]
    director_names_low = {str(d["name"]).strip().casefold() for d in directors_only}

    schema_shareholdings: list[dict[str, Any]] = []
    incomplete_shareholders: list[dict[str, Any]] = []
    missing_gov_entities: list[dict[str, Any]] = []
    missing_shareholder_companies: list[dict[str, Any]] = []
    evidences: list[FieldEvidence] = []
    running_total = 0.0

    verified_contacts = verified_contacts or []

    for item in raw_holders:
        if not isinstance(item, dict):
            continue
        htype = str(item.get("holder_type") or "").strip().title()
        if htype not in VALID_HOLDER_TYPES:
            continue
        hname = " ".join(str(item.get("holder_name") or "").split())
        if not hname:
            continue
        src_url = str(item.get("source_url") or "")
        quote = str(item.get("evidence_quote") or hname)

        # Never confuse a director with a shareholder unless shareholding quote explicitly supports equity ownership.
        if hname.casefold() in director_names_low and not any(
            kw in quote.casefold() for kw in ("%", "percent", "share", "equity", "stake", "holder", "owns")
        ):
            continue

        if pages and not (is_quote_grounded(hname, pages) or is_quote_grounded(quote, pages)):
            incomplete_shareholders.append({
                "holder_type": htype,
                "holder_name": hname,
                "reason": "Shareholder claim was not grounded in fetched source text.",
            })
            continue

        pct_raw = item.get("share_percentage")
        if not isinstance(pct_raw, (int, float)) or float(pct_raw) <= 0 or float(pct_raw) > 100:
            incomplete_shareholders.append({
                "holder_type": htype,
                "holder_name": hname,
                "share_percentage": None,
                "source_url": src_url,
                "evidence_quote": quote,
                "reason": "Exact share percentage is unavailable in public evidence (never fabricated).",
            })
            continue

        pct = round(float(pct_raw), 4)
        if running_total + pct > 100.0001:
            incomplete_shareholders.append({
                "holder_type": htype,
                "holder_name": hname,
                "share_percentage": pct,
                "source_url": src_url,
                "reason": "Adding this percentage would exceed 100% total equity; marked for manual review.",
            })
            continue

        entry: dict[str, Any] | None = None
        if htype == "Government":
            gov = await _lookup_government_entity(hname, company_country, api_client)
            if gov and gov.get("id"):
                entry = {
                    "holder_type": "Government",
                    "government_entity": str(gov["id"]),
                    "share_percentage": pct,
                }
            else:
                missing_gov_entities.append({
                    "name": hname,
                    "country": company_country or "",
                    "share_percentage": pct,
                    "source_url": src_url,
                })
                incomplete_shareholders.append({
                    "holder_type": htype,
                    "holder_name": hname,
                    "share_percentage": pct,
                    "source_url": src_url,
                    "reason": "Government Entity record does not exist in Market Intel; create it first.",
                })
        elif htype == "Company":
            if hname.casefold() == company_name.casefold():
                entry = {
                    "holder_type": "Company",
                    "is_self": True,
                    "share_percentage": pct,
                }
            else:
                comp = await _lookup_company_shareholder(hname, company_country, api_client)
                if comp and comp.get("id"):
                    entry = {
                        "holder_type": "Company",
                        "company": str(comp["id"]),
                        "share_percentage": pct,
                    }
                else:
                    missing_shareholder_companies.append({
                        "company_name": hname,
                        "share_percentage": pct,
                        "source_url": src_url,
                    })
                    incomplete_shareholders.append({
                        "holder_type": htype,
                        "holder_name": hname,
                        "share_percentage": pct,
                        "source_url": src_url,
                        "reason": "Corporate shareholder does not exist in Market Intel; create it first.",
                    })
        elif htype == "Individual":
            idx = next(
                (i for i, c in enumerate(verified_contacts) if c.get("contact_name", "").casefold() == hname.casefold()),
                None,
            )
            if idx is not None:
                entry = {
                    "holder_type": "Individual",
                    "contact_index": idx,
                    "share_percentage": pct,
                }
            else:
                # Preserve exact factual holder using schema-valid 'Other' ONLY when individual isn't a verified contact?
                # Wait: preserve holder_type distinction strictly! Do not misclassify Individual as Other.
                incomplete_shareholders.append({
                    "holder_type": "Individual",
                    "holder_name": hname,
                    "share_percentage": pct,
                    "source_url": src_url,
                    "reason": "Individual shareholder requires an existing contact UUID or verified contact_index.",
                })
        elif htype == "Other":
            entry = {
                "holder_type": "Other",
                "other_name": hname[:255],
                "share_percentage": pct,
            }

        if entry is not None:
            running_total += pct
            schema_shareholdings.append(entry)
            evidences.append(
                build_evidence(
                    "shareholdings",
                    {**entry, "holder_name": hname},
                    src_url,
                    pages_by_url,
                    excerpt=quote,
                    official_domain=official_domain,
                )
            )

    if schema_shareholdings and not incomplete_shareholders:
        status = VerificationStatus.VERIFIED
    elif schema_shareholdings or incomplete_shareholders:
        status = VerificationStatus.PARTIALLY_VERIFIED
    else:
        status = VerificationStatus.UNAVAILABLE

    return {
        "shareholdings": schema_shareholdings,
        "incomplete_shareholders": incomplete_shareholders,
        "directors_identified_not_shareholders": directors_only,
        "ownership_status": status.value,
        "total_verified_percentage": round(running_total, 4),
        "missing_prerequisites": {
            "government_entities": missing_gov_entities,
            "shareholder_companies": missing_shareholder_companies,
        },
        "evidence": {"shareholdings": evidences} if evidences else {},
    }
