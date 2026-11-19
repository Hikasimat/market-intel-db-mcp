"""SSRF-safe, size-bounded, robots-aware HTML/JSON fetcher with pooling, retries, and source caching."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import typing
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from config import Settings
from research.cache import ResearchCache, sha256_text
from research.models import FetchedPage, ResearchBudget, SourceType, utc_now_iso
from research.scoring import classify_source
from research.security import (
    SSRFBlockedError,
    UnsafeContentError,
    create_ssrf_guarded_transport,
    sanitize_untrusted_text,
    validate_content_type,
    validate_url,
)

log = logging.getLogger("prospect_mcp.research.fetcher")

EMAIL_EXTRACT_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE_EXTRACT_RE = re.compile(r"(?:\+\d{1,3}[\s\-().]*)?(?:\d[\s\-().]*){7,14}\d")

PRIORITY_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "offerings": (
        "product", "products", "service", "services", "solution", "solutions",
        "offering", "offerings", "capabilit", "platform", "technology", "portfolio",
        "catalog", "catalogue", "division", "business-unit", "what-we-do", "expertise",
    ),
    "industries": (
        "industr", "sector", "sectors", "market", "markets", "vertical", "application",
        "case-stud", "client", "project",
    ),
    "corporate": (
        "about", "company", "corporate", "group", "subsidiar", "history", "who-we-are",
        "overview", "legal", "imprint", "global", "location", "office",
    ),
    "ownership": (
        "investor", "shareholder", "governance", "filing", "annual-report", "disclosure",
        "ownership", "sec", "financial", "board",
    ),
    "contacts": (
        "leadership", "management", "executive", "team", "board", "contact", "director",
        "founder", "officer",
    ),
    "events": (
        "event", "events", "exhibitor", "exhibitors", "sponsor", "sponsors", "speaker",
        "speakers", "partner", "partners", "directory", "participant", "agenda", "news",
        "press", "summit", "expo", "conference",
    ),
}


class _SafeHTMLParser(HTMLParser):
    """Stdlib HTMLParser that strips scripts/styles/iframes while capturing structured text, JSON-LD, links, and tables."""

    SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "iframe", "object", "embed", "template"})
    BLOCK_TAGS = frozenset({
        "p", "div", "section", "article", "header", "footer", "li", "tr", "br",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "dd", "dt",
    })
    HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4"})

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._skip_depth = 0
        self._in_title = False
        self._in_json_ld = False
        self._in_heading: str | None = None
        self._in_link: str | None = None
        self._link_text: list[str] = []
        self._heading_text: list[str] = []
        self._json_ld_buf: list[str] = []
        self._in_table = False
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._in_cell = False
        self._cell_buf: list[str] = []

        self.title_parts: list[str] = []
        self.meta_description: str = ""
        self.publication_date: str | None = None
        self.text_parts: list[str] = []
        self.headings: list[str] = []
        self.links: list[dict[str, str]] = []
        self.json_ld: list[dict[str, Any]] = []
        self.tables: list[list[list[str]]] = []
        self.mailto_emails: list[str] = []
        self.tel_phones: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if t == "script":
            script_type = attr_map.get("type", "").lower()
            if "ld+json" in script_type:
                self._in_json_ld = True
                self._json_ld_buf = []
                return
            self._skip_depth += 1
            return

        if t in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        if t == "title":
            self._in_title = True
        elif t == "meta":
            name = (attr_map.get("name") or attr_map.get("property") or "").lower()
            content = attr_map.get("content", "").strip()
            if content:
                if name in ("description", "og:description") and not self.meta_description:
                    self.meta_description = content[:500]
                elif name in ("article:published_time", "og:updated_time", "pubdate", "date") and not self.publication_date:
                    self.publication_date = content[:64]
        elif t in self.HEADING_TAGS:
            self._in_heading = t
            self._heading_text = []
            self.text_parts.append("\n")
        elif t in self.BLOCK_TAGS:
            self.text_parts.append("\n")
        if t == "a":
            href = attr_map.get("href", "").strip()
            if href:
                low = href.lower()
                if low.startswith("mailto:"):
                    addr = href[7:].split("?", 1)[0].strip()
                    if addr and addr not in self.mailto_emails:
                        self.mailto_emails.append(addr)
                elif low.startswith("tel:"):
                    ph = href[4:].strip()
                    if ph and ph not in self.tel_phones:
                        self.tel_phones.append(ph)
                elif not low.startswith(("javascript:", "data:", "vbscript:", "#")):
                    abs_url = urljoin(self.base_url, href)
                    self._in_link = abs_url
                    self._link_text = []
        elif t == "table":
            self._in_table = True
            self._current_table = []
        elif t == "tr" and self._in_table:
            self._current_row = []
        elif t in ("td", "th") and self._in_table:
            self._in_cell = True
            self._cell_buf = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "script" and self._in_json_ld:
            raw = "".join(self._json_ld_buf).strip()
            self._in_json_ld = False
            self._json_ld_buf = []
            if raw and len(raw) <= 100_000:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        self.json_ld.append(parsed)
                    elif isinstance(parsed, list):
                        self.json_ld.extend(x for x in parsed if isinstance(x, dict))
                except json.JSONDecodeError:
                    pass
            return

        if t in self.SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return

        if t == "title":
            self._in_title = False
        elif t == self._in_heading:
            heading = " ".join("".join(self._heading_text).split())
            if heading and len(self.headings) < 80:
                self.headings.append(heading[:250])
            self._in_heading = None
            self.text_parts.append("\n")
        elif t in self.BLOCK_TAGS:
            self.text_parts.append("\n")
        if t == "a" and self._in_link is not None:
            anchor = " ".join("".join(self._link_text).split())[:160]
            if len(self.links) < 300:
                self.links.append({"url": self._in_link, "text": anchor})
            self._in_link = None
            self._link_text = []
        elif t in ("td", "th") and self._in_cell:
            cell = " ".join("".join(self._cell_buf).split())[:250]
            self._current_row.append(cell)
            self._in_cell = False
            self._cell_buf = []
        elif t == "tr" and self._in_table:
            if any(self._current_row) and len(self._current_table) < 100:
                self._current_table.append(self._current_row)
            self._current_row = []
        elif t == "table" and self._in_table:
            if self._current_table and len(self.tables) < 20:
                self.tables.append(self._current_table)
            self._in_table = False
            self._current_table = []

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_ld_buf.append(data)
            return
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._in_heading is not None:
            self._heading_text.append(data)
        if self._in_link is not None:
            self._link_text.append(data)
        if self._in_cell:
            self._cell_buf.append(data)
        self.text_parts.append(data)


def parse_html_document(url: str, html_text: str) -> dict[str, Any]:
    parser = _SafeHTMLParser(url)
    parser.feed(html_text)
    parser.close()
    title = " ".join("".join(parser.title_parts).split())[:300]
    raw_text = " ".join("".join(parser.text_parts).split("\n"))
    # Preserve line breaks from block tags cleanly.
    lines = [" ".join(line.split()) for line in "".join(parser.text_parts).splitlines()]
    structured_text = "\n".join(line for line in lines if line)
    if not structured_text:
        structured_text = raw_text
    return {
        "title": title,
        "meta_description": parser.meta_description,
        "publication_date": parser.publication_date,
        "text": structured_text,
        "headings": parser.headings,
        "links": parser.links,
        "json_ld": parser.json_ld,
        "tables": parser.tables,
        "mailto_emails": parser.mailto_emails,
        "tel_phones": parser.tel_phones,
    }


def _dedupe_preserve(items: list[str], limit: int = 50) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        clean = item.strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            out.append(clean)
            if len(out) >= limit:
                break
    return out


class SafeFetcher:
    """Pooled, SSRF-guarded, robots-aware, size-capped async HTTP fetcher."""

    USER_AGENT = "MarketIntelResearchBot/1.0 (+https://vortexen.com)"
    MAX_REDIRECTS = 5

    def __init__(
        self,
        settings: Settings,
        cache: ResearchCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: typing.Callable[[str, int], typing.Awaitable[list[str]]] | None = None,
        min_host_interval_seconds: float = 0.2,
    ) -> None:
        self._s = settings
        self._cache = cache
        self._resolver = resolver
        self._min_host_interval = min_host_interval_seconds
        self._owned_transport = transport is None
        actual_transport = transport or create_ssrf_guarded_transport(resolver=resolver)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.research_fetch_timeout_seconds),
            follow_redirects=False,  # Always handle redirects manually with SSRF re-validation
            transport=actual_transport,
            headers={
                "User-Agent": self.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json,text/xml;q=0.9,text/plain;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self._sem = asyncio.Semaphore(max(1, settings.research_max_concurrency))
        self._host_lock = asyncio.Lock()
        self._host_last_req: dict[str, float] = {}
        self._robots_cache: dict[str, RobotFileParser | None] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _throttle_host(self, host: str) -> None:
        if self._min_host_interval <= 0:
            return
        async with self._host_lock:
            now = time.monotonic()
            last = self._host_last_req.get(host, 0.0)
            wait = self._min_host_interval - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_last_req[host] = time.monotonic()

    async def _is_allowed_by_robots(self, url: str) -> bool:
        if not self._s.research_respect_robots_txt:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots_cache:
            rp = RobotFileParser()
            robots_url = f"{origin}/robots.txt"
            try:
                safe_robots = validate_url(robots_url)
                if self._resolver is not None:
                    p = urlparse(safe_robots)
                    from research.security import resolve_and_validate_host
                    await resolve_and_validate_host(p.hostname or "", p.port or 443, resolver=self._resolver)
                resp = await self._http.get(safe_robots, timeout=min(5.0, self._s.research_fetch_timeout_seconds))
                if resp.status_code == 200 and len(resp.content) <= 250_000:
                    rp.parse(resp.text.splitlines())
                    self._robots_cache[origin] = rp
                else:
                    self._robots_cache[origin] = None
            except SSRFBlockedError:
                raise
            except Exception:
                self._robots_cache[origin] = None
        rp = self._robots_cache.get(origin)
        if rp is None:
            return True
        return rp.can_fetch(self.USER_AGENT, url)

    async def fetch_page(
        self,
        url: str,
        *,
        official_domain: str | None = None,
        event_domain: str | None = None,
        budget: ResearchBudget | None = None,
        force_refresh: bool = False,
    ) -> FetchedPage:
        """Fetch and safely parse one URL with SSRF, redirect, MIME, size, robots, and cache enforcement."""
        safe_url = validate_url(url)
        cache_key = sha256_text(safe_url)

        if self._cache and not force_refresh:
            cached_data, is_fresh = self._cache.get_source(cache_key)
            if cached_data and is_fresh:
                if budget:
                    budget.record_page_fetch(cache_hit=True)
                return FetchedPage.model_validate(cached_data)

        if budget and not budget.can_fetch_page():
            raise UnsafeContentError("Page fetch budget exhausted for this research job.")

        if not await self._is_allowed_by_robots(safe_url):
            parsed = urlparse(safe_url)
            stype, srel = classify_source(safe_url, official_domain=official_domain, event_domain=event_domain)
            return FetchedPage(
                url=url,
                final_url=safe_url,
                domain=(parsed.hostname or "").lower(),
                status_code=403,
                source_type=stype,
                source_reliability=srel,
                robots_disallowed=True,
                fetch_error="Disallowed by site robots.txt.",
            )

        current_url = safe_url
        final_bytes = b""
        final_status = 0
        final_mime = "text/html"
        final_url = safe_url

        async with self._sem:
            for hop in range(self.MAX_REDIRECTS + 1):
                current_url = validate_url(current_url)
                parsed = urlparse(current_url)
                host = (parsed.hostname or "").lower()
                port = parsed.port or (443 if parsed.scheme == "https" else 80)

                if self._resolver is not None:
                    from research.security import resolve_and_validate_host
                    await resolve_and_validate_host(host, port, resolver=self._resolver)

                await self._throttle_host(host)
                resp_meta, body_bytes, redirect_loc = await self._stream_with_retry(current_url)
                final_status = resp_meta["status_code"]

                if final_status in (301, 302, 303, 307, 308) and redirect_loc:
                    if hop >= self.MAX_REDIRECTS:
                        raise UnsafeContentError("Exceeded maximum HTTP redirect limit.")
                    next_url = urljoin(current_url, redirect_loc)
                    current_url = validate_url(next_url)
                    continue

                final_url = current_url
                final_mime = validate_content_type(resp_meta.get("content_type"))
                final_bytes = body_bytes
                break

        if budget:
            budget.record_page_fetch(cache_hit=False)

        raw_text = final_bytes.decode("utf-8", errors="replace")
        final_domain = (urlparse(final_url).hostname or "").lower()
        source_type, source_rel = classify_source(
            final_url, official_domain=official_domain, event_domain=event_domain
        )

        if final_mime == "application/json":
            try:
                parsed_json = json.dumps(json.loads(raw_text), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                parsed_json = raw_text
            sanitized_text, warnings = sanitize_untrusted_text(
                parsed_json, max_chars=self._s.research_max_context_chars
            )
            page = FetchedPage(
                url=url,
                final_url=final_url,
                domain=final_domain,
                status_code=final_status,
                content_type=final_mime,
                title=f"JSON from {final_domain}",
                text=sanitized_text,
                content_hash=sha256_text(sanitized_text),
                retrieved_at=utc_now_iso(),
                source_type=source_type,
                source_reliability=source_rel,
                injection_warnings=warnings,
            )
        elif final_mime in ("text/plain", "application/xml", "text/xml", "application/rss+xml", "application/atom+xml"):
            sanitized_text, warnings = sanitize_untrusted_text(
                raw_text, max_chars=self._s.research_max_context_chars
            )
            page = FetchedPage(
                url=url,
                final_url=final_url,
                domain=final_domain,
                status_code=final_status,
                content_type=final_mime,
                title=final_domain,
                text=sanitized_text,
                emails_found=_dedupe_preserve(EMAIL_EXTRACT_RE.findall(raw_text), 25),
                content_hash=sha256_text(sanitized_text),
                retrieved_at=utc_now_iso(),
                source_type=source_type,
                source_reliability=source_rel,
                injection_warnings=warnings,
            )
        else:
            parsed_html = parse_html_document(final_url, raw_text)
            sanitized_text, warnings = sanitize_untrusted_text(
                parsed_html["text"], max_chars=self._s.research_max_context_chars
            )
            if parsed_html["meta_description"]:
                _, meta_warn = sanitize_untrusted_text(parsed_html["meta_description"], max_chars=500)
                warnings = list(dict.fromkeys(warnings + meta_warn))

            all_emails = parsed_html["mailto_emails"] + EMAIL_EXTRACT_RE.findall(sanitized_text)
            all_phones = parsed_html["tel_phones"] + [
                m.group(0).strip() for m in PHONE_EXTRACT_RE.finditer(sanitized_text)
                if len(re.sub(r"\D", "", m.group(0))) >= 7
            ]
            linkedin_urls = [
                lnk["url"] for lnk in parsed_html["links"]
                if "linkedin.com/" in lnk["url"].lower()
            ]
            page = FetchedPage(
                url=url,
                final_url=final_url,
                domain=final_domain,
                status_code=final_status,
                content_type=final_mime,
                title=parsed_html["title"],
                meta_description=parsed_html["meta_description"],
                publication_date=parsed_html["publication_date"],
                text=sanitized_text,
                headings=parsed_html["headings"],
                links=parsed_html["links"],
                json_ld=parsed_html["json_ld"],
                tables=parsed_html["tables"],
                emails_found=_dedupe_preserve(all_emails, 30),
                phones_found=_dedupe_preserve(all_phones, 20),
                linkedin_urls=_dedupe_preserve(linkedin_urls, 20),
                content_hash=sha256_text(sanitized_text),
                retrieved_at=utc_now_iso(),
                source_type=source_type,
                source_reliability=source_rel,
                injection_warnings=warnings,
            )

        if self._cache and 200 <= final_status < 400:
            self._cache.set_source(
                cache_key,
                page.final_url,
                page.domain,
                page.content_hash,
                page.title,
                page.model_dump(mode="json"),
                self._s.research_source_ttl_seconds,
            )
        return page

    async def _stream_with_retry(self, url: str) -> tuple[dict[str, Any], bytes, str | None]:
        max_bytes = self._s.research_max_response_bytes
        attempts = 3
        backoff = 0.4
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                async with self._http.stream("GET", url) as resp:
                    if resp.status_code in (429, 502, 503, 504) and attempt < attempts - 1:
                        retry_after = resp.headers.get("retry-after", "")
                        try:
                            delay = min(3.0, float(retry_after)) if retry_after else backoff
                        except ValueError:
                            delay = backoff
                        await asyncio.sleep(delay)
                        backoff *= 2
                        continue

                    if resp.status_code in (301, 302, 303, 307, 308):
                        return (
                            {"status_code": resp.status_code, "content_type": resp.headers.get("content-type")},
                            b"",
                            resp.headers.get("location"),
                        )

                    ct = resp.headers.get("content-type")
                    validate_content_type(ct)

                    cl = resp.headers.get("content-length")
                    if cl:
                        try:
                            if int(cl) > max_bytes:
                                raise UnsafeContentError(
                                    f"Content-Length ({cl} bytes) exceeds max limit ({max_bytes} bytes)."
                                )
                        except ValueError:
                            pass

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise UnsafeContentError(
                                f"Response body exceeded maximum allowed size ({max_bytes} bytes)."
                            )
                        chunks.append(chunk)

                    return (
                        {"status_code": resp.status_code, "content_type": ct},
                        b"".join(chunks),
                        None,
                    )
            except (SSRFBlockedError, UnsafeContentError):
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise UnsafeContentError(f"Failed fetching {url}: {type(exc).__name__}") from exc

        raise UnsafeContentError(f"Failed fetching {url}: {last_exc}")


def discover_priority_links(
    page: FetchedPage,
    *,
    same_domain_only: bool = True,
    categories: tuple[str, ...] = ("offerings", "industries", "corporate", "ownership", "contacts"),
    max_links: int = 8,
) -> list[str]:
    """Rank internal links by research priority (offerings & industries score highest)."""
    base_host = page.domain.removeprefix("www.")
    scored: dict[str, tuple[int, str]] = {}

    category_bonus = {
        "offerings": 30,
        "industries": 25,
        "events": 25,
        "corporate": 15,
        "ownership": 15,
        "contacts": 10,
    }

    for lnk in page.links:
        url = (lnk.get("url") or "").strip()
        anchor = (lnk.get("text") or "").lower()
        if not url or url.rstrip("/") == page.final_url.rstrip("/"):
            continue
        try:
            safe = validate_url(url)
        except SSRFBlockedError:
            continue
        parsed = urlparse(safe)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if same_domain_only and host != base_host and not host.endswith("." + base_host):
            continue
        path_low = parsed.path.lower()
        if any(path_low.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".css", ".js", ".zip", ".mp4")):
            continue

        score = 0
        haystack = f"{path_low} {anchor}"
        for cat in categories:
            kws = PRIORITY_CATEGORY_KEYWORDS.get(cat, ())
            matches = sum(1 for kw in kws if kw in haystack)
            if matches:
                score += category_bonus.get(cat, 10) + min(15, matches * 5)

        if score > 0:
            # Prefer concise top-level product/service URLs over deep query strings.
            depth = len([seg for seg in parsed.path.split("/") if seg])
            if depth <= 3 and not parsed.query:
                score += 5
            existing = scored.get(safe)
            if not existing or score > existing[0]:
                scored[safe] = (score, safe)

    ordered = sorted(scored.values(), key=lambda x: (-x[0], len(x[1])))
    return [u for _, u in ordered[:max_links]]
