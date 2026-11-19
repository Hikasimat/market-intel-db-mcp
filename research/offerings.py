"""Highest-priority research module: Primary Industries and Market Offerings (Products, Services, Solutions)."""
from __future__ import annotations

import re
from typing import Any

from research.models import FetchedPage, FieldEvidence, ResearchBudget, VerificationStatus
from research.prompts import offerings_and_industries_prompt
from research.provider_router import ProviderRouter
from research.security import format_sources_for_prompt
from research.verifier import build_evidence, is_quote_grounded
from schemas import OFFERING_FOR_PRIMARY, OFFERING_TYPES, PRIMARY_OFFERING_TYPES

OFFERING_SECTION_PATH_HINTS = (
    "product", "service", "solution", "offering", "industr", "capabilit",
    "portfolio", "division", "catalog", "platform", "what-we-do",
)

GENERIC_FILLER_OFFERINGS = frozenset({
    "products", "services", "solutions", "consulting", "technology", "software",
    "business solutions", "digital services", "industrial solutions",
})


def _prioritize_offering_pages(pages: list[FetchedPage]) -> list[FetchedPage]:
    def score(p: FetchedPage) -> tuple[int, int]:
        url_low = p.final_url.lower()
        hits = sum(1 for h in OFFERING_SECTION_PATH_HINTS if h in url_low)
        return (hits, 1 if p.json_ld else 0)

    return sorted(pages, key=score, reverse=True)


def _deterministic_offerings_from_json_ld(pages: list[FetchedPage]) -> tuple[list[str], list[dict[str, Any]]]:
    industries: list[str] = []
    offerings: list[dict[str, Any]] = []
    for p in pages:
        for item in p.json_ld:
            ind = item.get("industry") or item.get("knowsAbout")
            if isinstance(ind, str) and ind.strip():
                industries.append(ind.strip())
            elif isinstance(ind, list):
                industries.extend(str(x).strip() for x in ind if isinstance(x, str) and x.strip())

            t = str(item.get("@type") or "").casefold()
            name = str(item.get("name") or "").strip()
            if name and len(name) <= 255:
                if "product" in t:
                    offerings.append({
                        "offering_type": "Product",
                        "name": name,
                        "source_url": p.final_url,
                        "evidence_quote": name,
                    })
                elif "service" in t:
                    offerings.append({
                        "offering_type": "Service",
                        "name": name,
                        "source_url": p.final_url,
                        "evidence_quote": name,
                    })
    return list(dict.fromkeys(industries)), offerings


def _deterministic_offerings_from_pages(pages: list[FetchedPage]) -> list[dict[str, Any]]:
    """Extract named offerings from dedicated /products, /services, /solutions subpage headings when explicit."""
    out: list[dict[str, Any]] = []
    skip_headings = {
        "home", "about", "about us", "contact", "contact us", "menu", "navigation",
        "products", "our products", "services", "our services", "solutions", "our solutions",
        "industries", "news", "blog", "careers", "overview", "learn more", "get in touch",
    }
    for p in pages:
        path = p.final_url.lower()
        inferred_type: str | None = None
        if any(k in path for k in ("/product", "/catalog", "/equipment", "/hardware")):
            inferred_type = "Product"
        elif any(k in path for k in ("/service", "/consulting")):
            inferred_type = "Service"
        elif any(k in path for k in ("/solution", "/platform", "/system")):
            inferred_type = "Solution"
        if not inferred_type:
            continue

        for h in p.headings:
            clean = " ".join(h.split()).strip()
            low = clean.casefold()
            if (
                len(clean) < 3
                or len(clean) > 120
                or low in skip_headings
                or low in GENERIC_FILLER_OFFERINGS
            ):
                continue
            out.append({
                "offering_type": inferred_type,
                "name": clean,
                "source_url": p.final_url,
                "evidence_quote": clean,
            })
            if len(out) >= 20:
                return out
    return out


def _infer_primary_offering_type(offerings: list[dict[str, Any]], llm_choice: str | None = None) -> str | None:
    types_present = {o["offering_type"] for o in offerings if o.get("offering_type") in OFFERING_TYPES}
    if not types_present:
        if llm_choice in PRIMARY_OFFERING_TYPES:
            return llm_choice
        return None
    if len(types_present) > 1:
        return "Multiple"
    only = next(iter(types_present))
    for prim, item_type in OFFERING_FOR_PRIMARY.items():
        if item_type == only:
            return prim
    return "Multiple"


def _normalize_offerings(
    raw_offerings: list[dict[str, Any]],
    pages: list[FetchedPage],
) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    cleaned: list[dict[str, Any]] = []
    for o in raw_offerings:
        if not isinstance(o, dict):
            continue
        otype = str(o.get("offering_type") or "").strip().title()
        name = " ".join(str(o.get("name") or "").split())[:255]
        if otype not in OFFERING_TYPES or not name:
            continue
        if name.casefold() in GENERIC_FILLER_OFFERINGS:
            continue
        quote = str(o.get("evidence_quote") or name)
        if pages and not (is_quote_grounded(name, pages) or is_quote_grounded(quote, pages)):
            continue
        key = (otype, name.casefold())
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({
            "offering_type": otype,
            "name": name,
            "source_url": str(o.get("source_url") or ""),
            "evidence_quote": quote[:250],
        })
    return cleaned


