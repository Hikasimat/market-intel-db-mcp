import pytest

from config import ConfigError, load_settings
from server import create_mcp
from conftest import valid_prospect

EXPECTED = {"search_existing_prospects", "validate_prospect_data", "create_confirmed_prospect"}


@pytest.fixture
def mcp(wf):
    return create_mcp(lambda: wf)


async def test_exactly_three_tools_registered(mcp):
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED
    forbidden = ("delete", "remove", "update", "edit", "modify", "bulk", "import", "request", "http", "shell")
    assert not [n for n in names if any(f in n for f in forbidden)]


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
