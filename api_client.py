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
})

ALLOWED_ENDPOINTS = ALLOWED_STATIC_ENDPOINTS


def is_endpoint_allowed(method: str, path: str) -> bool:
    if method == "DELETE":
        return False
    if (method, path) in ALLOWED_STATIC_ENDPOINTS:
        return True
    if method in ("GET", "PATCH") and UUID_PATH_RE.match(path):
        return True
    return False


def is_retry_safe(method: str, path: str) -> bool:
    if method == "GET":
        return True
    if (method, path) == ("POST", "/email-verifications/instant-verify/"):
        return True
    return False
MAX_RESPONSE_BYTES = 2_000_000
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


def format_error(body: Any) -> str:
    """Flatten DRF-style error bodies into a short readable string."""
    def flat(value: Any, prefix: str = "") -> list[str]:
        if isinstance(value, dict):
            return [m for k, v in value.items() for m in flat(v, f"{prefix}{k}." if k not in ("detail", "non_field_errors") else prefix)]
        if isinstance(value, list):
            return [m for v in value for m in flat(v, prefix)]
        text = str(value)
        return [f"{prefix.rstrip('.')}: {text}" if prefix else text]
    text = "; ".join(flat(body)) or "No error details returned."
    return text[:ERROR_TEXT_LIMIT]


@dataclass
class Identity:
    user_id: str
    username: str
    role: str

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
        self._token_only: bool = bool(access_token and not settings.username)
        self._auth_lock = asyncio.Lock()
        self.identity: Identity | None = identity

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ low level
    async def _send(self, method: str, path: str, *, params=None, body=None, auth=True) -> Any:
        if not is_endpoint_allowed(method, path):
            raise EndpointNotAllowed(f"{method} {path} is not permitted by this MCP server.")
        if auth and not self._token:
            await self._login()
        # Network retries only for side-effect-free requests; never for creation or updates.
        network_retries = 1 if is_retry_safe(method, path) else 0
        reauthed = False
        while True:
            headers = {"Authorization": f"Bearer {self._token}"} if auth and self._token else {}
            try:
                resp = await self._http.request(
                    method, self._s.api_base_url + path, params=params, json=body, headers=headers)
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
            return self._parse(resp, path)

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

    async def authenticate(self) -> Identity:
        """Log in and confirm the account is a PRE user. Raises AuthError otherwise."""
        await self._login()
        data = await self._send("GET", "/auth/me/")
        user = data.get("user", data) if isinstance(data, dict) else None
        if not isinstance(user, dict):
            raise AuthError("Unexpected /auth/me/ response; cannot confirm identity.")
        role = user.get("role")
        if isinstance(role, dict):
            role = role.get("name") or role.get("code")
        role = str(role or "").strip().upper()
        if role != "PRE":
            self._token = None
            raise AuthError(f"Account role is {role or 'unknown'!r}; only PRE accounts are permitted.")
        uid = str(user.get("id") or user.get("pk") or "")
        username = str(user.get("username") or self._s.username)
        self.identity = Identity(uid, username, role)
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
