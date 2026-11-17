"""Restricted Market Intel API client.

Only the (method, path) pairs in ALLOWED_ENDPOINTS can ever be sent. Paths are
relative to MARKET_INTEL_API_BASE_URL (which ends in `/api`), so e.g.
`/prospects/` here means `GET {base}/prospects/`.

UNVERIFIED ASSUMPTIONS (the Django project was not available when this was
written - confirm each against the real backend; they are isolated here):
  * login/refresh return an access token as JSON `access` / `access_token` /
    `token`, or set an `access_token` / `access` cookie; refresh uses a cookie.
  * /auth/me/ returns a user object (optionally under `user`) with `role`.
  * instant-verify returns `status` (VALID/INVALID/UNKNOWN) and `reason`.
  * /email-verifications/verify/ body: {prospect_id, [contact_id], email_address}.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from config import Settings

log = logging.getLogger("prospect_mcp.api")

import re

UUID_PATH_RE = re.compile(
    r"^/(prospects|government-entities|market-events)/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/$"
)
WHATSAPP_CONV_MESSAGES_RE = re.compile(
    r"^/whatsapp/conversations/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/messages/$"
)
WHATSAPP_CONV_READ_RE = re.compile(
    r"^/whatsapp/conversations/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/read/$"
)
WHATSAPP_CONV_SYNC_RE = re.compile(
    r"^/whatsapp/conversations/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/sync/$"
)
WHATSAPP_CONV_LINK_CRM_RE = re.compile(
    r"^/whatsapp/conversations/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/link-crm/$"
)
WHATSAPP_MSG_EDIT_RE = re.compile(
    r"^/whatsapp/messages/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/edit/$"
)
WHATSAPP_GROUP_PARTICIPANTS_RE = re.compile(
    r"^/whatsapp/groups/(?:[0-9a-zA-Z_\-\.]+@g\.us|crm_group_[0-9a-zA-Z_\-]+)/participants/(?:add|remove)/$"
)
WHATSAPP_GROUP_INVITE_RE = re.compile(
    r"^/whatsapp/groups/(?:[0-9a-zA-Z_\-\.]+@g\.us|crm_group_[0-9a-zA-Z_\-]+)/invite-link/$"
)
WHATSAPP_CONTACT_INFO_RE = re.compile(
    r"^/whatsapp/contact-info/(?:[0-9a-zA-Z_\-\.]+@(?:c\.us|g\.us|lid|s\.whatsapp\.net)|crm_group_[0-9a-zA-Z_\-]+)/$"
)

ALLOWED_STATIC_ENDPOINTS: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/prospects/"),
    ("POST", "/prospects/"),
    ("GET", "/government-entities/"),
    ("POST", "/government-entities/"),
    ("GET", "/market-events/"),
    ("POST", "/market-events/"),
    ("POST", "/auth/login/"),
    ("POST", "/auth/refresh/"),
    ("GET", "/auth/me/"),
    ("POST", "/email-verifications/instant-verify/"),
    ("POST", "/email-verifications/verify/"),
    # WhatsApp endpoints (Market Intel DB REST API; message/conversation DELETE excluded)
    ("GET", "/whatsapp/status/"),
    ("GET", "/whatsapp/session/list/"),
    ("POST", "/whatsapp/session/start/"),
    ("POST", "/whatsapp/session/logout/"),
    ("GET", "/whatsapp/qr/"),
    ("GET", "/whatsapp/conversations/"),
    ("GET", "/whatsapp/crm-contacts/"),
    ("GET", "/whatsapp/media-proxy/"),
    ("POST", "/whatsapp/sync-history/"),
    ("POST", "/whatsapp/groups/create/"),
    ("POST", "/whatsapp/send/"),
    ("POST", "/whatsapp/send-media/"),
})

ALLOWED_ENDPOINTS = ALLOWED_STATIC_ENDPOINTS


def is_endpoint_allowed(method: str, path: str) -> bool:
    if method == "DELETE":
        return False
    if ".." in path:
        return False
    if (method, path) in ALLOWED_STATIC_ENDPOINTS:
        return True
    if method in ("GET", "PATCH") and UUID_PATH_RE.match(path):
        return True
    if method == "GET" and WHATSAPP_CONV_MESSAGES_RE.match(path):
        return True
    if method == "POST" and WHATSAPP_CONV_READ_RE.match(path):
        return True
    if method == "POST" and WHATSAPP_CONV_SYNC_RE.match(path):
        return True
    if method == "POST" and WHATSAPP_CONV_LINK_CRM_RE.match(path):
        return True
    if method == "POST" and WHATSAPP_MSG_EDIT_RE.match(path):
        return True
    if method == "POST" and WHATSAPP_GROUP_PARTICIPANTS_RE.match(path):
        return True
    if method == "GET" and WHATSAPP_GROUP_INVITE_RE.match(path):
        return True
    if method == "GET" and WHATSAPP_CONTACT_INFO_RE.match(path):
        return True
    return False


def is_retry_safe(method: str, path: str) -> bool:
    if method == "GET":
        return True
    if (method, path) == ("POST", "/email-verifications/instant-verify/"):
        return True
    return False
MAX_RESPONSE_BYTES = 2_000_000
MAX_MEDIA_PROXY_BYTES = 26_000_000
ERROR_TEXT_LIMIT = 1000
TOKEN_KEYS = ("access", "access_token", "token")


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class EndpointNotAllowed(ApiError):
    pass


class AuthError(ApiError):
    pass


class ApiTimeout(ApiError):
    """Outcome unknown: the request may or may not have been processed."""


SENSITIVE_ERROR_KEYS = frozenset({"qr", "qrbase64", "access", "access_token", "refresh", "refresh_token", "token", "password", "secret", "api_key"})


def format_error(body: Any) -> str:
    """Flatten DRF-style error bodies into a short readable string, redacting sensitive fields."""
    def flat(value: Any, prefix: str = "") -> list[str]:
        if isinstance(value, dict):
            out = []
            for k, v in value.items():
                if str(k).lower() in SENSITIVE_ERROR_KEYS:
                    out.append(f"{prefix}{k}: [REDACTED]" if prefix else f"{k}: [REDACTED]")
                else:
                    out.extend(flat(v, f"{prefix}{k}." if k not in ("detail", "non_field_errors") else prefix))
            return out
        if isinstance(value, list):
            return [m for v in value for m in flat(v, prefix)]
        text = str(value)
        if text.startswith("data:image/"):
            text = "[REDACTED_IMAGE_DATA]"
        return [f"{prefix.rstrip('.')}: {text}" if prefix else text]
    text = "; ".join(flat(body)) or "No error details returned."
    return text[:ERROR_TEXT_LIMIT]


@dataclass
class Identity:
    user_id: str
    username: str
    role: str
    is_superuser: bool = False
    whatsapp_sessions: tuple[dict[str, Any], ...] = ()

    @property
    def key(self) -> str:
        return f"{self.user_id}:{self.username}"


@dataclass
class EmailResult:
    email: str
    status: str  # VALID / INVALID / UNKNOWN / ERROR
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "VALID"


class MarketIntelClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        identity: Identity | None = None,
        on_token_refreshed: Any = None,
    ):
        self._s = settings
        # One pooled client + cookie jar so the cookie-based refresh flow works.
        self._http = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,  # never follow redirects off-host
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._token: str | None = access_token
        self._refresh_token: str | None = refresh_token
        self._on_token_refreshed = on_token_refreshed
        self._token_only: bool = bool(access_token)
        self._auth_lock = asyncio.Lock()
        self.identity: Identity | None = identity

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ low level
    async def _request(self, method: str, path: str, *, params=None, body=None, data=None, files=None, auth=True) -> httpx.Response:
        if not is_endpoint_allowed(method, path):
            raise EndpointNotAllowed(f"{method} {path} is not permitted by this MCP server.")
        if auth and not self._token:
            await self._login()
        # Network retries only for side-effect-free requests; never for creation or updates.
        network_retries = 1 if is_retry_safe(method, path) else 0
        reauthed = False
        while True:
            headers = {"Authorization": f"Bearer {self._token}"} if auth and self._token else {}
            req_kwargs: dict[str, Any] = {"params": params, "headers": headers}
            if files is not None:
                req_kwargs["data"] = data or {}
                req_kwargs["files"] = files
            else:
                req_kwargs["json"] = body
            try:
                resp = await self._http.request(method, self._s.api_base_url + path, **req_kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if network_retries > 0:
                    network_retries -= 1
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    raise ApiTimeout(f"Request to {path} timed out; the outcome is unknown.") from None
                raise ApiError(f"Network error calling {path}: {type(exc).__name__}.") from None
            if resp.status_code == 401 and auth and not reauthed:
                # Server rejected the token before processing: safe to re-auth and resend once.
                reauthed = True
                await self._reauthenticate()
                continue
            return resp

    async def _send(self, method: str, path: str, *, params=None, body=None, data=None, files=None, auth=True) -> Any:
        resp = await self._request(method, path, params=params, body=body, data=data, files=files, auth=auth)
        return self._parse(resp, path)

    async def _send_bytes(self, method: str, path: str, *, params=None, auth=True) -> tuple[bytes, str]:
        resp = await self._request(method, path, params=params, auth=auth)
        if len(resp.content) > MAX_MEDIA_PROXY_BYTES:
            raise ApiError(f"Media response from {path} exceeded the size limit.", resp.status_code)
        if resp.status_code >= 400:
            self._parse(resp, path)
        mime_type = (resp.headers.get("content-type") or "application/octet-stream").split(";")[0].strip().lower()
        return resp.content, mime_type

    @staticmethod
    def _parse(resp: httpx.Response, path: str) -> Any:
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise ApiError(f"Response from {path} exceeded the size limit.", resp.status_code)
        try:
            data = resp.json() if resp.content else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
            if resp.status_code < 400:
                raise ApiError(f"Malformed (non-JSON) response from {path}.", resp.status_code)
        if resp.status_code == 401:
            raise AuthError("Authentication failed (401).", 401)
        if resp.status_code == 403:
            raise ApiError(f"Permission denied (403): {format_error(data)}", 403)
        if resp.status_code >= 400:
            raise ApiError(format_error(data) if data is not None else f"HTTP {resp.status_code}", resp.status_code)
        return data

    # ------------------------------------------------------------------ auth
    def _extract_token(self, data: Any) -> str | None:
        if isinstance(data, dict):
            for k in TOKEN_KEYS:
                if isinstance(data.get(k), str) and data[k]:
                    return data[k]
        for name in ("access_token", "access"):
            val = self._http.cookies.get(name)
            if val:
                return val
        return None

    async def _login(self) -> None:
        async with self._auth_lock:
            data = await self._send_login()
            token = self._extract_token(data)
            if not token:
                raise AuthError("Login succeeded but no access token was found in the response.")
            self._token = token

    async def _send_login(self) -> Any:
        try:
            return await self._send("POST", "/auth/login/", auth=False,
                                    body={"username": self._s.username, "password": self._s.password})
        except ApiError as exc:
            if isinstance(exc, (AuthError, EndpointNotAllowed)) or exc.status in (400, 401, 403):
                raise AuthError(f"Login rejected by the Market Intel API: {exc}") from None
            raise

    async def _reauthenticate(self) -> None:
        self._token = None
        try:
            body = {"refresh": self._refresh_token} if self._refresh_token else {}
            data = await self._send("POST", "/auth/refresh/", auth=False, body=body)
            token = self._extract_token(data)
            if token:
                self._token = token
                if self._on_token_refreshed:
                    new_ref = data.get("refresh") if isinstance(data, dict) else None
                    res = self._on_token_refreshed(token, new_ref)
                    if asyncio.iscoroutine(res):
                        await res
                return
        except ApiError:
            pass
        if not self._token_only and self._s.username and self._s.password:
            await self._login()
        else:
            raise AuthError("Session expired and could not be refreshed. Please sign in again.")

    async def _fetch_me_identity(self) -> Identity:
        if not self._token:
            await self._login()
        data = await self._send("GET", "/auth/me/")
        user = data.get("user", data) if isinstance(data, dict) else None
        if not isinstance(user, dict):
            raise AuthError("Unexpected /auth/me/ response; cannot confirm identity.")
        role = user.get("role")
        if isinstance(role, dict):
            role = role.get("name") or role.get("code")
        role = str(role or "").strip().upper()
        uid = str(user.get("id") or user.get("pk") or "")
        username = str(user.get("username") or self._s.username)
        is_superuser = bool(user.get("is_superuser"))
        raw_sessions = user.get("whatsapp_sessions")
        sessions = tuple(s for s in raw_sessions if isinstance(s, dict)) if isinstance(raw_sessions, list) else ()
        return Identity(uid, username, role, is_superuser=is_superuser, whatsapp_sessions=sessions)

    async def authenticate(self) -> Identity:
        """Log in and confirm the account is a PRE user. Raises AuthError otherwise."""
        if not self._token_only:
            await self._login()
        ident = await self._fetch_me_identity()
        if ident.role != "PRE":
            self._token = None
            raise AuthError(f"Account role is {ident.role or 'unknown'!r}; only PRE accounts are permitted.")
        self.identity = ident
        return self.identity

    async def authenticate_whatsapp(self, allowed_roles: tuple[str, ...] | frozenset[str] | None = None) -> Identity:
        """Confirm authenticated Market Intel DB user and role for WhatsApp operations."""
        ident = await self._fetch_me_identity()
        roles = set(allowed_roles or ("PRE", "LQ", "MANAGER", "SUPERUSER"))
        if ident.role not in roles and not (ident.is_superuser and "SUPERUSER" in roles):
            raise AuthError(
                f"Account role is {ident.role or 'unknown'!r}; not permitted for this WhatsApp operation."
            )
        self.identity = ident
        return self.identity

    # ------------------------------------------------------- allowed actions
    # Prospects
    async def list_prospects(self, params: dict[str, Any] | None = None) -> Any:
        return await self._send("GET", "/prospects/", params=params)

    async def get_prospect(self, prospect_id: str) -> Any:
        return await self._send("GET", f"/prospects/{prospect_id}/")

    async def create_prospect(self, payload: dict[str, Any]) -> Any:
        """Single attempt, no retry. Timeouts surface as ApiTimeout (uncertain)."""
        return await self._send("POST", "/prospects/", body=payload)

    async def update_prospect(self, prospect_id: str, payload: dict[str, Any]) -> Any:
        """Single attempt, no retry. Timeouts surface as ApiTimeout (uncertain)."""
        return await self._send("PATCH", f"/prospects/{prospect_id}/", body=payload)

    # Government Entities
    async def list_government_entities(self, params: dict[str, Any] | None = None) -> Any:
        return await self._send("GET", "/government-entities/", params=params)

    async def get_government_entity(self, entity_id: str) -> Any:
        return await self._send("GET", f"/government-entities/{entity_id}/")

    async def create_government_entity(self, payload: dict[str, Any]) -> Any:
        """Single attempt, no retry. Timeouts surface as ApiTimeout (uncertain)."""
        return await self._send("POST", "/government-entities/", body=payload)

    async def update_government_entity(self, entity_id: str, payload: dict[str, Any]) -> Any:
        """Single attempt, no retry. Timeouts surface as ApiTimeout (uncertain)."""
        return await self._send("PATCH", f"/government-entities/{entity_id}/", body=payload)

    # Market Events
    async def list_market_events(self, params: dict[str, Any] | None = None) -> Any:
        return await self._send("GET", "/market-events/", params=params)

    async def get_market_event(self, event_id: str) -> Any:
        return await self._send("GET", f"/market-events/{event_id}/")

    async def create_market_event(self, payload: dict[str, Any]) -> Any:
        """Single attempt, no retry. Timeouts surface as ApiTimeout (uncertain)."""
        return await self._send("POST", "/market-events/", body=payload)

    async def update_market_event(self, event_id: str, payload: dict[str, Any]) -> Any:
        """Single attempt, no retry. Timeouts surface as ApiTimeout (uncertain)."""
        return await self._send("PATCH", f"/market-events/{event_id}/", body=payload)

    # Generic record helpers
    async def get_record(self, module: str, record_id: str) -> Any:
        endpoint_map = {
            "prospect": f"/prospects/{record_id}/",
            "prospects": f"/prospects/{record_id}/",
            "government_entity": f"/government-entities/{record_id}/",
            "government_entities": f"/government-entities/{record_id}/",
            "government-entity": f"/government-entities/{record_id}/",
            "government-entities": f"/government-entities/{record_id}/",
            "market_event": f"/market-events/{record_id}/",
            "market_events": f"/market-events/{record_id}/",
            "market-event": f"/market-events/{record_id}/",
            "market-events": f"/market-events/{record_id}/",
        }
        path = endpoint_map.get(module.lower())
        if not path:
            raise EndpointNotAllowed(f"Unsupported module: {module}")
        return await self._send("GET", path)

    async def update_record(self, module: str, record_id: str, payload: dict[str, Any]) -> Any:
        endpoint_map = {
            "prospect": f"/prospects/{record_id}/",
            "prospects": f"/prospects/{record_id}/",
            "government_entity": f"/government-entities/{record_id}/",
            "government_entities": f"/government-entities/{record_id}/",
            "government-entity": f"/government-entities/{record_id}/",
            "government-entities": f"/government-entities/{record_id}/",
            "market_event": f"/market-events/{record_id}/",
            "market_events": f"/market-events/{record_id}/",
            "market-event": f"/market-events/{record_id}/",
            "market-events": f"/market-events/{record_id}/",
        }
        path = endpoint_map.get(module.lower())
        if not path:
            raise EndpointNotAllowed(f"Unsupported module: {module}")
        return await self._send("PATCH", path, body=payload)

    # Verification
    async def instant_verify(self, email: str) -> EmailResult:
        """Never raises: any failure becomes a non-VALID result."""
        try:
            data = await self._send("POST", "/email-verifications/instant-verify/",
                                    body={"email_address": email})
        except AuthError:
            raise
        except ApiTimeout:
            return EmailResult(email, "ERROR", "Email verification timed out.")
        except ApiError as exc:
            return EmailResult(email, "ERROR", f"Email verification service error: {exc}")
        status = None
        if isinstance(data, dict):
            status = data.get("verification_status") or data.get("status")
        status = status.strip().upper() if isinstance(status, str) else ""
        if status not in {"VALID", "INVALID", "UNKNOWN"}:
            return EmailResult(email, "ERROR", "Unexpected or missing verification status in response.")
        reason = data.get("reason") or data.get("message") or ""
        return EmailResult(email, status, str(reason)[:300])

    async def persist_verification(self, prospect_id: str, email: str, contact_id: str | None = None) -> Any:
        body: dict[str, Any] = {"prospect_id": prospect_id, "email_address": email}
        if contact_id is not None:
            body["prospect_contact_id"] = contact_id
        return await self._send("POST", "/email-verifications/verify/", body=body)

    # WhatsApp
    async def get_whatsapp_status(self) -> Any:
        return await self._send("GET", "/whatsapp/status/")

    async def list_whatsapp_sessions(self) -> Any:
        return await self._send("GET", "/whatsapp/session/list/")

    async def start_whatsapp_session(self, session_name: str | None = None) -> Any:
        body = {"session_name": session_name} if session_name else {}
        return await self._send("POST", "/whatsapp/session/start/", body=body)

    async def logout_whatsapp_session(self, session_name: str | None = None) -> Any:
        body = {"session_name": session_name} if session_name else {}
        return await self._send("POST", "/whatsapp/session/logout/", body=body)

    async def get_whatsapp_qr(self, force_restart: bool = False) -> Any:
        params = {"force_restart": "true"} if force_restart else None
        return await self._send("GET", "/whatsapp/qr/", params=params)

    async def list_whatsapp_conversations(self) -> Any:
        return await self._send("GET", "/whatsapp/conversations/")

    async def get_whatsapp_conversation_messages(self, conversation_id: str, limit: int = 2000) -> Any:
        return await self._send(
            "GET",
            f"/whatsapp/conversations/{conversation_id}/messages/",
            params={"limit": limit},
        )

    async def mark_whatsapp_conversation_read(self, conversation_id: str) -> Any:
        return await self._send("POST", f"/whatsapp/conversations/{conversation_id}/read/", body={})

    async def get_whatsapp_contact_info(self, chat_id: str) -> Any:
        return await self._send("GET", f"/whatsapp/contact-info/{chat_id}/")

    async def list_whatsapp_crm_contacts(self, search: str | None = None) -> Any:
        params = {"search": search} if search else None
        return await self._send("GET", "/whatsapp/crm-contacts/", params=params)

    async def send_whatsapp_message(self, payload: dict[str, Any]) -> Any:
        """Single attempt, no retry. Timeouts surface as ApiTimeout (uncertain)."""
        return await self._send("POST", "/whatsapp/send/", body=payload)

    async def send_whatsapp_media(
        self,
        chat_id: str,
        filename: str,
        content: bytes,
        mime_type: str,
        caption: str = "",
    ) -> Any:
        """Single attempt multipart upload, no retry. Timeouts surface as ApiTimeout (uncertain)."""
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        files = {"file": (filename, content, mime_type)}
        return await self._send("POST", "/whatsapp/send-media/", data=data, files=files)

    async def sync_whatsapp_history(
        self,
        days: int = 2,
        chat_id: str | None = None,
        session_name: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {"days": days}
        if chat_id:
            body["chat_id"] = chat_id
        if session_name:
            body["session_name"] = session_name
        return await self._send("POST", "/whatsapp/sync-history/", body=body)

    async def sync_whatsapp_conversation(self, conversation_id: str, days: int = 2) -> Any:
        return await self._send(
            "POST",
            f"/whatsapp/conversations/{conversation_id}/sync/",
            body={"days": days},
        )

    async def link_whatsapp_conversation_crm(
        self,
        conversation_id: str,
        prospect_contact_id: str | None,
    ) -> Any:
        return await self._send(
            "POST",
            f"/whatsapp/conversations/{conversation_id}/link-crm/",
            body={"prospect_contact_id": prospect_contact_id},
        )

    async def edit_whatsapp_message(self, message_id: str, text: str) -> Any:
        return await self._send(
            "POST",
            f"/whatsapp/messages/{message_id}/edit/",
            body={"text": text},
        )

    async def create_whatsapp_group(self, name: str, participants: list[str]) -> Any:
        return await self._send(
            "POST",
            "/whatsapp/groups/create/",
            body={"name": name, "participants": participants},
        )

    async def add_whatsapp_group_participants(self, chat_id: str, participants: list[str]) -> Any:
        return await self._send(
            "POST",
            f"/whatsapp/groups/{chat_id}/participants/add/",
            body={"participants": participants},
        )

    async def remove_whatsapp_group_participants(self, chat_id: str, participants: list[str]) -> Any:
        return await self._send(
            "POST",
            f"/whatsapp/groups/{chat_id}/participants/remove/",
            body={"participants": participants},
        )

    async def get_whatsapp_group_invite_link(self, chat_id: str) -> Any:
        return await self._send("GET", f"/whatsapp/groups/{chat_id}/invite-link/")

    async def download_whatsapp_media(self, message_id: str) -> tuple[bytes, str]:
        return await self._send_bytes("GET", "/whatsapp/media-proxy/", params={"message_id": message_id})
