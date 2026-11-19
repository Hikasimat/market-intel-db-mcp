"""Research security controls: SSRF defense, DNS rebinding guard, URL/MIME checks, prompt-injection isolation, and secret redaction."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import typing
from urllib.parse import urlparse, urlunparse

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend

from research.models import FetchedPage


class SecurityError(Exception):
    """Base exception for research security violations."""


class SSRFBlockedError(SecurityError):
    """Raised when a URL, IP address, or DNS resolution target is unsafe."""


class UnsafeContentError(SecurityError):
    """Raised when a fetched resource has disallowed MIME type or oversized body."""


BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "host.docker.internal",
    "gateway.docker.internal",
})

BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
    ".lan",
)

# Cloud metadata & CGNAT ranges that must always be rejected even if misclassified.
EXTRA_BLOCKED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT / Alibaba metadata 100.100.100.200
    ipaddress.ip_network("169.254.0.0/16"),     # Link-local / AWS/GCP/Azure metadata
    ipaddress.ip_network("192.0.0.0/24"),       # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),      # Benchmarking
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IPv6 IMDS
)

ALLOWED_CONTENT_TYPES = frozenset({
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/json",
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/atom+xml",
})

DEFAULT_ALLOWED_PORTS = frozenset({80, 443})

INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_previous_instructions", re.compile(r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|system)\s+instructions", re.I)),
    ("disregard_instructions", re.compile(r"disregard\s+(all\s+|any\s+|the\s+)?(previous|prior|system|developer)\s+(instructions|prompt|rules)", re.I)),
    ("reveal_secrets", re.compile(r"(reveal|print|expose|output|leak|dump)\s+(your\s+|the\s+)?(api\s*key|secret|password|token|system\s*prompt|env|credentials)", re.I)),
    ("override_system", re.compile(r"(new\s+system\s+instructions|override\s+(system|safety)|you\s+are\s+now\s+in\s+developer\s+mode)", re.I)),
    ("tool_hijack", re.compile(r"(call|invoke|execute|run)\s+(create_confirmed_|update_confirmed_|create_prospect|os\.system|subprocess)", re.I)),
    ("chat_delimiter", re.compile(r"(<\|im_start\|>|<\|system\|>|\[INST\]|</untrusted_source_data>)", re.I)),
)

SECRET_LOG_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-~+/=]{6,}", re.I), r"\1[REDACTED]"),
    (re.compile(r"(x-api-key['\":=\s]+)[A-Za-z0-9._\-]{6,}", re.I), r"\1[REDACTED]"),
    (re.compile(r"\b(sk-ant-[A-Za-z0-9\-_]{8,}|sk-proj-[A-Za-z0-9\-_]{8,}|sk-[A-Za-z0-9\-_]{16,}|gsk_[A-Za-z0-9]{8,})\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(mcp_at_|mcp_rt_|mcp_code_)[A-Za-z0-9\-_]{6,}\b"), r"\1[REDACTED]"),
    (re.compile(r"([?&](?:token|api_key|key|access_token|secret|password)=)[^&\s]+", re.I), r"\1[REDACTED]"),
    (re.compile(r'("(?:password|secret|api_key|access|access_token|refresh|refresh_token)"\s*:\s*")[^"]+(")', re.I), r"\1[REDACTED]\2"),
)


def validate_ip_address(ip_str: str) -> None:
    """Reject any non-public, loopback, private, link-local, metadata, or reserved IP."""
    cleaned = ip_str.strip().split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError as exc:
        raise SSRFBlockedError(f"Invalid IP address {ip_str!r}.") from exc

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        raise SSRFBlockedError(f"Blocked non-public IP address: {ip}.")

    for net in EXTRA_BLOCKED_NETWORKS:
        if ip in net:
            raise SSRFBlockedError(f"Blocked restricted network IP address: {ip}.")


def validate_url(
    url: str,
    *,
    require_https: bool = False,
    allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
) -> str:
    """Validate external URL syntax, scheme, port, and literal host/IP safety."""
    if not isinstance(url, str):
        raise SSRFBlockedError("URL must be a string.")
    raw = url.strip()
    if not raw or len(raw) > 2048:
        raise SSRFBlockedError("URL is empty or exceeds maximum length.")
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise SSRFBlockedError("URL contains unsafe whitespace or control characters.")

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if require_https:
        if scheme != "https":
            raise SSRFBlockedError(f"HTTPS is required; rejected scheme {scheme!r}.")
    elif scheme not in ("http", "https"):
        raise SSRFBlockedError(f"Disallowed URL scheme {scheme!r}; only http/https allowed.")

    if parsed.username or parsed.password:
        raise SSRFBlockedError("Credentials embedded in URLs are not permitted.")

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise SSRFBlockedError("URL has no hostname.")

    if hostname in BLOCKED_HOSTNAMES or any(hostname.endswith(sfx) for sfx in BLOCKED_SUFFIXES):
        raise SSRFBlockedError(f"Blocked internal/local hostname: {hostname!r}.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise SSRFBlockedError("URL contains an invalid port.") from exc
    if port is not None and port not in allowed_ports:
        raise SSRFBlockedError(f"Blocked non-standard port: {port}.")

    # Check literal IPs synchronously; domain names still require DNS validation at connect time.
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname:
            raise SSRFBlockedError(f"Single-label hostname {hostname!r} is not permitted.") from None
    else:
        validate_ip_address(hostname)

    return urlunparse((scheme, parsed.netloc, parsed.path or "/", parsed.params, parsed.query, ""))


async def resolve_and_validate_host(
    hostname: str,
    port: int,
    resolver: typing.Callable[[str, int], typing.Awaitable[list[str]]] | None = None,
) -> list[str]:
    """Resolve hostname via DNS and verify EVERY returned IP is globally routable."""
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        raise SSRFBlockedError("Empty hostname.")
    if host in BLOCKED_HOSTNAMES or any(host.endswith(sfx) for sfx in BLOCKED_SUFFIXES):
        raise SSRFBlockedError(f"Blocked hostname {host!r}.")

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        validate_ip_address(host)
        return [host]

    if resolver is not None:
        ips = await resolver(host, port)
    else:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise SSRFBlockedError(f"DNS resolution failed for {host!r}: {exc}") from exc
        ips = []
        for _fam, _type, _proto, _canon, sockaddr in infos:
            if sockaddr:
                ips.append(str(sockaddr[0]))

    unique_ips = list(dict.fromkeys(ips))
    if not unique_ips:
        raise SSRFBlockedError(f"DNS returned no addresses for {host!r}.")

    for ip in unique_ips:
        validate_ip_address(ip)
    return unique_ips


class SSRFGuardedNetworkBackend(AnyIOBackend):
    """httpcore network backend that resolves DNS and blocks private/metadata IPs at TCP connect time.

    Connecting directly to the validated IP (while httpcore passes the original hostname as
    `server_hostname` to `start_tls`) closes the TOCTOU window against DNS rebinding attacks.
    """

    def __init__(
        self,
        resolver: typing.Callable[[str, int], typing.Awaitable[list[str]]] | None = None,
    ) -> None:
        self._resolver = resolver

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        validated_ips = await resolve_and_validate_host(host, port, resolver=self._resolver)
        # Connect directly to the first validated public IP so DNS cannot rebind between check and connect.
        target_ip = validated_ips[0]
        return await super().connect_tcp(
            host=target_ip,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


def create_ssrf_guarded_transport(
    *,
    resolver: typing.Callable[[str, int], typing.Awaitable[list[str]]] | None = None,
    verify: bool = True,
) -> httpx.AsyncHTTPTransport:
    """Create an httpx AsyncHTTPTransport backed by SSRFGuardedNetworkBackend."""
    transport = httpx.AsyncHTTPTransport(verify=verify, http1=True, http2=False)
    pool = transport._pool
    if hasattr(pool, "_network_backend"):
        pool._network_backend = SSRFGuardedNetworkBackend(resolver=resolver)
    return transport


def validate_content_type(content_type: str | None) -> str:
    """Allow only safe textual/structured document content types."""
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime not in ALLOWED_CONTENT_TYPES:
        raise UnsafeContentError(f"Disallowed Content-Type {mime!r}.")
    return mime


def detect_prompt_injection(text: str) -> list[str]:
    """Identify prompt-injection patterns in untrusted source content (diagnostic flag, never executed)."""
    if not text:
        return []
    hits: list[str] = []
    for name, pat in INJECTION_PATTERNS:
        if pat.search(text):
            hits.append(name)
    return hits


def sanitize_untrusted_text(text: str, max_chars: int = 12_000) -> tuple[str, list[str]]:
    """Strip control/tag-escape sequences, flag injection patterns, and bound length."""
    if not text:
        return "", []
    # Remove null bytes and non-whitespace control characters plus zero-width obfuscation chars.
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\ufeff]", "", text)
    # Detect prompt injection BEFORE stripping XML breakout delimiters so warnings reflect original attack attempts.
    warnings = detect_prompt_injection(cleaned)
    # Neutralize XML-like fence breakout attempts.
    cleaned = re.sub(r"</?untrusted_source_data[^>]*>", "[TAG_STRIPPED]", cleaned, flags=re.I)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "\n[TRUNCATED]"
    return cleaned, warnings


def wrap_untrusted_source(source_id: str, url: str, title: str, text: str) -> str:
    """Wrap external source text inside explicit untrusted-data boundaries for LLM prompts."""
    safe_title = sanitize_untrusted_text(title or "", max_chars=200)[0]
    safe_text = sanitize_untrusted_text(text or "", max_chars=6000)[0]
    return (
        f'<untrusted_source_data id="{source_id}" url="{url}" title="{safe_title}">\n'
        f"{safe_text}\n"
        f"</untrusted_source_data>"
    )


def format_sources_for_prompt(pages: list[FetchedPage], max_total_chars: int = 12_000) -> str:
    """Pack sanitized excerpts from multiple pages within a strict character budget."""
    if not pages:
        return ""
    per_page_budget = max(800, max_total_chars // max(1, len(pages)))
    blocks: list[str] = []
    remaining = max_total_chars
    for idx, page in enumerate(pages, start=1):
        if remaining <= 120:
            break
        budget = min(per_page_budget, remaining)
        excerpt, _ = sanitize_untrusted_text(page.text, max_chars=budget)
        header = f"[SOURCE {idx}] URL: {page.final_url} | Type: {page.source_type.value} | Title: {page.title}"
        if page.publication_date:
            header += f" | Published: {page.publication_date}"
        block = f"{header}\n{wrap_untrusted_source(f'src-{idx}', page.final_url, page.title, excerpt)}"
        if len(block) > remaining:
            block = block[:remaining]
        blocks.append(block)
        remaining -= len(block) + 2
    return "\n\n".join(blocks)


def redact_secrets(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Scrub tokens, API keys, passwords, and configured secrets from log messages."""
    if not text:
        return text
    out = text
    for pat, repl in SECRET_LOG_PATTERNS:
        out = pat.sub(repl, out)
    for secret in extra_secrets:
        if secret and len(secret) >= 4 and secret in out:
            out = out.replace(secret, "[REDACTED]")
    return out


class SecretRedactingFilter(logging.Filter):
    """Logging filter that strips secrets before any record hits stderr."""

    def __init__(self, secrets_provider: typing.Callable[[], tuple[str, ...]] | None = None):
        super().__init__()
        self._secrets_provider = secrets_provider

    def filter(self, record: logging.LogRecord) -> bool:
        extra = self._secrets_provider() if self._secrets_provider else ()
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg, extra)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(v, extra) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a, extra) if isinstance(a, str) else a
                    for a in record.args
                )
        return True
