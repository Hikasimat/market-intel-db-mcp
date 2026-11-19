"""Dynamic https://vortexen.com/ business profile builder, caching, refresh, and prospect alignment."""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from config import Settings
from research.cache import ResearchCache, sha256_text
from research.company_identity import extract_domain
from research.fetcher import SafeFetcher, discover_priority_links
from research.models import FetchedPage, ResearchBudget, VortexenProfile, VortexenRelevance
from research.prompts import vortexen_profile_prompt
from research.provider_router import ProviderRouter
from research.scoring import score_vortexen_relevance
from research.security import SSRFBlockedError, UnsafeContentError, format_sources_for_prompt, validate_url

log = logging.getLogger("prospect_mcp.research.vortexen")

SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def _extract_deterministic_vortexen_profile(
    website_url: str,
    pages: list[FetchedPage],
) -> VortexenProfile:
    """Build baseline VortexenProfile strictly from crawled website HTML/headings/JSON-LD (never hardcoded)."""
    services: list[str] = []
    products: list[str] = []
    solutions: list[str] = []
    tech_caps: list[str] = []
    industries: list[str] = []
    geo: list[str] = []
    overview = ""

    for idx, p in enumerate(pages):
        if idx == 0 and p.meta_description:
            overview = p.meta_description
        elif not overview and p.text:
            overview = p.text[:350]

        path = urlparse(p.final_url).path.lower()
        for obj in p.json_ld:
            if isinstance(obj.get("description"), str) and not overview:
                overview = obj["description"][:400]
            t = str(obj.get("@type") or "").casefold()
            if "service" in t and obj.get("name"):
                services.append(str(obj["name"]).strip())
            if "product" in t and obj.get("name"):
                products.append(str(obj["name"]).strip())

        skip = {"home", "about", "contact", "menu", "vortexen", "services", "products", "solutions"}
        for h in p.headings:
            clean = " ".join(h.split()).strip()
            if len(clean) < 3 or len(clean) > 120 or clean.casefold() in skip:
                continue
            if any(k in path for k in ("product", "material", "supply", "equipment")):
                products.append(clean)
            elif any(k in path for k in ("solution", "route", "protect", "source", "represent", "mobilise")):
                solutions.append(clean)
            elif any(k in path for k in ("industr", "sector", "market")):
                industries.append(clean)
            elif any(k in path for k in ("service", "capabilit")) or idx == 0:
                services.append(clean)

        # Look for explicit bullet/heading lines mentioning locations or capabilities on pages.
        for m in re.finditer(r"(?:industries|sectors)\s*[:\-]\s*([^\n.]{5,200})", p.text, re.I):
            for part in re.split(r"[,;/|]", m.group(1)):
                if part.strip():
                    industries.append(part.strip())
        for m in re.finditer(r"(?:offices|locations|countries|footprint)\s*[:\-]\s*([^\n.]{3,200})", p.text, re.I):
            for part in re.split(r"[,;/|]", m.group(1)):
                if part.strip():
                    geo.append(part.strip())

    combined_hash = sha256_text("|".join(p.content_hash for p in pages if p.content_hash))
    return VortexenProfile(
        company_name="Vortexen",
        website_url=website_url,
        overview=overview,
        services=list(dict.fromkeys(services))[:25],
        products=list(dict.fromkeys(products))[:25],
        solutions=list(dict.fromkeys(solutions))[:25],
        technology_capabilities=list(dict.fromkeys(tech_caps))[:20],
        primary_industries=list(dict.fromkeys(industries))[:20],
        target_industries=list(dict.fromkeys(industries))[:20],
        geographic_footprint=list(dict.fromkeys(geo))[:20],
        source_urls=[p.final_url for p in pages],
        content_hash=combined_hash,
        extraction_method="deterministic",
    )


