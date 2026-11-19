"""Transparent source authority classification, field confidence scoring, and Vortexen relevance rubric."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from research.models import (
    CompatibilityLevel,
    FieldEvidence,
    SourceReliability,
    SourceType,
    VerificationStatus,
    VortexenProfile,
    VortexenRelevance,
)

SOURCE_AUTHORITY_WEIGHTS: dict[SourceType, float] = {
    SourceType.OFFICIAL_WEBSITE: 0.95,
    SourceType.GOVERNMENT_REGISTRY: 0.95,
    SourceType.REGULATORY_FILING: 0.95,
    SourceType.ANNUAL_REPORT: 0.92,
    SourceType.STOCK_EXCHANGE: 0.92,
    SourceType.MARKET_INTEL_DB: 0.90,
    SourceType.OFFICIAL_EVENT_WEBSITE: 0.88,
    SourceType.OFFICIAL_PRESS_RELEASE: 0.85,
    SourceType.BUSINESS_DIRECTORY: 0.65,
    SourceType.PROFESSIONAL_PROFILE: 0.65,
    SourceType.INDUSTRY_PUBLICATION: 0.60,
    SourceType.WEB_SEARCH_SNIPPET: 0.35,
    SourceType.UNKNOWN: 0.25,
}

GOV_REGISTRY_HINTS = (
    "sec.gov",
    "companieshouse.gov.uk",
    "mca.gov.in",
    "edgar",
    "gov.",
    ".gov",
    ".gob.",
    ".gouv.",
    ".go.jp",
    "europa.eu",
    "registry",
)

STOCK_EXCHANGE_HINTS = (
    "nasdaq.com",
    "nyse.com",
    "londonstockexchange.com",
    "nseindia.com",
    "bseindia.com",
    "adx.ae",
    "dfm.ae",
    "saudiexchange.sa",
    "tadawul",
)

DIRECTORY_HINTS = (
    "crunchbase.com",
    "bloomberg.com",
    "dnb.com",
    "zoominfo.com",
    "apollo.io",
    "pitchbook.com",
    "zaubacorp.com",
    "tofler.in",
    "opencorporates.com",
    "wikipedia.org",
)

PRESS_HINTS = (
    "prnewswire.com",
    "businesswire.com",
    "globenewswire.com",
    "accesswire.com",
)


def _strip_www(host: str) -> str:
    h = (host or "").strip().lower()
    return h[4:] if h.startswith("www.") else h


def classify_source(
    url: str,
    *,
    official_domain: str | None = None,
    event_domain: str | None = None,
    is_snippet_only: bool = False,
) -> tuple[SourceType, SourceReliability]:
    """Classify URL into Phase-9 source authority hierarchy."""
    if is_snippet_only:
        return SourceType.WEB_SEARCH_SNIPPET, SourceReliability.LOW

    parsed = urlparse((url or "").strip())
    host = _strip_www(parsed.hostname or "")
    path = (parsed.path or "").lower()

    if official_domain and (host == _strip_www(official_domain) or host.endswith("." + _strip_www(official_domain))):
        if "annual-report" in path or "annual_report" in path or "/investors" in path:
            return SourceType.ANNUAL_REPORT, SourceReliability.HIGH
        if "/press" in path or "/news" in path or "/media-release" in path:
            return SourceType.OFFICIAL_PRESS_RELEASE, SourceReliability.HIGH
        return SourceType.OFFICIAL_WEBSITE, SourceReliability.HIGH

    if event_domain and (host == _strip_www(event_domain) or host.endswith("." + _strip_www(event_domain))):
        return SourceType.OFFICIAL_EVENT_WEBSITE, SourceReliability.HIGH

    if any(h in host for h in STOCK_EXCHANGE_HINTS):
        return SourceType.STOCK_EXCHANGE, SourceReliability.HIGH

    if any(h in host for h in GOV_REGISTRY_HINTS):
        if "sec.gov" in host or "edgar" in host:
            return SourceType.REGULATORY_FILING, SourceReliability.HIGH
        return SourceType.GOVERNMENT_REGISTRY, SourceReliability.HIGH

    if "linkedin.com" in host:
        return SourceType.PROFESSIONAL_PROFILE, SourceReliability.MEDIUM

    if any(h in host for h in PRESS_HINTS):
        return SourceType.OFFICIAL_PRESS_RELEASE, SourceReliability.HIGH

    if any(h in host for h in DIRECTORY_HINTS):
        return SourceType.BUSINESS_DIRECTORY, SourceReliability.MEDIUM

    if host:
        return SourceType.INDUSTRY_PUBLICATION, SourceReliability.MEDIUM
    return SourceType.UNKNOWN, SourceReliability.UNVERIFIED


def source_authority_weight(source_type: SourceType) -> float:
    return SOURCE_AUTHORITY_WEIGHTS.get(source_type, 0.25)


def compute_field_confidence(
    evidences: list[FieldEvidence],
    status: VerificationStatus,
) -> float:
    """Deterministic confidence score computed strictly from source authority, corroboration, and status."""
    if status == VerificationStatus.UNAVAILABLE or not evidences:
        return 0.0

    weights = [source_authority_weight(e.source_type) for e in evidences]
    best = max(weights)
    distinct_domains = {
        _strip_www(urlparse(e.source_url).hostname or "")
        for e in evidences
        if e.source_url
    }
    corroboration_bonus = min(0.05, 0.025 * max(0, len(distinct_domains) - 1))
    score = min(0.99, best + corroboration_bonus)

    if status == VerificationStatus.PARTIALLY_VERIFIED:
        score = min(score, 0.72)
    elif status == VerificationStatus.REQUIRES_REVIEW:
        score = min(score, 0.55)
    elif status == VerificationStatus.CONFLICTING:
        score = min(score * 0.65, 0.55)

    return round(score, 3)


_STOPWORDS = frozenset({
    "and", "or", "the", "for", "with", "from", "into", "across", "solutions",
    "services", "products", "systems", "industrial", "company", "limited", "ltd",
    "inc", "llc", "providing", "provider", "specialized", "specialised", "support",
})


def _keyword_tokens(items: list[str] | str) -> set[str]:
    if isinstance(items, str):
        text = items
    else:
        text = " ".join(items)
    tokens = {
        t for t in re.findall(r"[a-z0-9]+", text.casefold())
        if len(t) >= 3 and t not in _STOPWORDS
    }
    return tokens


def _overlap_terms(company_tokens: set[str], catalog: list[str]) -> list[str]:
    matched: list[str] = []
    for entry in catalog:
        entry_tokens = _keyword_tokens(entry)
        if not entry_tokens:
            continue
        common = company_tokens & entry_tokens
        if len(common) >= 1 and (len(common) / len(entry_tokens) >= 0.34 or len(common) >= 2):
            matched.append(entry)
    return matched


def score_vortexen_relevance(
    company_data: dict[str, Any],
    profile: VortexenProfile | None,
) -> VortexenRelevance:
    """Evaluate alignment between researched company facts and the cached Vortexen profile."""
    if profile is None or not (
        profile.services or profile.products or profile.solutions or profile.primary_industries or profile.target_industries
    ):
        return VortexenRelevance(
            compatibility=CompatibilityLevel.INSUFFICIENT_DATA,
            relevance_score=None,
            possible_business_relevance="Vortexen business profile is not yet available or empty.",
            unverified_assumptions_or_gaps=["Run refresh_vortexen_profile to crawl https://vortexen.com/."],
        )

    industries_raw = str(company_data.get("primary_industries") or "").strip()
    offerings_raw = [
        str(o.get("name") or "").strip()
        for o in (company_data.get("offerings_data") or [])
        if isinstance(o, dict) and o.get("name")
    ]
    country_raw = str(company_data.get("country_head_office") or "").strip()
    overview_raw = str(company_data.get("_business_summary") or "").strip()

    gaps: list[str] = []
    if not industries_raw:
        gaps.append("Primary industries for the researched company are unavailable.")
    if not offerings_raw:
        gaps.append("Verified company products/services/solutions are unavailable.")

    if not industries_raw and not offerings_raw:
        return VortexenRelevance(
            compatibility=CompatibilityLevel.INSUFFICIENT_DATA,
            relevance_score=None,
            possible_business_relevance="Insufficient verified industry and offering evidence to assess Vortexen fit.",
            unverified_assumptions_or_gaps=gaps,
            vortexen_profile_version=profile.version,
            vortexen_profile_hash=profile.content_hash,
        )

    company_ind_tokens = _keyword_tokens([industries_raw, overview_raw])
    company_off_tokens = _keyword_tokens(offerings_raw + [overview_raw, industries_raw])

    vortexen_industries = list(dict.fromkeys(profile.primary_industries + profile.target_industries))
    matched_industries = _overlap_terms(company_ind_tokens | company_off_tokens, vortexen_industries)
    matched_services = _overlap_terms(company_off_tokens | company_ind_tokens, profile.services + profile.solutions)
    matched_products = _overlap_terms(company_off_tokens | company_ind_tokens, profile.products)

    geo_match = False
    if country_raw and profile.geographic_footprint:
        c_low = country_raw.casefold()
        geo_match = any(c_low in g.casefold() or g.casefold() in c_low for g in profile.geographic_footprint)

    ind_score = min(35.0, round(len(matched_industries) * 17.5, 2))
    srv_score = min(40.0, round(len(matched_services) * 15.0, 2))
    prd_score = min(15.0, round(len(matched_products) * 7.5, 2))
    geo_score = 10.0 if geo_match else 0.0
    total = round(ind_score + srv_score + prd_score + geo_score, 2)

    if total >= 65.0:
        level = CompatibilityLevel.HIGH
    elif total >= 35.0:
        level = CompatibilityLevel.MEDIUM
    else:
        level = CompatibilityLevel.LOW

    evidence_notes: list[str] = []
    if matched_industries:
        evidence_notes.append(f"Matched industry focus: {', '.join(matched_industries[:5])}")
    if matched_services:
        evidence_notes.append(f"Aligned Vortexen services/solutions: {', '.join(matched_services[:5])}")
    if matched_products:
        evidence_notes.append(f"Aligned Vortexen products/materials: {', '.join(matched_products[:5])}")
    if geo_match:
        evidence_notes.append(f"Geographic footprint overlap in {country_raw}")
    if not evidence_notes:
        evidence_notes.append("No direct keyword overlap found between company evidence and Vortexen catalog.")

    if not country_raw:
        gaps.append("Company headquarters country is unverified.")
    gaps.append("Actual buyer project requirements or procurement needs must be confirmed separately.")

    summary = (
        f"Compatibility is {level.value} (score {total}/100) based on "
        f"{len(matched_industries)} industry match(es), {len(matched_services)} service/solution match(es), "
        f"and {len(matched_products)} product match(es)."
    )

    return VortexenRelevance(
        compatibility=level,
        relevance_score=total,
        score_breakdown={
            "industry_alignment": ind_score,
            "services_solutions_overlap": srv_score,
            "products_materials_overlap": prd_score,
            "geographic_overlap": geo_score,
        },
        relevant_vortexen_services=matched_services[:10],
        matching_products_or_solutions=list(dict.fromkeys(matched_products + matched_services))[:10],
        matched_industries=matched_industries[:10],
        supporting_evidence=evidence_notes,
        possible_business_relevance=summary,
        unverified_assumptions_or_gaps=gaps,
        vortexen_profile_version=profile.version,
        vortexen_profile_hash=profile.content_hash,
    )
