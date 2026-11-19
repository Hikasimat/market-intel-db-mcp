import pytest

from config import ConfigError, Settings, load_settings
from conftest import valid_prospect
from research.cache import SQLiteResearchCache
from research.fetcher import SafeFetcher
from research.provider_router import ProviderRouter
from research.search import SearchClient
from research.service import ResearchService
from research.vortexen_profile import VortexenProfileManager
from server import create_mcp

EXPECTED = {
    "get_prospect_blueprint",
    "search_existing_prospects",
    "search_government_entities",
    "search_market_events",
    "get_record_details",
    "validate_prospect_data",
    "create_confirmed_prospect",
    "validate_government_entity",
    "create_confirmed_government_entity",
    "validate_market_event",
    "create_confirmed_market_event",
    "validate_record_creation",
    "create_confirmed_record",
    "validate_record_update",
    "update_confirmed_record",
    "research_company_prospect",
    "research_market_event",
    "get_research_cache_status",
    "refresh_vortexen_profile",
}


@pytest.fixture
def mcp(wf, tmp_path):
    s = Settings(
        api_base_url=wf.settings.api_base_url,
        username=wf.settings.username,
        password=wf.settings.password,
        research_cache_db_path=tmp_path / "mcp_research.db",
        search_provider="none",
    )
    cache = SQLiteResearchCache(s.research_cache_db_path)
    fetcher = SafeFetcher(s, cache=cache)
    search_c = SearchClient(s)
    router = ProviderRouter(s, cache=cache)
    vx = VortexenProfileManager(s, fetcher, cache=cache, router=router)
    rs = ResearchService(
        wf.client,
        wf,
        s,
        cache=cache,
        fetcher=fetcher,
        search_client=search_c,
        router=router,
        vortexen_manager=vx,
    )
    return create_mcp(lambda: wf, get_research_service=lambda: rs)


async def test_tools_registered_and_forbidden_actions_absent(mcp):
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED
    assert not [n for n in names if "whatsapp" in n.lower()]
    forbidden = ("delete", "remove", "destroy", "drop", "bulk", "batch", "import", "request", "http", "shell", "exec")
    assert not [n for n in names if any(f in n.lower() for f in forbidden)]


async def test_create_tool_schema_takes_no_prospect_payload(mcp):
    t = {t.name: t for t in await mcp.list_tools()}["create_confirmed_prospect"]
    assert set(t.input_schema["properties"]) == {"confirmation_token", "user_has_explicitly_confirmed"}


async def test_end_to_end_through_mcp_tools(mcp, client):
    def data(res):  # mcp 2.x: CallToolResult with structured_content (dict returns may be wrapped in 'result')
        s = res.structured_content
        assert not res.is_error, res
        return s.get("result", s) if set(s) == {"result"} else s

    s = data(await mcp.call_tool("search_existing_prospects", {"query": "ABC"}))
    assert s["ok"] and client.count("create") == 0
    v = data(await mcp.call_tool("validate_prospect_data", {"prospect": valid_prospect()}))
    assert v["valid"] and client.count("create") == 0
    c = data(await mcp.call_tool("create_confirmed_prospect", {
        "confirmation_token": v["confirmation_token"], "user_has_explicitly_confirmed": True}))
    assert c["created"] and client.count("create") == 1


async def test_research_tools_are_strictly_read_only(mcp, client):
    def data(res):
        s = res.structured_content
        assert not res.is_error, res
        return s.get("result", s) if set(s) == {"result"} else s

    cache_st = data(await mcp.call_tool("get_research_cache_status", {}))
    assert cache_st["ok"] and cache_st["read_only"]

    res = data(await mcp.call_tool("research_company_prospect", {"company_name": "Unknown Test Co"}))
    assert res["ok"] and res["read_only"] is True
    assert client.count("create") == 0
    assert client.count("create_gov") == 0
    assert client.count("create_event") == 0
    assert client.count("update_prospect") == 0


def test_config_missing_values_readable(monkeypatch, tmp_path):
    for n in ("MARKET_INTEL_API_BASE_URL", "MARKET_INTEL_USERNAME", "MARKET_INTEL_PASSWORD"):
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(ConfigError, match="MARKET_INTEL_USERNAME"):
        load_settings(tmp_path / "nonexistent.env")


def test_config_rejects_plain_http_remote(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKET_INTEL_API_BASE_URL", "http://prod.example.com/api")
    monkeypatch.setenv("MARKET_INTEL_USERNAME", "u")
    monkeypatch.setenv("MARKET_INTEL_PASSWORD", "p")
    with pytest.raises(ConfigError, match="HTTPS"):
        load_settings(tmp_path / "none.env")
    monkeypatch.setenv("MARKET_INTEL_API_BASE_URL", "http://localhost:8000/api/")
    assert load_settings(tmp_path / "none.env").api_base_url == "http://localhost:8000/api"
