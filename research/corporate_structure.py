"""Corporate structure research, enum mapping, and evidence-backed Market Intel parent auto-linking."""
from __future__ import annotations

from typing import Any

from api_client import ApiError
from research.company_identity import extract_domain, normalize_company_name
from research.models import FetchedPage, FieldEvidence, VerificationStatus
from research.verifier import build_evidence, is_quote_grounded
from schemas import COMPANY_STRUCTURES, OPERATIONAL_STATUSES, OWNERSHIP_SECTORS

STRUCTURE_MAP: dict[str, str] = {
    "parent": "Parent",
    "parent organization": "Parent",
    "parent company": "Parent",
    "holding company": "Parent",
    "standalone": "Parent",
    "branch": "Branch",
    "branch office": "Branch",
    "regional office": "Branch",
    "subsidiary": "Subsidiary",
    "subsidiary company": "Subsidiary",
    "wholly owned subsidiary": "Subsidiary",
    "wholly-owned subsidiary": "Subsidiary",
}

STATUS_MAP: dict[str, str] = {
    "active": "Active",
    "operating": "Active",
    "inactive": "Inactive",
    "dormant": "Inactive",
    "permanently closed": "Permanently Closed",
    "closed": "Permanently Closed",
    "dissolved": "Permanently Closed",
    "liquidated": "Permanently Closed",
    "acquired": "Acquired",
    "merged": "Merged",
}

SECTOR_MAP: dict[str, str] = {
    "private": "Private Company",
    "private company": "Private Company",
    "public": "Private Company",
    "public company": "Private Company",
    "government": "Government Company",
    "government company": "Government Company",
    "state owned": "Government Company",
    "state-owned": "Government Company",
    "psu": "Government Company",
    "semi government": "Semi Government Company",
    "semi-government": "Semi Government Company",
    "semi government company": "Semi Government Company",
}


def map_company_structure(raw: str | None) -> str | None:
    if not raw:
        return None
    norm = " ".join(str(raw).strip().casefold().split())
    if raw.strip() in COMPANY_STRUCTURES:
        return raw.strip()
    return STRUCTURE_MAP.get(norm)


def map_operational_status(raw: str | None) -> str | None:
    if not raw:
        return None
    norm = " ".join(str(raw).strip().casefold().split())
    if raw.strip() in OPERATIONAL_STATUSES:
        return raw.strip()
    return STATUS_MAP.get(norm)


def map_ownership_sector(raw: str | None) -> str | None:
    if not raw:
        return None
    norm = " ".join(str(raw).strip().casefold().split())
    if raw.strip() in OWNERSHIP_SECTORS:
        return raw.strip()
    return SECTOR_MAP.get(norm)


