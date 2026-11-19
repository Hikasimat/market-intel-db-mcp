"""Environment configuration. Loads `.env` from this directory and fails readably."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

SERVER_DIR = Path(__file__).resolve().parent
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

VALID_SEARCH_PROVIDERS = frozenset({"auto", "brave", "tavily", "serper", "duckduckgo", "none"})
VALID_LLM_PROVIDERS = frozenset({"groq", "anthropic", "openai", "auto", "none"})


class ConfigError(Exception):
    """Raised when required settings are missing or unsafe."""


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    username: str
    password: str = ""  # never logged / repr'd
    confirmation_ttl_seconds: int = 600
    request_timeout_seconds: float = 30.0
    log_level: str = "INFO"
    max_parent_lookup_pages: int = 10
    auth_token: str | None = None
    allow_query_token: bool = False
    oauth_enabled: bool = False
    oauth_issuer_url: str | None = None
    oauth_db_path: Path | None = None
    oauth_access_token_ttl_seconds: int = 3600
    oauth_refresh_token_ttl_seconds: int = 604800
    cors_allowed_origins: tuple[str, ...] = ("https://claude.ai",)

    # Research engine & cache
    research_cache_db_path: Path | None = None
    vortexen_website_url: str = "https://vortexen.com/"
    vortexen_profile_ttl_seconds: int = 604800
    research_field_ttl_seconds: int = 604800
    research_volatile_field_ttl_seconds: int = 259200
    research_source_ttl_seconds: int = 86400
    research_max_pages_per_company: int = 8
    research_max_event_participants: int = 25
    research_max_concurrency: int = 3
    research_fetch_timeout_seconds: float = 12.0
    research_max_response_bytes: int = 1_500_000
    research_max_context_chars: int = 12_000
    research_max_llm_calls_per_company: int = 6
    research_max_tokens_per_company: int = 25_000
    research_respect_robots_txt: bool = True

    # Web search providers
    search_provider: str = "auto"
    brave_search_api_key: str | None = None
    tavily_api_key: str | None = None
    serper_api_key: str | None = None
    search_enable_ddg_fallback: bool = True

    # AI providers (Groq, Anthropic Claude, OpenAI)
    groq_api_key: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    research_primary_llm_provider: str = "groq"
    research_escalation_llm_provider: str = "anthropic"
    research_fallback_llm_provider: str = "openai"
    research_primary_model: str = "llama-3.3-70b-versatile"
    research_escalation_model: str = "claude-sonnet-4-20250514"
    research_fallback_model: str = "gpt-4o-mini"
    groq_model: str | None = None
    anthropic_model: str | None = None
    openai_model: str | None = None

    def secret_values(self) -> tuple[str, ...]:
        """Return all non-empty secret strings so log filters can scrub them."""
        raw = (
            self.password,
            self.auth_token,
            self.brave_search_api_key,
            self.tavily_api_key,
            self.serper_api_key,
            self.groq_api_key,
            self.anthropic_api_key,
            self.openai_api_key,
        )
        return tuple(s for s in raw if s and len(s) >= 4)

    def __repr__(self) -> str:  # keep secrets out of any accidental log line
        return (
            f"Settings(api_base_url={self.api_base_url!r}, username={self.username!r}, "
            f"password='***', oauth_enabled={self.oauth_enabled!r})"
        )


def _number(name: str, default: str, cast, minimum: float, maximum: float | None = None):
    raw = os.environ.get(name, default).strip()
    if not raw:
        raw = default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}.")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}.")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}.")


def _parse_cors_origins(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return ("https://claude.ai",)
    origins: list[str] = []
    for item in raw.split(","):
        origin = item.strip().rstrip("/")
        if not origin:
            continue
        if origin == "*":
            raise ConfigError("MCP_CORS_ALLOWED_ORIGINS cannot contain wildcard '*' when credentials are enabled.")
        p = urlparse(origin)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ConfigError(f"Invalid origin in MCP_CORS_ALLOWED_ORIGINS: {origin!r}.")
        if p.scheme == "http" and p.hostname not in LOCAL_HOSTS:
            raise ConfigError(f"Non-local origin in MCP_CORS_ALLOWED_ORIGINS must use https: {origin!r}.")
        if (p.path and p.path != "/") or p.query or p.fragment or p.username or p.password:
            raise ConfigError(f"Origin in MCP_CORS_ALLOWED_ORIGINS must be scheme://host[:port] only: {origin!r}.")
        origins.append(origin)
    if not origins:
        raise ConfigError("MCP_CORS_ALLOWED_ORIGINS must contain at least one valid origin.")
    return tuple(dict.fromkeys(origins))


def _validate_https_url(name: str, raw: str) -> str:
    val = raw.strip()
    p = urlparse(val)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ConfigError(f"{name} must be an absolute http(s) URL.")
    if p.scheme == "http" and p.hostname not in LOCAL_HOSTS:
        raise ConfigError(f"{name} must use HTTPS for non-local hosts.")
    return val


def load_settings(env_file: Path | None = None) -> Settings:
    """Load and validate settings. `env_file=None` uses `<server dir>/.env`."""
    load_dotenv(env_file or SERVER_DIR / ".env", override=False)

    missing = [
        n for n in ("MARKET_INTEL_API_BASE_URL", "MARKET_INTEL_USERNAME", "MARKET_INTEL_PASSWORD")
        if not os.environ.get(n, "").strip()
    ]
    if missing:
        raise ConfigError(
            "Missing required environment settings: " + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    base = os.environ["MARKET_INTEL_API_BASE_URL"].strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError("MARKET_INTEL_API_BASE_URL must be an absolute http(s) URL ending in /api.")
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        raise ConfigError("Use HTTPS for non-local hosts (plain http is allowed for localhost only).")

    oauth_issuer = os.environ.get("MCP_OAUTH_ISSUER_URL", os.environ.get("MCP_ISSUER_URL", "")).strip().rstrip("/") or None
    if oauth_issuer:
        _validate_https_url("MCP_OAUTH_ISSUER_URL", oauth_issuer)
    raw_oauth = os.environ.get("MCP_OAUTH_ENABLED", "").strip().lower()
    oauth_enabled = raw_oauth in ("1", "true", "yes") or bool(oauth_issuer)

    db_path_str = os.environ.get("MCP_OAUTH_DB_PATH", "").strip()
    oauth_db_path = Path(db_path_str).resolve() if db_path_str else (SERVER_DIR / "oauth.db")

    research_db_str = os.environ.get("RESEARCH_CACHE_DB_PATH", "").strip()
    research_cache_db_path = Path(research_db_str).resolve() if research_db_str else (SERVER_DIR / "research_cache.db")

    vortexen_url = _validate_https_url(
        "VORTEXEN_WEBSITE_URL",
        os.environ.get("VORTEXEN_WEBSITE_URL", "https://vortexen.com/").strip() or "https://vortexen.com/",
    )

    search_provider = (os.environ.get("RESEARCH_SEARCH_PROVIDER", "auto").strip().lower() or "auto")
    if search_provider not in VALID_SEARCH_PROVIDERS:
        raise ConfigError(
            f"RESEARCH_SEARCH_PROVIDER must be one of {sorted(VALID_SEARCH_PROVIDERS)}, got {search_provider!r}."
        )

    primary_llm = (os.environ.get("RESEARCH_PRIMARY_LLM_PROVIDER", "groq").strip().lower() or "groq")
    escalation_llm = (os.environ.get("RESEARCH_ESCALATION_LLM_PROVIDER", "anthropic").strip().lower() or "anthropic")
    fallback_llm = (os.environ.get("RESEARCH_FALLBACK_LLM_PROVIDER", "openai").strip().lower() or "openai")
    for env_name, val in (
        ("RESEARCH_PRIMARY_LLM_PROVIDER", primary_llm),
        ("RESEARCH_ESCALATION_LLM_PROVIDER", escalation_llm),
        ("RESEARCH_FALLBACK_LLM_PROVIDER", fallback_llm),
    ):
        if val not in VALID_LLM_PROVIDERS:
            raise ConfigError(f"{env_name} must be one of {sorted(VALID_LLM_PROVIDERS)}, got {val!r}.")

    return Settings(
        api_base_url=base,
        username=os.environ["MARKET_INTEL_USERNAME"].strip(),
        password=os.environ["MARKET_INTEL_PASSWORD"],
        confirmation_ttl_seconds=_number("MCP_CONFIRMATION_TTL_SECONDS", "600", int, 30),
        request_timeout_seconds=_number("MCP_REQUEST_TIMEOUT_SECONDS", "30", float, 1),
        log_level=os.environ.get("MCP_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        auth_token=os.environ.get("MCP_AUTH_TOKEN", "").strip() or None,
        allow_query_token=_bool("MCP_ALLOW_QUERY_TOKEN", False),
        oauth_enabled=oauth_enabled,
        oauth_issuer_url=oauth_issuer,
        oauth_db_path=oauth_db_path,
        oauth_access_token_ttl_seconds=_number("MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS", "3600", int, 300, 86400),
        oauth_refresh_token_ttl_seconds=_number("MCP_OAUTH_REFRESH_TOKEN_TTL_SECONDS", "604800", int, 3600, 2592000),
        cors_allowed_origins=_parse_cors_origins(os.environ.get("MCP_CORS_ALLOWED_ORIGINS")),
        research_cache_db_path=research_cache_db_path,
        vortexen_website_url=vortexen_url,
        vortexen_profile_ttl_seconds=_number("VORTEXEN_PROFILE_TTL_SECONDS", "604800", int, 300),
        research_field_ttl_seconds=_number("RESEARCH_FIELD_TTL_SECONDS", "604800", int, 300),
        research_volatile_field_ttl_seconds=_number("RESEARCH_VOLATILE_FIELD_TTL_SECONDS", "259200", int, 300),
        research_source_ttl_seconds=_number("RESEARCH_SOURCE_TTL_SECONDS", "86400", int, 60),
        research_max_pages_per_company=_number("RESEARCH_MAX_PAGES_PER_COMPANY", "8", int, 1, 25),
        research_max_event_participants=_number("RESEARCH_MAX_EVENT_PARTICIPANTS", "25", int, 1, 50),
        research_max_concurrency=_number("RESEARCH_MAX_CONCURRENCY", "3", int, 1, 10),
        research_fetch_timeout_seconds=_number("RESEARCH_FETCH_TIMEOUT_SECONDS", "12", float, 1, 60),
        research_max_response_bytes=_number("RESEARCH_MAX_RESPONSE_BYTES", "1500000", int, 10000, 10000000),
        research_max_context_chars=_number("RESEARCH_MAX_CONTEXT_CHARS", "12000", int, 1000, 60000),
        research_max_llm_calls_per_company=_number("RESEARCH_MAX_LLM_CALLS_PER_COMPANY", "6", int, 1, 20),
        research_max_tokens_per_company=_number("RESEARCH_MAX_TOKENS_PER_COMPANY", "25000", int, 1000, 200000),
        research_respect_robots_txt=_bool("RESEARCH_RESPECT_ROBOTS_TXT", True),
        search_provider=search_provider,
        brave_search_api_key=os.environ.get("BRAVE_SEARCH_API_KEY", "").strip() or None,
        tavily_api_key=os.environ.get("TAVILY_API_KEY", "").strip() or None,
        serper_api_key=os.environ.get("SERPER_API_KEY", "").strip() or None,
        search_enable_ddg_fallback=_bool("RESEARCH_SEARCH_ENABLE_DDG_FALLBACK", True),
        groq_api_key=os.environ.get("GROQ_API_KEY", "").strip() or None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip() or None,
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip() or None,
        research_primary_llm_provider=primary_llm,
        research_escalation_llm_provider=escalation_llm,
        research_fallback_llm_provider=fallback_llm,
        research_primary_model=os.environ.get("RESEARCH_PRIMARY_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile",
        research_escalation_model=os.environ.get("RESEARCH_ESCALATION_MODEL", "claude-sonnet-4-20250514").strip() or "claude-sonnet-4-20250514",
        research_fallback_model=os.environ.get("RESEARCH_FALLBACK_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        groq_model=os.environ.get("GROQ_MODEL", "").strip() or None,
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "").strip() or None,
        openai_model=os.environ.get("OPENAI_MODEL", "").strip() or None,
    )
