"""Market Intel prospect-entry MCP server (stdio). Exactly three tools; nothing else.

IMPORTANT: stdout carries the MCP protocol. All logging goes to stderr.
Run `python server.py --check` for an authentication/role health check.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer  # mcp 2.x (renamed from FastMCP in 1.x)

from api_client import AuthError, ApiError, MarketIntelClient
from config import ConfigError, load_settings
from confirmation_store import ConfirmationStore
from workflow import Workflow

INSTRUCTIONS = """Restricted prospect-entry assistant for Market Intel DB.
It can only (1) search prospects, (2) validate a NEW prospect, (3) create one confirmed prospect.
Updating and deleting prospects is NOT possible. Never invent field values, emails, IDs or phone numbers:
ask the user for anything missing. Workflow: search -> validate -> show summary -> get explicit user approval -> create."""

PROSPECT_FIELDS_HELP = """
Required: company_name, country_head_office, complete_address, primary_industries,
ownership_sector (Private Company | Government Company | Semi Government Company),
company_structure (Parent | Branch | Subsidiary), operational_status (Active | Inactive |
Permanently Closed | Acquired | Merged), primary_offering_type (Products | Services | Solutions | Multiple).
Optional: official_phone_number, official_email_address, official_website_url, linkedin_company_page,
parent_companies (list of existing prospect UUIDs; required for Branch/Subsidiary - find them with
search_existing_prospects), merging_companies, dissolved_companies, acquiring_companies_data
([{company_id, acquisition_date?}]), offerings_data ([{offering_type: Product|Service|Solution, name}]),
key_contacts ([{prefix?, contact_name, designation, official_email?, phone_number?, linkedin_profile?}]),
shareholdings. Unsupported fields are rejected."""


def create_mcp(get_workflow: Callable[[], Workflow]) -> MCPServer:
    mcp = MCPServer("market-intel-prospect-entry", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def search_existing_prospects(query: str, country: str | None = None, limit: int = 10) -> dict[str, Any]:
        """READ-ONLY. Search existing prospects by company name/phrase (optional country filter, limit max 25).
        Use it to spot possible duplicates and to find parent-company IDs. Returns minimal fields only.
        Results are limited to what this PRE account can see, so 'no match' does NOT prove the company is new;
        the backend performs the final global duplicate check at creation. Does not create anything."""
        return await get_workflow().search(query, country, limit)

    @mcp.tool(description=(
        "Validate a NEW prospect WITHOUT creating it. Checks required fields, enums, formats, parent companies, "
        "duplicates, offerings, key contacts, and verifies EVERY supplied company/contact email via the existing "
        "email-verification API (each must be VALID; INVALID/UNKNOWN/errors block creation). Missing required "
        "fields must be requested from the user - never invent values. On success returns a readable summary and a "
        "one-time confirmation_token; show the summary to the user and ask for explicit approval before calling "
        "create_confirmed_prospect. Updating/deleting prospects is not available." + PROSPECT_FIELDS_HELP))
    async def validate_prospect_data(prospect: dict[str, Any]) -> dict[str, Any]:
        return await get_workflow().validate(prospect)

    @mcp.tool()
    async def create_confirmed_prospect(confirmation_token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        """Create exactly ONE prospect from a previously validated payload. Call ONLY after the user has seen the
        validation summary and explicitly approved creation; set user_has_explicitly_confirmed=true only then.
        Takes no new data: the reviewed payload is bound to the token (single-use, expires). Emails are re-verified
        and duplicates re-checked first. On a timeout the outcome is UNCERTAIN: do NOT retry; search first."""
        return await get_workflow().create(confirmation_token, user_has_explicitly_confirmed)

    return mcp


def _build_workflow() -> tuple[Workflow, MarketIntelClient]:
    settings = load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs; keep quiet
    client = MarketIntelClient(settings)
    return Workflow(client, ConfirmationStore(settings.confirmation_ttl_seconds), settings), client


async def _health_check() -> int:
    workflow, client = _build_workflow()
    try:
        ident = await client.authenticate()
        print(f"OK: authenticated as '{ident.username}' with role {ident.role}.")
        return 0
    except (AuthError, ApiError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()


class TokenAuthMiddleware:
    """ASGI middleware to enforce token authentication on HTTP/SSE endpoints.

    Accepts token via:
      1. Query parameter: ?token=<secret> (ideal for Claude.ai custom connectors)
      2. Authorization header: Bearer <secret>
    """
    def __init__(self, app, secret_token: str):
        self.app = app
        self.secret_token = secret_token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from urllib.parse import parse_qs
            from starlette.responses import JSONResponse

            # Check query params: ?token=...
            query_string = scope.get("query_string", b"").decode("utf-8")
            params = parse_qs(query_string)
            token = params.get("token", [None])[0]

            # Check Authorization: Bearer ... header
            if not token:
                for name, value in scope.get("headers", []):
                    if name.lower() == b"authorization":
                        header_val = value.decode("utf-8")
                        if header_val.startswith("Bearer "):
                            token = header_val[7:].strip()
                        break

            if token != self.secret_token:
                response = JSONResponse(
                    {"error": "Unauthorized: valid token required (?token=<secret> or Bearer auth)"},
                    status_code=401,
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Market Intel prospect-entry MCP server")
    parser.add_argument("--check", action="store_true", help="Run pre-flight auth & role health check and exit")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio",
                        help="MCP transport protocol (default: stdio)")
    parser.add_argument("--sse", action="store_const", const="sse", dest="transport",
                        help="Run as an HTTP SSE server (alias for --transport sse)")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind on (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8001, help="Port to listen on (default: 8001)")
    parser.add_argument("--auth-token", default=None,
                        help="Secret token required to access SSE/HTTP endpoints (or set MCP_AUTH_TOKEN in .env)")
    args = parser.parse_args()

    if args.check:
        sys.exit(asyncio.run(_health_check()))

    try:
        workflow, _client = _build_workflow()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    auth_token = args.auth_token or workflow.settings.auth_token
    mcp = create_mcp(lambda: workflow)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        import uvicorn
        app = mcp.sse_app(host=args.host)
        if auth_token:
            app.add_middleware(TokenAuthMiddleware, secret_token=auth_token)
            print(f"Token protection ACTIVE (use ?token={auth_token[:4]}***).", file=sys.stderr)
        else:
            print("WARNING: Running without token protection! Set MCP_AUTH_TOKEN in .env to secure.", file=sys.stderr)
        print(f"Starting MCP SSE server on http://{args.host}:{args.port}/sse", file=sys.stderr)
        uvicorn.run(app, host=args.host, port=args.port, log_level=workflow.settings.log_level.lower())
    elif args.transport == "streamable-http":
        import uvicorn
        app = mcp.streamable_http_app(host=args.host)
        if auth_token:
            app.add_middleware(TokenAuthMiddleware, secret_token=auth_token)
            print(f"Token protection ACTIVE (use ?token={auth_token[:4]}***).", file=sys.stderr)
        else:
            print("WARNING: Running without token protection! Set MCP_AUTH_TOKEN in .env to secure.", file=sys.stderr)
        print(f"Starting MCP Streamable-HTTP server on http://{args.host}:{args.port}/mcp", file=sys.stderr)
        uvicorn.run(app, host=args.host, port=args.port, log_level=workflow.settings.log_level.lower())


if __name__ == "__main__":
    main()