class VortexenProfileManager:
    """Crawls, extracts, versions, caches, and refreshes our company's profile from https://vortexen.com/."""

    def __init__(
        self,
        settings: Settings,
        fetcher: SafeFetcher,
        cache: ResearchCache | None = None,
        router: ProviderRouter | None = None,
    ) -> None:
        self._s = settings
        self._fetcher = fetcher
        self._cache = cache
        self._router = router

    async def get_profile(
        self,
        *,
        force_refresh: bool = False,
        budget: ResearchBudget | None = None,
    ) -> VortexenProfile | None:
        if self._cache and not force_refresh:
            cached, is_fresh = self._cache.get_vortexen_profile()
            if cached is not None and is_fresh:
                return cached

        return await self.refresh_profile(force=force_refresh, budget=budget)

    async def refresh_profile(
        self,
        *,
        force: bool = True,
        budget: ResearchBudget | None = None,
    ) -> VortexenProfile | None:
        base_url = self._s.vortexen_website_url
        vortexen_domain = extract_domain(base_url)
        pages: list[FetchedPage] = []
        visited: set[str] = set()
        local_budget = budget or ResearchBudget(max_llm_calls=2, max_total_tokens=10_000, max_search_calls=0, max_pages_fetched=12)

        async def _try_fetch(url: str) -> FetchedPage | None:
            norm = url.rstrip("/")
            if norm in visited or len(pages) >= 12:
                return None
            visited.add(norm)
            try:
                p = await self._fetcher.fetch_page(
                    url,
                    official_domain=vortexen_domain,
                    budget=local_budget,
                    force_refresh=force,
                )
                if not p.robots_disallowed and p.status_code < 400 and p.text:
                    pages.append(p)
                    return p
            except (SSRFBlockedError, UnsafeContentError) as exc:
                log.warning("Vortexen fetch skipped %s (%s)", url, exc)
            return None

        home = await _try_fetch(base_url)
        candidate_urls: list[str] = []
        if home is not None:
            candidate_urls.extend(
                discover_priority_links(
                    home,
                    same_domain_only=True,
                    categories=("offerings", "industries", "corporate"),
                    max_links=10,
                )
            )

        # Also check sitemap.xml for full website coverage without hardcoding paths.
        sitemap_url = urljoin(base_url, "/sitemap.xml")
        try:
            sm_page = await self._fetcher.fetch_page(
                sitemap_url,
                official_domain=vortexen_domain,
                budget=local_budget,
                force_refresh=force,
            )
            if sm_page.status_code == 200 and sm_page.text:
                for loc in SITEMAP_LOC_RE.findall(sm_page.text):
                    try:
                        safe_loc = validate_url(loc)
                    except SSRFBlockedError:
                        continue
                    if extract_domain(safe_loc) == vortexen_domain and safe_loc not in candidate_urls:
                        candidate_urls.append(safe_loc)
        except Exception:
            pass

        for url in candidate_urls[:10]:
            if len(pages) >= 10:
                break
            await _try_fetch(url)

        if not pages:
            if self._cache:
                cached, _ = self._cache.get_vortexen_profile()
                return cached
            return None

        combined_hash = sha256_text("|".join(p.content_hash for p in pages if p.content_hash))
        if self._cache:
            existing, _ = self._cache.get_vortexen_profile()
            if existing is not None and existing.content_hash == combined_hash:
                # Website content unchanged: extend TTL with zero LLM token consumption!
                return self._cache.set_vortexen_profile(existing, self._s.vortexen_profile_ttl_seconds)

        profile = _extract_deterministic_vortexen_profile(base_url, pages)
        profile.content_hash = combined_hash

        if self._router is not None:
            sources_block = format_sources_for_prompt(pages, max_total_chars=self._s.research_max_context_chars)
            sys_p, usr_p = vortexen_profile_prompt(sources_block)
            llm_data = await self._router.complete_json(
                task_type="vortexen_profile",
                system_prompt=sys_p,
                user_prompt=usr_p,
                stage="primary",
                budget=local_budget,
                max_tokens=1200,
                force_refresh=force,
            )
            if isinstance(llm_data, dict):
                def _str_list(k: str, fallback: list[str]) -> list[str]:
                    vals = llm_data.get(k)
                    if isinstance(vals, list):
                        cleaned = [str(x).strip() for x in vals if isinstance(x, str) and x.strip()]
                        if cleaned:
                            return list(dict.fromkeys(cleaned))
                    return fallback

                profile = VortexenProfile(
                    company_name=str(llm_data.get("company_name") or profile.company_name).strip(),
                    website_url=base_url,
                    overview=str(llm_data.get("overview") or profile.overview).strip(),
                    services=_str_list("services", profile.services),
                    products=_str_list("products", profile.products),
                    solutions=_str_list("solutions", profile.solutions),
                    technology_capabilities=_str_list("technology_capabilities", profile.technology_capabilities),
                    primary_industries=_str_list("primary_industries", profile.primary_industries),
                    target_industries=_str_list("target_industries", profile.target_industries),
                    target_customers=_str_list("target_customers", profile.target_customers),
                    business_capabilities=_str_list("business_capabilities", profile.business_capabilities),
                    use_cases=_str_list("use_cases", profile.use_cases),
                    specialized_categories=_str_list("specialized_categories", profile.specialized_categories),
                    geographic_footprint=_str_list("geographic_footprint", profile.geographic_footprint),
                    source_urls=[p.final_url for p in pages],
                    content_hash=combined_hash,
                    extraction_method="llm+deterministic",
                )

        if self._cache:
            profile = self._cache.set_vortexen_profile(profile, self._s.vortexen_profile_ttl_seconds)
        return profile

    def evaluate_company(
        self,
        company_data: dict[str, Any],
        profile: VortexenProfile | None,
    ) -> VortexenRelevance:
        return score_vortexen_relevance(company_data, profile)
