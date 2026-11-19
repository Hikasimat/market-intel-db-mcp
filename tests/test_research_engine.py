"""Comprehensive mocked tests for 6-section Company Research Engine, accuracy rules, conflicts, and lookups."""
from __future__ import annotations

import uuid

import httpx
import pytest
import respx

from config import Settings
from research.cache import SQLiteResearchCache
from research.fetcher import SafeFetcher
from research.models import FieldEvidence, SourceReliability, SourceType, VerificationStatus
from research.provider_router import ProviderRouter
from research.search import SearchClient
from research.service import ResearchService
from research.verifier import resolve_field_candidates
from research.vortexen_profile import VortexenProfileManager


async def _pub_resolver(_host, _port):
    return ["93.184.216.34"]


def _build_service(client, wf, tmp_path, *, groq_key=None, ant_key=None, brave_key=None) -> ResearchService:
    s = Settings(
        api_base_url="https://api.test/api",
        username="pre_user",
        password="secret-pw",
        research_cache_db_path=tmp_path / "res.db",
        groq_api_key=groq_key,
        anthropic_api_key=ant_key,
        brave_search_api_key=brave_key,
        search_provider="brave" if brave_key else "none",
        research_respect_robots_txt=False,
    )
    wf.settings = s
    cache = SQLiteResearchCache(s.research_cache_db_path)
    fetcher = SafeFetcher(s, cache=cache, resolver=_pub_resolver, min_host_interval_seconds=0.0)
    search_c = SearchClient(s, resolver=_pub_resolver)
    router = ProviderRouter(s, cache=cache)
    vx = VortexenProfileManager(s, fetcher, cache=cache, router=router)
    return ResearchService(client, wf, s, cache=cache, fetcher=fetcher, search_client=search_c, router=router, vortexen_manager=vx)


