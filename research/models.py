"""Pydantic models and controlled enums for the Market Intel research engine."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICTING = "CONFLICTING"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


class SourceType(str, Enum):
    OFFICIAL_WEBSITE = "OFFICIAL_WEBSITE"
    GOVERNMENT_REGISTRY = "GOVERNMENT_REGISTRY"
    REGULATORY_FILING = "REGULATORY_FILING"
    ANNUAL_REPORT = "ANNUAL_REPORT"
    STOCK_EXCHANGE = "STOCK_EXCHANGE"
    OFFICIAL_EVENT_WEBSITE = "OFFICIAL_EVENT_WEBSITE"
    OFFICIAL_PRESS_RELEASE = "OFFICIAL_PRESS_RELEASE"
    MARKET_INTEL_DB = "MARKET_INTEL_DB"
    BUSINESS_DIRECTORY = "BUSINESS_DIRECTORY"
    PROFESSIONAL_PROFILE = "PROFESSIONAL_PROFILE"
    INDUSTRY_PUBLICATION = "INDUSTRY_PUBLICATION"
    WEB_SEARCH_SNIPPET = "WEB_SEARCH_SNIPPET"
    UNKNOWN = "UNKNOWN"


class SourceReliability(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNVERIFIED = "UNVERIFIED"


class CompatibilityLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class _ResearchModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class FieldEvidence(_ResearchModel):
    field_name: str
    value: Any = None
    source_title: str = ""
    source_url: str = ""
    source_type: SourceType = SourceType.UNKNOWN
    source_reliability: SourceReliability = SourceReliability.UNVERIFIED
    publication_date: str | None = None
    retrieved_at: str = Field(default_factory=utc_now_iso)
    excerpt: str = ""
    verification_status: VerificationStatus = VerificationStatus.UNAVAILABLE
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    conflict_notes: list[str] = Field(default_factory=list)


class ConflictRecord(_ResearchModel):
    field_name: str
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    resolution_status: Literal[
        "RESOLVED_BY_AUTHORITY",
        "RESOLVED_BY_RECENCY",
        "UNRESOLVED_CONFLICT",
    ] = "UNRESOLVED_CONFLICT"
    chosen_value: Any | None = None
    rationale: str = ""


class FetchedPage(_ResearchModel):
    url: str
    final_url: str
    domain: str
    status_code: int = 200
    content_type: str = "text/html"
    title: str = ""
    meta_description: str = ""
    publication_date: str | None = None
    text: str = ""
    headings: list[str] = Field(default_factory=list)
    links: list[dict[str, str]] = Field(default_factory=list)
    json_ld: list[dict[str, Any]] = Field(default_factory=list)
    tables: list[list[list[str]]] = Field(default_factory=list)
    emails_found: list[str] = Field(default_factory=list)
    phones_found: list[str] = Field(default_factory=list)
    linkedin_urls: list[str] = Field(default_factory=list)
    content_hash: str = ""
    retrieved_at: str = Field(default_factory=utc_now_iso)
    source_type: SourceType = SourceType.UNKNOWN
    source_reliability: SourceReliability = SourceReliability.UNVERIFIED
    injection_warnings: list[str] = Field(default_factory=list)
    robots_disallowed: bool = False
    fetch_error: str | None = None


class SearchResult(_ResearchModel):
    title: str
    url: str
    snippet: str = ""
    domain: str = ""
    provider: str = ""
    published_date: str | None = None
    source_type: SourceType = SourceType.WEB_SEARCH_SNIPPET
    authority_rank: int = 0


class CandidateContact(_ResearchModel):
    prefix: str | None = None
    contact_name: str
    designation: str | None = None
    official_email: str | None = None
    phone_number: str | None = None
    linkedin_profile: str | None = None
    source_url: str = ""
    source_title: str = ""
    source_type: SourceType = SourceType.UNKNOWN
    excerpt: str = ""
    company_verified: bool = False
    email_verification_status: str = "UNCHECKED"
    email_verification_reason: str = ""
    included_in_payload: bool = False
    exclusion_reason: str | None = None


class VortexenProfile(_ResearchModel):
    company_name: str = "Vortexen"
    website_url: str = "https://vortexen.com/"
    overview: str = ""
    services: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    solutions: list[str] = Field(default_factory=list)
    technology_capabilities: list[str] = Field(default_factory=list)
    primary_industries: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)
    target_customers: list[str] = Field(default_factory=list)
    business_capabilities: list[str] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)
    specialized_categories: list[str] = Field(default_factory=list)
    geographic_footprint: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    retrieved_at: str = Field(default_factory=utc_now_iso)
    content_hash: str = ""
    version: int = 1
    extraction_method: str = "deterministic"


class VortexenRelevance(_ResearchModel):
    compatibility: CompatibilityLevel = CompatibilityLevel.INSUFFICIENT_DATA
    relevance_score: float | None = None
    scoring_method: str = (
        "Deterministic rubric (0-100): Industry alignment (0-35), Services/Solutions overlap (0-40), "
        "Products/Materials overlap (0-15), Geographic/Market overlap (0-10). Score is omitted when "
        "company industries and offerings are both unavailable or Vortexen profile has not been loaded."
    )
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    relevant_vortexen_services: list[str] = Field(default_factory=list)
    matching_products_or_solutions: list[str] = Field(default_factory=list)
    matched_industries: list[str] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)
    possible_business_relevance: str = ""
    unverified_assumptions_or_gaps: list[str] = Field(default_factory=list)
    vortexen_profile_version: int | None = None
    vortexen_profile_hash: str | None = None


class TokenUsageRecord(_ResearchModel):
    provider: str
    model: str
    stage: str = "primary"
    task_type: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit: bool = False
    latency_ms: int = 0
    reported_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    cost_basis: Literal["provider_reported", "estimated", "zero_cache", "unavailable"] = "unavailable"


class UsageSummary(_ResearchModel):
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    llm_requests: int = 0
    llm_cache_hits: int = 0
    field_cache_hits: int = 0
    source_cache_hits: int = 0
    search_calls: int = 0
    pages_fetched: int = 0
    elapsed_ms: int = 0
    budget_exhausted: bool = False
    reported_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    by_provider: dict[str, dict[str, Any]] = Field(default_factory=dict)
    records: list[TokenUsageRecord] = Field(default_factory=list)


class ResearchBudget:
    """Bounded per-job resource & token budget controller."""

    def __init__(
        self,
        max_llm_calls: int = 6,
        max_total_tokens: int = 25_000,
        max_search_calls: int = 6,
        max_pages_fetched: int = 8,
    ):
        self.max_llm_calls = max_llm_calls
        self.max_total_tokens = max_total_tokens
        self.max_search_calls = max_search_calls
        self.max_pages_fetched = max_pages_fetched

        self.llm_calls = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.search_calls = 0
        self.pages_fetched = 0
        self.llm_cache_hits = 0
        self.field_cache_hits = 0
        self.source_cache_hits = 0
        self.budget_exhausted = False
        self.records: list[TokenUsageRecord] = []

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    def can_call_llm(self) -> bool:
        if self.llm_calls >= self.max_llm_calls or self.total_tokens >= self.max_total_tokens:
            self.budget_exhausted = True
            return False
        return True

    def can_search(self) -> bool:
        if self.search_calls >= self.max_search_calls:
            return False
        return True

    def can_fetch_page(self) -> bool:
        if self.pages_fetched >= self.max_pages_fetched:
            return False
        return True

    def record_search(self) -> None:
        self.search_calls += 1

    def record_page_fetch(self, cache_hit: bool = False) -> None:
        if cache_hit:
            self.source_cache_hits += 1
        else:
            self.pages_fetched += 1

    def record_field_cache_hit(self) -> None:
        self.field_cache_hits += 1

    def record_llm_usage(self, rec: TokenUsageRecord) -> None:
        self.records.append(rec)
        if rec.cache_hit:
            self.llm_cache_hits += 1
        else:
            self.llm_calls += 1
            self.total_input_tokens += rec.input_tokens
            self.total_output_tokens += rec.output_tokens
            if self.total_tokens >= self.max_total_tokens or self.llm_calls >= self.max_llm_calls:
                self.budget_exhausted = True

    def to_summary(self, elapsed_ms: int = 0) -> UsageSummary:
        by_prov: dict[str, dict[str, Any]] = {}
        reported_total: float | None = None
        estimated_total: float | None = None
        for r in self.records:
            p = by_prov.setdefault(
                f"{r.provider}:{r.model}",
                {
                    "provider": r.provider,
                    "model": r.model,
                    "requests": 0,
                    "cache_hits": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reported_cost_usd": None,
                    "estimated_cost_usd": None,
                },
            )
            if r.cache_hit:
                p["cache_hits"] += 1
            else:
                p["requests"] += 1
                p["input_tokens"] += r.input_tokens
                p["output_tokens"] += r.output_tokens
            if r.reported_cost_usd is not None:
                reported_total = (reported_total or 0.0) + r.reported_cost_usd
                p["reported_cost_usd"] = (p["reported_cost_usd"] or 0.0) + r.reported_cost_usd
            if r.estimated_cost_usd is not None:
                estimated_total = (estimated_total or 0.0) + r.estimated_cost_usd
                p["estimated_cost_usd"] = (p["estimated_cost_usd"] or 0.0) + r.estimated_cost_usd

        return UsageSummary(
            total_input_tokens=self.total_input_tokens,
            total_output_tokens=self.total_output_tokens,
            total_tokens=self.total_tokens,
            llm_requests=self.llm_calls,
            llm_cache_hits=self.llm_cache_hits,
            field_cache_hits=self.field_cache_hits,
            source_cache_hits=self.source_cache_hits,
            search_calls=self.search_calls,
            pages_fetched=self.pages_fetched,
            elapsed_ms=elapsed_ms,
            budget_exhausted=self.budget_exhausted,
            reported_cost_usd=round(reported_total, 8) if reported_total is not None else None,
            estimated_cost_usd=round(estimated_total, 8) if estimated_total is not None else None,
            by_provider=by_prov,
            records=list(self.records),
        )


class CompanyResearchResult(_ResearchModel):
    ok: bool = True
    read_only: bool = True
    blueprint_version: str = "1.0"
    company_query: str
    company_identity: dict[str, Any] = Field(default_factory=dict)
    prospect_candidate: dict[str, Any] = Field(default_factory=dict)
    sections: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, list[FieldEvidence]] = Field(default_factory=dict)
    conflicts: list[ConflictRecord] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    missing_prerequisites: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    duplicate_matches: dict[str, Any] = Field(default_factory=dict)
    contact_candidates: list[CandidateContact] = Field(default_factory=list)
    vortexen_relevance: VortexenRelevance = Field(default_factory=VortexenRelevance)
    quality_summary: dict[str, Any] = Field(default_factory=dict)
    sources_consulted: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    usage_summary: UsageSummary = Field(default_factory=UsageSummary)
    next_step: str = (
        "READ-ONLY RESEARCH COMPLETE. No database record was created or modified. "
        "Review evidence, resolve missing prerequisites/fields, then call validate_prospect_data "
        "and obtain explicit user approval before calling create_confirmed_prospect."
    )


class EventResearchResult(_ResearchModel):
    ok: bool = True
    read_only: bool = True
    event_query: str
    event_details: dict[str, Any] = Field(default_factory=dict)
    event_evidence: list[FieldEvidence] = Field(default_factory=list)
    existing_market_event_matches: dict[str, Any] = Field(default_factory=dict)
    participants_discovered: list[dict[str, Any]] = Field(default_factory=list)
    participant_companies_researched: list[dict[str, Any]] = Field(default_factory=list)
    access_limitations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    usage_summary: UsageSummary = Field(default_factory=UsageSummary)
    next_step: str = (
        "READ-ONLY EVENT RESEARCH COMPLETE. No database records were created or modified. "
        "If the event is missing from Market Intel, validate and create it with user approval first, "
        "then validate and approve each desired prospect individually."
    )
