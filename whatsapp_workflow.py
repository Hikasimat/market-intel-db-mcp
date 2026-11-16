"""WhatsApp orchestration workflow: session connection, identity matching, history, search, and confirmed messaging."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol
from uuid import UUID

from pydantic import ValidationError

from api_client import ApiError, ApiTimeout, AuthError, Identity
from config import Settings
from confirmation_store import ConfirmationError, ConfirmationStore
from whatsapp_schemas import (
    WhatsAppChatHistoryInput,
    WhatsAppConnectInput,
    WhatsAppContactDetailsInput,
    WhatsAppListChatsInput,
    WhatsAppMarkReadInput,
    WhatsAppQRInput,
    WhatsAppSearchMessagesInput,
    WhatsAppSendMediaInput,
    WhatsAppSendMessageInput,
    decode_base64_media,
    decode_cursor,
    encode_cursor,
    format_validation_errors,
    is_lid_identifier,
    normalize_phone_digits,
    normalize_phone_number,
    phone_to_chat_id,
    phones_match,
)

log = logging.getLogger("prospect_mcp.whatsapp")

WHATSAPP_READ_ROLES = frozenset({"PRE", "LQ", "MANAGER", "SUPERUSER"})
WHATSAPP_MANAGE_ROLES = frozenset({"PRE", "LQ", "SUPERUSER"})

CONNECTED_STATUSES = frozenset({"READY", "WORKING", "ONLINE", "AUTHENTICATED"})
QR_REQUIRED_STATUSES = frozenset({"QR_REQUIRED", "SCAN_QR_CODE"})
STARTING_STATUSES = frozenset({"STARTING", "CONNECTING", "RESUMING"})
DISCONNECTED_STATUSES = frozenset({"DISCONNECTED", "STOPPED", "FAILED", "NOT_FOUND", "LOGGED_OUT", "UNPAIRED"})

BACKEND_MAX_MESSAGE_LIMIT = 5000


class WhatsAppApi(Protocol):
    identity: Identity | None
    async def authenticate_whatsapp(self, allowed_roles: tuple[str, ...] | frozenset[str] | None = None) -> Identity: ...
    async def get_whatsapp_status(self) -> Any: ...
    async def list_whatsapp_sessions(self) -> Any: ...
    async def start_whatsapp_session(self, session_name: str | None = None) -> Any: ...
    async def logout_whatsapp_session(self, session_name: str | None = None) -> Any: ...
    async def get_whatsapp_qr(self, force_restart: bool = False) -> Any: ...
    async def list_whatsapp_conversations(self) -> Any: ...
    async def get_whatsapp_conversation_messages(self, conversation_id: str, limit: int = 2000) -> Any: ...
    async def mark_whatsapp_conversation_read(self, conversation_id: str) -> Any: ...
    async def get_whatsapp_contact_info(self, chat_id: str) -> Any: ...
    async def list_whatsapp_crm_contacts(self, search: str | None = None) -> Any: ...
    async def send_whatsapp_message(self, payload: dict[str, Any]) -> Any: ...
    async def send_whatsapp_media(
        self, chat_id: str, filename: str, content: bytes, mime_type: str, caption: str = ""
    ) -> Any: ...


class SlidingWindowRateLimiter:
    """Thread-safe per-user sliding-window rate limiter."""

    def __init__(self, max_per_minute: int = 30, clock: Callable[[], float] = time.monotonic):
        self._max = max(1, int(max_per_minute))
        self._clock = clock
        self._buckets: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def check_and_record(self, user_key: str, action: str = "send") -> tuple[bool, float]:
        now = self._clock()
        cutoff = now - 60.0
        key = (user_key, action)
        with self._lock:
            q = self._buckets.setdefault(key, deque())
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self._max:
                retry_after = max(0.1, round(60.0 - (now - q[0]), 2))
                return False, retry_after
            q.append(now)
            return True, 0.0


class StagedMediaStore:
    """Ephemeral in-memory store for confirmed media bytes bound to user + token + SHA-256."""

    def __init__(self, ttl_seconds: int = 600, max_total_bytes: int = 100 * 1024 * 1024, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._max_total = max_total_bytes
        self._clock = clock
        self._items: dict[str, tuple[str, str, bytes, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _slot(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _purge_expired(self, now: float) -> None:
        self._items = {k: v for k, v in self._items.items() if v[3] > now}

    def put(self, token: str, identity_key: str, sha256_hex: str, content: bytes) -> None:
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            total = sum(len(v[2]) for v in self._items.values())
            if total + len(content) > self._max_total:
                # Evict oldest entries until space fits
                for k, _v in sorted(self._items.items(), key=lambda kv: kv[1][3]):
                    removed = self._items.pop(k)
                    total -= len(removed[2])
                    if total + len(content) <= self._max_total:
                        break
            self._items[self._slot(token)] = (identity_key, sha256_hex, content, now + self._ttl)

    def pop(self, token: str, identity_key: str, expected_sha256: str) -> bytes | None:
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            entry = self._items.pop(self._slot(token), None)
            if not entry:
                return None
            owner, sha_hex, content, expires_at = entry
            if expires_at <= now or owner != identity_key or not hmac.compare_digest(sha_hex, expected_sha256):
                return None
            return content


ALLOWED_QR_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
MAX_QR_IMAGE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class QRRecord:
    """In-memory ephemeral QR image bound to a single Market Intel user."""
    user_key: str
    username: str
    session_name: str
    image_bytes: bytes
    mime_type: str
    created_at: float
    expires_at: float


def parse_qr_image_data(qr_base64: str = "", qr_raw: str = "") -> tuple[bytes, str] | None:
    """Decode a base64 or data-URI QR image payload into validated raster bytes and MIME type."""
    candidate = (qr_base64 or "").strip()
    if not candidate and qr_raw and str(qr_raw).strip().startswith("data:image/"):
        candidate = str(qr_raw).strip()
    if not candidate:
        return None

    mime_type = "image/png"
    b64_part = candidate
    if candidate.startswith("data:"):
        if "," not in candidate:
            return None
        header, b64_part = candidate.split(",", 1)
        if ";base64" not in header.lower():
            return None
        raw_mime = header[5:].split(";", 1)[0].strip().lower()
        if raw_mime == "image/jpg":
            raw_mime = "image/jpeg"
        if raw_mime not in ALLOWED_QR_IMAGE_MIMES:
            return None
        mime_type = raw_mime

    compact = "".join(b64_part.split())
    if not compact:
        return None
    if len(compact) > ((MAX_QR_IMAGE_BYTES * 4) // 3) + 16:
        return None
    try:
        img_bytes = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not img_bytes or len(img_bytes) > MAX_QR_IMAGE_BYTES:
        return None
    return img_bytes, mime_type


class EphemeralQRStore:
    """Short-lived, token-addressed in-memory vault for WhatsApp pairing QR images."""

    def __init__(
        self,
        ttl_seconds: int = 120,
        max_items: int = 200,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._ttl = max(30, int(ttl_seconds))
        self._max_items = max(1, int(max_items))
        self._clock = clock
        self._items: dict[str, QRRecord] = {}
        self._lock = threading.Lock()

    def now(self) -> float:
        return self._clock()

    @staticmethod
    def _slot(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _purge_expired(self, now: float) -> None:
        self._items = {k: v for k, v in self._items.items() if v.expires_at > now}

    def put(
        self,
        user_key: str,
        username: str,
        session_name: str,
        image_bytes: bytes,
        mime_type: str = "image/png",
    ) -> tuple[str, int]:
        now = self._clock()
        token = secrets.token_urlsafe(32)
        rec = QRRecord(
            user_key=user_key,
            username=username,
            session_name=session_name,
            image_bytes=image_bytes,
            mime_type=mime_type,
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._lock:
            self._purge_expired(now)
            while len(self._items) >= self._max_items:
                oldest_key = min(self._items, key=lambda k: self._items[k].expires_at)
                self._items.pop(oldest_key, None)
            self._items[self._slot(token)] = rec
        return token, self._ttl

    def get(self, token: str) -> QRRecord | None:
        tok = (token or "").strip()
        if not (16 <= len(tok) <= 128):
            return None
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            rec = self._items.get(self._slot(tok))
            if rec is None or rec.expires_at <= now:
                return None
            return rec

    def purge_user(self, user_key: str) -> int:
        with self._lock:
            before = len(self._items)
            self._items = {k: v for k, v in self._items.items() if v.user_key != user_key}
            return before - len(self._items)


def _extract_status(status_data: Any) -> str:
    if not isinstance(status_data, dict):
        return "UNKNOWN"
    raw = (
        status_data.get("status")
        or (status_data.get("waha_status") or {}).get("status")
        or (status_data.get("session") or {}).get("status")
        or "UNKNOWN"
    )
    return str(raw).strip().upper() or "UNKNOWN"


def _extract_phone_from_status(status_data: Any) -> str | None:
    if not isinstance(status_data, dict):
        return None
    candidates: list[Any] = [
        status_data.get("phone_number"),
        (status_data.get("session") or {}).get("phone_number") if isinstance(status_data.get("session"), dict) else None,
        (status_data.get("me") or {}).get("phone") if isinstance(status_data.get("me"), dict) else None,
        (status_data.get("me") or {}).get("id") if isinstance(status_data.get("me"), dict) else None,
    ]
    waha = status_data.get("waha_status")
    if isinstance(waha, dict):
        me = waha.get("me")
        if isinstance(me, dict):
            candidates.extend([me.get("phone"), me.get("id")])
    for cand in candidates:
        norm = normalize_phone_number(str(cand)) if cand else None
        if norm:
            return norm
    return None


def _is_connected_status(status_str: str, payload: Any = None) -> bool:
    if isinstance(payload, dict) and payload.get("connected") is True:
        return True
    return status_str.upper() in CONNECTED_STATUSES


def _format_conversation(c: dict[str, Any]) -> dict[str, Any]:
    chat_id = str(c.get("chat_id") or "")
    raw_phone = c.get("phone_number") or c.get("prospect_phone")
    norm_phone = normalize_phone_number(str(raw_phone)) if raw_phone else None
    if not norm_phone and chat_id and not chat_id.endswith("@g.us") and not is_lid_identifier(chat_id):
        norm_phone = normalize_phone_number(chat_id)
    return {
        "conversation_id": str(c.get("id") or ""),
        "chat_id": chat_id,
        "contact_name": c.get("contact_name") or c.get("whatsapp_name") or norm_phone or "WhatsApp Contact",
        "whatsapp_name": c.get("whatsapp_name") or "",
        "phone_number": norm_phone,
        "is_group": bool(c.get("is_group")) or chat_id.endswith("@g.us"),
        "is_matched": bool(c.get("is_matched")),
        "prospect_id": c.get("prospect_id"),
        "prospect_name": c.get("prospect_name"),
        "prospect_contact_id": str(c["prospect_contact"]) if c.get("prospect_contact") else None,
        "unread_count": int(c.get("unread_count") or 0),
        "last_message_at": c.get("last_message_at"),
        "last_message_preview": c.get("last_message_preview"),
        "last_message_from_me": c.get("last_message_from_me"),
    }


def _format_message(m: dict[str, Any]) -> dict[str, Any]:
    eff_ts = m.get("waha_timestamp") or m.get("sent_at") or m.get("created_at") or ""
    body = m.get("full_body")
    if body is None or body == "":
        body = m.get("body_preview") or ""
    return {
        "id": str(m.get("id") or ""),
        "waha_message_id": m.get("waha_message_id") or "",
        "direction": m.get("direction") or "",
        "status": m.get("status") or "",
        "body": body,
        "body_preview": m.get("body_preview") or "",
        "message_type": m.get("message_type") or "chat",
        "performed_by_name": m.get("performed_by_name"),
        "timestamp": eff_ts,
        "waha_timestamp": m.get("waha_timestamp"),
        "sent_at": m.get("sent_at"),
        "delivered_at": m.get("delivered_at"),
        "read_at": m.get("read_at"),
        "created_at": m.get("created_at"),
        "has_media": bool(m.get("has_media")),
        "media_mime_type": m.get("media_mime_type"),
        "media_url": m.get("media_url"),
        "media_file": m.get("media_file"),
        "is_edited": bool(m.get("is_edited")),
        "reply_context": m.get("reply_context"),
    }


class WhatsAppWorkflow:
    """User-isolated WhatsApp orchestration delegating exclusively to Market Intel DB APIs."""

    def __init__(
        self,
        client: WhatsAppApi,
        store: ConfirmationStore,
        settings: Settings,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        media_store: StagedMediaStore | None = None,
        sleep_fn: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        qr_store: EphemeralQRStore | None = None,
        qr_public_base_url: str | None = None,
    ):
        self.client = client
        self.store = store
        self.settings = settings
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter(settings.whatsapp_rate_limit_per_minute, clock)
        self.media_store = media_store or StagedMediaStore(settings.confirmation_ttl_seconds, clock=clock)
        self.qr_store = qr_store or EphemeralQRStore(clock=clock)
        raw_base = qr_public_base_url if qr_public_base_url is not None else settings.oauth_issuer_url
        self.qr_public_base_url = raw_base.strip().rstrip("/") if raw_base else None
        self._sleep = sleep_fn
        self._clock = clock

    def _attach_qr_output(
        self,
        out: dict[str, Any],
        ident: Identity,
        session_name: str,
        qr_base64: str,
        qr_raw: str,
    ) -> bool:
        """Attach a short-lived hosted QR link when public URL is available, else fallback to raw QR fields."""
        if self.qr_public_base_url:
            parsed = parse_qr_image_data(qr_base64, qr_raw)
            if parsed is not None:
                img_bytes, mime_type = parsed
                token, ttl = self.qr_store.put(
                    user_key=ident.key,
                    username=ident.username,
                    session_name=session_name,
                    image_bytes=img_bytes,
                    mime_type=mime_type,
                )
                qr_url = f"{self.qr_public_base_url}/whatsapp/qr/{token}"
                out["qr_url"] = qr_url
                out["qr_expires_in_seconds"] = ttl
                out["qr_omitted"] = True
                return True
        if qr_base64:
            out["qr_base64"] = qr_base64
        if qr_raw and qr_raw != qr_base64:
            out["qr"] = qr_raw
        return False

    async def _identity(self, *, require_manage: bool = False, refresh_from_api: bool = False) -> Identity:
        allowed = WHATSAPP_MANAGE_ROLES if require_manage else WHATSAPP_READ_ROLES
        if refresh_from_api or self.client.identity is None or not self.client.identity.user_id:
            ident = await self.client.authenticate_whatsapp(allowed_roles=allowed)
        else:
            ident = self.client.identity
            role = (ident.role or "").strip().upper()
            if role not in allowed and not (ident.is_superuser and "SUPERUSER" in allowed):
                raise AuthError(
                    f"Account role is {role or 'unknown'!r}; not permitted for this WhatsApp operation."
                )
        if not ident.user_id:
            raise AuthError("Authenticated Market Intel user ID could not be resolved.")
        return ident

    def _verify_session_ownership(
        self,
        ident: Identity,
        status_data: Any,
        sessions_list: Any = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Verify that returned session data belongs strictly to `ident` and extract normalized phone."""
        session_obj: dict[str, Any] = {}
        if isinstance(status_data, dict) and isinstance(status_data.get("session"), dict):
            session_obj = status_data["session"]
        elif isinstance(sessions_list, list) and sessions_list:
            first = sessions_list[0]
            if isinstance(first, dict):
                session_obj = first

        owner_username = session_obj.get("owner_username")
        if owner_username and ident.username and str(owner_username).strip() != ident.username.strip():
            raise AuthError("WhatsApp session ownership mismatch: session belongs to a different user.")

        session_name = session_obj.get("session_name") or (
            status_data.get("session_name") if isinstance(status_data, dict) else None
        )
        if ident.whatsapp_sessions and session_name:
            owned_names = {s.get("session_name") for s in ident.whatsapp_sessions if isinstance(s, dict)}
            if owned_names and session_name not in owned_names:
                # Re-check against sessions_list if provided (newly created session may not be in cached /auth/me/)
                listed_names = (
                    {s.get("session_name") for s in sessions_list if isinstance(s, dict)}
                    if isinstance(sessions_list, list)
                    else set()
                )
                if session_name not in listed_names:
                    raise AuthError("WhatsApp session is not owned by the authenticated Market Intel user.")

        phone = _extract_phone_from_status(status_data)
        if not phone and session_obj.get("phone_number"):
            phone = normalize_phone_number(str(session_obj["phone_number"]))
        return session_obj, phone

    async def _get_verified_session_state(
        self,
        ident: Identity,
        *,
        sync_sessions: bool = True,
    ) -> dict[str, Any]:
        status_data = await self.client.get_whatsapp_status()
        sessions_list = await self.client.list_whatsapp_sessions() if sync_sessions else None
        session_obj, phone = self._verify_session_ownership(ident, status_data, sessions_list)

        # Prefer reconciled phone from synced session list if available
        if isinstance(sessions_list, list) and sessions_list and isinstance(sessions_list[0], dict):
            synced_sess = sessions_list[0]
            synced_owner = synced_sess.get("owner_username")
            if synced_owner and ident.username and str(synced_owner).strip() != ident.username.strip():
                raise AuthError("Synced WhatsApp session belongs to a different user.")
            if synced_sess.get("phone_number"):
                synced_phone = normalize_phone_number(str(synced_sess["phone_number"]))
                if synced_phone:
                    phone = synced_phone
            if not session_obj:
                session_obj = synced_sess

        status_str = _extract_status(status_data)
        if status_str == "UNKNOWN" and session_obj.get("status"):
            status_str = str(session_obj["status"]).strip().upper()
        connected = _is_connected_status(status_str, status_data)
        if connected and not phone and session_obj.get("phone_number"):
            phone = normalize_phone_number(str(session_obj["phone_number"]))

        config = status_data.get("config") if isinstance(status_data, dict) and isinstance(status_data.get("config"), dict) else {}
        return {
            "status": status_str,
            "connected": connected,
            "session_name": session_obj.get("session_name") or "",
            "phone_number": phone,
            "owner_username": session_obj.get("owner_username") or ident.username,
            "outbound_enabled": config.get("outbound_enabled", True),
            "safety_stop_active": bool(config.get("safety_stop_active", False)),
            "historical_sessions_count": len(status_data.get("historical_sessions") or []) if isinstance(status_data, dict) else 0,
            "identity_matched": bool(connected and phone),
        }

    async def _list_owned_conversations(
        self,
        ident: Identity,
        session_phone: str | None = None,
    ) -> list[dict[str, Any]]:
        raw = await self.client.list_whatsapp_conversations()
        if isinstance(raw, dict) and "results" in raw:
            raw = raw["results"]
        if not isinstance(raw, list):
            return []
        owned: list[dict[str, Any]] = []
        for conv in raw:
            if not isinstance(conv, dict):
                continue
            acct_phone = conv.get("account_phone")
            # Defense-in-depth: if both session_phone and conversation account_phone are present,
            # ensure they match (normalized) so another WhatsApp account's chats are never surfaced.
            if session_phone and acct_phone and not phones_match(session_phone, str(acct_phone)):
                continue
            owned.append(conv)
        return owned

    async def _resolve_owned_conversation(
        self,
        ident: Identity,
        *,
        conversation_id: str | None = None,
        chat_id: str | None = None,
        phone_number: str | None = None,
        session_phone: str | None = None,
    ) -> dict[str, Any] | None:
        convs = await self._list_owned_conversations(ident, session_phone=session_phone)
        if conversation_id:
            target_id = str(conversation_id)
            for c in convs:
                if str(c.get("id")) == target_id:
                    return c
            return None
        if chat_id:
            norm_chat = chat_id.strip()
            alt_chat = None
            if norm_chat.endswith("@c.us"):
                alt_chat = norm_chat.replace("@c.us", "@s.whatsapp.net")
            elif norm_chat.endswith("@s.whatsapp.net"):
                alt_chat = norm_chat.replace("@s.whatsapp.net", "@c.us")
            for c in convs:
                cid = str(c.get("chat_id") or "")
                if cid == norm_chat or (alt_chat and cid == alt_chat):
                    return c
            # Fallback to normalized phone match within this user's own conversations ONLY
            chat_phone = normalize_phone_number(norm_chat)
            if chat_phone:
                for c in convs:
                    if phones_match(chat_phone, c.get("phone_number") or c.get("chat_id")):
                        return c
        if phone_number:
            for c in convs:
                if phones_match(phone_number, c.get("phone_number") or c.get("prospect_phone") or c.get("chat_id")):
                    return c
        return None

    # ------------------------------------------------------------------
    # Connection tools
    # ------------------------------------------------------------------

    async def connect_whatsapp(
        self,
        wait_for_connection: bool = False,
        timeout_seconds: int = 30,
        force_restart: bool = False,
    ) -> dict[str, Any]:
        try:
            params = WhatsAppConnectInput.model_validate({
                "wait_for_connection": wait_for_connection,
                "timeout_seconds": timeout_seconds,
                "force_restart": force_restart,
            })
        except ValidationError as exc:
            return {"ok": False, "connected": False, "errors": format_validation_errors(exc)}

        try:
            ident = await self._identity(require_manage=True, refresh_from_api=True)
            allowed, retry_after = self.rate_limiter.check_and_record(ident.key, "connect")
            if not allowed:
                return {
                    "ok": False,
                    "connected": False,
                    "errors": [f"Rate limit exceeded for WhatsApp connection requests. Retry in {retry_after}s."],
                }

            state = await self._get_verified_session_state(ident, sync_sessions=True)
            if state["connected"] and not params.force_restart:
                self.qr_store.purge_user(ident.key)
                return {
                    "ok": True,
                    "connected": True,
                    "status": state["status"],
                    "user_id": ident.user_id,
                    "username": ident.username,
                    "role": ident.role,
                    "session_name": state["session_name"],
                    "phone_number": state["phone_number"],
                    "identity_matched": state["identity_matched"],
                    "message": "WhatsApp is already connected and bound to your Market Intel account.",
                }

            if not params.force_restart:
                await self.client.start_whatsapp_session(state["session_name"] or None)

            qr_resp = await self.client.get_whatsapp_qr(force_restart=params.force_restart)
            qr_status = _extract_status(qr_resp)
            if _is_connected_status(qr_status, qr_resp):
                self.qr_store.purge_user(ident.key)
                synced = await self._get_verified_session_state(ident, sync_sessions=True)
                phone = synced["phone_number"] or _extract_phone_from_status(qr_resp)
                return {
                    "ok": True,
                    "connected": True,
                    "status": synced["status"],
                    "user_id": ident.user_id,
                    "username": ident.username,
                    "role": ident.role,
                    "session_name": synced["session_name"],
                    "phone_number": phone,
                    "identity_matched": bool(phone),
                    "message": "WhatsApp connected and matched to your Market Intel user.",
                }

            deadline = self._clock() + params.timeout_seconds
            last_qr = qr_resp if isinstance(qr_resp, dict) else {}

            if params.wait_for_connection:
                poll_interval = max(0.05, float(self.settings.whatsapp_connect_poll_interval))
                while self._clock() < deadline:
                    await self._sleep(min(poll_interval, max(0.05, deadline - self._clock())))
                    polled = await self._get_verified_session_state(ident, sync_sessions=True)
                    if polled["connected"]:
                        self.qr_store.purge_user(ident.key)
                        return {
                            "ok": True,
                            "connected": True,
                            "status": polled["status"],
                            "user_id": ident.user_id,
                            "username": ident.username,
                            "role": ident.role,
                            "session_name": polled["session_name"],
                            "phone_number": polled["phone_number"],
                            "identity_matched": polled["identity_matched"],
                            "message": "WhatsApp authenticated and bound to your Market Intel account.",
                        }
                    qr_poll = await self.client.get_whatsapp_qr(force_restart=False)
                    if isinstance(qr_poll, dict):
                        last_qr = qr_poll
                        if _is_connected_status(_extract_status(qr_poll), qr_poll):
                            self.qr_store.purge_user(ident.key)
                            synced = await self._get_verified_session_state(ident, sync_sessions=True)
                            phone = synced["phone_number"] or _extract_phone_from_status(qr_poll)
                            return {
                                "ok": True,
                                "connected": True,
                                "status": synced["status"],
                                "user_id": ident.user_id,
                                "username": ident.username,
                                "role": ident.role,
                                "session_name": synced["session_name"],
                                "phone_number": phone,
                                "identity_matched": bool(phone),
                                "message": "WhatsApp authenticated and bound to your Market Intel account.",
                            }

            cur_status = _extract_status(last_qr)
            qr_base64 = last_qr.get("qrBase64") or ""
            qr_raw = last_qr.get("qr") or last_qr.get("value") or ""
            eff_session = last_qr.get("session_name") or state["session_name"]
            resp_out: dict[str, Any] = {
                "ok": True,
                "connected": False,
                "timed_out": bool(params.wait_for_connection),
                "status": cur_status,
                "user_id": ident.user_id,
                "username": ident.username,
                "session_name": eff_session,
            }
            if qr_base64 or qr_raw:
                resp_out["qr_required"] = True
                hosted = self._attach_qr_output(resp_out, ident, eff_session, qr_base64, qr_raw)
                if hosted:
                    resp_out["next_step"] = (
                        f"Give the user this link to view and scan the QR code (expires in {resp_out['qr_expires_in_seconds']}s): "
                        f"{resp_out['qr_url']} . Do NOT paste base64 image data into chat. "
                        "Once the user confirms they scanned it, call get_whatsapp_connection_status "
                        "or connect_whatsapp(wait_for_connection=true)."
                    )
                else:
                    resp_out["next_step"] = (
                        "Scan this QR code in WhatsApp (Settings -> Linked Devices -> Link a Device) "
                        "or on your Market Intel WhatsApp page, then call get_whatsapp_connection_status "
                        "or connect_whatsapp(wait_for_connection=true)."
                    )
            else:
                resp_out["qr_required"] = cur_status in QR_REQUIRED_STATUSES
                resp_out["next_step"] = (
                    "WhatsApp session is initializing. Call get_whatsapp_qr_code or "
                    "get_whatsapp_connection_status shortly."
                )
            return resp_out

        except asyncio.CancelledError:
            log.info("connect_whatsapp cancelled (client disconnected).")
            raise
        except AuthError as exc:
            return {"ok": False, "connected": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"ok": False, "connected": False, "errors": [f"WhatsApp connection error: {exc}"]}

    async def get_whatsapp_connection_status(self) -> dict[str, Any]:
        try:
            ident = await self._identity(require_manage=False, refresh_from_api=True)
            state = await self._get_verified_session_state(ident, sync_sessions=True)
            if state["connected"]:
                self.qr_store.purge_user(ident.key)
            return {
                "ok": True,
                "user_id": ident.user_id,
                "username": ident.username,
                "role": ident.role,
                "identity_verified": True,
                **state,
            }
        except AuthError as exc:
            return {"ok": False, "connected": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"ok": False, "connected": False, "errors": [f"Failed to retrieve WhatsApp status: {exc}"]}

    async def get_whatsapp_qr_code(self, force_restart: bool = False) -> dict[str, Any]:
        try:
            params = WhatsAppQRInput.model_validate({"force_restart": force_restart})
        except ValidationError as exc:
            return {"ok": False, "errors": format_validation_errors(exc)}

        try:
            ident = await self._identity(require_manage=True, refresh_from_api=True)
            allowed, retry_after = self.rate_limiter.check_and_record(ident.key, "connect")
            if not allowed:
                return {"ok": False, "errors": [f"Rate limit exceeded for QR retrieval. Retry in {retry_after}s."]}

            qr_data = await self.client.get_whatsapp_qr(force_restart=params.force_restart)
            if not isinstance(qr_data, dict):
                return {"ok": False, "errors": ["Unexpected QR response format from Market Intel API."]}

            qr_status = _extract_status(qr_data)
            if _is_connected_status(qr_status, qr_data):
                self.qr_store.purge_user(ident.key)
                state = await self._get_verified_session_state(ident, sync_sessions=True)
                phone = state["phone_number"] or _extract_phone_from_status(qr_data)
                return {
                    "ok": True,
                    "connected": True,
                    "status": state["status"],
                    "session_name": state["session_name"],
                    "phone_number": phone,
                    "user_id": ident.user_id,
                    "username": ident.username,
                    "message": "WhatsApp is already connected; no QR code required.",
                }

            eff_session = qr_data.get("session_name") or ""
            out: dict[str, Any] = {
                "ok": True,
                "connected": False,
                "status": qr_status,
                "session_name": eff_session,
                "user_id": ident.user_id,
                "username": ident.username,
            }
            qr_base64 = qr_data.get("qrBase64") or ""
            qr_raw = qr_data.get("qr") or qr_data.get("value") or ""
            if qr_base64 or qr_raw:
                hosted = self._attach_qr_output(out, ident, eff_session, qr_base64, qr_raw)
                if hosted:
                    out["next_step"] = (
                        f"Give the user this link to view and scan the QR code (expires in {out['qr_expires_in_seconds']}s): "
                        f"{out['qr_url']} . Do NOT paste base64 image data into chat."
                    )
            if qr_data.get("message"):
                out["message"] = qr_data["message"]
            return out
        except AuthError as exc:
            return {"ok": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"ok": False, "errors": [f"Failed to retrieve WhatsApp QR code: {exc}"]}

    async def disconnect_whatsapp(
        self,
        confirmation_token: str | None = None,
        user_has_explicitly_confirmed: bool = False,
    ) -> dict[str, Any]:
        try:
            ident = await self._identity(require_manage=True, refresh_from_api=True)
        except AuthError as exc:
            return {"disconnected": False, "errors": [f"Authentication/permission error: {exc}"]}

        if not confirmation_token or user_has_explicitly_confirmed is not True:
            try:
                state = await self._get_verified_session_state(ident, sync_sessions=False)
            except ApiError as exc:
                return {"disconnected": False, "errors": [f"Could not inspect WhatsApp session: {exc}"]}

            if not state["connected"] and state["status"] in ("DISCONNECTED", "STOPPED"):
                return {
                    "disconnected": False,
                    "already_disconnected": True,
                    "status": state["status"],
                    "session_name": state["session_name"],
                    "message": "WhatsApp session is already disconnected.",
                }

            payload = {
                "action": "logout",
                "user_id": ident.user_id,
                "session_name": state["session_name"],
                "phone_number": state["phone_number"],
            }
            token, ttl = self.store.issue(
                ident.key,
                payload,
                op_type="whatsapp_disconnect",
                entity_type="whatsapp_session",
                record_id=state["session_name"] or None,
            )
            return {
                "disconnected": False,
                "requires_confirmation": True,
                "summary": (
                    f"Disconnect WhatsApp session '{state['session_name']}' "
                    f"({state['phone_number'] or 'unpaired'}) for user '{ident.username}'. "
                    "Persisted conversation history in Market Intel DB will be preserved, "
                    "but reconnecting will require scanning a QR code again."
                ),
                "confirmation_token": token,
                "token_expires_in_seconds": ttl,
                "next_step": (
                    "Ask the user to explicitly confirm disconnecting their WhatsApp session. "
                    "Then call disconnect_whatsapp with this confirmation_token and "
                    "user_has_explicitly_confirmed=true."
                ),
            }

        try:
            op = self.store.consume(confirmation_token, ident.key)
        except ConfirmationError as exc:
            return {"disconnected": False, "errors": [str(exc)]}

        if op.op_type != "whatsapp_disconnect" or op.entity_type != "whatsapp_session":
            return {"disconnected": False, "errors": ["Confirmation token is not for WhatsApp disconnection."]}

        session_name = op.payload.get("session_name") or None
        try:
            res = await self.client.logout_whatsapp_session(session_name)
            self.qr_store.purge_user(ident.key)
            self.store.mark(op, "UPDATED")
            return {
                "disconnected": True,
                "status": "DISCONNECTED",
                "session_name": session_name or "",
                "history_preserved": True,
                "detail": res.get("detail") if isinstance(res, dict) else "WhatsApp session disconnected.",
            }
        except ApiError as exc:
            if exc.status is None:
                self.store.mark(op, "UNCERTAIN")
                return {
                    "disconnected": False,
                    "outcome": "UNCERTAIN",
                    "errors": [f"{exc} Check get_whatsapp_connection_status before retrying."],
                }
            return {"disconnected": False, "errors": [f"Failed to disconnect WhatsApp: {exc}"]}

    # ------------------------------------------------------------------
    # Conversation tools
    # ------------------------------------------------------------------

    async def list_whatsapp_chats(
        self,
        search: str | None = None,
        is_group: bool | None = None,
        matched_only: bool = False,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        try:
            params = WhatsAppListChatsInput.model_validate({
                "search": search,
                "is_group": is_group,
                "matched_only": matched_only,
                "limit": limit,
                "cursor": cursor,
            })
            offset = decode_cursor(params.cursor, expected_scope="chats")
        except (ValidationError, ValueError) as exc:
            errs = format_validation_errors(exc) if isinstance(exc, ValidationError) else [str(exc)]
            return {"ok": False, "errors": errs}

        try:
            ident = await self._identity(require_manage=False)
            state = await self._get_verified_session_state(ident, sync_sessions=False)
            convs = await self._list_owned_conversations(ident, session_phone=state["phone_number"])
        except AuthError as exc:
            return {"ok": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"ok": False, "errors": [f"Failed to list WhatsApp chats: {exc}"]}

        formatted = [_format_conversation(c) for c in convs]
        if params.is_group is not None:
            formatted = [c for c in formatted if c["is_group"] is params.is_group]
        if params.matched_only:
            formatted = [c for c in formatted if c["is_matched"]]
        if params.search:
            q = params.search.casefold()
            q_digits = normalize_phone_digits(params.search)
            filtered = []
            for c in formatted:
                hay = " ".join(
                    str(c.get(k) or "")
                    for k in ("contact_name", "whatsapp_name", "phone_number", "prospect_name", "chat_id")
                ).casefold()
                if q in hay or (q_digits and c.get("phone_number") and q_digits in normalize_phone_digits(c["phone_number"])):
                    filtered.append(c)
            formatted = filtered

        total = len(formatted)
        page = formatted[offset : offset + params.limit]
        next_offset = offset + len(page)
        has_more = next_offset < total
        return {
            "ok": True,
            "user_id": ident.user_id,
            "session_phone_number": state["phone_number"],
            "chats": page,
            "pagination": {
                "limit": params.limit,
                "returned": len(page),
                "total_available": total,
                "has_more": has_more,
                "next_cursor": encode_cursor(next_offset, scope="chats") if has_more else None,
            },
        }

    async def get_whatsapp_chat_history(
        self,
        conversation_id: str | None = None,
        chat_id: str | None = None,
        phone_number: str | None = None,
        page_size: int = 50,
        cursor: str | None = None,
        order: str = "asc",
    ) -> dict[str, Any]:
        try:
            params = WhatsAppChatHistoryInput.model_validate({
                "conversation_id": conversation_id,
                "chat_id": chat_id,
                "phone_number": phone_number,
                "page_size": page_size,
                "cursor": cursor,
                "order": order,
            })
        except ValidationError as exc:
            return {"ok": False, "errors": format_validation_errors(exc)}

        try:
            ident = await self._identity(require_manage=False)
            state = await self._get_verified_session_state(ident, sync_sessions=False)

            resolved_conv_id = str(params.conversation_id) if params.conversation_id else None
            if not resolved_conv_id:
                conv_match = await self._resolve_owned_conversation(
                    ident,
                    chat_id=params.chat_id,
                    phone_number=params.phone_number,
                    session_phone=state["phone_number"],
                )
                if not conv_match:
                    return {
                        "ok": False,
                        "errors": ["Conversation not found among your owned WhatsApp chats."],
                    }
                resolved_conv_id = str(conv_match["id"])

            offset = decode_cursor(params.cursor, expected_order=params.order, expected_scope=resolved_conv_id)

            # Request bounded window from Django DB (newest first on backend).
            fetch_limit = (
                BACKEND_MAX_MESSAGE_LIMIT
                if params.order == "asc"
                else min(max(offset + params.page_size, 100), BACKEND_MAX_MESSAGE_LIMIT)
            )
            data = await self.client.get_whatsapp_conversation_messages(resolved_conv_id, limit=fetch_limit)
        except ValueError as exc:
            return {"ok": False, "errors": [str(exc)]}
        except AuthError as exc:
            return {"ok": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"ok": False, "errors": [f"Failed to retrieve chat history: {exc}"]}

        if not isinstance(data, dict):
            return {"ok": False, "errors": ["Unexpected conversation history response from Market Intel API."]}

        conv_raw = data.get("conversation") if isinstance(data.get("conversation"), dict) else {}
        acct_phone = conv_raw.get("account_phone")
        if state["phone_number"] and acct_phone and not phones_match(state["phone_number"], str(acct_phone)):
            return {"ok": False, "errors": ["Conversation belongs to a different WhatsApp account."]}

        raw_msgs = data.get("messages") if isinstance(data.get("messages"), list) else []
        formatted_msgs = [_format_message(m) for m in raw_msgs if isinstance(m, dict)]

        # Deterministic sort by effective timestamp + id
        formatted_msgs.sort(
            key=lambda m: (str(m.get("timestamp") or ""), str(m.get("created_at") or ""), str(m.get("id") or "")),
            reverse=(params.order == "desc"),
        )

        total = len(formatted_msgs)
        page = formatted_msgs[offset : offset + params.page_size]
        next_offset = offset + len(page)
        has_more = next_offset < total

        return {
            "ok": True,
            "conversation": _format_conversation(conv_raw) if conv_raw else {"conversation_id": resolved_conv_id},
            "messages": page,
            "pagination": {
                "page_size": params.page_size,
                "returned": len(page),
                "total_available": total,
                "order": params.order,
                "has_more": has_more,
                "next_cursor": (
                    encode_cursor(next_offset, order=params.order, scope=resolved_conv_id) if has_more else None
                ),
            },
        }

    async def get_whatsapp_contact_details(
        self,
        chat_id: str | None = None,
        phone_number: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            params = WhatsAppContactDetailsInput.model_validate({
                "chat_id": chat_id,
                "phone_number": phone_number,
                "conversation_id": conversation_id,
            })
        except ValidationError as exc:
            return {"ok": False, "errors": format_validation_errors(exc)}

        try:
            ident = await self._identity(require_manage=False)
            state = await self._get_verified_session_state(ident, sync_sessions=False)

            target_chat_id = params.chat_id
            if not target_chat_id and params.conversation_id:
                conv = await self._resolve_owned_conversation(
                    ident,
                    conversation_id=str(params.conversation_id),
                    session_phone=state["phone_number"],
                )
                if not conv:
                    return {"ok": False, "errors": ["Conversation not found among your owned WhatsApp chats."]}
                target_chat_id = str(conv["chat_id"])
            elif not target_chat_id and params.phone_number:
                conv = await self._resolve_owned_conversation(
                    ident,
                    phone_number=params.phone_number,
                    session_phone=state["phone_number"],
                )
                target_chat_id = str(conv["chat_id"]) if conv else phone_to_chat_id(params.phone_number)

            info = await self.client.get_whatsapp_contact_info(target_chat_id)
            if not isinstance(info, dict):
                return {"ok": False, "errors": ["Unexpected contact details response from Market Intel API."]}

            return {
                "ok": True,
                "chat_id": info.get("chatId") or target_chat_id,
                "phone_number": normalize_phone_number(info.get("phoneNumber")) or info.get("phoneNumber") or "",
                "name": info.get("name") or "",
                "contact_name": info.get("contact_name") or "",
                "company": info.get("company") or "",
                "designation": info.get("designation") or "",
                "official_email": info.get("official_email") or "",
                "is_matched": bool(info.get("is_matched")),
                "prospect_id": info.get("prospect_id"),
                "prospect_contact_id": info.get("prospect_contact_id"),
                "is_group": bool(info.get("is_group")),
                "members": info.get("members") if isinstance(info.get("members"), list) else [],
            }
        except ValueError as exc:
            return {"ok": False, "errors": [str(exc)]}
        except AuthError as exc:
            return {"ok": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"ok": False, "errors": [f"Failed to retrieve contact details: {exc}"]}

    async def search_whatsapp_messages(
        self,
        query: str,
        conversation_id: str | None = None,
        chat_id: str | None = None,
        direction: str | None = None,
        has_media: bool | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        try:
            params = WhatsAppSearchMessagesInput.model_validate({
                "query": query,
                "conversation_id": conversation_id,
                "chat_id": chat_id,
                "direction": direction,
                "has_media": has_media,
                "limit": limit,
                "cursor": cursor,
            })
            scope_key = f"search:{params.query.casefold()}:{params.conversation_id or ''}:{params.chat_id or ''}"
            offset = decode_cursor(params.cursor, expected_scope=scope_key)
        except (ValidationError, ValueError) as exc:
            errs = format_validation_errors(exc) if isinstance(exc, ValidationError) else [str(exc)]
            return {"ok": False, "errors": errs}

        try:
            ident = await self._identity(require_manage=False)
            state = await self._get_verified_session_state(ident, sync_sessions=False)

            if params.conversation_id or params.chat_id:
                conv = await self._resolve_owned_conversation(
                    ident,
                    conversation_id=str(params.conversation_id) if params.conversation_id else None,
                    chat_id=params.chat_id,
                    session_phone=state["phone_number"],
                )
                if not conv:
                    return {"ok": False, "errors": ["Target conversation not found among your owned WhatsApp chats."]}
                target_convs = [conv]
            else:
                all_convs = await self._list_owned_conversations(ident, session_phone=state["phone_number"])
                target_convs = all_convs[: self.settings.whatsapp_max_search_conversations]

            q_lower = params.query.casefold()
            matches: list[dict[str, Any]] = []

            for conv in target_convs:
                cid = str(conv.get("id") or "")
                if not cid:
                    continue
                hist = await self.client.get_whatsapp_conversation_messages(cid, limit=1000)
                if not isinstance(hist, dict):
                    continue
                conv_summary = _format_conversation(
                    hist.get("conversation") if isinstance(hist.get("conversation"), dict) else conv
                )
                msgs = hist.get("messages") if isinstance(hist.get("messages"), list) else []
                for m in msgs:
                    if not isinstance(m, dict):
                        continue
                    fm = _format_message(m)
                    if params.direction and fm["direction"] != params.direction:
                        continue
                    if params.has_media is not None and fm["has_media"] is not params.has_media:
                        continue
                    hay = f"{fm['body']} {fm['body_preview']}".casefold()
                    if q_lower in hay:
                        matches.append({
                            "conversation_id": conv_summary["conversation_id"],
                            "chat_id": conv_summary["chat_id"],
                            "contact_name": conv_summary["contact_name"],
                            "phone_number": conv_summary["phone_number"],
                            "is_group": conv_summary["is_group"],
                            "message": fm,
                        })

            matches.sort(key=lambda item: str(item["message"].get("timestamp") or ""), reverse=True)
            total = len(matches)
            page = matches[offset : offset + params.limit]
            next_offset = offset + len(page)
            has_more = next_offset < total

            return {
                "ok": True,
                "query": params.query,
                "conversations_scanned": len(target_convs),
                "matches": page,
                "pagination": {
                    "limit": params.limit,
                    "returned": len(page),
                    "total_available": total,
                    "has_more": has_more,
                    "next_cursor": encode_cursor(next_offset, scope=scope_key) if has_more else None,
                },
            }
        except AuthError as exc:
            return {"ok": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"ok": False, "errors": [f"Message search failed: {exc}"]}

    # ------------------------------------------------------------------
    # Messaging tools
    # ------------------------------------------------------------------

    async def send_whatsapp_message(
        self,
        recipient: str | None = None,
        message_body: str = "",
        chat_id: str | None = None,
        prospect_contact_id: str | None = None,
        reply_to: str | None = None,
        confirmation_token: str | None = None,
        user_has_explicitly_confirmed: bool = False,
    ) -> dict[str, Any]:
        # Phase 2: Confirmed execution
        if confirmation_token and user_has_explicitly_confirmed is True:
            try:
                ident = await self._identity(require_manage=True, refresh_from_api=True)
                allowed, retry_after = self.rate_limiter.check_and_record(ident.key, "send")
                if not allowed:
                    return {
                        "sent": False,
                        "outcome": "RATE_LIMITED",
                        "errors": [f"Rate limit exceeded for outbound WhatsApp messages. Retry in {retry_after}s."],
                    }
                op = self.store.consume(confirmation_token, ident.key)
            except ConfirmationError as exc:
                return {"sent": False, "outcome": "NOT_SENT", "errors": [str(exc)]}
            except AuthError as exc:
                return {"sent": False, "outcome": "NOT_SENT", "errors": [f"Authentication/permission error: {exc}"]}

            if op.op_type != "whatsapp_send" or op.entity_type != "whatsapp_message":
                return {"sent": False, "outcome": "NOT_SENT", "errors": ["Confirmation token is not for sending a text message."]}

            try:
                state = await self._get_verified_session_state(ident, sync_sessions=False)
            except AuthError as exc:
                return {"sent": False, "outcome": "NOT_SENT", "errors": [f"Session verification failed: {exc}"]}
            except ApiError as exc:
                return {"sent": False, "outcome": "NOT_SENT", "errors": [f"Could not verify WhatsApp session: {exc}"]}

            if not state["connected"]:
                return {
                    "sent": False,
                    "outcome": "NOT_SENT",
                    "errors": [f"WhatsApp session is not connected (current status: {state['status']}). Connect WhatsApp first."],
                }
            if not state["outbound_enabled"] or state["safety_stop_active"]:
                return {
                    "sent": False,
                    "outcome": "NOT_SENT",
                    "errors": ["Outbound WhatsApp messaging is currently disabled or safety-stopped by the backend."],
                }

            try:
                created = await self.client.send_whatsapp_message(op.payload)
            except ApiTimeout as exc:
                self.store.mark(op, "UNCERTAIN")
                return {
                    "sent": False,
                    "outcome": "UNCERTAIN",
                    "errors": [f"{exc} Do NOT blindly retry; check get_whatsapp_chat_history first."],
                }
            except ApiError as exc:
                return {"sent": False, "outcome": "NOT_SENT", "errors": [f"Market Intel API rejected send: {exc}"]}

            if not isinstance(created, dict) or not created.get("id"):
                self.store.mark(op, "UNCERTAIN")
                return {
                    "sent": False,
                    "outcome": "UNCERTAIN",
                    "errors": ["API did not return a confirmed message ID. Check chat history before retrying."],
                }

            self.store.mark(op, "CREATED")
            return {
                "sent": True,
                "outcome": "CONFIRMED_SUCCESS",
                "message_id": str(created["id"]),
                "waha_message_id": created.get("waha_message_id") or "",
                "status": created.get("status") or "SENT",
                "sent_at": created.get("sent_at") or created.get("created_at"),
                "recipient": op.payload.get("chat_id") or op.payload.get("prospect_contact_id"),
                "message": _format_message(created),
            }

        # Phase 1: Validate & stage
        try:
            params = WhatsAppSendMessageInput.model_validate({
                "recipient": recipient,
                "chat_id": chat_id,
                "prospect_contact_id": prospect_contact_id,
                "message_body": message_body,
                "reply_to": reply_to,
            })
        except ValidationError as exc:
            return {"sent": False, "requires_confirmation": False, "errors": format_validation_errors(exc)}

        if confirmation_token and user_has_explicitly_confirmed is not True:
            return {
                "sent": False,
                "requires_confirmation": True,
                "errors": [
                    "Explicit user confirmation is required (user_has_explicitly_confirmed=true). "
                    "The confirmation token has not been used."
                ],
            }

        try:
            ident = await self._identity(require_manage=True, refresh_from_api=True)
            state = await self._get_verified_session_state(ident, sync_sessions=False)
        except AuthError as exc:
            return {"sent": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"sent": False, "errors": [f"Failed to verify WhatsApp session: {exc}"]}

        if not state["connected"]:
            return {
                "sent": False,
                "errors": [f"WhatsApp session is not connected (status: {state['status']}). Call connect_whatsapp first."],
            }
        if not state["outbound_enabled"] or state["safety_stop_active"]:
            return {
                "sent": False,
                "errors": ["Outbound WhatsApp messaging is currently disabled or safety-stopped in Market Intel DB."],
            }

        target_chat_id = params.chat_id
        resolved_contact_name = ""
        resolved_phone = normalize_phone_number(target_chat_id) if target_chat_id else None

        # Match against user's existing owned conversations if possible
        if target_chat_id:
            existing_conv = await self._resolve_owned_conversation(
                ident,
                chat_id=target_chat_id,
                session_phone=state["phone_number"],
            )
            if existing_conv:
                target_chat_id = str(existing_conv.get("chat_id") or target_chat_id)
                resolved_contact_name = str(
                    existing_conv.get("contact_name") or existing_conv.get("whatsapp_name") or ""
                )
                if existing_conv.get("phone_number"):
                    resolved_phone = normalize_phone_number(existing_conv["phone_number"]) or resolved_phone

        api_payload: dict[str, Any] = {"message_body": params.message_body}
        if params.prospect_contact_id:
            api_payload["prospect_contact_id"] = str(params.prospect_contact_id)
        if target_chat_id:
            api_payload["chat_id"] = target_chat_id
        if params.reply_to:
            api_payload["reply_to"] = params.reply_to

        token, ttl = self.store.issue(
            ident.key,
            api_payload,
            op_type="whatsapp_send",
            entity_type="whatsapp_message",
            record_id=target_chat_id or str(params.prospect_contact_id),
        )
        return {
            "sent": False,
            "requires_confirmation": True,
            "preview": {
                "from_user": ident.username,
                "from_phone": state["phone_number"],
                "to_chat_id": target_chat_id,
                "to_phone": resolved_phone,
                "to_name": resolved_contact_name or resolved_phone or str(params.prospect_contact_id or target_chat_id),
                "prospect_contact_id": str(params.prospect_contact_id) if params.prospect_contact_id else None,
                "message_body": params.message_body,
                "reply_to": params.reply_to,
            },
            "confirmation_token": token,
            "token_expires_in_seconds": ttl,
            "next_step": (
                "NO MESSAGE HAS BEEN SENT. Show this recipient and message preview to the user and ask for "
                "explicit approval. Only after approval, call send_whatsapp_message with this confirmation_token "
                "and user_has_explicitly_confirmed=true."
            ),
        }

    async def send_whatsapp_media(
        self,
        recipient: str | None = None,
        chat_id: str | None = None,
        filename: str = "",
        mime_type: str = "",
        file_base64: str = "",
        caption: str = "",
        confirmation_token: str | None = None,
        user_has_explicitly_confirmed: bool = False,
    ) -> dict[str, Any]:
        # Phase 2: Confirmed execution
        if confirmation_token and user_has_explicitly_confirmed is True:
            try:
                ident = await self._identity(require_manage=True, refresh_from_api=True)
                allowed, retry_after = self.rate_limiter.check_and_record(ident.key, "send")
                if not allowed:
                    return {
                        "sent": False,
                        "outcome": "RATE_LIMITED",
                        "errors": [f"Rate limit exceeded for outbound WhatsApp media. Retry in {retry_after}s."],
                    }
                op = self.store.consume(confirmation_token, ident.key)
            except ConfirmationError as exc:
                return {"sent": False, "outcome": "NOT_SENT", "errors": [str(exc)]}
            except AuthError as exc:
                return {"sent": False, "outcome": "NOT_SENT", "errors": [f"Authentication/permission error: {exc}"]}

            if op.op_type != "whatsapp_send_media" or op.entity_type != "whatsapp_media":
                return {"sent": False, "outcome": "NOT_SENT", "errors": ["Confirmation token is not for sending media."]}

            expected_sha = str(op.payload.get("file_sha256") or "")
            media_bytes = self.media_store.pop(confirmation_token, ident.key, expected_sha)
            if media_bytes is None and file_base64:
                try:
                    max_bytes = self.settings.whatsapp_max_media_mb * 1024 * 1024
                    candidate = decode_base64_media(file_base64, max_bytes=max_bytes)
                    if hmac.compare_digest(hashlib.sha256(candidate).hexdigest(), expected_sha):
                        media_bytes = candidate
                except ValueError:
                    media_bytes = None
            if media_bytes is None:
                return {
                    "sent": False,
                    "outcome": "NOT_SENT",
                    "errors": ["Staged media payload expired or failed integrity verification. Re-stage the media send."],
                }

            try:
                state = await self._get_verified_session_state(ident, sync_sessions=False)
            except AuthError as exc:
                return {"sent": False, "outcome": "NOT_SENT", "errors": [f"Session verification failed: {exc}"]}
            except ApiError as exc:
                return {"sent": False, "outcome": "NOT_SENT", "errors": [f"Could not verify WhatsApp session: {exc}"]}

            if not state["connected"]:
                return {
                    "sent": False,
                    "outcome": "NOT_SENT",
                    "errors": [f"WhatsApp session is not connected (status: {state['status']})."],
                }
            if not state["outbound_enabled"] or state["safety_stop_active"]:
                return {
                    "sent": False,
                    "outcome": "NOT_SENT",
                    "errors": ["Outbound WhatsApp messaging is currently disabled or safety-stopped."],
                }

            try:
                created = await self.client.send_whatsapp_media(
                    chat_id=op.payload["chat_id"],
                    filename=op.payload["filename"],
                    content=media_bytes,
                    mime_type=op.payload["mime_type"],
                    caption=op.payload.get("caption") or "",
                )
            except ApiTimeout as exc:
                self.store.mark(op, "UNCERTAIN")
                return {
                    "sent": False,
                    "outcome": "UNCERTAIN",
                    "errors": [f"{exc} Do NOT retry blindly; inspect chat history first."],
                }
            except ApiError as exc:
                return {"sent": False, "outcome": "NOT_SENT", "errors": [f"Market Intel API rejected media send: {exc}"]}

            if not isinstance(created, dict) or not created.get("id"):
                self.store.mark(op, "UNCERTAIN")
                return {
                    "sent": False,
                    "outcome": "UNCERTAIN",
                    "errors": ["API did not return a confirmed message ID for media send."],
                }

            self.store.mark(op, "CREATED")
            return {
                "sent": True,
                "outcome": "CONFIRMED_SUCCESS",
                "message_id": str(created["id"]),
                "waha_message_id": created.get("waha_message_id") or "",
                "status": created.get("status") or "SENT",
                "message_type": created.get("message_type") or "",
                "media_mime_type": created.get("media_mime_type") or op.payload["mime_type"],
                "recipient": op.payload["chat_id"],
                "message": _format_message(created),
            }

        # Phase 1: Validate & stage
        try:
            params = WhatsAppSendMediaInput.model_validate({
                "recipient": recipient,
                "chat_id": chat_id,
                "filename": filename,
                "mime_type": mime_type,
                "file_base64": file_base64,
                "caption": caption,
            })
            max_bytes = self.settings.whatsapp_max_media_mb * 1024 * 1024
            media_bytes = decode_base64_media(params.file_base64, max_bytes=max_bytes)
        except (ValidationError, ValueError) as exc:
            errs = format_validation_errors(exc) if isinstance(exc, ValidationError) else [str(exc)]
            return {"sent": False, "requires_confirmation": False, "errors": errs}

        if confirmation_token and user_has_explicitly_confirmed is not True:
            return {
                "sent": False,
                "requires_confirmation": True,
                "errors": [
                    "Explicit user confirmation is required (user_has_explicitly_confirmed=true). "
                    "The confirmation token has not been used."
                ],
            }

        try:
            ident = await self._identity(require_manage=True, refresh_from_api=True)
            state = await self._get_verified_session_state(ident, sync_sessions=False)
        except AuthError as exc:
            return {"sent": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"sent": False, "errors": [f"Failed to verify WhatsApp session: {exc}"]}

        if not state["connected"]:
            return {
                "sent": False,
                "errors": [f"WhatsApp session is not connected (status: {state['status']}). Call connect_whatsapp first."],
            }
        if not state["outbound_enabled"] or state["safety_stop_active"]:
            return {
                "sent": False,
                "errors": ["Outbound WhatsApp messaging is currently disabled or safety-stopped."],
            }

        sha_hex = hashlib.sha256(media_bytes).hexdigest()
        staged_payload = {
            "chat_id": params.chat_id,
            "filename": params.filename,
            "mime_type": params.mime_type,
            "caption": params.caption,
            "file_size_bytes": len(media_bytes),
            "file_sha256": sha_hex,
        }
        token, ttl = self.store.issue(
            ident.key,
            staged_payload,
            op_type="whatsapp_send_media",
            entity_type="whatsapp_media",
            record_id=params.chat_id,
        )
        self.media_store.put(token, ident.key, sha_hex, media_bytes)

        return {
            "sent": False,
            "requires_confirmation": True,
            "preview": {
                "from_user": ident.username,
                "from_phone": state["phone_number"],
                "to_chat_id": params.chat_id,
                "filename": params.filename,
                "mime_type": params.mime_type,
                "file_size_bytes": len(media_bytes),
                "caption": params.caption,
            },
            "confirmation_token": token,
            "token_expires_in_seconds": ttl,
            "next_step": (
                "NO MEDIA HAS BEEN SENT. Show this recipient and media summary to the user and ask for "
                "explicit approval. Only after approval, call send_whatsapp_media with this confirmation_token "
                "and user_has_explicitly_confirmed=true."
            ),
        }

    async def mark_whatsapp_chat_read(
        self,
        conversation_id: str | None = None,
        chat_id: str | None = None,
        phone_number: str | None = None,
    ) -> dict[str, Any]:
        try:
            params = WhatsAppMarkReadInput.model_validate({
                "conversation_id": conversation_id,
                "chat_id": chat_id,
                "phone_number": phone_number,
            })
        except ValidationError as exc:
            return {"ok": False, "errors": format_validation_errors(exc)}

        try:
            ident = await self._identity(require_manage=False)
            state = await self._get_verified_session_state(ident, sync_sessions=False)

            conv = await self._resolve_owned_conversation(
                ident,
                conversation_id=str(params.conversation_id) if params.conversation_id else None,
                chat_id=params.chat_id,
                phone_number=params.phone_number,
                session_phone=state["phone_number"],
            )
            if not conv:
                return {"ok": False, "errors": ["Conversation not found among your owned WhatsApp chats."]}

            target_conv_id = str(conv["id"])
            res = await self.client.mark_whatsapp_conversation_read(target_conv_id)
            return {
                "ok": True,
                "conversation_id": target_conv_id,
                "chat_id": conv.get("chat_id") or "",
                "detail": res.get("detail") if isinstance(res, dict) else "Marked as read.",
            }
        except AuthError as exc:
            return {"ok": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"ok": False, "errors": [f"Failed to mark conversation as read: {exc}"]}