async def extract_industries_and_offerings(
    company_name: str,
    pages: list[FetchedPage],
    *,
    official_domain: str | None,
    router: ProviderRouter | None,
    budget: ResearchBudget | None,
    max_context_chars: int = 12_000,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Extract evidence-backed Primary Industries and Market Offerings with highest priority."""
    ordered_pages = _prioritize_offering_pages(pages)
    pages_by_url = {p.final_url: p for p in ordered_pages}
    for p in ordered_pages:
        pages_by_url.setdefault(p.final_url.rstrip("/"), p)

    jsonld_industries, jsonld_offerings = _deterministic_offerings_from_json_ld(ordered_pages)
    heading_offerings = _deterministic_offerings_from_pages(ordered_pages)

    llm_data: dict[str, Any] | None = None
    if router is not None and ordered_pages:
        sources_block = format_sources_for_prompt(ordered_pages, max_total_chars=max_context_chars)
        sys_p, usr_p = offerings_and_industries_prompt(company_name, sources_block)
        llm_data = await router.complete_json(
            task_type="industries_and_offerings",
            system_prompt=sys_p,
            user_prompt=usr_p,
            stage="primary",
            budget=budget,
            max_tokens=1100,
            force_refresh=force_refresh,
        )

    primary_industries: str | None = None
    industries_source_url = ""
    industries_quote = ""
    business_summary = ""
    llm_primary_type: str | None = None
    merged_raw_offerings: list[dict[str, Any]] = list(jsonld_offerings)

    if isinstance(llm_data, dict):
        ind_val = llm_data.get("primary_industries")
        if isinstance(ind_val, str) and ind_val.strip() and ind_val.strip().lower() != "null":
            cand_ind = " ".join(ind_val.split())[:255]
            ind_quote = str(llm_data.get("evidence_quote") or cand_ind)
            ind_url = str(llm_data.get("source_url") or (ordered_pages[0].final_url if ordered_pages else ""))
            # Guard against hallucinated industry classification not grounded in sources.
            if not ordered_pages or is_quote_grounded(ind_quote, ordered_pages) or is_quote_grounded(cand_ind, ordered_pages):
                primary_industries = cand_ind
                industries_source_url = ind_url
                industries_quote = ind_quote

        bsum = llm_data.get("business_summary")
        if isinstance(bsum, str) and bsum.strip():
            business_summary = " ".join(bsum.split())[:500]

        pt = llm_data.get("primary_offering_type")
        if isinstance(pt, str) and pt in PRIMARY_OFFERING_TYPES:
            llm_primary_type = pt

        llm_offs = llm_data.get("offerings")
        if isinstance(llm_offs, list):
            merged_raw_offerings.extend(x for x in llm_offs if isinstance(x, dict))

    # Deterministic fallback when LLM unavailable or returned nothing for industries/offerings.
    if not primary_industries and jsonld_industries:
        primary_industries = ", ".join(jsonld_industries)[:255]
        industries_source_url = ordered_pages[0].final_url if ordered_pages else ""
        industries_quote = primary_industries

    if not primary_industries and ordered_pages:
        # Look for explicit industry keywords in meta/headings/text without inventing from company name.
        industry_line_re = re.compile(
            r"(?:industries\s*(?:we\s*serve|served)?|sector|specializ(?:ing|es)\s+in)\s*[:\-]\s*([^\n.]{5,180})",
            re.I,
        )
        for p in ordered_pages:
            m = industry_line_re.search(p.text)
            if m:
                primary_industries = " ".join(m.group(1).split())[:255]
                industries_source_url = p.final_url
                industries_quote = m.group(0)[:250]
                break

    if len(merged_raw_offerings) == 0:
        merged_raw_offerings.extend(heading_offerings)

    verified_offerings = _normalize_offerings(merged_raw_offerings, ordered_pages)
    primary_offering_type = _infer_primary_offering_type(verified_offerings, llm_primary_type)

    evidences: dict[str, list[FieldEvidence]] = {}
    if primary_industries:
        ev = build_evidence(
            "primary_industries",
            primary_industries,
            industries_source_url or (ordered_pages[0].final_url if ordered_pages else ""),
            pages_by_url,
            excerpt=industries_quote or primary_industries,
            official_domain=official_domain,
        )
        evidences["primary_industries"] = [ev]
    if primary_offering_type:
        src = verified_offerings[0]["source_url"] if verified_offerings else (industries_source_url or "")
        ev_pt = build_evidence(
            "primary_offering_type",
            primary_offering_type,
            src,
            pages_by_url,
            excerpt=f"Derived from {len(verified_offerings)} extracted offering(s)",
            official_domain=official_domain,
        )
        evidences["primary_offering_type"] = [ev_pt]
    if verified_offerings:
        ev_list: list[FieldEvidence] = []
        for off in verified_offerings:
            ev_list.append(
                build_evidence(
                    "offerings_data",
                    {"offering_type": off["offering_type"], "name": off["name"]},
                    off["source_url"],
                    pages_by_url,
                    excerpt=off["evidence_quote"],
                    official_domain=official_domain,
                )
            )
        evidences["offerings_data"] = ev_list

    return {
        "primary_industries": primary_industries,
        "primary_offering_type": primary_offering_type,
        "offerings_data": [
            {"offering_type": o["offering_type"], "name": o["name"]}
            for o in verified_offerings
        ],
        "business_summary": business_summary,
        "evidence": evidences,
        "offerings_status": (
            VerificationStatus.VERIFIED.value if verified_offerings
            else VerificationStatus.UNAVAILABLE.value
        ),
        "industries_status": (
            evidences["primary_industries"][0].verification_status.value
            if "primary_industries" in evidences
            else VerificationStatus.UNAVAILABLE.value
        ),
    }
