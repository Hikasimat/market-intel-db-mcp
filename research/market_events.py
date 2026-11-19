"""Event-based prospect research: verifies events, discovers exhibitors/sponsors/speakers, and runs full company research."""
from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlparse

from api_client import ApiError
from config import Settings
from research.company_identity import extract_domain, normalize_company_name
from research.fetcher import SafeFetcher, discover_priority_links
from research.models import EventResearchResult, FetchedPage, FieldEvidence, ResearchBudget
from research.prompts import event_participants_prompt
from research.provider_router import ProviderRouter
from research.search import SearchClient
from research.security import SSRFBlockedError, UnsafeContentError, format_sources_for_prompt, validate_url
from research.verifier import build_evidence, is_quote_grounded
from validation import find_market_event_duplicates

EVENT_SUBPAGE_HINTS = ("exhibitor", "sponsor", "speaker", "partner", "participant", "directory", "company", "list")
PARTICIPANT_LINE_RE = re.compile(
    r"^\s*(?:[-*]|\d+\.)\s+([A-Z][A-Za-z0-9&.,'/\- ]{2,80}?)(?:\s*(?:[-–—|:]\s*(?:Booth|Stand|Hall|Exhibitor|Sponsor|Partner)[^\n]*)?)$",
    re.M,
)


def _extract_deterministic_participants(pages: list[FetchedPage]) -> list[dict[str, Any]]:
    participants: list[dict[str, Any]] = []
    for p in pages:
        url_low = p.final_url.lower()
        role = "Participant"
        if "exhibitor" in url_low:
            role = "Exhibitor"
        elif "sponsor" in url_low:
            role = "Sponsor"
        elif "speaker" in url_low:
            role = "Speaker"
        elif "partner" in url_low:
            role = "Partner"

        for obj in p.json_ld:
            for k in ("performer", "sponsor", "organizer", "contributor", "attendee"):
                val = obj.get(k)
                if isinstance(val, dict):
                    val = [val]
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict) and item.get("name"):
                            participants.append({
                                "company_name": str(item["name"]).strip(),
                                "role": "Sponsor" if k == "sponsor" else role,
                                "website": item.get("url") or item.get("sameAs"),
                                "source_url": p.final_url,
                                "evidence_quote": str(item["name"]).strip(),
                            })

        # Parse table first column if table header mentions exhibitor/company/sponsor/partner.
        for tbl in p.tables:
            if len(tbl) < 2:
                continue
            header = " ".join(tbl[0]).casefold()
            if any(w in header for w in ("exhibitor", "company", "organization", "sponsor", "partner", "speaker")):
                for row in tbl[1:]:
                    if row and row[0] and 2 <= len(row[0]) <= 100:
                        participants.append({
                            "company_name": row[0].strip(),
                            "role": role,
                            "website": None,
                            "source_url": p.final_url,
                            "evidence_quote": " | ".join(row[:3]),
                        })

        if any(h in url_low for h in EVENT_SUBPAGE_HINTS):
            for m in PARTICIPANT_LINE_RE.finditer(p.text):
                cand = " ".join(m.group(1).split())
                if len(cand) >= 3 and not cand.casefold().startswith(("exhibitor", "sponsor", "contact", "about")):
                    participants.append({
                        "company_name": cand,
                        "role": role,
                        "website": None,
                        "source_url": p.final_url,
                        "evidence_quote": m.group(0).strip()[:200],
                    })
    return participants


def _dedupe_participants(raw_list: list[dict[str, Any]], event_organizer: str | None = None) -> list[dict[str, Any]]:
    by_norm: dict[str, dict[str, Any]] = {}
    org_norm = normalize_company_name(event_organizer or "")
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        cname = " ".join(str(item.get("company_name") or "").split())
        if not cname or len(cname) < 2:
            continue
        norm = normalize_company_name(cname)
        if not norm or (org_norm and norm == org_norm):
            continue
        role = str(item.get("role") or "Participant").strip()
        if norm not in by_norm:
            by_norm[norm] = {
                "company_name": cname,
                "normalized_name": norm,
                "roles": [role],
                "website": item.get("website"),
                "country": item.get("country"),
                "source_urls": [item.get("source_url")] if item.get("source_url") else [],
                "evidence_quotes": [item.get("evidence_quote")] if item.get("evidence_quote") else [],
            }
        else:
            existing = by_norm[norm]
            if role not in existing["roles"]:
                existing["roles"].append(role)
            if item.get("source_url") and item["source_url"] not in existing["source_urls"]:
                existing["source_urls"].append(item["source_url"])
            if item.get("website") and not existing["website"]:
                existing["website"] = item["website"]
            if item.get("country") and not existing["country"]:
                existing["country"] = item["country"]
    return list(by_norm.values())


