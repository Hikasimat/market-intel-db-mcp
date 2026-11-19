"""Configurable web search adapter supporting Brave, Tavily, Serper, and DuckDuckGo HTML fallback."""
from __future__ import annotations

import logging
import re
import typing
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from config import Settings
from research.models import ResearchBudget, SearchResult
from research.scoring import classify_source, source_authority_weight
from research.security import SSRFBlockedError, create_ssrf_guarded_transport, validate_url

log = logging.getLogger("prospect_mcp.research.search")

DDG_RESULT_LINK_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.I | re.S,
)
DDG_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>|<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>',
    re.I | re.S,
)


def _strip_tags(raw: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", raw or "")).split())


def _extract_ddg_url(href: str) -> str:
    href = unescape(href.strip())
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in (parsed.hostname or "") and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        uddg = qs.get("uddg", [None])[0]
        if uddg:
            return unquote(uddg)
    return href


class SearchClient:
    """Rate-limited, query-deduplicated web search client."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: typing.Callable[[str, int], typing.Awaitable[list[str]]] | None = None,
    ) -> None:
        self._s = settings
        actual_transport = transport or create_ssrf_guarded_transport(resolver=resolver)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.research_fetch_timeout_seconds),
            follow_redirects=False,
            transport=actual_transport,
            headers={"User-Agent": "MarketIntelResearchBot/1.0"},
        )
        self._query_cache: dict[str, list[SearchResult]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    def active_provider(self) -> str:
        pref = (self._s.search_provider or "auto").lower()
        if pref == "none":
            return "none"
        if pref == "brave" and self._s.brave_search_api_key:
            return "brave"
        if pref == "tavily" and self._s.tavily_api_key:
            return "tavily"
        if pref == "serper" and self._s.serper_api_key:
            return "serper"
        if pref == "duckduckgo":
            return "duckduckgo"
        if pref == "auto":
            if self._s.brave_search_api_key:
                return "brave"
            if self._s.tavily_api_key:
                return "tavily"
            if self._s.serper_api_key:
                return "serper"
            if self._s.search_enable_ddg_fallback:
                return "duckduckgo"
            return "none"
        # Explicitly chosen provider lacked an API key: fall back only if enabled.
        if self._s.search_enable_ddg_fallback:
            return "duckduckgo"
        return "none"

    async def search(
        self,
        query: str,
        *,
        max_results: int = 8,
        budget: ResearchBudget | None = None,
        official_domain: str | None = None,
    ) -> list[SearchResult]:
        q = " ".join((query or "").split())
        if not q:
            return []

        cache_key = f"{self.active_provider()}:{q.casefold()}:{max_results}"
        if cache_key in self._query_cache:
            return list(self._query_cache[cache_key])

        provider = self.active_provider()
        if provider == "none":
            return []

        if budget is not None:
            if not budget.can_search():
                return []
            budget.record_search()

        raw_items: list[dict[str, str | None]] = []
        try:
            if provider == "brave":
                raw_items = await self._search_brave(q, max_results)
            elif provider == "tavily":
                raw_items = await self._search_tavily(q, max_results)
            elif provider == "serper":
                raw_items = await self._search_serper(q, max_results)
            elif provider == "duckduckgo":
                raw_items = await self._search_ddg(q, max_results)
        except Exception as exc:
            log.warning("Search provider %s failed (%s)", provider, type(exc).__name__)
            if provider != "duckduckgo" and self._s.search_enable_ddg_fallback:
                try:
                    raw_items = await self._search_ddg(q, max_results)
                    provider = "duckduckgo"
                except Exception:
                    raw_items = []

        results: list[SearchResult] = []
        seen_urls: set[str] = set()
        for item in raw_items:
            raw_url = str(item.get("url") or "").strip()
            if not raw_url:
                continue
            try:
                safe_url = validate_url(raw_url)
            except SSRFBlockedError:
                continue
            norm_url = safe_url.rstrip("/")
            if norm_url in seen_urls:
                continue
            seen_urls.add(norm_url)
            domain = (urlparse(safe_url).hostname or "").lower().removeprefix("www.")
            stype, _ = classify_source(safe_url, official_domain=official_domain)
            rank = int(round(source_authority_weight(stype) * 100))
            results.append(
                SearchResult(
                    title=str(item.get("title") or domain)[:300],
                    url=safe_url,
                    snippet=str(item.get("snippet") or "")[:600],
                    domain=domain,
                    provider=provider,
                    published_date=item.get("published_date"),
                    source_type=stype,
                    authority_rank=rank,
                )
            )
            if len(results) >= max_results:
                break

        self._query_cache[cache_key] = list(results)
        return results

    async def _search_brave(self, query: str, count: int) -> list[dict[str, str | None]]:
        resp = await self._http.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(count, 20)},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self._s.brave_search_api_key or "",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        web = (data.get("web") or {}).get("results") or []
        return [
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "snippet": r.get("description"),
                "published_date": r.get("page_age"),
            }
            for r in web if isinstance(r, dict)
        ]

    async def _search_tavily(self, query: str, count: int) -> list[dict[str, str | None]]:
        resp = await self._http.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self._s.tavily_api_key or "",
                "query": query,
                "max_results": min(count, 10),
                "search_depth": "basic",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "snippet": r.get("content"),
                "published_date": r.get("published_date"),
            }
            for r in (data.get("results") or []) if isinstance(r, dict)
        ]

    async def _search_serper(self, query: str, count: int) -> list[dict[str, str | None]]:
        resp = await self._http.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": min(count, 10)},
            headers={
                "X-API-KEY": self._s.serper_api_key or "",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "title": r.get("title"),
                "url": r.get("link"),
                "snippet": r.get("snippet"),
                "published_date": r.get("date"),
            }
            for r in (data.get("organic") or []) if isinstance(r, dict)
        ]

    async def _search_ddg(self, query: str, count: int) -> list[dict[str, str | None]]:
        resp = await self._http.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        html = resp.text
        links = DDG_RESULT_LINK_RE.findall(html)
        snippets = DDG_SNIPPET_RE.findall(html)
        out: list[dict[str, str | None]] = []
        for idx, (href, raw_title) in enumerate(links[:count]):
            snip_tuple = snippets[idx] if idx < len(snippets) else ("", "")
            snip = _strip_tags(snip_tuple[0] or snip_tuple[1])
            out.append({
                "title": _strip_tags(raw_title),
                "url": _extract_ddg_url(href),
                "snippet": snip,
                "published_date": None,
            })
        return out
