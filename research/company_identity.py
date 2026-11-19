"""Company identity resolution, official domain detection, disambiguation, and Market Intel duplicate checking."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from api_client import ApiError
from config import Settings
from research.cache import ResearchCache, make_company_key
from research.models import ResearchBudget, SearchResult
from research.search import SearchClient
from research.security import SSRFBlockedError, validate_url
from validation import find_duplicates

LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "pvt", "private", "plc", "public",
    "corp", "corporation", "co", "company", "gmbh", "ag", "sa", "sarl", "bv", "nv",
    "pjsc", "psc", "jsc", "kk", "pty", "fze", "fzc", "fzco", "lp", "llp",
})

NON_OFFICIAL_DOMAINS = frozenset({
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "wikipedia.org", "wikidata.org", "crunchbase.com", "bloomberg.com",
    "reuters.com", "forbes.com", "dnb.com", "zoominfo.com", "apollo.io",
    "glassdoor.com", "indeed.com", "opencorporates.com", "zaubacorp.com",
    "tofler.in", "pitchbook.com", "sec.gov", "companieshouse.gov.uk",
})


def normalize_company_name(name: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", (name or "").casefold())
    core = [t for t in tokens if t not in LEGAL_SUFFIXES]
    return " ".join(core or tokens)


def extract_domain(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _is_non_official_domain(domain: str) -> bool:
    return any(domain == bad or domain.endswith("." + bad) for bad in NON_OFFICIAL_DOMAINS)


def _rank_official_website(
    company_name: str,
    country: str | None,
    results: list[SearchResult],
) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    """Select best official company website candidate and flag multi-entity ambiguity."""
    norm_tokens = [t for t in normalize_company_name(company_name).split() if len(t) >= 2]
    candidates: list[tuple[int, SearchResult, str]] = []

    for r in results:
        domain = r.domain
        if not domain or _is_non_official_domain(domain):
            continue
        score = 0
        sld = domain.split(".", 1)[0]
        title_low = r.title.casefold()
        snip_low = r.snippet.casefold()
        for tok in norm_tokens:
            if tok in sld:
                score += 40
            elif tok in domain:
                score += 25
            if tok in title_low:
                score += 15
            if tok in snip_low:
                score += 5
        if country and country.casefold() in (title_low + " " + snip_low):
            score += 15
        path = urlparse(r.url).path
        if path in ("", "/"):
            score += 12
        if "official" in title_low:
            score += 5
        if score > 0:
            candidates.append((score, r, domain))

    if not candidates:
        return None, None, []

    candidates.sort(key=lambda x: (-x[0], len(x[1].url)))
    top_score, top_res, top_domain = candidates[0]
    parsed = urlparse(top_res.url)
    root_url = f"{parsed.scheme}://{parsed.netloc}/"

    # Detect ambiguity if distinct non-subsidiary domains score close to the top candidate.
    distinct: list[dict[str, Any]] = []
    seen_doms: set[str] = set()
    for sc, res, dom in candidates:
        if dom in seen_doms:
            continue
        seen_doms.add(dom)
        distinct.append({"domain": dom, "url": res.url, "title": res.title, "score": sc})

    return root_url, top_domain, distinct


async def resolve_company_identity(
    company_name: str,
    *,
    country: str | None = None,
    official_website: str | None = None,
    api_client: Any,
    search_client: SearchClient,
    settings: Settings,
    cache: ResearchCache | None = None,
    budget: ResearchBudget | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Resolve canonical company identity, official domain/URL, and Market Intel duplicates."""
    clean_name = " ".join((company_name or "").split())
    clean_country = " ".join((country or "").split())
    comp_key = make_company_key(clean_name, clean_country or None)

    # 1. Existing Market Intel prospect duplicate lookup (always live-checked to avoid stale DB duplicates).
    exact_dups: list[dict[str, Any]] = []
    similar_dups: list[dict[str, Any]] = []
    dup_error: str | None = None
    try:
        resp = await api_client.list_prospects({"search": clean_name, "page_size": 25})
        recs = resp.get("results", []) if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])
        recs = [r for r in recs if isinstance(r, dict)]
        exact_dups, similar_dups = find_duplicates(clean_name, recs)
    except ApiError as exc:
        dup_error = f"Could not search existing prospects: {exc}"

    # 2. Check identity cache.
    cached_ident = None if force_refresh or not cache else cache.get_company_identity(comp_key)
    if cached_ident is not None:
        return {
            **cached_ident,
            "cache_hit": True,
            "duplicate_matches": {
                "exact_matches": exact_dups,
                "similar_matches": similar_dups,
                "error": dup_error,
            },
        }

    # 3. Official website resolution.
    resolved_website: str | None = None
    resolved_domain: str | None = None
    website_candidates: list[dict[str, Any]] = []
    search_hits: list[SearchResult] = []
    ambiguity_detected = False

    if official_website and official_website.strip():
        try:
            resolved_website = validate_url(official_website.strip())
            resolved_domain = extract_domain(resolved_website)
        except SSRFBlockedError:
            resolved_website = None
            resolved_domain = None

    # Search web for official website and authoritative filings/pages.
    q_parts = [f'"{clean_name}"']
    if clean_country:
        q_parts.append(clean_country)
    q_parts.append("official website products services")
    search_hits = await search_client.search(
        " ".join(q_parts),
        max_results=8,
        budget=budget,
        official_domain=resolved_domain,
    )

    if resolved_website is None and search_hits:
        resolved_website, resolved_domain, website_candidates = _rank_official_website(
            clean_name, clean_country or None, search_hits
        )
        if len(website_candidates) >= 2:
            top_s = website_candidates[0]["score"]
            sec_s = website_candidates[1]["score"]
            if sec_s >= top_s * 0.85:
                ambiguity_detected = True

    ident_data = {
        "company_key": comp_key,
        "canonical_name": clean_name,
        "normalized_name": normalize_company_name(clean_name),
        "country_hint": clean_country or None,
        "official_website": resolved_website,
        "official_domain": resolved_domain,
        "ambiguity_detected": ambiguity_detected,
        "website_candidates": website_candidates[:5],
        "search_results": [s.model_dump(mode="json") for s in search_hits],
        "market_intel_existing_id": str(exact_dups[0]["id"]) if exact_dups and exact_dups[0].get("id") else None,
        "cache_hit": False,
    }

    if cache is not None and resolved_website:
        cache.set_company_identity(
            comp_key,
            clean_name,
            clean_country,
            resolved_domain,
            resolved_website,
            ident_data["market_intel_existing_id"],
            ident_data,
            settings.research_field_ttl_seconds,
        )

    return {
        **ident_data,
        "duplicate_matches": {
            "exact_matches": exact_dups,
            "similar_matches": similar_dups,
            "error": dup_error,
        },
    }
