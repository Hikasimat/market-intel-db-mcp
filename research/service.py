"""Main orchestration facade for read-only Company Research, Event Research, Cache Status, and Vortexen Profile."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from config import Settings
from research.cache import ResearchCache, SQLiteResearchCache, make_company_key
from research.company_identity import resolve_company_identity
from research.extractor import ALL_SECTIONS, extract_all_sections
from research.fetcher import SafeFetcher, discover_priority_links
from research.market_events import EventResearcher
from research.models import (
    CompanyResearchResult,
    FetchedPage,
    ResearchBudget,
    VerificationStatus,
)
from research.provider_router import ProviderRouter
from research.search import SearchClient
from research.security import SSRFBlockedError, UnsafeContentError
from research.vortexen_profile import VortexenProfileManager

log = logging.getLogger("prospect_mcp.research.service")


class ResearchService:
    """Read-only, blueprint-driven company and event research engine."""

    def __init__(
        self,
        client: Any,
        workflow: Any,
        settings: Settings,
        *,
        cache: ResearchCache | None = None,
        fetcher: SafeFetcher | None = None,
        search_client: SearchClient | None = None,
        router: ProviderRouter | None = None,
        vortexen_manager: VortexenProfileManager | None = None,
    ) -> None:
        self.client = client
        self.workflow = workflow
        self.settings = settings

        self._owns_cache = cache is None
        self.cache: ResearchCache = cache or SQLiteResearchCache(
            settings.research_cache_db_path or "research_cache.db"
        )
        self._owns_fetcher = fetcher is None
        self.fetcher = fetcher or SafeFetcher(settings, cache=self.cache)
        self._owns_search = search_client is None
        self.search_client = search_client or SearchClient(settings)
        self._owns_router = router is None
        self.router = router or ProviderRouter(settings, cache=self.cache)
        self.vortexen = vortexen_manager or VortexenProfileManager(
            settings,
            self.fetcher,
            cache=self.cache,
            router=self.router,
        )

    async def aclose(self) -> None:
        if self._owns_fetcher:
            await self.fetcher.aclose()
        if self._owns_search:
            await self.search_client.aclose()
        if self._owns_router:
            await self.router.aclose()

    def _record_usage_to_cache(self, job_id: str, job_type: str, target_name: str, summary: Any) -> None:
        try:
            for key, prov in (summary.by_provider or {}).items():
                self.cache.record_usage(
                    job_id=job_id,
                    job_type=job_type,
                    target_name=target_name,
                    provider=str(prov.get("provider") or "none"),
                    model=str(prov.get("model") or "none"),
                    input_tokens=int(prov.get("input_tokens") or 0),
                    output_tokens=int(prov.get("output_tokens") or 0),
                    requests=int(prov.get("requests") or 0),
                    cache_hits=int(prov.get("cache_hits") or 0) + int(summary.field_cache_hits),
                    search_calls=int(summary.search_calls),
                    pages_fetched=int(summary.pages_fetched),
                    elapsed_ms=int(summary.elapsed_ms),
                    reported_cost_usd=prov.get("reported_cost_usd"),
                    estimated_cost_usd=prov.get("estimated_cost_usd"),
                )
            if not summary.by_provider:
                self.cache.record_usage(
                    job_id=job_id,
                    job_type=job_type,
                    target_name=target_name,
                    provider="deterministic_only",
                    model="none",
                    input_tokens=0,
                    output_tokens=0,
                    requests=0,
                    cache_hits=int(summary.field_cache_hits) + int(summary.source_cache_hits),
                    search_calls=int(summary.search_calls),
                    pages_fetched=int(summary.pages_fetched),
                    elapsed_ms=int(summary.elapsed_ms),
                    reported_cost_usd=None,
                    estimated_cost_usd=0.0,
                )
        except Exception as exc:
            log.warning("Failed recording usage ledger (%s)", type(exc).__name__)

    async def research_company(
        self,
        company_name: str,
        *,
        country: str | None = None,
        official_website: str | None = None,
        sections: list[str] | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Execute full read-only six-section research for one company."""
        t0 = time.monotonic()
        clean_name = " ".join((company_name or "").split())
        if not clean_name or len(clean_name) > 200:
            return {"ok": False, "read_only": True, "errors": ["company_name must be 1-200 characters."]}

        # Enforce Market Intel PRE role authentication before accessing Market Intel lookups.
        await self.workflow._identity()

        # Step 1: Discover authoritative Prospect Blueprint.
        blueprint_resp = self.workflow.get_prospect_blueprint()
        blueprint = blueprint_resp.get("blueprint", blueprint_resp)
        blueprint_version = str(blueprint.get("version") or "1.0")

        req_sections = set(ALL_SECTIONS)
        if sections:
            norm_req = {s.strip().lower() for s in sections if isinstance(s, str) and s.strip()}
            valid_req = norm_req & set(ALL_SECTIONS)
            if valid_req:
                # Always ensure general_information or market_offerings context is coherent.
                req_sections = valid_req

        budget = ResearchBudget(
            max_llm_calls=self.settings.research_max_llm_calls_per_company,
            max_total_tokens=self.settings.research_max_tokens_per_company,
            max_search_calls=6,
            max_pages_fetched=self.settings.research_max_pages_per_company,
        )

        # Step 2 & 3: Company Identity & Market Intel Duplicate Lookup.
        identity = await resolve_company_identity(
            clean_name,
            country=country,
            official_website=official_website,
            api_client=self.client,
            search_client=self.search_client,
            settings=self.settings,
            cache=self.cache,
            budget=budget,
            force_refresh=force_refresh,
        )

        resolved_website = identity.get("official_website")
        resolved_domain = identity.get("official_domain")
        warnings: list[str] = []
        if identity.get("ambiguity_detected"):
            warnings.append(
                "Multiple distinct company websites matched this name; verify official_website_url against website_candidates."
            )

        # Step 4 & 5: Crawl official website (offerings/industries priority) + authoritative search pages.
        pages: list[FetchedPage] = []
        visited_urls: set[str] = set()

        async def _fetch_safe(url: str) -> FetchedPage | None:
            norm = url.rstrip("/")
            if norm in visited_urls or not budget.can_fetch_page():
                return None
            visited_urls.add(norm)
            try:
                p = await self.fetcher.fetch_page(
                    url,
                    official_domain=resolved_domain,
                    budget=budget,
                    force_refresh=force_refresh,
                )
                if p.robots_disallowed:
                    warnings.append(f"Skipped {url}: disallowed by robots.txt.")
                    return None
                if p.status_code >= 400 or not p.text:
                    return None
                if p.injection_warnings:
                    warnings.append(f"Prompt-injection text patterns neutralized in {p.final_url}.")
                pages.append(p)
                return p
            except (SSRFBlockedError, UnsafeContentError) as exc:
                warnings.append(f"Skipped source {url}: {exc}")
                return None

        if resolved_website:
            home = await _fetch_safe(resolved_website)
            if home is not None:
                priority_links = discover_priority_links(
                    home,
                    same_domain_only=True,
                    categories=("offerings", "industries", "corporate", "ownership", "contacts"),
                    max_links=max(1, self.settings.research_max_pages_per_company - 2),
                )
                for link in priority_links:
                    if not budget.can_fetch_page():
                        break
                    await _fetch_safe(link)

        # Supplement with high-authority search pages (filings/registries/directories) while budget remains.
        for s_res in identity.get("search_results") or []:
            if len(pages) >= self.settings.research_max_pages_per_company or not budget.can_fetch_page():
                break
            url = s_res.get("url")
            if url and not any(b in str(url).lower() for b in ("linkedin.com", "facebook.com")):
                await _fetch_safe(url)

        if not pages:
            warnings.append("No authoritative web pages could be fetched; returned only verified Market Intel / unavailable fields.")

        # Step 6–9: Prioritized 6-section extraction, fact verification, and lookup resolution.
        extracted = await extract_all_sections(
            clean_name,
            company_key=identity["company_key"],
            country_hint=country,
            official_website=resolved_website,
            official_domain=resolved_domain,
            pages=pages,
            requested_sections=req_sections,
            api_client=self.client,
            router=self.router,
            cache=self.cache,
            settings=self.settings,
            budget=budget,
            force_refresh=force_refresh,
        )
        warnings.extend(extracted.get("warnings") or [])
        prospect_candidate = dict(extracted.get("prospect_fields") or {})

        # Step 10: Dynamic Vortexen profile & prospect qualification.
        vx_profile = await self.vortexen.get_profile(force_refresh=False, budget=budget)
        vx_rel = self.vortexen.evaluate_company(
            {**prospect_candidate, "_business_summary": extracted.get("business_summary", "")},
            vx_profile,
        )

        # Identify missing required and optional blueprint fields honestly.
        required_bp_fields = (
            "company_name",
            "country_head_office",
            "complete_address",
            "primary_industries",
            "ownership_sector",
            "company_structure",
            "operational_status",
            "primary_offering_type",
        )
        optional_bp_fields = (
            "official_phone_number",
            "official_email_address",
            "official_website_url",
            "linkedin_company_page",
            "offerings_data",
            "key_contacts",
            "shareholdings",
            "market_event_ids",
        )
        missing_fields = [
            f for f in required_bp_fields + optional_bp_fields
            if prospect_candidate.get(f) in (None, "", [])
        ]

        evidence_map = extracted.get("evidence") or {}
        verified_count = sum(
            1 for evs in evidence_map.values()
            if evs and evs[0].verification_status == VerificationStatus.VERIFIED
        )
        conflicting_count = len(extracted.get("conflicts") or [])
        quality_summary = {
            "pages_fetched_count": len(pages),
            "verified_fields_count": verified_count,
            "conflicting_fields_count": conflicting_count,
            "missing_required_fields": [f for f in required_bp_fields if f in missing_fields],
            "ready_for_validation": not any(f in missing_fields for f in required_bp_fields),
            "llm_providers_enabled": self.router.enabled_providers(),
            "search_provider_active": self.search_client.active_provider(),
        }

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        usage_summary = budget.to_summary(elapsed_ms=elapsed_ms)
        self._record_usage_to_cache(str(uuid.uuid4()), "company_research", clean_name, usage_summary)

        result = CompanyResearchResult(
            ok=True,
            read_only=True,
            blueprint_version=blueprint_version,
            company_query=clean_name,
            company_identity={
                k: v for k, v in identity.items()
                if k not in ("duplicate_matches", "search_results")
            },
            prospect_candidate=prospect_candidate,
            sections=extracted.get("sections") or {},
            evidence=evidence_map,
            conflicts=extracted.get("conflicts") or [],
            missing_fields=missing_fields,
            missing_prerequisites=extracted.get("missing_prerequisites") or {},
            duplicate_matches=identity.get("duplicate_matches") or {},
            contact_candidates=extracted.get("contact_candidates") or [],
            vortexen_relevance=vx_rel,
            quality_summary=quality_summary,
            sources_consulted=[
                {
                    "url": p.final_url,
                    "title": p.title,
                    "source_type": p.source_type.value,
                    "source_reliability": p.source_reliability.value,
                    "content_hash": p.content_hash,
                }
                for p in pages
            ],
            warnings=list(dict.fromkeys(warnings)),
            usage_summary=usage_summary,
        )
        return result.model_dump(mode="json")

    async def research_event(
        self,
        event_name: str,
        *,
        country: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_website: str | None = None,
        max_participants: int = 10,
        research_companies: bool = True,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Execute read-only Market Event verification, participant extraction, and company research."""
        t0 = time.monotonic()
        clean_name = " ".join((event_name or "").split())
        if not clean_name or len(clean_name) > 200:
            return {"ok": False, "read_only": True, "errors": ["event_name must be 1-200 characters."]}

        await self.workflow._identity()

        researcher = EventResearcher(
            self.settings,
            self.client,
            self.fetcher,
            self.search_client,
            self.router,
        )

        async def _research_participant(**kwargs: Any) -> dict[str, Any]:
            return await self.research_company(**kwargs)

        res = await researcher.research_event(
            clean_name,
            country=country,
            start_date=start_date,
            end_date=end_date,
            event_website=event_website,
            max_participants=max_participants,
            research_companies=research_companies,
            force_refresh=force_refresh,
            company_research_cb=_research_participant,
        )
        res.usage_summary.elapsed_ms = int((time.monotonic() - t0) * 1000)
        self._record_usage_to_cache(str(uuid.uuid4()), "event_research", clean_name, res.usage_summary)
        return res.model_dump(mode="json")

    async def get_cache_status(
        self,
        *,
        company_name: str | None = None,
        country: str | None = None,
    ) -> dict[str, Any]:
        """Return read-only research cache availability, freshness, and token-usage summary."""
        await self.workflow._identity()
        ck = make_company_key(company_name, country) if company_name else None
        status = self.cache.get_status(company_key=ck)
        status["enabled_ai_providers"] = self.router.enabled_providers()
        status["active_search_provider"] = self.search_client.active_provider()
        return status

    async def refresh_vortexen_profile(self, *, force: bool = True) -> dict[str, Any]:
        """Read-only refresh of Vortexen profile from https://vortexen.com/. Never touches prospect records."""
        await self.workflow._identity()
        budget = ResearchBudget(max_llm_calls=2, max_total_tokens=10_000, max_search_calls=0, max_pages_fetched=12)
        profile = await self.vortexen.refresh_profile(force=force, budget=budget)
        if profile is None:
            return {
                "ok": False,
                "read_only": True,
                "errors": [f"Could not retrieve Vortexen website content from {self.settings.vortexen_website_url}."],
                "usage_summary": budget.to_summary().model_dump(mode="json"),
            }
        return {
            "ok": True,
            "read_only": True,
            "profile": profile.model_dump(mode="json"),
            "usage_summary": budget.to_summary().model_dump(mode="json"),
            "note": "Refreshed Vortexen profile cache only. No Market Intel database records were created or modified.",
        }
