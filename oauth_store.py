"""SQLite-based persistent storage for OAuth 2.0 clients, sessions, and tokens."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    OAuthClientInformationFull,
    RefreshToken,
)


@dataclass
class AuthSession:
    session_id: str
    client_id: str
    redirect_uri: str
    state: str | None
    code_challenge: str | None
    scopes: list[str]
    resource: str | None
    expires_at: float


@dataclass
class StoredAuthCode:
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    scopes: list[str]
    subject: str
    django_access_token: str
    django_refresh_token: str | None
    expires_at: float
    used: bool


@dataclass
class StoredTokenData:
    token: str
    client_id: str
    scopes: list[str]
    subject: str
    resource: str | None
    django_access_token: str
    django_refresh_token: str | None
    expires_at: float | None
    refresh_token: str | None = None


class OAuthStore:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    client_secret TEXT,
                    client_name TEXT,
                    redirect_uris_json TEXT,
                    grant_types_json TEXT,
                    scope TEXT,
                    token_endpoint_auth_method TEXT,
                    client_secret_expires_at REAL,
                    client_id_issued_at REAL,
                    created_at REAL
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    session_id TEXT PRIMARY KEY,
                    client_id TEXT,
                    redirect_uri TEXT,
                    state TEXT,
                    code_challenge TEXT,
                    scopes_json TEXT,
                    resource TEXT,
                    expires_at REAL
                );

                CREATE TABLE IF NOT EXISTS auth_codes (
                    code TEXT PRIMARY KEY,
                    client_id TEXT,
                    redirect_uri TEXT,
                    code_challenge TEXT,
                    scopes_json TEXT,
                    subject TEXT,
                    django_access_token TEXT,
                    django_refresh_token TEXT,
                    expires_at REAL,
                    used INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS access_tokens (
                    token TEXT PRIMARY KEY,
                    refresh_token TEXT,
                    client_id TEXT,
                    scopes_json TEXT,
                    subject TEXT,
                    resource TEXT,
                    django_access_token TEXT,
                    django_refresh_token TEXT,
                    expires_at REAL,
                    created_at REAL
                );

                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    token TEXT PRIMARY KEY,
                    access_token TEXT,
                    client_id TEXT,
                    scopes_json TEXT,
                    subject TEXT,
                    resource TEXT,
                    django_access_token TEXT,
                    django_refresh_token TEXT,
                    expires_at REAL,
                    created_at REAL
                );
            """)

            # Safe schema migration for existing databases:
            cur = conn.execute("PRAGMA table_info(oauth_clients)")
            existing_cols = {row["name"] for row in cur.fetchall()}
            if "token_endpoint_auth_method" not in existing_cols:
                conn.execute("ALTER TABLE oauth_clients ADD COLUMN token_endpoint_auth_method TEXT")
            if "client_secret_expires_at" not in existing_cols:
                conn.execute("ALTER TABLE oauth_clients ADD COLUMN client_secret_expires_at REAL")
            if "client_id_issued_at" not in existing_cols:
                conn.execute("ALTER TABLE oauth_clients ADD COLUMN client_id_issued_at REAL")

            # Backfill any existing clients with NULL token_endpoint_auth_method:
            conn.execute("""
                UPDATE oauth_clients
                SET token_endpoint_auth_method = CASE
                    WHEN client_secret IS NULL OR client_secret = '' THEN 'none'
                    ELSE 'client_secret_post'
                END
                WHERE token_endpoint_auth_method IS NULL
            """)
            conn.commit()

    # ------------------------------------------------------------- Clients
    def save_client(self, client: OAuthClientInformationFull) -> None:
        with self._get_connection() as conn:
            redirect_uris = [str(u) for u in client.redirect_uris] if client.redirect_uris else []
            auth_method = client.token_endpoint_auth_method
            if not auth_method:
                auth_method = "none" if not client.client_secret else "client_secret_post"

            conn.execute(
                """
                INSERT OR REPLACE INTO oauth_clients (
                    client_id, client_secret, client_name, redirect_uris_json, grant_types_json,
                    scope, token_endpoint_auth_method, client_secret_expires_at, client_id_issued_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client.client_id,
                    client.client_secret,
                    client.client_name,
                    json.dumps(redirect_uris),
                    json.dumps(client.grant_types),
                    client.scope,
                    auth_method,
                    client.client_secret_expires_at,
                    client.client_id_issued_at,
                    time.time(),
                ),
            )
            conn.commit()

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,))
            row = cur.fetchone()
            if not row:
                return None
            redirect_uris = [AnyUrl(u) for u in json.loads(row["redirect_uris_json"])] if row["redirect_uris_json"] else None
            grant_types = json.loads(row["grant_types_json"]) if row["grant_types_json"] else []
            keys = row.keys()
            auth_method = row["token_endpoint_auth_method"] if "token_endpoint_auth_method" in keys and row["token_endpoint_auth_method"] else None
            if not auth_method:
                auth_method = "none" if not row["client_secret"] else "client_secret_post"

            client_secret_expires_at = (
                float(row["client_secret_expires_at"])
                if "client_secret_expires_at" in keys and row["client_secret_expires_at"] is not None
                else None
            )
            client_id_issued_at = (
                int(row["client_id_issued_at"])
                if "client_id_issued_at" in keys and row["client_id_issued_at"] is not None
                else None
            )

            return OAuthClientInformationFull(
                client_id=row["client_id"],
                client_secret=row["client_secret"],
                client_name=row["client_name"],
                redirect_uris=redirect_uris,
                grant_types=grant_types,
                token_endpoint_auth_method=auth_method,
                client_secret_expires_at=client_secret_expires_at,
                client_id_issued_at=client_id_issued_at,
                scope=row["scope"],
            )

    # ------------------------------------------------------- Auth Sessions
    def save_auth_session(
        self,
        session_id: str,
        client_id: str,
        redirect_uri: str,
        state: str | None,
        code_challenge: str | None,
        scopes: list[str],
        resource: str | None,
        ttl_seconds: int = 600,
    ) -> None:
        expires_at = time.time() + ttl_seconds
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO auth_sessions (
                    session_id, client_id, redirect_uri, state, code_challenge, scopes_json, resource, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    client_id,
                    redirect_uri,
                    state,
                    code_challenge,
                    json.dumps(scopes),
                    resource,
                    expires_at,
                ),
            )
            conn.commit()

    def get_auth_session(self, session_id: str) -> AuthSession | None:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM auth_sessions WHERE session_id = ? AND expires_at > ?",
                (session_id, time.time()),
            )
            row = cur.fetchone()
            if not row:
                return None
            return AuthSession(
                session_id=row["session_id"],
                client_id=row["client_id"],
                redirect_uri=row["redirect_uri"],
                state=row["state"],
                code_challenge=row["code_challenge"],
                scopes=json.loads(row["scopes_json"]) if row["scopes_json"] else [],
                resource=row["resource"],
                expires_at=float(row["expires_at"]),
            )

    def delete_auth_session(self, session_id: str) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE session_id = ?", (session_id,))
            conn.commit()

    # ---------------------------------------------------------- Auth Codes
    def save_auth_code(
        self,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        scopes: list[str],
        subject: str,
        django_access_token: str,
        django_refresh_token: str | None = None,
        ttl_seconds: int = 300,
    ) -> None:
        expires_at = time.time() + ttl_seconds
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO auth_codes (
                    code, client_id, redirect_uri, code_challenge, scopes_json,
                    subject, django_access_token, django_refresh_token, expires_at, used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    code,
                    client_id,
                    redirect_uri,
                    code_challenge,
                    json.dumps(scopes),
                    subject,
                    django_access_token,
                    django_refresh_token,
                    expires_at,
                ),
            )
            conn.commit()

    def get_auth_code(self, code: str) -> StoredAuthCode | None:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM auth_codes WHERE code = ?", (code,))
            row = cur.fetchone()
            if not row:
                return None
            return StoredAuthCode(
                code=row["code"],
                client_id=row["client_id"],
                redirect_uri=row["redirect_uri"],
                code_challenge=row["code_challenge"],
                scopes=json.loads(row["scopes_json"]) if row["scopes_json"] else [],
                subject=row["subject"],
                django_access_token=row["django_access_token"],
                django_refresh_token=row["django_refresh_token"],
                expires_at=float(row["expires_at"]),
                used=bool(row["used"]),
            )

    def mark_auth_code_used(self, code: str) -> None:
        with self._get_connection() as conn:
            conn.execute("UPDATE auth_codes SET used = 1 WHERE code = ?", (code,))
            conn.commit()

    # ------------------------------------------------------------- Tokens
    def save_tokens(
        self,
        access_token: str,
        refresh_token: str | None,
        client_id: str,
        scopes: list[str],
        subject: str,
        resource: str | None,
        django_access_token: str,
        django_refresh_token: str | None,
        access_token_ttl: int = 30 * 86400,  # 30 days
        refresh_token_ttl: int = 90 * 86400,  # 90 days
    ) -> None:
        now = time.time()
        access_expires_at = now + access_token_ttl
        refresh_expires_at = now + refresh_token_ttl if refresh_token else None

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO access_tokens (
                    token, refresh_token, client_id, scopes_json, subject, resource,
                    django_access_token, django_refresh_token, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    access_token,
                    refresh_token,
                    client_id,
                    json.dumps(scopes),
                    subject,
                    resource,
                    django_access_token,
                    django_refresh_token,
                    access_expires_at,
                    now,
                ),
            )
            if refresh_token:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO refresh_tokens (
                        token, access_token, client_id, scopes_json, subject, resource,
                        django_access_token, django_refresh_token, expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        refresh_token,
                        access_token,
                        client_id,
                        json.dumps(scopes),
                        subject,
                        resource,
                        django_access_token,
                        django_refresh_token,
                        refresh_expires_at,
                        now,
                    ),
                )
            conn.commit()

    def get_token_data(self, access_token: str) -> StoredTokenData | None:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM access_tokens WHERE token = ?", (access_token,))
            row = cur.fetchone()
            if not row:
                return None
            return StoredTokenData(
                token=row["token"],
                refresh_token=row["refresh_token"],
                client_id=row["client_id"],
                scopes=json.loads(row["scopes_json"]) if row["scopes_json"] else [],
                subject=row["subject"],
                resource=row["resource"],
                django_access_token=row["django_access_token"],
                django_refresh_token=row["django_refresh_token"],
                expires_at=float(row["expires_at"]) if row["expires_at"] else None,
            )

    def get_refresh_token_data(self, refresh_token: str) -> StoredTokenData | None:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM refresh_tokens WHERE token = ?", (refresh_token,))
            row = cur.fetchone()
            if not row:
                return None
            return StoredTokenData(
                token=row["token"],
                refresh_token=row["token"],
                client_id=row["client_id"],
                scopes=json.loads(row["scopes_json"]) if row["scopes_json"] else [],
                subject=row["subject"],
                resource=row["resource"],
                django_access_token=row["django_access_token"],
                django_refresh_token=row["django_refresh_token"],
                expires_at=float(row["expires_at"]) if row["expires_at"] else None,
            )

    def update_django_tokens(
        self,
        mcp_access_token: str,
        new_django_access_token: str,
        new_django_refresh_token: str | None = None,
    ) -> None:
        with self._get_connection() as conn:
            if new_django_refresh_token:
                conn.execute(
                    """
                    UPDATE access_tokens
                    SET django_access_token = ?, django_refresh_token = ?
                    WHERE token = ?
                    """,
                    (new_django_access_token, new_django_refresh_token, mcp_access_token),
                )
            else:
                conn.execute(
                    """
                    UPDATE access_tokens
                    SET django_access_token = ?
                    WHERE token = ?
                    """,
                    (new_django_access_token, mcp_access_token),
                )
            conn.commit()

    def revoke_token(self, token_str: str) -> None:
        with self._get_connection() as conn:
            # Delete from access_tokens and linked refresh token
            cur = conn.execute("SELECT refresh_token FROM access_tokens WHERE token = ?", (token_str,))
            row = cur.fetchone()
            if row and row["refresh_token"]:
                conn.execute("DELETE FROM refresh_tokens WHERE token = ?", (row["refresh_token"],))
            conn.execute("DELETE FROM access_tokens WHERE token = ?", (token_str,))

            # Also delete if token_str was a refresh_token
            cur = conn.execute("SELECT access_token FROM refresh_tokens WHERE token = ?", (token_str,))
            row = cur.fetchone()
            if row and row["access_token"]:
                conn.execute("DELETE FROM access_tokens WHERE token = ?", (row["access_token"],))
            conn.execute("DELETE FROM refresh_tokens WHERE token = ?", (token_str,))
            conn.commit()
