"""Market Intel prospect-entry MCP server (stdio). Exactly three tools; nothing else.

IMPORTANT: stdout carries the MCP protocol. All logging goes to stderr.
Run `python server.py --check` for an authentication/role health check.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer  # mcp 2.x (renamed from FastMCP in 1.x)

from api_client import AuthError, ApiError, Identity, MarketIntelClient
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


def create_mcp(
    get_workflow: Callable[[], Workflow | Awaitable[Workflow]],
    auth_settings: Any = None,
    auth_server_provider: Any = None,
) -> MCPServer:
    mcp = MCPServer(
        "market-intel-prospect-entry",
        instructions=INSTRUCTIONS,
        auth=auth_settings,
        auth_server_provider=auth_server_provider,
    )

    async def _resolve_wf() -> Workflow:
        wf = get_workflow()
        if asyncio.iscoroutine(wf):
            return await wf
        return wf

    @mcp.tool()
    async def search_existing_prospects(query: str, country: str | None = None, limit: int = 10) -> dict[str, Any]:
        """READ-ONLY. Search existing prospects by company name/phrase (optional country filter, limit max 25).
        Use it to spot possible duplicates and to find parent-company IDs. Returns minimal fields only.
        Results are limited to what this PRE account can see, so 'no match' does NOT prove the company is new;
        the backend performs the final global duplicate check at creation. Does not create anything."""
        wf = await _resolve_wf()
        return await wf.search(query, country, limit)

    @mcp.tool(description=(
        "Validate a NEW prospect WITHOUT creating it. Checks required fields, enums, formats, parent companies, "
        "duplicates, offerings, key contacts, and verifies EVERY supplied company/contact email via the existing "
        "email-verification API (each must be VALID; INVALID/UNKNOWN/errors block creation). Missing required "
        "fields must be requested from the user - never invent values. On success returns a readable summary and a "
        "one-time confirmation_token; show the summary to the user and ask for explicit approval before calling "
        "create_confirmed_prospect. Updating/deleting prospects is not available." + PROSPECT_FIELDS_HELP))
    async def validate_prospect_data(prospect: dict[str, Any]) -> dict[str, Any]:
        wf = await _resolve_wf()
        return await wf.validate(prospect)

    @mcp.tool()
    async def create_confirmed_prospect(confirmation_token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        """Create exactly ONE prospect from a previously validated payload. Call ONLY after the user has seen the
        validation summary and explicitly approved creation; set user_has_explicitly_confirmed=true only then.
        Takes no new data: the reviewed payload is bound to the token (single-use, expires). Emails are re-verified
        and duplicates re-checked first. On a timeout the outcome is UNCERTAIN: do NOT retry; search first."""
        wf = await _resolve_wf()
        return await wf.create(confirmation_token, user_has_explicitly_confirmed)

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
    parser.add_argument("--oauth", action="store_true", default=None,
                        help="Enable OAuth 2.0 Sign In flow (or set MCP_OAUTH_ENABLED=true in .env)")
    parser.add_argument("--issuer-url", default=None,
                        help="Public HTTPS URL for OAuth (e.g. https://mcp.yourdomain.com)")
    parser.add_argument("--db-path", default=None,
                        help="Path to SQLite database for OAuth tokens/sessions (default: oauth.db)")
    args = parser.parse_args()

    if args.check:
        sys.exit(asyncio.run(_health_check()))

    try:
        workflow, _client = _build_workflow()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    enable_oauth = args.oauth if args.oauth is not None else workflow.settings.oauth_enabled
    issuer_url = args.issuer_url or workflow.settings.oauth_issuer_url

    oauth_provider = None
    auth_settings = None
    oauth_store = None

    if enable_oauth and args.transport != "stdio":
        if not issuer_url:
            issuer_url = f"http://{args.host}:{args.port}"
        from pydantic import AnyUrl
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
        from oauth_store import OAuthStore
        from oauth_provider import SQLiteOAuthProvider

        db_path = args.db_path or workflow.settings.oauth_db_path
        oauth_store = OAuthStore(db_path)
        oauth_provider = SQLiteOAuthProvider(oauth_store, issuer_url)
        auth_settings = AuthSettings(
            issuer_url=AnyUrl(issuer_url),
            resource_server_url=AnyUrl(issuer_url),
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )

    shared_store = ConfirmationStore(workflow.settings.confirmation_ttl_seconds)

    async def get_active_workflow() -> Workflow:
        if oauth_store:
            from mcp.server.auth.middleware.auth_context import get_access_token
            access_tok = get_access_token()
            if access_tok:
                token_data = oauth_store.get_token_data(access_tok.token)
                if token_data:
                    uid, uname = token_data.subject.split(":", 1) if ":" in token_data.subject else ("", token_data.subject)
                    ident = Identity(user_id=uid, username=uname, role="PRE")

                    async def on_refresh(new_acc: str, new_ref: str | None = None) -> None:
                        oauth_store.update_django_tokens(access_tok.token, new_acc, new_ref)

                    user_client = MarketIntelClient(
                        workflow.settings,
                        access_token=token_data.django_access_token,
                        refresh_token=token_data.django_refresh_token,
                        identity=ident,
                        on_token_refreshed=on_refresh,
                    )
                    return Workflow(user_client, shared_store, workflow.settings)
        return workflow

    mcp = create_mcp(get_active_workflow, auth_settings=auth_settings, auth_server_provider=oauth_provider)

    if enable_oauth and oauth_store:
        from login_views import create_login_handler
        mcp.custom_route("/login", methods=["GET", "POST"])(create_login_handler(oauth_store, workflow.settings))

    auth_token = args.auth_token or workflow.settings.auth_token

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware
        app = mcp.sse_app(host=args.host)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
            expose_headers=["*"],
        )
        if enable_oauth:
            print(f"OAuth 2.0 Sign In ACTIVE at {issuer_url} (login route: {issuer_url}/login)", file=sys.stderr)
        elif auth_token:
            app.add_middleware(TokenAuthMiddleware, secret_token=auth_token)
            print(f"Token protection ACTIVE (use ?token={auth_token[:4]}***).", file=sys.stderr)
        else:
            print("WARNING: Running without authentication! Set MCP_AUTH_TOKEN or MCP_OAUTH_ENABLED in .env to secure.", file=sys.stderr)
        print(f"Starting MCP SSE server on http://{args.host}:{args.port}/sse", file=sys.stderr)
        uvicorn.run(app, host=args.host, port=args.port, log_level=workflow.settings.log_level.lower())
    elif args.transport == "streamable-http":
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware
        app = mcp.streamable_http_app(host=args.host)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
            expose_headers=["*"],
        )
        if enable_oauth:
            print(f"OAuth 2.0 Sign In ACTIVE at {issuer_url} (login route: {issuer_url}/login)", file=sys.stderr)
        elif auth_token:
            app.add_middleware(TokenAuthMiddleware, secret_token=auth_token)
            print(f"Token protection ACTIVE (use ?token={auth_token[:4]}***).", file=sys.stderr)
        else:
            print("WARNING: Running without authentication! Set MCP_AUTH_TOKEN or MCP_OAUTH_ENABLED in .env to secure.", file=sys.stderr)
        print(f"Starting MCP Streamable-HTTP server on http://{args.host}:{args.port}/mcp", file=sys.stderr)
        uvicorn.run(app, host=args.host, port=args.port, log_level=workflow.settings.log_level.lower())


if __name__ == "__main__":
    main()
