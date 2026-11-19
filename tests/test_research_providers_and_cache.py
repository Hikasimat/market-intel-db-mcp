"""Tests for Groq/Anthropic/OpenAI provider routing, caching, expiration, field-level refresh, and token budgets."""
from __future__ import annotations

import httpx
import pytest
import respx

from config import Settings
from research.cache import SQLiteResearchCache
from research.models import FieldEvidence, ResearchBudget, SourceType, VerificationStatus
from research.provider_router import ProviderRouter


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@respx.mock
async def test_provider_routing_primary_escalation_fallback_and_cost_tracking(tmp_path):
    s = Settings(
        api_base_url="https://api.test/api",
        username="u",
        password="p",
        groq_api_key="gsk_test_1",
        anthropic_api_key="sk-ant-test_1",
        openai_api_key="sk-test_1",
        research_cache_db_path=tmp_path / "cache.db",
    )
    cache = SQLiteResearchCache(s.research_cache_db_path)
    router = ProviderRouter(s, cache=cache)

    groq_route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"primary_industries": "Industrial Coatings"}'}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            },
        )
    )
    ant_route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": '{"resolution_status": "RESOLVED_BY_AUTHORITY"}'}],
                "usage": {"input_tokens": 200, "output_tokens": 50},
            },
        )
    )

    try:
        assert set(router.enabled_providers()) == {"groq", "openai", "anthropic"}
        budget = ResearchBudget(max_llm_calls=5, max_total_tokens=10_000)

        # 1. Stage 2 primary calls Groq once
        out1 = await router.complete_json(
            task_type="offerings",
            system_prompt="sys",
            user_prompt="usr1",
            stage="primary",
            budget=budget,
        )
        assert out1 == {"primary_industries": "Industrial Coatings"}
        assert groq_route.call_count == 1
        assert ant_route.call_count == 0

        # 2. Repeated identical prompt hits LLM cache -> 0 new HTTP calls, 0 new tokens
        out2 = await router.complete_json(
            task_type="offerings",
            system_prompt="sys",
            user_prompt="usr1",
            stage="primary",
            budget=budget,
        )
        assert out2 == out1
        assert groq_route.call_count == 1
        assert budget.llm_cache_hits == 1

        # 3. Stage 3 escalation routes to Anthropic Claude
        out3 = await router.complete_json(
            task_type="conflict",
            system_prompt="sys",
            user_prompt="usr2",
            stage="escalation",
            budget=budget,
        )
        assert out3["resolution_status"] == "RESOLVED_BY_AUTHORITY"
        assert ant_route.call_count == 1

        summary = budget.to_summary()
        assert summary.total_input_tokens == 320
        assert summary.total_output_tokens == 80
        assert summary.reported_cost_usd is None
        assert summary.estimated_cost_usd is not None and summary.estimated_cost_usd > 0

        # 4. When Groq fails with 503, falls back to OpenAI automatically
        respx.post("https://api.groq.com/openai/v1/chat/completions").mock(return_value=httpx.Response(503, json={}))
        oai_route = respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 10},
                },
            )
        )
        out4 = await router.complete_json(
            task_type="fallback_check",
            system_prompt="sys",
            user_prompt="usr3",
            stage="primary",
            budget=budget,
        )
        assert out4 == {"ok": True}
        assert oai_route.call_count == 1
    finally:
        await router.aclose()


async def test_only_configured_providers_enabled_and_budget_stops_excess_calls(tmp_path):
    s = Settings(
        api_base_url="https://api.test/api",
        username="u",
        password="p",
        groq_api_key=None,
        anthropic_api_key=None,
        openai_api_key=None,
        research_cache_db_path=tmp_path / "cache2.db",
    )
    router = ProviderRouter(s)
    try:
        assert router.enabled_providers() == []
        budget = ResearchBudget(max_llm_calls=1, max_total_tokens=100)
        assert await router.complete_json(task_type="t", system_prompt="s", user_prompt="u", budget=budget) is None
    finally:
        await router.aclose()


def test_field_cache_expiration_and_stale_data_never_treated_as_verified(tmp_path):
    clock = FakeClock(1000.0)
    cache = SQLiteResearchCache(tmp_path / "ttl.db", clock=clock)
    ev = [FieldEvidence(
        field_name="primary_industries",
        value="Industrial Automation",
        source_url="https://acme.example",
        source_type=SourceType.OFFICIAL_WEBSITE,
        verification_status=VerificationStatus.VERIFIED,
        confidence_score=0.95,
    )]
    cache.set_company_field("acme|germany", "primary_industries", "Industrial Automation", ev, VerificationStatus.VERIFIED, ["h1"], ttl_seconds=60)

    fresh, stale = cache.get_company_fields("acme|germany")
    assert "primary_industries" in fresh and "primary_industries" not in stale

    # Advance past TTL -> must move to stale, NEVER fresh!
    clock.t += 61.0
    fresh2, stale2 = cache.get_company_fields("acme|germany")
    assert "primary_industries" not in fresh2
    assert "primary_industries" in stale2