class EventResearcher:
    """Verifies market events, extracts verified participants, and runs full company research per participant."""

    def __init__(
        self,
        settings: Settings,
        api_client: Any,
        fetcher: SafeFetcher,
        search_client: SearchClient,
        router: ProviderRouter | None,
    ) -> None:
        self._s = settings
        self._api = api_client
        self._fetcher = fetcher
        self._search = search_client
        self._router = router

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
        company_research_cb: Any = None,
        budget: ResearchBudget | None = None,
    ) -> EventResearchResult:
        clean_name = " ".join((event_name or "").split())
        cap = max(1, min(int(max_participants), self._s.research_max_event_participants))
        job_budget = budget or ResearchBudget(
            max_llm_calls=max(6, self._s.research_max_llm_calls_per_company * 3),
            max_total_tokens=self._s.research_max_tokens_per_company * 3,
            max_search_calls=10,
            max_pages_fetched=15,
        )

        # 1. Search existing Market Intel Market Events first (no duplicate event creation).
        exact_events: list[dict[str, Any]] = []
        similar_events: list[dict[str, Any]] = []
        try:
            resp = await self._api.list_market_events({"search": clean_name})
            recs = resp.get("results", []) if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])
            recs = [r for r in recs if isinstance(r, dict)]
            exact_events, similar_events = find_market_event_duplicates(clean_name, recs)
        except ApiError:
            pass

        # 2. Resolve official event URL via input or search.
        official_event_url: str | None = None
        if event_website:
            try:
                official_event_url = validate_url(event_website)
            except SSRFBlockedError:
                official_event_url = None
        if not official_event_url and exact_events and exact_events[0].get("website_url"):
            try:
                official_event_url = validate_url(str(exact_events[0]["website_url"]))
            except SSRFBlockedError:
                official_event_url = None

        search_hits = []
        if not official_event_url:
            q = f'"{clean_name}" {country or ""} exhibitors sponsors official website'.strip()
            search_hits = await self._search.search(q, max_results=8, budget=job_budget)
            for hit in search_hits:
                if not any(b in hit.domain for b in ("linkedin.com", "facebook.com", "wikipedia.org")):
                    official_event_url = hit.url
                    break

        event_domain = extract_domain(official_event_url) if official_event_url else None
        event_pages: list[FetchedPage] = []
        access_limitations: list[str] = []
        warnings: list[str] = []

        if official_event_url:
            try:
                root_page = await self._fetcher.fetch_page(
                    official_event_url,
                    event_domain=event_domain,
                    budget=job_budget,
                    force_refresh=force_refresh,
                )
                if root_page.robots_disallowed:
                    access_limitations.append(f"Access to {official_event_url} is disallowed by robots.txt.")
                elif root_page.status_code in (401, 403):
                    access_limitations.append(f"Official event site returned HTTP {root_page.status_code}; access restricted.")
                else:
                    event_pages.append(root_page)
                    sub_links = discover_priority_links(
                        root_page,
                        same_domain_only=True,
                        categories=("events", "corporate"),
                        max_links=6,
                    )
                    for sub_u in sub_links:
                        if not job_budget.can_fetch_page():
                            break
                        try:
                            sp = await self._fetcher.fetch_page(
                                sub_u,
                                event_domain=event_domain,
                                budget=job_budget,
                                force_refresh=force_refresh,
                            )
                            if sp.robots_disallowed:
                                access_limitations.append(f"Participant page {sub_u} disallowed by robots.txt.")
                            elif sp.status_code in (401, 403):
                                access_limitations.append(f"Participant page {sub_u} requires authorization (HTTP {sp.status_code}); skipped.")
                            elif sp.status_code < 400 and sp.text:
                                event_pages.append(sp)
                        except (SSRFBlockedError, UnsafeContentError) as exc:
                            access_limitations.append(f"Skipped {sub_u}: {exc}")
            except (SSRFBlockedError, UnsafeContentError) as exc:
                access_limitations.append(f"Could not fetch event website {official_event_url}: {exc}")

        if not event_pages:
            warnings.append("No official event web pages could be retrieved; participants cannot be verified from search snippets alone.")

        # 3. Extract event metadata & verified participants.
        pages_by_url = {p.final_url: p for p in event_pages}
        raw_participants = _extract_deterministic_participants(event_pages)
        event_details: dict[str, Any] = {
            "event_title": clean_name,
            "official_website": official_event_url,
            "organizer": None,
            "host_country": country,
            "location": None,
            "start_date": start_date,
            "end_date": end_date,
            "category": None,
            "industry_focus": None,
            "matched_market_event_id": str(exact_events[0]["id"]) if exact_events and exact_events[0].get("id") else None,
        }
        event_evidences: list[FieldEvidence] = []

        if self._router is not None and event_pages:
            sources_block = format_sources_for_prompt(event_pages, max_total_chars=self._s.research_max_context_chars)
            sys_p, usr_p = event_participants_prompt(clean_name, sources_block)
            llm_ev = await self._router.complete_json(
                task_type="event_participants",
                system_prompt=sys_p,
                user_prompt=usr_p,
                stage="primary",
                budget=job_budget,
                max_tokens=1300,
                force_refresh=force_refresh,
            )
            if isinstance(llm_ev, dict):
                for fk in ("event_title", "organizer", "host_country", "location", "start_date", "end_date", "category", "industry_focus"):
                    node = llm_ev.get(fk)
                    if isinstance(node, dict) and node.get("value"):
                        val = str(node["value"]).strip()
                        if val and val.lower() != "null":
                            event_details[fk] = val
                            event_evidences.append(
                                build_evidence(
                                    fk,
                                    val,
                                    str(node.get("source_url") or (event_pages[0].final_url if event_pages else "")),
                                    pages_by_url,
                                    excerpt=str(node.get("evidence_quote") or val),
                                    event_domain=event_domain,
                                )
                            )
                for p_item in llm_ev.get("participants") or []:
                    if isinstance(p_item, dict) and p_item.get("company_name"):
                        cname = str(p_item["company_name"]).strip()
                        quote = str(p_item.get("evidence_quote") or cname)
                        if is_quote_grounded(cname, event_pages) or is_quote_grounded(quote, event_pages):
                            raw_participants.append(p_item)

        if official_event_url and event_pages:
            event_evidences.append(
                build_evidence(
                    "official_website",
                    official_event_url,
                    event_pages[0].final_url,
                    pages_by_url,
                    excerpt=event_pages[0].title or official_event_url,
                    event_domain=event_domain,
                )
            )

        deduped_participants = _dedupe_participants(raw_participants, event_details.get("organizer"))
        selected = deduped_participants[:cap]

        # 4. Run full Company Research Engine on each verified participant with bounded concurrency.
        researched_companies: list[dict[str, Any]] = []
        if research_companies and company_research_cb is not None and selected:
            sem = asyncio.Semaphore(max(1, self._s.research_max_concurrency))

            async def _run_one(p_entry: dict[str, Any]) -> dict[str, Any]:
                async with sem:
                    try:
                        res = await company_research_cb(
                            company_name=p_entry["company_name"],
                            country=p_entry.get("country"),
                            official_website=p_entry.get("website"),
                            force_refresh=force_refresh,
                        )
                        # Attach event participation evidence and matched event UUID if available.
                        ev_id = event_details.get("matched_market_event_id")
                        if ev_id and isinstance(res.get("prospect_candidate"), dict):
                            existing_ids = list(res["prospect_candidate"].get("market_event_ids") or [])
                            if ev_id not in existing_ids:
                                existing_ids.append(ev_id)
                            res["prospect_candidate"]["market_event_ids"] = existing_ids
                        res["event_participation"] = {
                            "event_title": event_details["event_title"],
                            "roles": p_entry["roles"],
                            "source_urls": p_entry["source_urls"],
                            "evidence_quotes": p_entry["evidence_quotes"],
                        }
                        return res
                    except Exception as exc:
                        return {
                            "ok": False,
                            "company_query": p_entry["company_name"],
                            "error": f"Company research failed: {type(exc).__name__}",
                            "event_participation": p_entry,
                        }

            researched_companies = list(await asyncio.gather(*(_run_one(p) for p in selected)))

        return EventResearchResult(
            ok=True,
            read_only=True,
            event_query=clean_name,
            event_details=event_details,
            event_evidence=event_evidences,
            existing_market_event_matches={
                "exact_matches": exact_events,
                "similar_matches": similar_events,
            },
            participants_discovered=deduped_participants,
            participant_companies_researched=researched_companies,
            access_limitations=access_limitations,
            warnings=warnings,
            usage_summary=job_budget.to_summary(),
        )
