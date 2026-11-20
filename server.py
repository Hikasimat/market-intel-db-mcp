"""Market Intel prospect-entry & company research MCP server.

IMPORTANT: stdout carries the MCP protocol. All logging goes to stderr.
Run `python server.py --check` for an authentication/role health check.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import sys
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer  # mcp 2.x (renamed from FastMCP in 1.x)

from api_client import AuthError, ApiError, Identity, MarketIntelClient
from config import ConfigError, load_settings
from confirmation_store import ConfirmationStore
from research.cache import SQLiteResearchCache
from research.fetcher import SafeFetcher
from research.provider_router import ProviderRouter
from research.search import SearchClient
from research.security import SecretRedactingFilter
from research.service import ResearchService
from research.vortexen_profile import VortexenProfileManager
from workflow import Workflow

INSTRUCTIONS = """Restricted Market Intel MCP server supporting three data modules and an evidence-based Research Engine:
1. Government Entities
2. Market Events
3. Prospects
4. Read-Only Company & Event Research Engine (aligned with our dynamic Vortexen business profile)

Mandatory operating workflow for AI assistants:
1. Inspect the authoritative blueprint first (call get_prospect_blueprint).
2. Research the company via research_company_prospect (or event + participants via research_market_event).
3. Resolve company identity and check existing prospects via search_existing_prospects for duplicates.
4. Prioritize Market Offerings (Products, Services, Solutions) and Primary Industries from official evidence.
5. Verify source evidence, corporate structure, shareholding/ownership (never confuse directors with shareholders or fabricate percentages), and key contacts (name, designation, and VALID official email required).
6. Resolve real lookup UUIDs (parent companies, government entities, market events); never invent IDs or missing values.
7. Review Vortexen service alignment derived from the live Vortexen website profile.
8. Present structured candidate, evidence, conflicts, and missing fields to the user.
9. Run validate_prospect_data (or module validator) — validation NEVER writes to Market Intel DB.
10. Obtain explicit user approval after showing validation summary/diff, then call create_confirmed_prospect (or confirmed create/update tool) with confirmation_token and user_has_explicitly_confirmed=true.
11. Delete operations are strictly PROHIBITED for all modules. Research tools are strictly read-only."""

PROSPECT_FIELDS_HELP = """
Blueprint Sections:
1. Corporate Structure & Parent Auto-Linking: company_structure (Parent|Branch|Subsidiary), operational_status (Active|Inactive|Permanently Closed|Acquired|Merged), ownership_sector (Private Company|Government Company|Semi Government Company), parent_companies (list of UUIDs; required for Branch/Subsidiary), merging_companies, dissolved_companies, acquiring_companies_data.
2. General Information: company_name, country_head_office, complete_address, primary_industries, official_phone_number, official_email_address (must be VALID), official_website_url, linkedin_company_page.
3. Shareholding / Ownership: shareholdings ([{holder_type: Company|Individual|Government|Other, share_percentage, company?, is_self?, contact?, contact_index?, government_entity?, other_name?}]).
4. Market Offerings: primary_offering_type (Products|Services|Solutions|Multiple), offerings_data ([{offering_type: Product|Service|Solution, name}]).
5. Market Events: market_event_ids (list of real Market Event UUIDs).
6. Key Contacts: key_contacts ([{prefix?, contact_name, designation?, official_email?, phone_number?, linkedin_profile?}]).
7. Government Entity association: Required for Government/Semi-Government companies via shareholdings with holder_type='Government' and government_entity=<UUID>.
Unsupported fields are rejected."""


def create_mcp(
    get_workflow: Callable[[], Workflow | Awaitable[Workflow]],
    auth_settings: Any = None,
    auth_server_provider: Any = None,
    get_research_service: Callable[[], ResearchService | Awaitable[ResearchService]] | None = None,
    shared_research_components: dict[str, Any] | None = None,
) -> MCPServer:
    mcp = MCPServer(
        "market-intel-prospect-entry",
        instructions=INSTRUCTIONS,
        auth=auth_settings,
        auth_server_provider=auth_server_provider,
    )

    lazy_components: dict[str, Any] = dict(shared_research_components or {})

    async def _resolve_wf() -> Workflow:
        wf = get_workflow()
        if asyncio.iscoroutine(wf):
            return await wf
        return wf

    async def _close_if_ephemeral(holder: Any) -> None:
        if getattr(holder, "_ephemeral_client", False) and hasattr(holder, "client") and hasattr(holder.client, "aclose"):
            try:
                await holder.client.aclose()
            except Exception:
                pass

    async def _resolve_research() -> tuple[ResearchService, bool]:
        if get_research_service is not None:
            rs = get_research_service()
            if asyncio.iscoroutine(rs):
                rs = await rs
            return rs, bool(getattr(rs, "_ephemeral_client", False))

        wf = await _resolve_wf()
        if "cache" not in lazy_components:
            cache = SQLiteResearchCache(wf.settings.research_cache_db_path or "research_cache.db")
            fetcher = SafeFetcher(wf.settings, cache=cache)
            search_client = SearchClient(wf.settings)
            router = ProviderRouter(wf.settings, cache=cache)
            vortexen = VortexenProfileManager(wf.settings, fetcher, cache=cache, router=router)
            lazy_components.update({
                "cache": cache,
                "fetcher": fetcher,
                "search_client": search_client,
                "router": router,
                "vortexen": vortexen,
            })

        rs = ResearchService(
            wf.client,
            wf,
            wf.settings,
            cache=lazy_components["cache"],
            fetcher=lazy_components["fetcher"],
            search_client=lazy_components["search_client"],
            router=lazy_components["router"],
            vortexen_manager=lazy_components["vortexen"],
        )
        ephemeral = getattr(wf, "_ephemeral_client", False)
        setattr(rs, "_ephemeral_client", ephemeral)
        return rs, ephemeral

    # --- Blueprint Discovery ---
    @mcp.tool()
    async def get_prospect_blueprint() -> dict[str, Any]:
        """READ-ONLY. Retrieve the complete, authoritative Market Intel Prospect blueprint.
        Call this tool BEFORE researching a company to obtain the exact fields, required/optional status,
        allowed choices, numeric limits, conditional rules, lookup dependencies, and validation rules across all
        7 business sections (Corporate Structure, General Info, Shareholding, Offerings, Events, Contacts, Gov Entity)."""
        wf = await _resolve_wf()
        try:
            return wf.get_prospect_blueprint()
        finally:
            await _close_if_ephemeral(wf)

    # --- Search Tools ---
    @mcp.tool()
    async def search_existing_prospects(query: str, country: str | None = None, limit: int = 10) -> dict[str, Any]:
        """READ-ONLY. Search existing prospects by company name/phrase (optional country filter, limit max 25).
        Use it to spot possible duplicates and find real parent-company IDs for hierarchy linking.
        Results are limited to what this account can see; backend duplicate checks decide at creation."""
        wf = await _resolve_wf()
        try:
            return await wf.search(query, country, limit)
        finally:
            await _close_if_ephemeral(wf)

    @mcp.tool()
    async def search_government_entities(
        query: str, country: str | None = None, is_state_government: bool | None = None, limit: int = 10
    ) -> dict[str, Any]:
        """READ-ONLY. Search existing government entities by name or country.
        Use it to verify if a government entity already exists before creating a new one or linking to prospects."""
        wf = await _resolve_wf()
        try:
            return await wf.search_government_entities(query, country, is_state_government, limit)
        finally:
            await _close_if_ephemeral(wf)

    @mcp.tool()
    async def search_market_events(
        query: str, host_country: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        """READ-ONLY. Search existing market events (conferences, expos, trade summits) by event title or country.
        Use it to find real event IDs for linking to prospects via market_event_ids."""
        wf = await _resolve_wf()
        try:
            return await wf.search_market_events(query, host_country, limit)
        finally:
            await _close_if_ephemeral(wf)

    @mcp.tool()
    async def get_record_details(entity_type: str, record_id: str) -> dict[str, Any]:
        """READ-ONLY. Retrieve complete details of a specific record by module and UUID.
        Supported entity_types: 'prospect', 'government_entity', 'market_event'."""
        wf = await _resolve_wf()
        try:
            return await wf.get_record_details(entity_type, record_id)
        finally:
            await _close_if_ephemeral(wf)

    # --- Government Entity Staged Creation ---
    @mcp.tool(description=(
        "Validate a NEW Government Entity without creating it. Required fields: name, country. "
        "Optional: is_state_government, state_name, central_government, official_website, official_email, "
        "phone_number, complete_address. Checks duplicates. On success returns summary and a confirmation_token."))
    async def validate_government_entity(entity: dict[str, Any]) -> dict[str, Any]:
        wf = await _resolve_wf()
        try:
            return await wf.validate_government_entity(entity)
        finally:
            await _close_if_ephemeral(wf)

    @mcp.tool()
    async def create_confirmed_government_entity(confirmation_token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        """Create exactly ONE Government Entity from a previously validated payload.
        Call ONLY after explicit user approval. Returns the created record and its database UUID."""
        wf = await _resolve_wf()
        try:
            return await wf.create_government_entity(confirmation_token, user_has_explicitly_confirmed)
        finally:
            await _close_if_ephemeral(wf)

    # --- Market Event Staged Creation ---
    @mcp.tool(description=(
        "Validate a NEW Market Event without creating it. Required fields: event_title, host_country. "
        "Optional: start_date, end_date, website_url. Checks duplicates. On success returns summary and a confirmation_token."))
    async def validate_market_event(event: dict[str, Any]) -> dict[str, Any]:
        wf = await _resolve_wf()
        try:
            return await wf.validate_market_event(event)
        finally:
            await _close_if_ephemeral(wf)

    @mcp.tool()
    async def create_confirmed_market_event(confirmation_token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        """Create exactly ONE Market Event from a previously validated payload.
        Call ONLY after explicit user approval. Returns the created record and its database UUID."""
        wf = await _resolve_wf()
        try:
            return await wf.create_market_event(confirmation_token, user_has_explicitly_confirmed)
        finally:
            await _close_if_ephemeral(wf)

    # --- Prospect Staged Creation ---
    @mcp.tool(description=(
        "Validate a NEW prospect WITHOUT creating it. Checks required fields, enums, formats, parent companies, "
        "referenced Government Entities, Market Events, duplicates, offerings, key contacts, and verifies EVERY "
        "supplied company/contact email via the email-verification API (each must be VALID). "
        "Missing required fields must be requested from the user - never invent values. On success returns a readable "
        "summary and a one-time confirmation_token; show summary to the user and obtain explicit approval." + PROSPECT_FIELDS_HELP))
    async def validate_prospect_data(prospect: dict[str, Any]) -> dict[str, Any]:
        wf = await _resolve_wf()
        try:
            return await wf.validate(prospect)
        finally:
            await _close_if_ephemeral(wf)

    @mcp.tool()
    async def create_confirmed_prospect(confirmation_token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        """Create exactly ONE prospect from a previously validated payload. Call ONLY after the user has seen the
        validation summary and explicitly approved creation; set user_has_explicitly_confirmed=true only then.
        Takes no new data: the reviewed payload is bound to the token (single-use, expires). Emails are re-verified
        and duplicates re-checked first. On a timeout the outcome is UNCERTAIN: do NOT retry; search first."""
        wf = await _resolve_wf()
        try:
            return await wf.create(confirmation_token, user_has_explicitly_confirmed)
        finally:
            await _close_if_ephemeral(wf)

    # --- Generic & Edit Workflows ---
    @mcp.tool(description=(
        "Validate changes to an existing record ('prospect', 'government_entity', or 'market_event'). "
        "Retrieves current record, merges changes, validates against full blueprint, checks duplicates and emails, "
        "and returns a before-and-after diff with a confirmation_token. Unspecified fields are preserved. "
        "Does NOT write to the database."))
    async def validate_record_update(entity_type: str, record_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        wf = await _resolve_wf()
        try:
            return await wf.validate_record_update(entity_type, record_id, changes)
        finally:
            await _close_if_ephemeral(wf)

    @mcp.tool()
    async def update_confirmed_record(confirmation_token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        """Apply an approved update to an existing record. Call ONLY after the user has seen the before-and-after diff "
        "and explicitly confirmed approval. Verifies the record has not been concurrently modified before applying update."""
        wf = await _resolve_wf()
        try:
            return await wf.update_confirmed_record(confirmation_token, user_has_explicitly_confirmed)
        finally:
            await _close_if_ephemeral(wf)

    @mcp.tool(description="Generic validation dispatcher for any of the three modules ('prospect', 'government_entity', 'market_event').")
    async def validate_record_creation(entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        wf = await _resolve_wf()
        try:
            return await wf.validate_record_creation(entity_type, data)
        finally:
            await _close_if_ephemeral(wf)

    @mcp.tool(description="Generic commitment dispatcher for any confirmed creation or update token.")
    async def create_confirmed_record(confirmation_token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        wf = await _resolve_wf()
        try:
            return await wf.create_confirmed_record(confirmation_token, user_has_explicitly_confirmed)
        finally:
            await _close_if_ephemeral(wf)

    # --- Read-Only Company & Event Research Tools ---
    @mcp.tool(description=(
        "READ-ONLY. Conduct comprehensive, evidence-backed six-section company research prioritized around "
        "Primary Industries and Market Offerings, Corporate Structure & parent auto-linking, Shareholding/Ownership, "
        "Market Events, and strictly verified Key Contacts. Checks Market Intel duplicates, aligns against the live "
        "Vortexen profile, and tracks field-level sources, conflicts, and token usage. Never creates or updates records."
    ))
    async def research_company_prospect(
        company_name: str,
        country: str | None = None,
        official_website: str | None = None,
        sections: list[str] | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        rs, ephemeral = await _resolve_research()
        try:
            return await rs.research_company(
                company_name,
                country=country,
                official_website=official_website,
                sections=sections,
                force_refresh=force_refresh,
            )
        finally:
            if ephemeral and hasattr(rs.client, "aclose"):
                await rs.client.aclose()

    @mcp.tool(description=(
        "READ-ONLY. Research a market event (expo, conference, summit), verify event details from official sources, "
        "discover and deduplicate exhibitors/sponsors/speakers, and run full 6-section company research with Vortexen "
        "alignment up to max_participants. Never creates or updates database records."
    ))
    async def research_market_event(
        event_name: str,
        country: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        event_website: str | None = None,
        max_participants: int = 10,
        research_companies: bool = True,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        rs, ephemeral = await _resolve_research()
        try:
            return await rs.research_event(
                event_name,
                country=country,
                start_date=start_date,
                end_date=end_date,
                event_website=event_website,
                max_participants=max_participants,
                research_companies=research_companies,
                force_refresh=force_refresh,
            )
        finally:
            if ephemeral and hasattr(rs.client, "aclose"):
                await rs.client.aclose()

    @mcp.tool(description=(
        "READ-ONLY. Inspect persistent research cache availability, field freshness, Vortexen profile status, "
        "enabled AI/search providers, and cumulative token/cost usage metrics."
    ))
    async def get_research_cache_status(
        company_name: str | None = None,
        country: str | None = None,
    ) -> dict[str, Any]:
        rs, ephemeral = await _resolve_research()
        try:
            return await rs.get_cache_status(company_name=company_name, country=country)
        finally:
            if ephemeral and hasattr(rs.client, "aclose"):
                await rs.client.aclose()

    @mcp.tool(description=(
        "READ-ONLY (to Market Intel DB). Crawl https://vortexen.com/ safely and refresh the cached versioned "
        "Vortexen business profile used for prospect qualification. Does NOT modify any Market Intel records."
    ))
    async def refresh_vortexen_profile(force: bool = True) -> dict[str, Any]:
        rs, ephemeral = await _resolve_research()
        try:
            return await rs.refresh_vortexen_profile(force=force)
        finally:
            if ephemeral and hasattr(rs.client, "aclose"):
                await rs.client.aclose()

    return mcp


def _build_workflow() -> tuple[Workflow, MarketIntelClient]:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    redactor = SecretRedactingFilter(settings.secret_values)
    root_logger = logging.getLogger()
    root_logger.addFilter(redactor)
    for handler in root_logger.handlers:
        handler.addFilter(redactor)
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
    """ASGI middleware to enforce constant-time Bearer token authentication on HTTP/SSE endpoints."""

    def __init__(self, app, secret_token: str, allow_query_token: bool = False):
        self.app = app
        self.secret_token = secret_token
        self.allow_query_token = allow_query_token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from urllib.parse import parse_qs
            from starlette.responses import JSONResponse

            token: str | None = None
            for name, value in scope.get("headers", []):
                if name.lower() == b"authorization":
                    header_val = value.decode("utf-8", errors="replace")
                    if header_val.startswith("Bearer "):
                        token = header_val[7:].strip()
                    break

            if not token and self.allow_query_token:
                query_string = scope.get("query_string", b"").decode("utf-8", errors="replace")
                params = parse_qs(query_string)
                token = params.get("token", [None])[0]

            if not token or not hmac.compare_digest(token.encode("utf-8"), self.secret_token.encode("utf-8")):
                response = JSONResponse(
                    {"error": "Unauthorized: valid Bearer token required in Authorization header."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


class DefaultPublicClientAuthMethodMiddleware:
    """ASGI middleware for dynamic client registration (/register).

    If an OAuth client omits `token_endpoint_auth_method`, default it to 'none'
    so public clients can exchange authorization codes using PKCE S256 without a client secret.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/register" and scope.get("method") == "POST":
            import json
            body_chunks = []
            more_body = True
            while more_body:
                message = await receive()
                if message.get("type") == "http.request":
                    body_chunks.append(message.get("body", b""))
                    more_body = message.get("more_body", False)
            body = b"".join(body_chunks)

            try:
                data = json.loads(body.decode("utf-8")) if body else {}
                if isinstance(data, dict) and not data.get("token_endpoint_auth_method"):
                    data["token_endpoint_auth_method"] = "none"
                    body = json.dumps(data).encode("utf-8")
            except Exception:
                pass

            headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode("ascii")))
            scope = dict(scope)
            scope["headers"] = headers

            async def new_receive():
                return {"type": "http.request", "body": body, "more_body": False}

            await self.app(scope, new_receive, send)
            return

        await self.app(scope, receive, send)


def validate_remote_transport_auth(transport: str, enable_oauth: bool, auth_token: str | None) -> None:
    """Refuse to start remote HTTP/SSE transports without OAuth or Bearer token auth."""
    if transport in ("sse", "streamable-http") and not enable_oauth and not auth_token:
        raise ConfigError(
            "Remote HTTP/SSE transport requires authentication. Enable OAuth (MCP_OAUTH_ENABLED=true) "
            "or configure MCP_AUTH_TOKEN before starting the server."
        )


def configure_cors(app: Any, allowed_origins: tuple[str, ...]) -> None:
    """Attach explicit CORS allowlist middleware (never wildcard with credentials)."""
    from starlette.middleware.cors import CORSMiddleware

    if not allowed_origins or "*" in allowed_origins:
        raise ConfigError("CORS configuration with wildcard '*' and credentials is not permitted.")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Accept",
            "mcp-protocol-version",
            "mcp-session-id",
            "last-event-id",
        ],
        allow_credentials=True,
        expose_headers=["mcp-session-id", "mcp-protocol-version"],
    )


def configure_oauth_endpoints(app: Any, issuer_url: str) -> None:
    """Configure standards-compliant OAuth 2.0 metadata and Protected Resource endpoints."""
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route
    from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
    from mcp.server.auth.json_response import PydanticJSONResponse

    issuer_clean = issuer_url.rstrip("/")

    # RFC 8414 OAuth 2.0 Authorization Server Metadata
    metadata = OAuthMetadata.model_validate({
        "issuer": issuer_clean,
        "authorization_endpoint": f"{issuer_clean}/authorize",
        "token_endpoint": f"{issuer_clean}/token",
        "registration_endpoint": f"{issuer_clean}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
    })

    async def metadata_handler(request: Request) -> Response:
        return PydanticJSONResponse(
            content=metadata,
            headers={
                "Cache-Control": "public, max-age=3600",
                "Access-Control-Allow-Origin": "*",
            },
        )

    # RFC 9728 OAuth 2.0 Protected Resource Metadata
    res_metadata = ProtectedResourceMetadata.model_validate({
        "resource": f"{issuer_clean}/mcp",
        "authorization_servers": [issuer_clean],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [],
    })

    async def pr_metadata_handler(request: Request) -> Response:
        return PydanticJSONResponse(
            content=res_metadata,
            headers={
                "Cache-Control": "public, max-age=3600",
                "Access-Control-Allow-Origin": "*",
            },
        )

    # Replace existing metadata route in place
    for i, r in enumerate(app.routes):
        if getattr(r, "path", "") == "/.well-known/oauth-authorization-server":
            app.routes[i] = Route(
                "/.well-known/oauth-authorization-server",
                endpoint=metadata_handler,
                methods=["GET", "OPTIONS"],
            )
            break

    # Provide RFC 9728 protected resource metadata at both root and /mcp paths
    app.routes.append(
        Route("/.well-known/oauth-protected-resource", endpoint=pr_metadata_handler, methods=["GET", "OPTIONS"])
    )
    app.routes.append(
        Route("/.well-known/oauth-protected-resource/mcp", endpoint=pr_metadata_handler, methods=["GET", "OPTIONS"])
    )


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Market Intel prospect-entry & research MCP server")
    parser.add_argument("--check", action="store_true", help="Run pre-flight auth & role health check and exit")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio",
                        help="MCP transport protocol (default: stdio)")
    parser.add_argument("--sse", action="store_const", const="sse", dest="transport",
                        help="Run as an HTTP SSE server (alias for --transport sse)")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind on (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8001, help="Port to listen on (default: 8001)")
    parser.add_argument("--auth-token", default=None,
                        help="Secret Bearer token required to access SSE/HTTP endpoints (or set MCP_AUTH_TOKEN in .env)")
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
    auth_token = args.auth_token or workflow.settings.auth_token

    try:
        validate_remote_transport_auth(args.transport, enable_oauth, auth_token)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    oauth_provider = None
    auth_settings = None
    oauth_store = None

    if enable_oauth and args.transport != "stdio":
        if not issuer_url:
            issuer_url = f"http://{args.host}:{args.port}"
        issuer_url = issuer_url.rstrip("/")
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
        from oauth_store import OAuthStore
        from oauth_provider import SQLiteOAuthProvider

        db_path = args.db_path or workflow.settings.oauth_db_path
        oauth_store = OAuthStore(db_path)
        oauth_provider = SQLiteOAuthProvider(
            oauth_store,
            issuer_url,
            access_token_ttl=workflow.settings.oauth_access_token_ttl_seconds,
            refresh_token_ttl=workflow.settings.oauth_refresh_token_ttl_seconds,
        )
        auth_settings = AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=f"{issuer_url}/mcp",
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )

    shared_store = ConfirmationStore(workflow.settings.confirmation_ttl_seconds)
    research_cache = SQLiteResearchCache(workflow.settings.research_cache_db_path or "research_cache.db")
    research_fetcher = SafeFetcher(workflow.settings, cache=research_cache)
    research_search = SearchClient(workflow.settings)
    research_router = ProviderRouter(workflow.settings, cache=research_cache)
    vortexen_mgr = VortexenProfileManager(workflow.settings, research_fetcher, cache=research_cache, router=research_router)
    shared_research_components = {
        "cache": research_cache,
        "fetcher": research_fetcher,
        "search_client": research_search,
        "router": research_router,
        "vortexen": vortexen_mgr,
    }

    _refresh_locks: dict[str, asyncio.Lock] = {}

    def _build_oauth_client() -> MarketIntelClient | None:
        if oauth_store:
            from mcp.server.auth.middleware.auth_context import get_access_token
            access_tok = get_access_token()
            if access_tok:
                token_data = oauth_store.get_token_data(access_tok.token)
                if token_data:
                    uid, uname = token_data.subject.split(":", 1) if ":" in token_data.subject else ("", token_data.subject)
                    ident = Identity(
                        user_id=uid,
                        username=uname,
                        role=getattr(token_data, "role", "PRE") or "PRE",
                        is_superuser=bool(getattr(token_data, "is_superuser", False)),
                    )

                    async def on_refresh(new_acc: str, new_ref: str | None = None) -> None:
                        oauth_store.update_django_tokens(access_tok.token, new_acc, new_ref)

                    def reload_tokens() -> tuple[str, str | None] | None:
                        fresh = oauth_store.get_token_data(access_tok.token)
                        if not fresh:
                            return None
                        return fresh.django_access_token, fresh.django_refresh_token

                    lock = _refresh_locks.setdefault(access_tok.token, asyncio.Lock())
                    if len(_refresh_locks) > 256:
                        for k in list(_refresh_locks)[:128]:
                            if k != access_tok.token and not _refresh_locks[k].locked():
                                _refresh_locks.pop(k, None)

                    return MarketIntelClient(
                        workflow.settings,
                        access_token=token_data.django_access_token,
                        refresh_token=token_data.django_refresh_token,
                        identity=ident,
                        on_token_refreshed=on_refresh,
                        reload_tokens=reload_tokens,
                        refresh_lock=lock,
                    )
        return None

    async def get_active_workflow() -> Workflow:
        if enable_oauth and args.transport != "stdio":
            user_client = _build_oauth_client()
            if user_client is None:
                raise AuthError("Valid authenticated OAuth session is required.")
            wf = Workflow(user_client, shared_store, workflow.settings)
            setattr(wf, "_ephemeral_client", True)
            return wf
        return workflow

    async def get_active_research_service() -> ResearchService:
        active_wf = await get_active_workflow()
        rs = ResearchService(
            active_wf.client,
            active_wf,
            workflow.settings,
            cache=research_cache,
            fetcher=research_fetcher,
            search_client=research_search,
            router=research_router,
            vortexen_manager=vortexen_mgr,
        )
        setattr(rs, "_ephemeral_client", getattr(active_wf, "_ephemeral_client", False))
        return rs

    mcp = create_mcp(
        get_active_workflow,
        auth_settings=auth_settings,
        auth_server_provider=oauth_provider,
        get_research_service=get_active_research_service,
        shared_research_components=shared_research_components,
    )

    if enable_oauth and oauth_store:
        from login_views import create_login_handler
        mcp.custom_route("/login", methods=["GET", "POST"])(create_login_handler(oauth_store, workflow.settings))

    if args.transport == "stdio":
        try:
            mcp.run(transport="stdio")
        finally:
            async def _cleanup():
                await research_fetcher.aclose()
                await research_search.aclose()
                await research_router.aclose()
                await _client.aclose()
            try:
                asyncio.run(_cleanup())
            except Exception:
                pass
    elif args.transport in ("sse", "streamable-http"):
        import uvicorn

        app = mcp.sse_app(host=args.host) if args.transport == "sse" else mcp.streamable_http_app(host=args.host)
        if enable_oauth:
            configure_oauth_endpoints(app, issuer_url)
            app.add_middleware(DefaultPublicClientAuthMethodMiddleware)
        configure_cors(app, workflow.settings.cors_allowed_origins)
        if enable_oauth:
            print(f"OAuth 2.0 Sign In ACTIVE at {issuer_url} (login route: {issuer_url}/login)", file=sys.stderr)
        elif auth_token:
            app.add_middleware(
                TokenAuthMiddleware,
                secret_token=auth_token,
                allow_query_token=workflow.settings.allow_query_token,
            )
            print("Bearer token authentication ACTIVE.", file=sys.stderr)

        endpoint_suffix = "sse" if args.transport == "sse" else "mcp"
        print(f"Starting MCP {args.transport} server on http://{args.host}:{args.port}/{endpoint_suffix}", file=sys.stderr)
        try:
            uvicorn.run(app, host=args.host, port=args.port, log_level=workflow.settings.log_level.lower())
        finally:
            async def _cleanup_http():
                await research_fetcher.aclose()
                await research_search.aclose()
                await research_router.aclose()
                await _client.aclose()
            try:
                asyncio.run(_cleanup_http())
            except Exception:
                pass


if __name__ == "__main__":
    main()
