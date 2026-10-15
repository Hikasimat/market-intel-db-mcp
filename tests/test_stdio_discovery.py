"""Real MCP protocol check: spawn server.py over stdio and discover tools (no backend contact needed)."""
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).resolve().parent.parent / "server.py"


async def test_stdio_discovery_lists_exactly_three_tools():
    env = {**os.environ, "MARKET_INTEL_API_BASE_URL": "https://example.invalid/api",
           "MARKET_INTEL_USERNAME": "x", "MARKET_INTEL_PASSWORD": "y"}
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {
                "search_existing_prospects", "validate_prospect_data", "create_confirmed_prospect"}
