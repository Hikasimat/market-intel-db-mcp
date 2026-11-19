"""Persistent SQLite research cache for identities, sources, field-level evidence, LLM responses, Vortexen profile, and token usage."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from research.models import FieldEvidence, VerificationStatus, VortexenProfile


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_company_key(company_name: str, country: str | None = None) -> str:
    norm_name = " ".join((company_name or "").strip().casefold().split())
    norm_country = " ".join((country or "").strip().casefold().split())
    return f"{norm_name}|{norm_country}"


class ResearchCache(Protocol):
    def get_company_identity(self, company_key: str, now: float | None = None) -> dict[str, Any] | None: ...
    def set_company_identity(
        self, company_key: str, canonical_name: str, country: str,
        official_domain: str | None, official_website: str | None,
        market_intel_id: str | None, data: dict[str, Any], ttl_seconds: int,
    ) -> None: ...
    def get_source(self, url_key: str, now: float | None = None) -> tuple[dict[str, Any] | None, bool]: ...
    def set_source(
        self, url_key: str, final_url: str, domain: str, content_hash: str,
        title: str, extracted: dict[str, Any], ttl_seconds: int,
    ) -> None: ...
    def get_company_fields(
        self, company_key: str, now: float | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]: ...
    def set_company_field(
        self, company_key: str, field_name: str, value: Any,
        evidence: list[FieldEvidence], verification_status: VerificationStatus,
        source_hashes: list[str], ttl_seconds: int,
    ) -> None: ...
    def get_provider_response(self, cache_key: str, now: float | None = None) -> dict[str, Any] | None: ...
    def set_provider_response(
        self, cache_key: str, provider: str, model: str, response: dict[str, Any],
        input_tokens: int, output_tokens: int, ttl_seconds: int,
    ) -> None: ...
    def get_vortexen_profile(
        self, profile_key: str = "vortexen_default", now: float | None = None,
    ) -> tuple[VortexenProfile | None, bool]: ...
    def set_vortexen_profile(
        self, profile: VortexenProfile, ttl_seconds: int, profile_key: str = "vortexen_default",
    ) -> VortexenProfile: ...
    def record_usage(
        self, job_id: str, job_type: str, target_name: str, provider: str, model: str,
        input_tokens: int, output_tokens: int, requests: int, cache_hits: int,
        search_calls: int, pages_fetched: int, elapsed_ms: int,
        reported_cost_usd: float | None, estimated_cost_usd: float | None,
    ) -> None: ...
    def get_status(self, company_key: str | None = None, now: float | None = None) -> dict[str, Any]: ...


class SQLiteResearchCache:
    """Thread-safe SQLite research cache with WAL mode and strict TTL separation."""

    def __init__(self, db_path: Path | str, clock: Any = time.time):
        self.db_path = str(db_path)
        self._clock = clock
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_db(self) -> None:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS company_identities (
                    company_key TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL,
                    country TEXT DEFAULT '',
                    official_domain TEXT,
                    official_website TEXT,
                    market_intel_id TEXT,
                    data_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fetched_sources (
                    url_key TEXT PRIMARY KEY,
                    final_url TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    extracted_json TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS company_fields (
                    company_key TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    verification_status TEXT NOT NULL,
                    source_hashes_json TEXT NOT NULL,
                    verified_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (company_key, field_name)
                );

                CREATE TABLE IF NOT EXISTS provider_responses (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vortexen_profiles (
                    profile_key TEXT PRIMARY KEY,
                    version INTEGER NOT NULL DEFAULT 1,
                    content_hash TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    retrieved_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS usage_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    requests INTEGER NOT NULL DEFAULT 0,
                    cache_hits INTEGER NOT NULL DEFAULT 0,
                    search_calls INTEGER NOT NULL DEFAULT 0,
                    pages_fetched INTEGER NOT NULL DEFAULT 0,
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    reported_cost_usd REAL,
                    estimated_cost_usd REAL,
                    created_at REAL NOT NULL
                );
            """)
            conn.commit()
        if self.db_path != ":memory:":
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass

    # ----------------------------------------------------- Company Identity
    def get_company_identity(self, company_key: str, now: float | None = None) -> dict[str, Any] | None:
        ts = self._clock() if now is None else now
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT data_json, expires_at FROM company_identities WHERE company_key = ?",
                (company_key,),
            ).fetchone()
            if not row or float(row["expires_at"]) <= ts:
                return None
            return json.loads(row["data_json"])

    def set_company_identity(
        self,
        company_key: str,
        canonical_name: str,
        country: str,
        official_domain: str | None,
        official_website: str | None,
        market_intel_id: str | None,
        data: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        ts = self._clock()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO company_identities (
                    company_key, canonical_name, country, official_domain,
                    official_website, market_intel_id, data_json, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_key,
                    canonical_name,
                    country or "",
                    official_domain,
                    official_website,
                    market_intel_id,
                    json.dumps(data, ensure_ascii=False),
                    ts,
                    ts + ttl_seconds,
                ),
            )
            conn.commit()

    # ----------------------------------------------------- Source Cache
    def get_source(self, url_key: str, now: float | None = None) -> tuple[dict[str, Any] | None, bool]:
        ts = self._clock() if now is None else now
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT extracted_json, expires_at FROM fetched_sources WHERE url_key = ?",
                (url_key,),
            ).fetchone()
            if not row:
                return None, False
            is_fresh = float(row["expires_at"]) > ts
            return json.loads(row["extracted_json"]), is_fresh

    def set_source(
        self,
        url_key: str,
        final_url: str,
        domain: str,
        content_hash: str,
        title: str,
        extracted: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        ts = self._clock()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO fetched_sources (
                    url_key, final_url, domain, content_hash, title,
                    extracted_json, fetched_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    url_key,
                    final_url,
                    domain,
                    content_hash,
                    title[:300],
                    json.dumps(extracted, ensure_ascii=False),
                    ts,
                    ts + ttl_seconds,
                ),
            )
            conn.commit()

    # ----------------------------------------------------- Field-Level Cache
    def get_company_fields(
        self, company_key: str, now: float | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Return (fresh_fields, stale_fields) so expired fields are NEVER treated as verified."""
        ts = self._clock() if now is None else now
        fresh: dict[str, dict[str, Any]] = {}
        stale: dict[str, dict[str, Any]] = {}
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM company_fields WHERE company_key = ?",
                (company_key,),
            ).fetchall()
            for r in rows:
                item = {
                    "field_name": r["field_name"],
                    "value": json.loads(r["value_json"]),
                    "evidence": json.loads(r["evidence_json"]),
                    "verification_status": r["verification_status"],
                    "source_hashes": json.loads(r["source_hashes_json"]),
                    "verified_at": float(r["verified_at"]),
                    "expires_at": float(r["expires_at"]),
                }
                if float(r["expires_at"]) > ts:
                    fresh[r["field_name"]] = item
                else:
                    stale[r["field_name"]] = item
        return fresh, stale

    def set_company_field(
        self,
        company_key: str,
        field_name: str,
        value: Any,
        evidence: list[FieldEvidence],
        verification_status: VerificationStatus,
        source_hashes: list[str],
        ttl_seconds: int,
    ) -> None:
        ts = self._clock()
        ev_json = json.dumps([e.model_dump(mode="json") for e in evidence], ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO company_fields (
                    company_key, field_name, value_json, evidence_json,
                    verification_status, source_hashes_json, verified_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_key,
                    field_name,
                    json.dumps(value, ensure_ascii=False),
                    ev_json,
                    verification_status.value,
                    json.dumps(list(dict.fromkeys(source_hashes))),
                    ts,
                    ts + ttl_seconds,
                ),
            )
            conn.commit()

    # ----------------------------------------------------- LLM Provider Cache
    def get_provider_response(self, cache_key: str, now: float | None = None) -> dict[str, Any] | None:
        ts = self._clock() if now is None else now
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT provider, model, response_json, input_tokens, output_tokens, expires_at "
                "FROM provider_responses WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if not row or float(row["expires_at"]) <= ts:
                return None
            return {
                "provider": row["provider"],
                "model": row["model"],
                "response": json.loads(row["response_json"]),
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
            }

    def set_provider_response(
        self,
        cache_key: str,
        provider: str,
        model: str,
        response: dict[str, Any],
        input_tokens: int,
        output_tokens: int,
        ttl_seconds: int,
    ) -> None:
        ts = self._clock()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO provider_responses (
                    cache_key, provider, model, response_json,
                    input_tokens, output_tokens, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    provider,
                    model,
                    json.dumps(response, ensure_ascii=False),
                    int(input_tokens),
                    int(output_tokens),
                    ts,
                    ts + ttl_seconds,
                ),
            )
            conn.commit()

    # ----------------------------------------------------- Vortexen Profile
    def get_vortexen_profile(
        self, profile_key: str = "vortexen_default", now: float | None = None,
    ) -> tuple[VortexenProfile | None, bool]:
        ts = self._clock() if now is None else now
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT profile_json, expires_at, version FROM vortexen_profiles WHERE profile_key = ?",
                (profile_key,),
            ).fetchone()
            if not row:
                return None, False
            prof = VortexenProfile.model_validate(json.loads(row["profile_json"]))
            prof.version = int(row["version"])
            is_fresh = float(row["expires_at"]) > ts
            return prof, is_fresh

    def set_vortexen_profile(
        self,
        profile: VortexenProfile,
        ttl_seconds: int,
        profile_key: str = "vortexen_default",
    ) -> VortexenProfile:
        ts = self._clock()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT version, content_hash FROM vortexen_profiles WHERE profile_key = ?",
                (profile_key,),
            ).fetchone()
            version = 1
            if existing:
                old_ver = int(existing["version"])
                version = old_ver if existing["content_hash"] == profile.content_hash else old_ver + 1
            updated = profile.model_copy(update={"version": version})
            conn.execute(
                """
                INSERT OR REPLACE INTO vortexen_profiles (
                    profile_key, version, content_hash, profile_json, retrieved_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_key,
                    version,
                    updated.content_hash,
                    updated.model_dump_json(),
                    ts,
                    ts + ttl_seconds,
                ),
            )
            conn.commit()
            return updated

    # ----------------------------------------------------- Usage Ledger & Status
    def record_usage(
        self,
        job_id: str,
        job_type: str,
        target_name: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        requests: int,
        cache_hits: int,
        search_calls: int,
        pages_fetched: int,
        elapsed_ms: int,
        reported_cost_usd: float | None,
        estimated_cost_usd: float | None,
    ) -> None:
        ts = self._clock()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_ledger (
                    job_id, job_type, target_name, provider, model,
                    input_tokens, output_tokens, requests, cache_hits,
                    search_calls, pages_fetched, elapsed_ms,
                    reported_cost_usd, estimated_cost_usd, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    target_name[:200],
                    provider,
                    model,
                    int(input_tokens),
                    int(output_tokens),
                    int(requests),
                    int(cache_hits),
                    int(search_calls),
                    int(pages_fetched),
                    int(elapsed_ms),
                    reported_cost_usd,
                    estimated_cost_usd,
                    ts,
                ),
            )
            conn.commit()

    def get_status(self, company_key: str | None = None, now: float | None = None) -> dict[str, Any]:
        ts = self._clock() if now is None else now
        with self._lock, self._connect() as conn:
            ident_count = conn.execute("SELECT COUNT(*) FROM company_identities WHERE expires_at > ?", (ts,)).fetchone()[0]
            fresh_sources = conn.execute("SELECT COUNT(*) FROM fetched_sources WHERE expires_at > ?", (ts,)).fetchone()[0]
            stale_sources = conn.execute("SELECT COUNT(*) FROM fetched_sources WHERE expires_at <= ?", (ts,)).fetchone()[0]
            fresh_fields_total = conn.execute("SELECT COUNT(*) FROM company_fields WHERE expires_at > ?", (ts,)).fetchone()[0]
            stale_fields_total = conn.execute("SELECT COUNT(*) FROM company_fields WHERE expires_at <= ?", (ts,)).fetchone()[0]
            llm_cached = conn.execute("SELECT COUNT(*) FROM provider_responses WHERE expires_at > ?", (ts,)).fetchone()[0]

            usage_row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0) AS in_tok,
                    COALESCE(SUM(output_tokens), 0) AS out_tok,
                    COALESCE(SUM(requests), 0) AS reqs,
                    COALESCE(SUM(cache_hits), 0) AS hits,
                    COALESCE(SUM(search_calls), 0) AS searches,
                    COALESCE(SUM(pages_fetched), 0) AS pages,
                    SUM(reported_cost_usd) AS reported_cost,
                    SUM(estimated_cost_usd) AS estimated_cost
                FROM usage_ledger
                """
            ).fetchone()

            vx_row = conn.execute(
                "SELECT version, content_hash, retrieved_at, expires_at FROM vortexen_profiles WHERE profile_key = 'vortexen_default'"
            ).fetchone()

            company_detail: dict[str, Any] | None = None
            if company_key:
                ident = conn.execute(
                    "SELECT canonical_name, country, official_domain, official_website, expires_at "
                    "FROM company_identities WHERE company_key = ?",
                    (company_key,),
                ).fetchone()
                f_rows = conn.execute(
                    "SELECT field_name, verification_status, verified_at, expires_at "
                    "FROM company_fields WHERE company_key = ?",
                    (company_key,),
                ).fetchall()
                company_detail = {
                    "company_key": company_key,
                    "identity_cached": bool(ident),
                    "identity_fresh": bool(ident and float(ident["expires_at"]) > ts),
                    "official_website": ident["official_website"] if ident else None,
                    "fresh_fields": [
                        {"field_name": r["field_name"], "status": r["verification_status"]}
                        for r in f_rows if float(r["expires_at"]) > ts
                    ],
                    "expired_fields": [
                        {"field_name": r["field_name"], "status": r["verification_status"]}
                        for r in f_rows if float(r["expires_at"]) <= ts
                    ],
                }

        return {
            "ok": True,
            "read_only": True,
            "cache_summary": {
                "fresh_company_identities": int(ident_count),
                "fresh_sources": int(fresh_sources),
                "expired_sources": int(stale_sources),
                "fresh_company_fields": int(fresh_fields_total),
                "expired_company_fields": int(stale_fields_total),
                "fresh_llm_responses": int(llm_cached),
            },
            "vortexen_profile": (
                {
                    "cached": True,
                    "version": int(vx_row["version"]),
                    "content_hash": vx_row["content_hash"],
                    "fresh": float(vx_row["expires_at"]) > ts,
                    "expires_in_seconds": max(0, int(float(vx_row["expires_at"]) - ts)),
                }
                if vx_row
                else {"cached": False, "fresh": False}
            ),
            "cumulative_usage": {
                "input_tokens": int(usage_row["in_tok"]),
                "output_tokens": int(usage_row["out_tok"]),
                "total_tokens": int(usage_row["in_tok"]) + int(usage_row["out_tok"]),
                "llm_requests": int(usage_row["reqs"]),
                "cache_hits": int(usage_row["hits"]),
                "search_calls": int(usage_row["searches"]),
                "pages_fetched": int(usage_row["pages"]),
                "reported_cost_usd": usage_row["reported_cost"],
                "estimated_cost_usd": usage_row["estimated_cost"],
            },
            "company_status": company_detail,
        }