@respx.mock
async def test_incremental_field_level_refresh_and_hash_invalidation(tmp_path):
    """Verify verified cached fields skip LLM extraction until source content hash changes."""
    from conftest import FakeClient
    from confirmation_store import ConfirmationStore
    from research.extractor import extract_all_sections
    from research.models import FetchedPage, SourceReliability
    from research.cache import sha256_text
    from workflow import Workflow

    s = Settings(
        api_base_url="https://api.test/api",
        username="pre_user",
        password="p",
        groq_api_key="gsk_test",
        research_cache_db_path=tmp_path / "inc.db",
    )
    cache = SQLiteResearchCache(s.research_cache_db_path)
    router = ProviderRouter(s, cache=cache)
    fc = FakeClient()
    wf = Workflow(fc, ConfirmationStore(600), s)

    page_v1_text = "Acme builds industrial valves and offers valve maintenance services. Industry: Industrial Valves."
    page_v1 = FetchedPage(
        url="https://acme.example/",
        final_url="https://acme.example/",
        domain="acme.example",
        title="Acme Valves",
        text=page_v1_text,
        content_hash=sha256_text(page_v1_text),
        source_type=SourceType.OFFICIAL_WEBSITE,
        source_reliability=SourceReliability.HIGH,
    )
    # 1. Seed verified cached fields for offerings & industries matching page_v1 hash
    ev = [FieldEvidence(
        field_name="primary_industries",
        value="Industrial Valves",
        source_url="https://acme.example/",
        source_type=SourceType.OFFICIAL_WEBSITE,
        source_reliability=SourceReliability.HIGH,
        verification_status=VerificationStatus.VERIFIED,
    )]
    cache.set_company_field("acme|usa", "primary_industries", "Industrial Valves", ev, VerificationStatus.VERIFIED, [page_v1.content_hash], 600)
    cache.set_company_field("acme|usa", "offerings_data", [{"offering_type": "Service", "name": "Valve Maintenance"}], ev, VerificationStatus.VERIFIED, [page_v1.content_hash], 600)
    cache.set_company_field("acme|usa", "primary_offering_type", "Services", ev, VerificationStatus.VERIFIED, [page_v1.content_hash], 600)

    groq_route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": '{"primary_industries": "Flow Control", "primary_offering_type": "Services", "offerings": [{"offering_type": "Service", "name": "Valve Calibration", "source_url": "https://acme.example/", "evidence_quote": "Valve Calibration"}], "evidence_quote": "Flow Control", "source_url": "https://acme.example/"}'}}], "usage": {"prompt_tokens": 30, "completion_tokens": 10}})
    )

    try:
        # Request ONLY market_offerings with matching source hash -> 3 field cache hits, 0 Groq calls!
        budget1 = ResearchBudget()
        out1 = await extract_all_sections(
            "Acme",
            company_key="acme|usa",
            country_hint="USA",
            official_website="https://acme.example/",
            official_domain="acme.example",
            pages=[page_v1],
            requested_sections={"market_offerings"},
            api_client=fc,
            router=router,
            cache=cache,
            settings=s,
            budget=budget1,
        )
        assert budget1.field_cache_hits == 3
        assert groq_route.call_count == 0
        assert out1["sections"]["market_offerings"]["from_cache"] is True

        # Change source page content hash -> cached fields are invalidated and Groq IS called!
        page_v2_text = "Acme now provides Flow Control and Valve Calibration."
        page_v2 = page_v1.model_copy(update={"text": page_v2_text, "content_hash": sha256_text(page_v2_text)})
        budget2 = ResearchBudget()
        out2 = await extract_all_sections(
            "Acme",
            company_key="acme|usa",
            country_hint="USA",
            official_website="https://acme.example/",
            official_domain="acme.example",
            pages=[page_v2],
            requested_sections={"market_offerings"},
            api_client=fc,
            router=router,
            cache=cache,
            settings=s,
            budget=budget2,
        )
        assert groq_route.call_count == 1
        assert out2["sections"]["market_offerings"]["from_cache"] is False
    finally:
        await router.aclose()


@respx.mock
async def test_search_providers_brave_tavily_serper_ddg_and_query_dedup():
    """Verify search adapters, SSRF filtering of malicious URLs, and query deduplication."""
    from research.search import SearchClient

    # Brave with an SSRF URL that must be stripped out
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "Local Trap", "url": "http://127.0.0.1/secret", "description": "bad"},
                        {"title": "Acme Official", "url": "https://acme.example/", "description": "ok"},
                    ]
                }
            },
        )
    )
    sb = SearchClient(Settings(api_base_url="https://api.test/api", username="u", password="p", search_provider="brave", brave_search_api_key="brv_1"))
    b_budget = ResearchBudget()
    res_b1 = await sb.search("Acme Corp", budget=b_budget)
    res_b2 = await sb.search("Acme Corp", budget=b_budget)  # deduplicated in-job!
    assert [r.url for r in res_b1] == ["https://acme.example/"]
    assert res_b2 == res_b1
    assert b_budget.search_calls == 1
    await sb.aclose()

    # Tavily
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json={"results": [{"title": "Tavily Hit", "url": "https://tav.example/", "content": "info"}]})
    )
    st = SearchClient(Settings(api_base_url="https://api.test/api", username="u", password="p", search_provider="tavily", tavily_api_key="tv_1"))
    assert [r.url for r in await st.search("q")] == ["https://tav.example/"]
    await st.aclose()

    # Serper
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(200, json={"organic": [{"title": "Serper Hit", "link": "https://serp.example/", "snippet": "info"}]})
    )
    ss = SearchClient(Settings(api_base_url="https://api.test/api", username="u", password="p", search_provider="serper", serper_api_key="sp_1"))
    assert [r.url for r in await ss.search("q")] == ["https://serp.example/"]
    await ss.aclose()

    # DuckDuckGo HTML fallback
    respx.post("https://html.duckduckgo.com/html/").mock(
        return_value=httpx.Response(
            200,
            text='<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fddg.example%2F">DDG Hit</a><a class="result__snippet">Snippet</a>',
        )
    )
    sd = SearchClient(Settings(api_base_url="https://api.test/api", username="u", password="p", search_provider="duckduckgo"))
    assert [r.url for r in await sd.search("q")] == ["https://ddg.example/"]
    await sd.aclose()
