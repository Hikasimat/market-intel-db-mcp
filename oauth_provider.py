"""OAuth 2.0 authorization server provider implementation for FastMCP."""
from __future__ import annotations

import hmac
import logging
import secrets
import time
from urllib.parse import urlparse

import httpx
from pydantic import AnyUrl

from config import LOCAL_HOSTS
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    OAuthClientInformationFull,
    OAuthToken,
    RefreshToken,
    TokenError,
)
from oauth_store import OAuthStore
from research.security import SSRFBlockedError, create_ssrf_guarded_transport, validate_url

log = logging.getLogger("prospect_mcp.oauth")

MAX_CIMD_BYTES = 102_400


def _is_allowed_redirect_uri(uri: str) -> bool:
    parsed = urlparse((uri or "").strip())
    if parsed.scheme == "https" and bool(parsed.hostname):
        return True
    if parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS:
        return True
    return False


class SQLiteOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(
        self,
        store: OAuthStore,
        issuer_url: str,
        access_token_ttl: int = 3600,
        refresh_token_ttl: int = 604800,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.store = store
        self.issuer_url = issuer_url.rstrip("/")
        self.access_token_ttl = access_token_ttl
        self.refresh_token_ttl = refresh_token_ttl
        self._transport = transport

    # -------------------------------------------------------- Client Management
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self.store.get_client(client_id)
        if client:
            return client

        # Support CIMD (Client ID Metadata Document) over validated HTTPS only
        if client_id.startswith("https://") or client_id.startswith("http://"):
            try:
                safe_url = validate_url(client_id, require_https=True)
            except SSRFBlockedError as exc:
                log.warning("Blocked unsafe CIMD client_id URL: %s", exc)
                return None

            try:
                transport = self._transport or create_ssrf_guarded_transport()
                async with httpx.AsyncClient(
                    timeout=10.0,
                    follow_redirects=False,
                    transport=transport,
                ) as http_client:
                    resp = await http_client.get(safe_url, headers={"Accept": "application/json"})
                    if resp.status_code == 200 and len(resp.content) <= MAX_CIMD_BYTES:
                        data = resp.json()
                        raw_redirects = data.get("redirect_uris", [])
                        redirect_uris = [
                            AnyUrl(u)
                            for u in raw_redirects
                            if isinstance(u, str) and _is_allowed_redirect_uri(u)
                        ]
                        if not redirect_uris:
                            log.warning("CIMD metadata did not contain valid redirect_uris.")
                            return None
                        client_info = OAuthClientInformationFull(
                            client_id=client_id,
                            client_name=str(data.get("client_name", "MCP Client"))[:120],
                            redirect_uris=redirect_uris,
                            grant_types=data.get("grant_types", ["authorization_code", "refresh_token"]),
                            token_endpoint_auth_method=data.get("token_endpoint_auth_method", "none"),
                            scope=data.get("scope"),
                        )
                        self.store.save_client(client_info)
                        return client_info
            except Exception as exc:
                log.warning("Failed to fetch CIMD client metadata (%s)", type(exc).__name__)

        return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        log.info(
            "Registering OAuth client: %s (auth_method=%s)",
            client_info.client_name,
            client_info.token_endpoint_auth_method,
        )
        if not client_info.token_endpoint_auth_method:
            client_info.token_endpoint_auth_method = "none" if not client_info.client_secret else "client_secret_post"
        self.store.save_client(client_info)

    # ------------------------------------------------------------ Authorization
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        session_id = secrets.token_urlsafe(32)
        redirect_uri_str = str(params.redirect_uri) if params.redirect_uri else ""
        if redirect_uri_str:
            if not _is_allowed_redirect_uri(redirect_uri_str):
                raise ValueError("Unsupported redirect_uri scheme or host.")
            if client.redirect_uris:
                allowed = {str(u) for u in client.redirect_uris}
                if redirect_uri_str not in allowed:
                    raise ValueError("redirect_uri is not registered for this client.")
        self.store.save_auth_session(
            session_id=session_id,
            client_id=client.client_id,
            redirect_uri=redirect_uri_str,
            state=params.state,
            code_challenge=params.code_challenge,
            scopes=params.scopes or [],
            resource=params.resource,
            ttl_seconds=600,
        )
        return f"{self.issuer_url}/login?session_id={session_id}"

    # ------------------------------------------------------- Authorization Code
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code_data = self.store.get_auth_code(authorization_code)
        if not code_data:
            return None
        if not hmac.compare_digest(code_data.client_id, client.client_id):
            log.warning("Authorization code client_id mismatch.")
            return None
        if code_data.used:
            log.warning("Attempted reuse of authorization code.")
            return None
        if code_data.expires_at < time.time():
            log.warning("Rejected expired authorization code.")
            return None

        return AuthorizationCode(
            code=code_data.code,
            scopes=code_data.scopes,
            expires_at=code_data.expires_at,
            client_id=code_data.client_id,
            code_challenge=code_data.code_challenge,
            redirect_uri=AnyUrl(code_data.redirect_uri),
            redirect_uri_provided_explicitly=True,
            subject=code_data.subject,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        code_data = self.store.get_auth_code(authorization_code.code)
        if not code_data or code_data.used or code_data.expires_at < time.time():
            raise TokenError("invalid_grant", "Authorization code invalid, expired, or already used.")
        if not hmac.compare_digest(code_data.client_id, client.client_id):
            raise TokenError("invalid_grant", "Authorization code was issued to another client.")

        if not self.store.mark_auth_code_used(authorization_code.code):
            raise TokenError("invalid_grant", "Authorization code invalid or already used.")

        access_token = f"mcp_at_{secrets.token_urlsafe(32)}"
        refresh_token = f"mcp_rt_{secrets.token_urlsafe(32)}"
        token_ttl = self.access_token_ttl

        self.store.save_tokens(
            access_token=access_token,
            refresh_token=refresh_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            subject=authorization_code.subject or "",
            resource=authorization_code.resource or self.issuer_url,
            django_access_token=code_data.django_access_token,
            django_refresh_token=code_data.django_refresh_token,
            access_token_ttl=token_ttl,
            refresh_token_ttl=self.refresh_token_ttl,
            role=code_data.role,
            is_superuser=code_data.is_superuser,
        )

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=token_ttl,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
            refresh_token=refresh_token,
        )

    # ------------------------------------------------------------ Refresh Token
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token_data = self.store.get_refresh_token_data(refresh_token)
        if not token_data:
            return None
        if not hmac.compare_digest(token_data.client_id, client.client_id):
            return None
        if token_data.expires_at and token_data.expires_at < time.time():
            return None

        return RefreshToken(
            token=token_data.token,
            client_id=token_data.client_id,
            scopes=token_data.scopes,
            expires_at=int(token_data.expires_at) if token_data.expires_at else None,
            resource=token_data.resource,
            subject=token_data.subject,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        old_data = self.store.get_refresh_token_data(refresh_token.token)
        if not old_data:
            raise TokenError("invalid_grant", "Refresh token not found.")
        if not hmac.compare_digest(old_data.client_id, client.client_id):
            raise TokenError("invalid_grant", "Refresh token was issued to another client.")
        if old_data.expires_at and old_data.expires_at < time.time():
            self.store.revoke_token(refresh_token.token)
            raise TokenError("invalid_grant", "Refresh token expired.")

        # Revoke old refresh token & access token
        self.store.revoke_token(refresh_token.token)

        new_access_token = f"mcp_at_{secrets.token_urlsafe(32)}"
        new_refresh_token = f"mcp_rt_{secrets.token_urlsafe(32)}"
        token_ttl = self.access_token_ttl

        self.store.save_tokens(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            subject=refresh_token.subject or "",
            resource=refresh_token.resource or self.issuer_url,
            django_access_token=old_data.django_access_token,
            django_refresh_token=old_data.django_refresh_token,
            access_token_ttl=token_ttl,
            refresh_token_ttl=self.refresh_token_ttl,
            role=old_data.role,
            is_superuser=old_data.is_superuser,
        )

        return OAuthToken(
            access_token=new_access_token,
            token_type="Bearer",
            expires_in=token_ttl,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=new_refresh_token,
        )

    # ------------------------------------------------------------- Access Token
    async def load_access_token(self, token: str) -> AccessToken | None:
        token_data = self.store.get_token_data(token)
        if not token_data:
            return None
        if token_data.expires_at and token_data.expires_at < time.time():
            return None

        return AccessToken(
            token=token_data.token,
            client_id=token_data.client_id,
            scopes=token_data.scopes,
            expires_at=int(token_data.expires_at) if token_data.expires_at else None,
            resource=token_data.resource or self.issuer_url,
            subject=token_data.subject,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify token implementing the TokenVerifier protocol directly."""
        return await self.load_access_token(token)

    # --------------------------------------------------------------- Revocation
    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        token_str = getattr(token, "token", str(token))
        self.store.revoke_token(token_str)
