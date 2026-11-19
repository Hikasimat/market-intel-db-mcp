"""Tests for dynamic Vortexen profile crawling/caching and Event-Based Prospect Research."""
from __future__ import annotations

from pathlib import Path
import uuid

import httpx
import respx

from config import Settings
from research.cache import SQLiteResearchCache
from research.fetcher import SafeFetcher
from research.provider_router import ProviderRouter
from research.search import SearchClient
from research.service import ResearchService
from research.vortexen_profile import VortexenProfileManager


async def _pub_resolver(_h, _p):
    return ["93.184.216.34"]


def test_vortexen_services_not_hardcoded_in_research_source():
    research_dir = Path(__file__).resolve().parent.parent / "research"
    for py_file in research_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        assert "Vortexen Dynamics Industrial Solutions" not in text
        assert "Passive Fire Protection" not in text


@respx.mock
async def test_dynamic_vortexen_profile_crawl_cache_and_refresh(client, wf, tmp_path):
    s = Settings(
        api_base_url="https://api.test/api",
        username="pre_user",
        password="p",
        research_cache_db_path=tmp_path / "vx.db",
        search_provider="none",
        research_respect_robots_txt=False,
    )
    cache = SQLiteResearchCache(s.research_cache_db_path)
    fetcher = SafeFetcher(s, cache=cache, resolver=_pub_resolver, min_host_interval_seconds=0.0)
    search_c = SearchClient(s, resolver=_pub_resolver)
    router = ProviderRouter(s, cache=cache)
    vx = VortexenProfileManager(s, fetcher, cache=cache, router=router)
    service = ResearchService(client, wf, s, cache=cache, fetcher=fetcher, search_client=search_c, router=router, vortexen_manager=vx)

    home_route = respx.get("https://vortexen.com/").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                '<html><head><title>Vortexen</title>'
                '<meta name="description" content="Industrial engineering network."></head>'
                '<body><a href="/services">Services</a>'
                '<p>Industries: Oil &amp; Gas, Petrochemical</p>'
                '<p>Offices: UAE, Saudi Arabia</p></body></html>'
            ),
        )
    )
    srv_route = respx.get("https://vortexen.com/services").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><h1>Corrosion Protection &amp; Coatings</h1><h1>Technical Inspection</h1></body></html>",
        )
    )
    respx.get("https://vortexen.com/sitemap.xml").mock(return_value=httpx.Response(404, text=""))

    try:
        r1 = await service.refresh_vortexen_profile(force=True)
        assert r1["ok"] and r1["read_only"] is True
        prof = r1["profile"]
        assert "Corrosion Protection & Coatings" in prof["services"]
        assert prof["version"] == 1
        assert home_route.call_count == 1
        assert srv_route.call_count == 1

        # Calling get_profile again uses cached profile without network calls
        prof2 = await vx.get_profile(force_refresh=False)
        assert prof2 is not None and prof2.content_hash == prof["content_hash"]
        assert home_route.call_count == 1
        assert client.count("create") == 0
    finally:
        await service.aclose()


@respx.mock
async def test_event_research_verifies_exhibitors_dedupes_and_respects_limits(client, wf, tmp_path):
    ev_id = str(uuid.uuid4())
    client.visible_market_events = [{
        "id": ev_id,
        "event_title": "Gulf Manufacturing Expo 2026",
        "host_country": "UAE",
        "website_url": "https://gulfexpo.example/",
    }]
    s = Settings(
        api_base_url="https://api.test/api",
        username="pre_user",
        password="p",
        research_cache_db_path=tmp_path / "ev.db",
        search_provider="none",
        research_max_event_participants=5,
        research_respect_robots_txt=False,
    )
    cache = SQLiteResearchCache(s.research_cache_db_path)
    fetcher = SafeFetcher(s, cache=cache, resolver=_pub_resolver, min_host_interval_seconds=0.0)
    search_c = SearchClient(s, resolver=_pub_resolver)
    router = ProviderRouter(s, cache=cache)
    vx = VortexenProfileManager(s, fetcher, cache=cache, router=router)
    service = ResearchService(client, wf, s, cache=cache, fetcher=fetcher, search_client=search_c, router=router, vortexen_manager=vx)

    # Mock Vortexen
    respx.get("https://vortexen.com/").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><h1>Industrial Coatings</h1><p>Industries: Manufacturing</p></body></html>",
        )
    )
    respx.get("https://vortexen.com/sitemap.xml").mock(return_value=httpx.Response(404, text=""))

    # Mock official event pages with table listing exhibitors (including a duplicate across sponsor/exhibitor)
    respx.get("https://gulfexpo.example/").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                '<html><head><title>Gulf Manufacturing Expo 2026</title></head>'
                '<body><a href="/exhibitors">Exhibitors</a><a href="/sponsors">Sponsors</a>'
                '<a href="/vip-portal">VIP Portal</a></body></html>'
            ),
        )
    )
    respx.get("https://gulfexpo.example/exhibitors").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<html><body><h1>Exhibitors</h1>"
                "<table><tr><th>Exhibitor</th><th>Country</th></tr>"
                "<tr><td>Beta Coatings Co</td><td>UAE</td></tr>"
                "<tr><td>Gamma Valves Ltd</td><td>India</td></tr>"
                "</table></body></html>"
            ),
        )
    )
    respx.get("https://gulfexpo.example/sponsors").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<html><body><h1>Sponsors</h1>"
                "<table><tr><th>Sponsor</th><th>Tier</th></tr>"
                "<tr><td>Beta Coatings Co</td><td>Gold</td></tr>"
                "</table></body></html>"
            ),
        )
    )
    respx.get("https://gulfexpo.example/vip-portal").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )

    try:
        res = await service.research_event("Gulf Manufacturing Expo 2026", max_participants=2, research_companies=True)
        assert res["ok"] and res["read_only"] is True
        assert res["event_details"]["matched_market_event_id"] == ev_id

        # Beta Coatings appeared in both exhibitors & sponsors -> deduplicated to 1 entry with both roles!
        discovered = res["participants_discovered"]
        assert len(discovered) == 2
        beta = [p for p in discovered if p["company_name"] == "Beta Coatings Co"][0]
        assert set(beta["roles"]) == {"Exhibitor", "Sponsor"}

        # Two participants researched cleanly with event UUID attached; zero DB writes!
        assert len(res["participant_companies_researched"]) == 2
        assert all(
            ev_id in comp["prospect_candidate"].get("market_event_ids", [])
            for comp in res["participant_companies_researched"]
        )
        assert client.count("create") == 0
        assert client.count("create_event") == 0
    finally:
        await service.aclose()
