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
from whatsapp_workflow import EphemeralQRStore, SlidingWindowRateLimiter, StagedMediaStore, WhatsAppWorkflow
from workflow import Workflow

INSTRUCTIONS = """Restricted Market Intel MCP server supporting four data/integration modules:
1. Government Entities
2. Market Events
3. Prospects
4. WhatsApp Messaging & Session Integration

Mandatory operating rules for Claude:
1. Inspect the blueprint before researching a new prospect (call get_prospect_blueprint).
2. Research every applicable blueprint field systematically, not just company name and website.
3. Never invent facts, IDs, lookups, phone numbers, emails, or verification results.
4. Always search existing records before attempting to create Government Entities or Market Events.
5. Missing prerequisite lookup records (Government Entities, Market Events, Parent Companies) must be created and verified before creating the prospect.
6. Every write must be validated and explicitly approved by the user (show full preview/diff and confirmation token).
7. Edits require a before-and-after review and explicit user approval.
8. Delete operations are strictly PROHIBITED for all modules.
9. Research sources and unresolved fields must be disclosed to the user.
10. No record or WhatsApp message may be reported as created, updated, or sent without verified API confirmation.
11. WhatsApp operations are strictly scoped to the authenticated Market Intel user's own connected session and conversations.
12. Outbound WhatsApp messages, media sends, and session disconnects require explicit user approval via confirmation_token and user_has_explicitly_confirmed=true."""

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
    get_whatsapp_workflow: Callable[[], WhatsAppWorkflow | Awaitable[WhatsAppWorkflow]] | None = None,
) -> MCPServer:
    mcp = MCPServer(
        "market-intel-prospect-entry",
        instructions=INSTRUCTIONS,
        auth=auth_settings,
        auth_server_provider=auth_server_provider,
    )

    fallback_rate_limiter: SlidingWindowRateLimiter | None = None
    fallback_media_store: StagedMediaStore | None = None

    async def _resolve_wf() -> Workflow:
        wf = get_workflow()
        if asyncio.iscoroutine(wf):
            return await wf
        return wf

    async def _resolve_wa_wf() -> WhatsAppWorkflow:
        nonlocal fallback_rate_limiter, fallback_media_store
        if get_whatsapp_workflow is not None:
            wa = get_whatsapp_workflow()
            if asyncio.iscoroutine(wa):
                return await wa
            return wa
        wf = await _resolve_wf()
        if fallback_rate_limiter is None:
            fallback_rate_limiter = SlidingWindowRateLimiter(wf.settings.whatsapp_rate_limit_per_minute)
        if fallback_media_store is None:
            fallback_media_store = StagedMediaStore(wf.settings.confirmation_ttl_seconds)
        wa = WhatsAppWorkflow(
            wf.client,
            wf.store,
            wf.settings,
            rate_limiter=fallback_rate_limiter,
            media_store=fallback_media_store,
        )
        setattr(wa, "_ephemeral_client", getattr(wf, "_ephemeral_client", False))
        return wa

    async def _close_if_ephemeral(holder: Any) -> None:
        if getattr(holder, "_ephemeral_client", False) and hasattr(holder.client, "aclose"):
            try:
                await holder.client.aclose()
            except Exception:
                pass

    # --- Blueprint Discovery ---
    @mcp.tool()
    async def get_prospect_blueprint() -> dict[str, Any]:
        """READ-ONLY. Retrieve the complete, authoritative Market Intel Prospect blueprint.
        Call this tool BEFORE researching a company to obtain the exact fields, required/optional status,
        allowed choices, numeric limits, conditional rules, lookup dependencies, and validation rules across all
        7 business sections (Corporate Structure, General Info, Shareholding, Offerings, Events, Contacts, Gov Entity)."""
        wf = await _resolve_wf()
        return wf.get_prospect_blueprint()

    # --- Search Tools ---
    @mcp.tool()
    async def search_existing_prospects(query: str, country: str | None = None, limit: int = 10) -> dict[str, Any]:
        """READ-ONLY. Search existing prospects by company name/phrase (optional country filter, limit max 25).
        Use it to spot possible duplicates and find real parent-company IDs for hierarchy linking.
        Results are limited to what this account can see; backend duplicate checks decide at creation."""
        wf = await _resolve_wf()
        return await wf.search(query, country, limit)

    @mcp.tool()
    async def search_government_entities(
        query: str, country: str | None = None, is_state_government: bool | None = None, limit: int = 10
    ) -> dict[str, Any]:
        """READ-ONLY. Search existing government entities by name or country.
        Use it to verify if a government entity already exists before creating a new one or linking to prospects."""
        wf = await _resolve_wf()
        return await wf.search_government_entities(query, country, is_state_government, limit)

    @mcp.tool()
    async def search_market_events(
        query: str, host_country: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        """READ-ONLY. Search existing market events (conferences, expos, trade summits) by event title or country.
        Use it to find real event IDs for linking to prospects via market_event_ids."""
        wf = await _resolve_wf()
        return await wf.search_market_events(query, host_country, limit)

    @mcp.tool()
    async def get_record_details(entity_type: str, record_id: str) -> dict[str, Any]:
        """READ-ONLY. Retrieve complete details of a specific record by module and UUID.
        Supported entity_types: 'prospect', 'government_entity', 'market_event'."""
        wf = await _resolve_wf()
        return await wf.get_record_details(entity_type, record_id)

    # --- Government Entity Staged Creation ---
    @mcp.tool(description=(
        "Validate a NEW Government Entity without creating it. Required fields: name, country. "
        "Optional: is_state_government, state_name, central_government, official_website, official_email, "
        "phone_number, complete_address. Checks duplicates. On success returns summary and a confirmation_token."))
    async def validate_government_entity(entity: dict[str, Any]) -> dict[str, Any]:
        wf = await _resolve_wf()
        return await wf.validate_government_entity(entity)

    @mcp.tool()
    async def create_confirmed_government_entity(confirmation_token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        """Create exactly ONE Government Entity from a previously validated payload.
        Call ONLY after explicit user approval. Returns the created record and its database UUID."""
        wf = await _resolve_wf()
        return await wf.create_government_entity(confirmation_token, user_has_explicitly_confirmed)

    # --- Market Event Staged Creation ---
    @mcp.tool(description=(
        "Validate a NEW Market Event without creating it. Required fields: event_title, host_country. "
        "Optional: start_date, end_date, website_url. Checks duplicates. On success returns summary and a confirmation_token."))
    async def validate_market_event(event: dict[str, Any]) -> dict[str, Any]:
        wf = await _resolve_wf()
        return await wf.validate_market_event(event)

    @mcp.tool()
    async def create_confirmed_market_event(confirmation_token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        """Create exactly ONE Market Event from a previously validated payload.
        Call ONLY after explicit user approval. Returns the created record and its database UUID."""
        wf = await _resolve_wf()
        return await wf.create_market_event(confirmation_token, user_has_explicitly_confirmed)

    # --- Prospect Staged Creation ---
    @mcp.tool(description=(
        "Validate a NEW prospect WITHOUT creating it. Checks required fields, enums, formats, parent companies, "
        "referenced Government Entities, Market Events, duplicates, offerings, key contacts, and verifies EVERY "
        "supplied company/contact email via the email-verification API (each must be VALID). "
        "Missing required fields must be requested from the user - never invent values. On success returns a readable "
        "summary and a one-time confirmation_token; show summary to the user and obtain explicit approval." + PROSPECT_FIELDS_HELP))
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

    # --- Generic & Edit Workflows ---
    @mcp.tool(description=(
        "Validate changes to an existing record ('prospect', 'government_entity', or 'market_event'). "
        "Retrieves current record, merges changes, validates against full blueprint, checks duplicates and emails, "
        "and returns a before-and-after diff with a confirmation_token. Unspecified fields are preserved. "
        "Does NOT write to the database."))
    async def validate_record_update(entity_type: str, record_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        wf = await _resolve_wf()
        return await wf.validate_record_update(entity_type, record_id, changes)

    @mcp.tool()
    async def update_confirmed_record(confirmation_token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        """Apply an approved update to an existing record. Call ONLY after the user has seen the before-and-after diff "
        "and explicitly confirmed approval. Verifies the record has not been concurrently modified before applying update."""
        wf = await _resolve_wf()
        return await wf.update_confirmed_record(confirmation_token, user_has_explicitly_confirmed)

    @mcp.tool(description="Generic validation dispatcher for any of the three modules ('prospect', 'government_entity', 'market_event').")
    async def validate_record_creation(entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        wf = await _resolve_wf()
        return await wf.validate_record_creation(entity_type, data)

    @mcp.tool(description="Generic commitment dispatcher for any confirmed creation or update token.")
    async def create_confirmed_record(confirmation_token: str, user_has_explicitly_confirmed: bool) -> dict[str, Any]:
        wf = await _resolve_wf()
        return await wf.create_confirmed_record(confirmation_token, user_has_explicitly_confirmed)

    # --- WhatsApp Connection Tools ---
    @mcp.tool(description=(
        "Initiate or resume the authenticated Market Intel user's WhatsApp session. "
        "Returns connection status and phone number when connected, or a scannable QR link (`qr_url`) / QR data "
        "when scanning is required. When `qr_url` is returned, give that clickable link directly to the user so they can scan it. "
        "Set wait_for_connection=true (and optional timeout_seconds, max 60) to poll until connected."
    ))
    async def connect_whatsapp(
        wait_for_connection: bool = False,
        timeout_seconds: int = 30,
        force_restart: bool = False,
    ) -> dict[str, Any]:
        wa = await _resolve_wa_wf()
        try:
            return await wa.connect_whatsapp(
                wait_for_connection=wait_for_connection,
                timeout_seconds=timeout_seconds,
                force_restart=force_restart,
            )
        finally:
            await _close_if_ephemeral(wa)

    @mcp.tool(description=(
        "READ-ONLY. Check the authenticated user's WhatsApp session connection status, "
        "bound WhatsApp phone number, identity verification state, and outbound safety configuration."
    ))
    async def get_whatsapp_connection_status() -> dict[str, Any]:
        wa = await _resolve_wa_wf()
        try:
            return await wa.get_whatsapp_connection_status()
        finally:
            await _close_if_ephemeral(wa)

    @mcp.tool(description=(
        "Retrieve the live WhatsApp QR code for the authenticated user's session if authentication is needed, "
        "or report connected status if already authenticated. When `qr_url` is returned, give that clickable link "
        "directly to the user instead of raw base64 image data. Set force_restart=true to regenerate the QR code."
    ))
    async def get_whatsapp_qr_code(force_restart: bool = False) -> dict[str, Any]:
        wa = await _resolve_wa_wf()
        try:
            return await wa.get_whatsapp_qr_code(force_restart=force_restart)
        finally:
            await _close_if_ephemeral(wa)

    @mcp.tool(description=(
        "Disconnect (logout) the authenticated user's WhatsApp session while preserving all persisted "
        "conversations and message history in Market Intel DB. Requires explicit user confirmation via "
        "confirmation_token and user_has_explicitly_confirmed=true."
    ))
    async def disconnect_whatsapp(
        confirmation_token: str | None = None,
        user_has_explicitly_confirmed: bool = False,
    ) -> dict[str, Any]:
        wa = await _resolve_wa_wf()
        try:
            return await wa.disconnect_whatsapp(
                confirmation_token=confirmation_token,
                user_has_explicitly_confirmed=user_has_explicitly_confirmed,
            )
        finally:
            await _close_if_ephemeral(wa)

    # --- WhatsApp Conversation Tools ---
    @mcp.tool(description=(
        "READ-ONLY. List WhatsApp chats owned by the authenticated user's connected session. "
        "Supports optional search filter, is_group filter, matched_only filter, configurable limit (1-100), and cursor pagination."
    ))
    async def list_whatsapp_chats(
        search: str | None = None,
        is_group: bool | None = None,
        matched_only: bool = False,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        wa = await _resolve_wa_wf()
        try:
            return await wa.list_whatsapp_chats(
                search=search,
                is_group=is_group,
                matched_only=matched_only,
                limit=limit,
                cursor=cursor,
            )
        finally:
            await _close_if_ephemeral(wa)

    @mcp.tool(description=(
        "READ-ONLY. Retrieve persisted conversation message history from Market Intel DB for one of the "
        "authenticated user's owned chats (by conversation_id, chat_id, or phone_number). "
        "Includes direction, timestamps, delivery/read status, media metadata, reply_context, and cursor pagination."
    ))
    async def get_whatsapp_chat_history(
        conversation_id: str | None = None,
        chat_id: str | None = None,
        phone_number: str | None = None,
        page_size: int = 50,
        cursor: str | None = None,
        order: str = "asc",
    ) -> dict[str, Any]:
        wa = await _resolve_wa_wf()
        try:
            return await wa.get_whatsapp_chat_history(
                conversation_id=conversation_id,
                chat_id=chat_id,
                phone_number=phone_number,
                page_size=page_size,
                cursor=cursor,
                order=order,
            )
        finally:
            await _close_if_ephemeral(wa)

    @mcp.tool(description=(
        "READ-ONLY. Retrieve contact details, CRM Prospect/Key Person linkage, and group members "
        "for an owned WhatsApp chat (by chat_id, phone_number, or conversation_id)."
    ))
    async def get_whatsapp_contact_details(
        chat_id: str | None = None,
        phone_number: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        wa = await _resolve_wa_wf()
        try:
            return await wa.get_whatsapp_contact_details(
                chat_id=chat_id,
                phone_number=phone_number,
                conversation_id=conversation_id,
            )
        finally:
            await _close_if_ephemeral(wa)

    @mcp.tool(description=(
        "READ-ONLY. Search persisted WhatsApp messages strictly within the authenticated user's owned conversations. "
        "Supports optional conversation_id/chat_id scope, direction ('INBOUND'/'OUTBOUND'), has_media filter, limit, and cursor."
    ))
    async def search_whatsapp_messages(
        query: str,
        conversation_id: str | None = None,
        chat_id: str | None = None,
        direction: str | None = None,
        has_media: bool | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        wa = await _resolve_wa_wf()
        try:
            return await wa.search_whatsapp_messages(
                query=query,
                conversation_id=conversation_id,
                chat_id=chat_id,
                direction=direction,
                has_media=has_media,
                limit=limit,
                cursor=cursor,
            )
        finally:
            await _close_if_ephemeral(wa)

    # --- WhatsApp Messaging Tools ---
    @mcp.tool(description=(
        "Send a WhatsApp text message through the authenticated user's connected Market Intel session. "
        "Call first without confirmation_token to validate the recipient/session and obtain a preview + confirmation_token; "
        "after explicit user approval, call again with confirmation_token and user_has_explicitly_confirmed=true."
    ))
    async def send_whatsapp_message(
        recipient: str | None = None,
        message_body: str = "",
        chat_id: str | None = None,
        prospect_contact_id: str | None = None,
        reply_to: str | None = None,
        confirmation_token: str | None = None,
        user_has_explicitly_confirmed: bool = False,
    ) -> dict[str, Any]:
        wa = await _resolve_wa_wf()
        try:
            return await wa.send_whatsapp_message(
                recipient=recipient,
                message_body=message_body,
                chat_id=chat_id,
                prospect_contact_id=prospect_contact_id,
                reply_to=reply_to,
                confirmation_token=confirmation_token,
                user_has_explicitly_confirmed=user_has_explicitly_confirmed,
            )
        finally:
            await _close_if_ephemeral(wa)

    @mcp.tool(description=(
        "Send a media attachment (image, document, audio, video as base64) via the authenticated user's connected WhatsApp session. "
        "Call first with file details to validate and obtain a preview + confirmation_token; "
        "after explicit user approval, call again with confirmation_token and user_has_explicitly_confirmed=true."
    ))
    async def send_whatsapp_media(
        recipient: str | None = None,
        chat_id: str | None = None,
        filename: str = "",
        mime_type: str = "",
        file_base64: str = "",
        caption: str = "",
        confirmation_token: str | None = None,
        user_has_explicitly_confirmed: bool = False,
    ) -> dict[str, Any]:
        wa = await _resolve_wa_wf()
        try:
            return await wa.send_whatsapp_media(
                recipient=recipient,
                chat_id=chat_id,
                filename=filename,
                mime_type=mime_type,
                file_base64=file_base64,
                caption=caption,
                confirmation_token=confirmation_token,
                user_has_explicitly_confirmed=user_has_explicitly_confirmed,
            )
        finally:
            await _close_if_ephemeral(wa)

    @mcp.tool(description=(
        "Mark all inbound messages in an owned WhatsApp conversation as read in Market Intel DB and WAHA."
    ))
    async def mark_whatsapp_chat_read(
        conversation_id: str | None = None,
        chat_id: str | None = None,
        phone_number: str | None = None,
    ) -> dict[str, Any]:
        wa = await _resolve_wa_wf()
        try:
            return await wa.mark_whatsapp_chat_read(
                conversation_id=conversation_id,
                chat_id=chat_id,
                phone_number=phone_number,
            )
        finally:
            await _close_if_ephemeral(wa)

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
            path = scope.get("path", "")
            method = scope.get("method", "GET")
            if method in ("GET", "HEAD") and path.startswith("/whatsapp/qr/"):
                await self.app(scope, receive, send)
                return

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


class DefaultPublicClientAuthMethodMiddleware:
    """ASGI middleware for dynamic client registration (/register).

    If an OAuth client (like Claude's automatic client registration) omits
    `token_endpoint_auth_method`, default it to 'none' so that public clients
    can exchange authorization codes using PKCE S256 without needing a client secret.
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
        issuer_url = issuer_url.rstrip("/")
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
        from oauth_store import OAuthStore
        from oauth_provider import SQLiteOAuthProvider

        db_path = args.db_path or workflow.settings.oauth_db_path
        oauth_store = OAuthStore(db_path)
        oauth_provider = SQLiteOAuthProvider(oauth_store, issuer_url)
        auth_settings = AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=f"{issuer_url}/mcp",
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )

    shared_store = ConfirmationStore(workflow.settings.confirmation_ttl_seconds)
    shared_rate_limiter = SlidingWindowRateLimiter(workflow.settings.whatsapp_rate_limit_per_minute)
    shared_media_store = StagedMediaStore(workflow.settings.confirmation_ttl_seconds)
    shared_qr_store = EphemeralQRStore()
    qr_public_base_url = (
        issuer_url.strip().rstrip("/")
        if (args.transport in ("sse", "streamable-http") and issuer_url)
        else None
    )
    default_wa_workflow = WhatsAppWorkflow(
        workflow.client,
        shared_store,
        workflow.settings,
        rate_limiter=shared_rate_limiter,
        media_store=shared_media_store,
        qr_store=shared_qr_store,
        qr_public_base_url=qr_public_base_url,
    )

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

                    return MarketIntelClient(
                        workflow.settings,
                        access_token=token_data.django_access_token,
                        refresh_token=token_data.django_refresh_token,
                        identity=ident,
                        on_token_refreshed=on_refresh,
                    )
        return None

    async def get_active_workflow() -> Workflow:
        user_client = _build_oauth_client()
        if user_client is not None:
            wf = Workflow(user_client, shared_store, workflow.settings)
            setattr(wf, "_ephemeral_client", True)
            return wf
        return workflow

    async def get_active_whatsapp_workflow() -> WhatsAppWorkflow:
        user_client = _build_oauth_client()
        if user_client is not None:
            wa = WhatsAppWorkflow(
                user_client,
                shared_store,
                workflow.settings,
                rate_limiter=shared_rate_limiter,
                media_store=shared_media_store,
                qr_store=shared_qr_store,
                qr_public_base_url=qr_public_base_url,
            )
            setattr(wa, "_ephemeral_client", True)
            return wa
        return default_wa_workflow

    mcp = create_mcp(
        get_active_workflow,
        auth_settings=auth_settings,
        auth_server_provider=oauth_provider,
        get_whatsapp_workflow=get_active_whatsapp_workflow,
    )

    if enable_oauth and oauth_store:
        from login_views import create_login_handler
        mcp.custom_route("/login", methods=["GET", "POST"])(create_login_handler(oauth_store, workflow.settings))

    if args.transport in ("sse", "streamable-http"):
        from whatsapp_qr_views import create_qr_image_handler, create_qr_page_handler
        mcp.custom_route("/whatsapp/qr/{token}", methods=["GET"])(create_qr_page_handler(shared_qr_store))
        mcp.custom_route("/whatsapp/qr/{token}/image", methods=["GET"])(create_qr_image_handler(shared_qr_store))

    auth_token = args.auth_token or workflow.settings.auth_token

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware
        app = mcp.sse_app(host=args.host)
        if enable_oauth:
            configure_oauth_endpoints(app, issuer_url)
            app.add_middleware(DefaultPublicClientAuthMethodMiddleware)
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
        if enable_oauth:
            configure_oauth_endpoints(app, issuer_url)
            app.add_middleware(DefaultPublicClientAuthMethodMiddleware)
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