def _match_prospect_record(
    target_name: str,
    target_country: str | None,
    target_domain: str | None,
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Confirm existing Market Intel company match using normalized legal name + domain/country alignment."""
    norm_target = normalize_company_name(target_name)
    raw_target = " ".join(target_name.split()).casefold()
    if not norm_target:
        return None

    for r in records:
        r_id = r.get("id")
        r_name = str(r.get("company_name") or "").strip()
        if not r_id or not r_name:
            continue
        norm_rec = normalize_company_name(r_name)
        raw_rec = " ".join(r_name.split()).casefold()

        # Never link merely because one string is a loose substring of another.
        name_aligned = (raw_rec == raw_target) or (norm_rec == norm_target)
        if not name_aligned:
            continue

        r_domain = extract_domain(r.get("official_website_url"))
        r_country = str(r.get("country_head_office") or "").strip().casefold()

        # If target_domain is provided and record also has a domain, require domain alignment.
        if target_domain and r_domain and target_domain != r_domain:
            continue
        # If target_country is provided and record has country, require country alignment unless domains already match.
        if target_country and r_country and target_country.strip().casefold() != r_country:
            if not (target_domain and r_domain and target_domain == r_domain):
                continue

        return r
    return None


async def _lookup_existing_prospect(
    name: str,
    country: str | None,
    domain: str | None,
    api_client: Any,
) -> dict[str, Any] | None:
    try:
        resp = await api_client.list_prospects({"search": name, "page_size": 25})
    except ApiError:
        return None
    recs = resp.get("results", []) if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])
    recs = [r for r in recs if isinstance(r, dict)]
    return _match_prospect_record(name, country, domain, recs)


async def resolve_corporate_structure(
    company_name: str,
    extracted_corp: dict[str, Any] | None,
    pages: list[FetchedPage],
    *,
    official_domain: str | None,
    api_client: Any,
) -> dict[str, Any]:
    """Map corporate hierarchy and perform strict evidence-based parent/acquirer/merger auto-linking."""
    pages_by_url = {p.final_url: p for p in pages}
    for p in pages:
        pages_by_url.setdefault(p.final_url.rstrip("/"), p)

    evidences: dict[str, list[FieldEvidence]] = {}
    missing_parents: list[dict[str, Any]] = []
    missing_acquirers: list[dict[str, Any]] = []
    missing_mergers: list[dict[str, Any]] = []

    raw = extracted_corp or {}

    def _unwrap(field: str, mapper) -> tuple[str | None, str, str]:
        node = raw.get(field)
        if isinstance(node, dict):
            val = mapper(node.get("value"))
            return val, str(node.get("source_url") or ""), str(node.get("evidence_quote") or "")
        return mapper(node), "", ""

    structure, st_url, st_quote = _unwrap("company_structure", map_company_structure)
    op_status, os_url, os_quote = _unwrap("operational_status", map_operational_status)
    sector, sec_url, sec_quote = _unwrap("ownership_sector", map_ownership_sector)

    # Resolve parent organizations strictly when company is Branch or Subsidiary (or when parent evidence exists).
    parent_ids: list[str] = []
    parent_names_verified: list[dict[str, Any]] = []
    raw_parents = raw.get("parent_organizations") or []
    for item in raw_parents:
        if not isinstance(item, dict):
            continue
        pname = " ".join(str(item.get("name") or "").split())
        if not pname or normalize_company_name(pname) == normalize_company_name(company_name):
            continue
        p_url = str(item.get("source_url") or "")
        p_quote = str(item.get("evidence_quote") or pname)
        if pages and not (is_quote_grounded(pname, pages) or is_quote_grounded(p_quote, pages)):
            continue
        matched = await _lookup_existing_prospect(
            pname,
            item.get("country"),
            extract_domain(item.get("domain")),
            api_client,
        )
        parent_names_verified.append({
            "name": pname,
            "country": item.get("country"),
            "domain": item.get("domain"),
            "source_url": p_url,
            "evidence_quote": p_quote,
            "matched_prospect_id": str(matched["id"]) if matched else None,
            "matched_company_name": matched.get("company_name") if matched else None,
        })
        if matched:
            pid = str(matched["id"])
            if pid not in parent_ids:
                parent_ids.append(pid)
        else:
            missing_parents.append({
                "company_name": pname,
                "country": item.get("country"),
                "domain": item.get("domain"),
                "source_url": p_url,
                "reason": "Verified parent organization does not yet exist in Market Intel; create parent first if approved.",
            })

    # Enforce blueprint consistency: Parent cannot have parent_companies.
    if structure is None:
        if parent_names_verified:
            structure = "Subsidiary"
        elif pages:
            structure = "Parent"

    if structure == "Parent":
        parent_ids = []

    if op_status is None and pages:
        op_status = "Active"
    if sector is None and pages:
        sector = "Private Company"

    # Acquiring companies (only if Acquired)
    acquiring_data: list[dict[str, Any]] = []
    if op_status == "Acquired":
        for acq in raw.get("acquiring_companies") or []:
            if not isinstance(acq, dict):
                continue
            aname = " ".join(str(acq.get("name") or "").split())
            if not aname:
                continue
            a_quote = str(acq.get("evidence_quote") or aname)
            if pages and not (is_quote_grounded(aname, pages) or is_quote_grounded(a_quote, pages)):
                continue
            matched = await _lookup_existing_prospect(aname, None, None, api_client)
            shares = acq.get("acquired_shares")
            if isinstance(shares, (int, float)) and 0 <= float(shares) <= 100:
                shares_val = float(shares)
            else:
                shares_val = None
            if matched:
                acquiring_data.append({
                    "acquiring_company": str(matched["id"]),
                    **({"acquired_shares": shares_val} if shares_val is not None else {}),
                })
            else:
                missing_acquirers.append({
                    "company_name": aname,
                    "acquired_shares": shares_val,
                    "source_url": acq.get("source_url", ""),
                })

    # Merging / dissolved companies (only if Merged)
    merging_ids: list[str] = []
    dissolved_ids: list[str] = []
    if op_status == "Merged":
        for m in raw.get("merging_companies") or []:
            if not isinstance(m, dict):
                continue
            mname = " ".join(str(m.get("name") or "").split())
            if not mname:
                continue
            matched = await _lookup_existing_prospect(mname, None, None, api_client)
            if matched:
                mid = str(matched["id"])
                if m.get("dissolved"):
                    if mid not in dissolved_ids and mid not in merging_ids:
                        dissolved_ids.append(mid)
                else:
                    if mid not in merging_ids and mid not in dissolved_ids:
                        merging_ids.append(mid)
            else:
                missing_mergers.append({
                    "company_name": mname,
                    "dissolved": bool(m.get("dissolved")),
                    "source_url": m.get("source_url", ""),
                })

    default_url = pages[0].final_url if pages else ""
    if structure:
        evidences["company_structure"] = [
            build_evidence(
                "company_structure",
                structure,
                st_url or default_url,
                pages_by_url,
                excerpt=st_quote or f"Company structure classified as {structure}",
                official_domain=official_domain,
            )
        ]
    if op_status:
        evidences["operational_status"] = [
            build_evidence(
                "operational_status",
                op_status,
                os_url or default_url,
                pages_by_url,
                excerpt=os_quote or f"Operational status: {op_status}",
                official_domain=official_domain,
            )
        ]
    if sector:
        evidences["ownership_sector"] = [
            build_evidence(
                "ownership_sector",
                sector,
                sec_url or default_url,
                pages_by_url,
                excerpt=sec_quote or f"Ownership sector: {sector}",
                official_domain=official_domain,
            )
        ]
    if parent_ids:
        evidences["parent_companies"] = [
            build_evidence(
                "parent_companies",
                parent_ids,
                parent_names_verified[0]["source_url"] if parent_names_verified else default_url,
                pages_by_url,
                excerpt=parent_names_verified[0]["evidence_quote"] if parent_names_verified else "Verified parent link",
                official_domain=official_domain,
            )
        ]

    subsidiaries = [
        {
            "name": s.get("name"),
            "relation": s.get("relation"),
            "source_url": s.get("source_url"),
            "evidence_quote": s.get("evidence_quote"),
        }
        for s in (raw.get("subsidiaries_and_branches") or [])
        if isinstance(s, dict) and s.get("name")
    ]

    return {
        "company_structure": structure,
        "operational_status": op_status,
        "ownership_sector": sector,
        "parent_companies": parent_ids,
        "acquiring_companies_data": acquiring_data,
        "merging_companies": merging_ids,
        "dissolved_companies": dissolved_ids,
        "verified_parent_details": parent_names_verified,
        "subsidiaries_and_branches": subsidiaries,
        "missing_prerequisites": {
            "parent_companies": missing_parents,
            "acquiring_companies": missing_acquirers,
            "merging_companies": missing_mergers,
        },
        "ambiguity_notes": [str(n) for n in (raw.get("ambiguity_notes") or []) if n],
        "evidence": evidences,
    }