@respx.mock
async def test_full_six_section_research_and_strict_verification(client, wf, tmp_path):
    parent_uuid = str(uuid.uuid4())
    gov_uuid = str(uuid.uuid4())
    event_uuid = str(uuid.uuid4())
    client.visible = [{
        "id": parent_uuid,
        "company_name": "Global Holdings Group Ltd",
        "country_head_office": "UAE",
        "official_website_url": "https://globalholdings.example",
    }]
    client.visible_gov_entities = [{"id": gov_uuid, "name": "Abu Dhabi Investment Authority", "country": "UAE"}]
    client.visible_market_events = [{"id": event_uuid, "event_title": "ADIPEC 2026", "host_country": "UAE"}]

    # Email statuses: valid contact + valid company email; invalid guessed email rejected
    client.email_status["info@alpha.example"] = "VALID"
    client.email_status["sara.ali@alpha.example"] = "VALID"
    client.email_status["bad@alpha.example"] = "INVALID"

    # Mock Vortexen site minimally
    respx.get("https://vortexen.com/").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<html><head><title>Vortexen</title></head><body>"
                "<h1>Protective Coatings &amp; Passive Fire Protection</h1>"
                "<p>Industries: Oil &amp; Gas, Petrochemical, Industrial Manufacturing</p>"
                "<p>Offices: UAE, Saudi Arabia, India</p>"
                "</body></html>"
            ),
        )
    )
    respx.get("https://vortexen.com/sitemap.xml").mock(return_value=httpx.Response(404, text=""))

    # Mock Alpha company pages
    respx.get("https://alpha.example/").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                '<html><head><title>Alpha Industrial Systems</title>'
                '<script type="application/ld+json">{"@type":"Organization","name":"Alpha Industrial Systems LLC",'
                '"address":{"streetAddress":"Plot 42 Musaffah","addressCountry":"UAE"},'
                '"telephone":"+971 2 555 0100","email":"info@alpha.example",'
                '"sameAs":["https://www.linkedin.com/company/alpha-industrial"]}</script></head>'
                '<body><a href="/solutions">Our Solutions</a><a href="/investors">Investors</a>'
                '<a href="/leadership">Leadership</a>'
                '<p>Alpha Industrial Systems specializes in Oil &amp; Gas downstream automation and tank protection. '
                'Visit us at ADIPEC 2026.</p></body></html>'
            ),
        )
    )
    respx.get("https://alpha.example/solutions").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<html><body><h1>Solutions</h1>"
                "<p>Products and Services: Intumescent Fireproofing Systems, Industrial Tank Coatings, Plant Shutdown Turnaround Support</p>"
                "</body></html>"
            ),
        )
    )
    respx.get("https://alpha.example/investors").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<html><body><h1>Investors</h1>"
                "<p>Alpha Industrial Systems LLC is a subsidiary of Global Holdings Group Ltd. "
                "Shareholders: Global Holdings Group holds 70%, Abu Dhabi Investment Authority holds 30%. "
                "Board Director John Doe serves as Independent Director (no shares disclosed).</p></body></html>"
            ),
        )
    )
    respx.get("https://alpha.example/leadership").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<html><body><h1>Leadership</h1>"
                "<p>Sara Ali - Head of Engineering - sara.ali@alpha.example</p>"
                "<p>Omar Hassan - Procurement Manager - bad@alpha.example</p>"
                "<p>Ali Khan - Director (no email listed)</p>"
                "</body></html>"
            ),
        )
    )

    # Mock Groq LLM returning structured JSON grounded in the pages above
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        side_effect=[
            # 1. offerings_and_industries
            httpx.Response(200, json={
                "choices": [{"message": {"content": """{
                    "primary_industries": "Oil & Gas, Industrial Coatings",
                    "primary_offering_type": "Multiple",
                    "offerings": [
                        {"offering_type": "Product", "name": "Intumescent Fireproofing Systems", "source_url": "https://alpha.example/solutions", "evidence_quote": "Intumescent Fireproofing Systems"},
                        {"offering_type": "Service", "name": "Plant Shutdown Turnaround Support", "source_url": "https://alpha.example/solutions", "evidence_quote": "Plant Shutdown Turnaround Support"}
                    ],
                    "business_summary": "Oil & Gas downstream automation and industrial tank protection provider.",
                    "evidence_quote": "Oil & Gas downstream automation and tank protection",
                    "source_url": "https://alpha.example/"
                }"""}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }),
            # 2. corporate_structure_and_ownership
            httpx.Response(200, json={
                "choices": [{"message": {"content": """{
                    "company_structure": {"value": "Subsidiary", "source_url": "https://alpha.example/investors", "evidence_quote": "subsidiary of Global Holdings Group Ltd"},
                    "operational_status": {"value": "Active", "source_url": "https://alpha.example/", "evidence_quote": "Alpha Industrial Systems specializes"},
                    "ownership_sector": {"value": "Semi Government Company", "source_url": "https://alpha.example/investors", "evidence_quote": "Abu Dhabi Investment Authority holds 30%"},
                    "parent_organizations": [{"name": "Global Holdings Group Ltd", "country": "UAE", "domain": "globalholdings.example", "source_url": "https://alpha.example/investors", "evidence_quote": "subsidiary of Global Holdings Group Ltd"}],
                    "subsidiaries_and_branches": [],
                    "acquiring_companies": [],
                    "merging_companies": [],
                    "shareholders": [
                        {"holder_type": "Company", "holder_name": "Global Holdings Group Ltd", "share_percentage": 70, "source_url": "https://alpha.example/investors", "evidence_quote": "Global Holdings Group holds 70%"},
                        {"holder_type": "Government", "holder_name": "Abu Dhabi Investment Authority", "share_percentage": 30, "source_url": "https://alpha.example/investors", "evidence_quote": "Abu Dhabi Investment Authority holds 30%"},
                        {"holder_type": "Individual", "holder_name": "John Doe", "share_percentage": null, "source_url": "https://alpha.example/investors", "evidence_quote": "Independent Director"}
                    ],
                    "directors_only": [{"name": "John Doe", "role": "Independent Director", "source_url": "https://alpha.example/investors"}],
                    "ambiguity_notes": []
                }"""}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 60},
            }),
            # 3. key_contacts (includes one valid, one INVALID email, one missing email, one pattern-guessed ungrounded email)
            httpx.Response(200, json={
                "choices": [{"message": {"content": """{
                    "contacts": [
                        {"contact_name": "Sara Ali", "designation": "Head of Engineering", "official_email": "sara.ali@alpha.example", "source_url": "https://alpha.example/leadership", "evidence_quote": "Sara Ali - Head of Engineering"},
                        {"contact_name": "Omar Hassan", "designation": "Procurement Manager", "official_email": "bad@alpha.example", "source_url": "https://alpha.example/leadership", "evidence_quote": "Omar Hassan - Procurement Manager"},
                        {"contact_name": "Ali Khan", "designation": "Director", "official_email": "ali.khan@alpha.example", "source_url": "https://alpha.example/leadership", "evidence_quote": "Ali Khan - Director"},
                        {"contact_name": "No Email Exec", "designation": "CEO", "official_email": null, "source_url": "https://alpha.example/leadership", "evidence_quote": ""}
                    ]
                }"""}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 40},
            }),
            # 4. vortexen profile LLM extraction
            httpx.Response(200, json={
                "choices": [{"message": {"content": """{
                    "company_name": "Vortexen",
                    "overview": "Industrial protection and turnaround solutions.",
                    "services": ["Protective Coatings", "Passive Fire Protection", "Shutdown Turnaround Support"],
                    "products": ["Intumescent Fireproofing Systems"],
                    "solutions": ["Protect"],
                    "primary_industries": ["Oil & Gas", "Petrochemical"],
                    "target_industries": ["Oil & Gas", "Industrial Manufacturing"],
                    "geographic_footprint": ["UAE", "Saudi Arabia", "India"]
                }"""}}],
                "usage": {"prompt_tokens": 90, "completion_tokens": 40},
            }),
        ]
    )

    service = _build_service(client, wf, tmp_path, groq_key="gsk_test_1")
    try:
        res = await service.research_company("Alpha Industrial Systems", country="UAE", official_website="https://alpha.example/")
        assert res["ok"] and res["read_only"] is True
        # Ensure zero writes occurred!
        assert client.count("create") == 0
        assert client.count("create_gov") == 0
        assert client.count("create_event") == 0

        cand = res["prospect_candidate"]
        assert cand["company_name"] == "Alpha Industrial Systems LLC"
        assert cand["country_head_office"] == "UAE"
        assert cand["company_structure"] == "Subsidiary"
        assert cand["parent_companies"] == [parent_uuid]
        assert cand["primary_offering_type"] == "Multiple"
        assert len(cand["offerings_data"]) == 2
        assert cand["market_event_ids"] == [event_uuid]

        # Shareholdings: John Doe (director without shares) MUST NOT appear!
        assert len(cand["shareholdings"]) == 2
        assert any(s["holder_type"] == "Government" and s["government_entity"] == gov_uuid for s in cand["shareholdings"])

        # Key Contacts: ONLY Sara Ali passes (grounded email + designation + VALID instant_verify)
        assert len(cand["key_contacts"]) == 1
        assert cand["key_contacts"][0]["contact_name"] == "Sara Ali"
        assert cand["key_contacts"][0]["official_email"] == "sara.ali@alpha.example"
        assert len(res["contact_candidates"]) == 4  # 3 excluded transparently with reasons

        # Vortexen alignment
        assert res["vortexen_relevance"]["compatibility"] in ("HIGH", "MEDIUM")
        assert res["vortexen_relevance"]["relevance_score"] > 0

        # Candidate payload must pass existing prospect validation cleanly!
        val = await wf.validate(cand)
        assert val["valid"], val["errors"]
    finally:
        await service.aclose()


async def test_nonexistent_company_returns_unavailable_without_fabrication(client, wf, tmp_path):
    service = _build_service(client, wf, tmp_path)
    try:
        res = await service.research_company("Completely Nonexistent ZZZ Corp 999")
        assert res["ok"] and res["read_only"] is True
        cand = res["prospect_candidate"]
        assert cand == {} or "offerings_data" not in cand
        assert "primary_industries" in res["missing_fields"]
        assert res["vortexen_relevance"]["compatibility"] == "INSUFFICIENT_DATA"
        assert res["vortexen_relevance"]["relevance_score"] is None
    finally:
        await service.aclose()


def test_conflict_resolution_preserves_candidates_and_protects_cached_verified_values():
    ev_official = FieldEvidence(
        field_name="country_head_office",
        value="Germany",
        source_url="https://acme.de/imprint",
        source_type=SourceType.OFFICIAL_WEBSITE,
        source_reliability=SourceReliability.HIGH,
        verification_status=VerificationStatus.VERIFIED,
    )
    ev_snippet = FieldEvidence(
        field_name="country_head_office",
        value="United States",
        source_url="https://dir.example/acme",
        source_type=SourceType.WEB_SEARCH_SNIPPET,
        source_reliability=SourceReliability.LOW,
        verification_status=VerificationStatus.REQUIRES_REVIEW,
    )
    val, status, _, conflict = resolve_field_candidates("country_head_office", [ev_official, ev_snippet])
    assert val == "Germany"
    assert status == VerificationStatus.VERIFIED
    assert conflict is not None and conflict.resolution_status == "RESOLVED_BY_AUTHORITY"
    assert len(conflict.candidates) == 2

    # Equal-authority conflict with no clear winner -> CONFLICTING
    ev_dir2 = FieldEvidence(
        field_name="country_head_office",
        value="Switzerland",
        source_url="https://other.de/about",
        source_type=SourceType.OFFICIAL_WEBSITE,
        source_reliability=SourceReliability.HIGH,
    )
    val2, status2, _, conflict2 = resolve_field_candidates("country_head_office", [ev_official, ev_dir2])
    assert val2 is None
    assert status2 == VerificationStatus.CONFLICTING
    assert conflict2.resolution_status == "UNRESOLVED_CONFLICT"

    # Existing verified cached value is never silently overwritten on conflict
    val3, status3, _, conflict3 = resolve_field_candidates(
        "country_head_office", [ev_dir2], existing_verified_value="Germany"
    )
    assert val3 == "Germany"
    assert status3 == VerificationStatus.CONFLICTING


@respx.mock
async def test_parent_not_linked_by_loose_name_and_missing_parent_reported_as_prerequisite(client, wf, tmp_path):
    # Similar-looking unrelated company in DB must NOT be auto-linked!
    client.visible = [{
        "id": str(uuid.uuid4()),
        "company_name": "Atlas Corp Global",
        "country_head_office": "Canada",
    }]
    respx.get("https://vortexen.com/").mock(return_value=httpx.Response(404, text=""))
    respx.get("https://vortexen.com/sitemap.xml").mock(return_value=httpx.Response(404, text=""))
    respx.get("https://atlaskids.example/").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><p>Atlas Kids Co is a subsidiary of Atlas Holdings International.</p></body></html>",
        )
    )
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json={"choices": [{"message": {"content": '{"primary_industries": null, "primary_offering_type": null, "offerings": []}'}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": """{
                "company_structure": {"value": "Subsidiary", "source_url": "https://atlaskids.example/", "evidence_quote": "subsidiary of Atlas Holdings International"},
                "operational_status": {"value": "Active", "source_url": "https://atlaskids.example/", "evidence_quote": "Atlas Kids Co"},
                "ownership_sector": {"value": "Private Company", "source_url": "https://atlaskids.example/", "evidence_quote": "Atlas Kids Co"},
                "parent_organizations": [{"name": "Atlas Holdings International", "country": "UK", "source_url": "https://atlaskids.example/", "evidence_quote": "subsidiary of Atlas Holdings International"}],
                "shareholders": []
            }"""}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": '{"contacts": []}'}}]}),
        ]
    )
    service = _build_service(client, wf, tmp_path, groq_key="gsk_test")
    try:
        res = await service.research_company("Atlas Kids Co", official_website="https://atlaskids.example/")
        assert "parent_companies" not in res["prospect_candidate"]
        assert len(res["missing_prerequisites"]["parent_companies"]) == 1
        assert res["missing_prerequisites"]["parent_companies"][0]["company_name"] == "Atlas Holdings International"
    finally:
        await service.aclose()


@respx.mock
async def test_acquisition_merger_unknown_email_and_low_quality_sources(client, wf, tmp_path):
    acquirer_uuid = str(uuid.uuid4())
    client.visible = [{
        "id": acquirer_uuid,
        "company_name": "Acquirer Global Corp",
        "country_head_office": "USA",
        "official_website_url": "https://acquirerglobal.example",
    }]
    client.email_status["lead@acquired.example"] = "UNKNOWN"

    respx.get("https://vortexen.com/").mock(return_value=httpx.Response(404, text=""))
    respx.get("https://vortexen.com/sitemap.xml").mock(return_value=httpx.Response(404, text=""))
    respx.get("https://acquired.example/").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<html><body><h1>Acquired Tech Inc</h1>"
                "<p>Acquired by Acquirer Global Corp in 2025 (100% shares acquired).</p>"
                "<p>Contact Lead: Jane Smith, VP Engineering, lead@acquired.example</p>"
                "</body></html>"
            ),
        )
    )
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json={"choices": [{"message": {"content": '{"primary_industries": "Industrial Software", "primary_offering_type": "Solutions", "offerings": [], "evidence_quote": "Acquired Tech Inc", "source_url": "https://acquired.example/"}'}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": '{"country_head_office": {"value": "USA", "source_url": "https://acquired.example/", "evidence_quote": "Acquired Tech Inc"}}'}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": """{
                "company_structure": {"value": "Parent", "source_url": "https://acquired.example/", "evidence_quote": "Acquired Tech Inc"},
                "operational_status": {"value": "Acquired", "source_url": "https://acquired.example/", "evidence_quote": "Acquired by Acquirer Global Corp"},
                "ownership_sector": {"value": "Private Company", "source_url": "https://acquired.example/", "evidence_quote": "Acquired Tech Inc"},
                "acquiring_companies": [{"name": "Acquirer Global Corp", "acquired_shares": 100, "source_url": "https://acquired.example/", "evidence_quote": "Acquired by Acquirer Global Corp"}],
                "shareholders": []
            }"""}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": """{
                "contacts": [{"contact_name": "Jane Smith", "designation": "VP Engineering", "official_email": "lead@acquired.example", "source_url": "https://acquired.example/", "evidence_quote": "Jane Smith, VP Engineering"}]
            }"""}}]}),
        ]
    )

    service = _build_service(client, wf, tmp_path, groq_key="gsk_test")
    try:
        res = await service.research_company("Acquired Tech Inc", official_website="https://acquired.example/")
        cand = res["prospect_candidate"]
        assert cand["operational_status"] == "Acquired"
        assert cand["acquiring_companies_data"] == [{"acquiring_company": acquirer_uuid, "acquired_shares": 100.0}]
        # UNKNOWN email excludes contact without blocking remaining fields!
        assert "key_contacts" not in cand
        assert res["contact_candidates"][0]["email_verification_status"] == "UNKNOWN"
        assert res["contact_candidates"][0]["included_in_payload"] is False

        # Snippet-only evidence must be marked REQUIRES_REVIEW, never VERIFIED
        from research.verifier import build_evidence
        snip_ev = build_evidence(
            "complete_address",
            "123 Main St",
            "https://snippet.example/x",
            {},
            is_snippet_only=True,
        )
        assert snip_ev.verification_status == VerificationStatus.REQUIRES_REVIEW
    finally:
        await service.aclose()
