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
    WhatsAppCreateGroupInput,
    WhatsAppEditMessageInput,
    WhatsAppForwardMessageInput,
    WhatsAppGroupInviteLinkInput,
    WhatsAppIncomingMessagesInput,
    WhatsAppLinkCRMInput,
    WhatsAppListChatsInput,
    WhatsAppManageGroupParticipantsInput,
    WhatsAppMarkReadInput,
    WhatsAppQRInput,
    WhatsAppSearchMessagesInput,
    WhatsAppSendMediaInput,
    WhatsAppSendMessageInput,
    WhatsAppSyncHistoryInput,
    decode_base64_media,
    decode_cursor,
    encode_cursor,
    format_validation_errors,
    is_lid_identifier,
    normalize_phone_digits,
    normalize_phone_number,
    phone_to_chat_id,
    phones_match,
    validate_media_mime,
    validate_safe_filename,
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
    async def sync_whatsapp_history(
        self, days: int = 2, chat_id: str | None = None, session_name: str | None = None
    ) -> Any: ...
    async def sync_whatsapp_conversation(self, conversation_id: str, days: int = 2) -> Any: ...
    async def link_whatsapp_conversation_crm(self, conversation_id: str, prospect_contact_id: str | None) -> Any: ...
    async def edit_whatsapp_message(self, message_id: str, text: str) -> Any: ...
    async def create_whatsapp_group(self, name: str, participants: list[str]) -> Any: ...
    async def add_whatsapp_group_participants(self, chat_id: str, participants: list[str]) -> Any: ...
    async def remove_whatsapp_group_participants(self, chat_id: str, participants: list[str]) -> Any: ...
    async def get_whatsapp_group_invite_link(self, chat_id: str) -> Any: ...
    async def download_whatsapp_media(self, message_id: str) -> tuple[bytes, str]: ...


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
    waha_raw = str((status_data.get("waha_status") or {}).get("status") or "").strip().upper()
    top_raw = str(status_data.get("status") or "").strip().upper()
    sess_raw = str((status_data.get("session") or {}).get("status") or "").strip().upper()
    if waha_raw in CONNECTED_STATUSES:
        return waha_raw
    if top_raw in CONNECTED_STATUSES:
        return top_raw
    if sess_raw in CONNECTED_STATUSES:
        return sess_raw
    return top_raw or waha_raw or sess_raw or "UNKNOWN"


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
        if (
            not ident.is_superuser
            and owner_username
            and ident.username
            and str(owner_username).strip() != ident.username.strip()
        ):
            raise AuthError("WhatsApp session ownership mismatch: session belongs to a different user.")

        session_name = session_obj.get("session_name") or (
            status_data.get("session_name") if isinstance(status_data, dict) else None
        )
        if not ident.is_superuser and ident.whatsapp_sessions and session_name:
            owned_names = {s.get("session_name") for s in ident.whatsapp_sessions if isinstance(s, dict)}
            if owned_names and session_name not in owned_names:
                # Re-check against sessions_list if provided (newly created or reconciled session)
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

        # Prefer reconciled status/phone from synced session list if available
        if isinstance(sessions_list, list) and sessions_list and isinstance(sessions_list[0], dict):
            synced_sess = sessions_list[0]
            synced_owner = synced_sess.get("owner_username")
            if (
                not ident.is_superuser
                and synced_owner
                and ident.username
                and str(synced_owner).strip() != ident.username.strip()
            ):
                raise AuthError("Synced WhatsApp session belongs to a different user.")
            if synced_sess.get("phone_number"):
                synced_phone = normalize_phone_number(str(synced_sess["phone_number"]))
                if synced_phone:
                    phone = synced_phone
            if not session_obj:
                session_obj = synced_sess

        status_str = _extract_status(status_data)
        sess_obj_status = str(session_obj.get("status") or "").strip().upper()
        if (status_str == "UNKNOWN" or status_str in DISCONNECTED_STATUSES) and sess_obj_status in CONNECTED_STATUSES:
            status_str = sess_obj_status
        elif status_str == "UNKNOWN" and sess_obj_status:
            status_str = sess_obj_status
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
            # Defense-in-depth: if both session_phone and conversation account_phone are valid phones,
            # ensure they match (normalized) so another WhatsApp account's chats are never surfaced.
            if (
                session_phone
                and acct_phone
                and normalize_phone_digits(str(acct_phone))
                and not phones_match(session_phone, str(acct_phone))
            ):
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

    async def _sync_from_waha_best_effort(
        self,
        *,
        days: int = 2,
        chat_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Pull recent WAHA messages into Market Intel DB without breaking reads on transient WAHA hiccups."""
        try:
            if conversation_id and hasattr(self.client, "sync_whatsapp_conversation"):
                res = await self.client.sync_whatsapp_conversation(conversation_id, days=days)
                return res if isinstance(res, dict) else {"synced": 0}
            if hasattr(self.client, "sync_whatsapp_history"):
                res = await self.client.sync_whatsapp_history(days=days, chat_id=chat_id)
                return res if isinstance(res, dict) else {"synced": 0}
        except (ApiError, AuthError) as exc:
            log.debug("WhatsApp history sync skipped (%s)", exc)
            return {"synced": 0, "sync_warning": str(exc)}
        return {"synced": 0}

    async def _match_number_in_market_intel_db(
        self,
        *,
        phone_number: str | None = None,
        chat_id: str | None = None,
        sender_id: str | None = None,
    ) -> dict[str, Any]:
        """Match a WhatsApp ID / sender ID / mobile number against Market Intel DB contacts and prospects."""
        raw_jid = chat_id or (sender_id if sender_id and "@" in sender_id else None)
        target_digits = (
            normalize_phone_digits(phone_number)
            or normalize_phone_digits(raw_jid)
            or normalize_phone_digits(sender_id)
        )
        contact_info: dict[str, Any] = {}
        matched_contact: dict[str, Any] | None = None
        matched_prospect: dict[str, Any] | None = None
        match_source: str | None = None

        if raw_jid and hasattr(self.client, "get_whatsapp_contact_info"):
            try:
                info = await self.client.get_whatsapp_contact_info(raw_jid)
                if isinstance(info, dict):
                    contact_info = info
                    if not target_digits and info.get("phoneNumber"):
                        target_digits = normalize_phone_digits(info.get("phoneNumber"))
                    if info.get("prospect_contact_id"):
                        matched_contact = {
                            "id": str(info["prospect_contact_id"]),
                            "contact_name": info.get("contact_name") or info.get("name") or "",
                            "designation": info.get("designation") or "",
                            "official_email": info.get("official_email") or "",
                            "phone_number": info.get("phoneNumber") or "",
                            "company_name": info.get("company") or "",
                            "prospect_id": str(info["prospect_id"]) if info.get("prospect_id") else None,
                        }
                        match_source = "whatsapp_contact_info"
            except ApiError:
                pass

        if not matched_contact and target_digits and hasattr(self.client, "list_whatsapp_crm_contacts"):
            search_terms: list[str] = []
            if len(target_digits) >= 10:
                search_terms.append(target_digits[-10:])
            if len(target_digits) >= 4:
                search_terms.append(target_digits[-4:])
            seen_terms: set[str] = set()
            for term in search_terms:
                if term in seen_terms:
                    continue
                seen_terms.add(term)
                try:
                    contacts = await self.client.list_whatsapp_crm_contacts(search=term)
                except ApiError:
                    continue
                if isinstance(contacts, dict) and "results" in contacts:
                    contacts = contacts["results"]
                if not isinstance(contacts, list):
                    continue
                for c in contacts:
                    if isinstance(c, dict) and phones_match(target_digits, c.get("phone_number")):
                        matched_contact = {
                            "id": str(c.get("id") or ""),
                            "contact_name": c.get("contact_name") or "",
                            "designation": c.get("designation") or "",
                            "official_email": c.get("official_email") or "",
                            "phone_number": c.get("phone_number") or "",
                            "company_name": c.get("company_name") or "",
                            "prospect_id": str(c["prospect_id"]) if c.get("prospect_id") else None,
                        }
                        match_source = "crm_contacts"
                        break
                if matched_contact:
                    break

        if target_digits and hasattr(self.client, "list_prospects"):
            tail = target_digits[-10:] if len(target_digits) >= 10 else target_digits
            try:
                prospect_res = await self.client.list_prospects({"search": tail})
                p_list = prospect_res.get("results") if isinstance(prospect_res, dict) else prospect_res
                if isinstance(p_list, list):
                    for p in p_list:
                        if not isinstance(p, dict):
                            continue
                        p_phone = p.get("official_phone_number")
                        kcs = p.get("key_contacts") if isinstance(p.get("key_contacts"), list) else []
                        matched_kc = next(
                            (kc for kc in kcs if isinstance(kc, dict) and phones_match(target_digits, kc.get("phone_number"))),
                            None,
                        )
                        if (p_phone and phones_match(target_digits, str(p_phone))) or matched_kc or (
                            matched_contact and matched_contact.get("prospect_id") == str(p.get("id"))
                        ):
                            matched_prospect = {
                                "id": str(p.get("id") or ""),
                                "company_name": p.get("company_name") or "",
                                "country_head_office": p.get("country_head_office") or "",
                                "primary_industries": p.get("primary_industries") or "",
                                "official_phone_number": p.get("official_phone_number") or "",
                                "official_email_address": p.get("official_email_address") or "",
                                "operational_status": p.get("operational_status") or "",
                            }
                            if not matched_contact and matched_kc and matched_kc.get("id"):
                                matched_contact = {
                                    "id": str(matched_kc["id"]),
                                    "contact_name": matched_kc.get("contact_name") or "",
                                    "designation": matched_kc.get("designation") or "",
                                    "official_email": matched_kc.get("official_email") or "",
                                    "phone_number": matched_kc.get("phone_number") or "",
                                    "company_name": p.get("company_name") or "",
                                    "prospect_id": str(p.get("id") or ""),
                                }
                                match_source = "prospect_key_contact"
                            elif not match_source:
                                match_source = "prospect_phone"
                            break
            except ApiError:
                pass

        norm_phone = normalize_phone_number(target_digits) if target_digits else normalize_phone_number(contact_info.get("phoneNumber"))
        return {
            "matched": bool(matched_contact or matched_prospect),
            "match_source": match_source,
            "phone_number": norm_phone,
            "prospect_contact_id": matched_contact.get("id") if matched_contact and matched_contact.get("id") else None,
            "contact_name": (matched_contact.get("contact_name") if matched_contact else None) or contact_info.get("contact_name") or None,
            "designation": matched_contact.get("designation") if matched_contact else None,
            "official_email": matched_contact.get("official_email") if matched_contact else None,
            "prospect_id": (matched_contact.get("prospect_id") if matched_contact else None) or (matched_prospect.get("id") if matched_prospect else None),
            "company_name": (matched_contact.get("company_name") if matched_contact else None) or (matched_prospect.get("company_name") if matched_prospect else None),
            "prospect": matched_prospect,
        }

    async def _ensure_conversation_crm_link(
        self,
        conv: dict[str, Any] | None,
        *,
        prospect_contact_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Ensure an unmatched 1:1 conversation is linked to its matching Market Intel ProspectContact."""
        if not isinstance(conv, dict):
            return conv, {"matched": False}
        if bool(conv.get("chat_id", "").endswith("@g.us")) or bool(conv.get("is_group")):
            return conv, {"matched": bool(conv.get("is_matched")), "is_group": True}
        conv_id = str(conv.get("id") or "")
        existing_cid = conv.get("prospect_contact")
        if existing_cid and not prospect_contact_id:
            match_info = await self._match_number_in_market_intel_db(
                phone_number=conv.get("phone_number"),
                chat_id=conv.get("chat_id"),
            )
            if not match_info["prospect_contact_id"]:
                match_info["prospect_contact_id"] = str(existing_cid)
                match_info["matched"] = True
            return conv, match_info

        match_info = await self._match_number_in_market_intel_db(
            phone_number=conv.get("phone_number"),
            chat_id=conv.get("chat_id"),
        )
        link_id = prospect_contact_id or match_info.get("prospect_contact_id")
        if conv_id and link_id and hasattr(self.client, "link_whatsapp_conversation_crm") and not existing_cid:
            try:
                linked = await self.client.link_whatsapp_conversation_crm(conv_id, str(link_id))
                if isinstance(linked, dict) and linked.get("id"):
                    conv = linked
            except ApiError as exc:
                log.debug("Auto CRM link failed for %s: %s", conv_id, exc)
        return conv, match_info

    async def _resolve_reply_to_waha_id(
        self,
        reply_to: str | None,
        *,
        conversation_id: str | None = None,
        chat_id: str | None = None,
        session_phone: str | None = None,
    ) -> str | None:
        if not reply_to:
            return None
        cleaned = reply_to.strip()
        try:
            UUID(cleaned)
        except ValueError:
            return cleaned  # Already a WAHA message ID (e.g. false_91...@c.us_...)
        target_conv_id = conversation_id
        if not target_conv_id and chat_id:
            conv = await self._resolve_owned_conversation(
                self.client.identity,  # type: ignore[arg-type]
                chat_id=chat_id,
                session_phone=session_phone,
            )
            if conv:
                target_conv_id = str(conv.get("id") or "")
        if target_conv_id:
            try:
                hist = await self.client.get_whatsapp_conversation_messages(target_conv_id, limit=500)
                msgs = hist.get("messages") if isinstance(hist, dict) else []
                if isinstance(msgs, list):
                    for m in msgs:
                        if isinstance(m, dict) and str(m.get("id") or "") == cleaned:
                            return str(m.get("waha_message_id") or cleaned)
            except ApiError:
                pass
        return cleaned

    async def _locate_owned_message(
        self,
        ident: Identity,
        *,
        message_id: str,
        conversation_id: str | None = None,
        session_phone: str | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Find a message strictly inside the authenticated user's owned conversations."""
        if conversation_id:
            conv = await self._resolve_owned_conversation(
                ident, conversation_id=conversation_id, session_phone=session_phone
            )
            convs = [conv] if conv else []
        else:
            convs = (await self._list_owned_conversations(ident, session_phone=session_phone))[
                : self.settings.whatsapp_max_search_conversations
            ]
        for conv in convs:
            cid = str(conv.get("id") or "")
            if not cid:
                continue
            try:
                hist = await self.client.get_whatsapp_conversation_messages(cid, limit=1000)
            except ApiError:
                continue
            if not isinstance(hist, dict):
                continue
            conv_obj = hist.get("conversation") if isinstance(hist.get("conversation"), dict) else conv
            msgs = hist.get("messages") if isinstance(hist.get("messages"), list) else []
            for m in msgs:
                if isinstance(m, dict) and str(m.get("id") or "") == str(message_id):
                    return m, conv_obj
        return None, None

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
                state = await self._get_verified_session_state(ident, sync_sessions=True)
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
        sync: bool = True,
    ) -> dict[str, Any]:
        try:
            params = WhatsAppListChatsInput.model_validate({
                "search": search,
                "is_group": is_group,
                "matched_only": matched_only,
                "limit": limit,
                "cursor": cursor,
                "sync": sync,
            })
            offset = decode_cursor(params.cursor, expected_scope="chats")
        except (ValidationError, ValueError) as exc:
            errs = format_validation_errors(exc) if isinstance(exc, ValidationError) else [str(exc)]
            return {"ok": False, "errors": errs}

        try:
            ident = await self._identity(require_manage=False)
            state = await self._get_verified_session_state(ident, sync_sessions=True)
            if params.sync and state["connected"]:
                await self._sync_from_waha_best_effort(days=2)
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
        sync: bool = True,
    ) -> dict[str, Any]:
        try:
            params = WhatsAppChatHistoryInput.model_validate({
                "conversation_id": conversation_id,
                "chat_id": chat_id,
                "phone_number": phone_number,
                "page_size": page_size,
                "cursor": cursor,
                "order": order,
                "sync": sync,
            })
        except ValidationError as exc:
            return {"ok": False, "errors": format_validation_errors(exc)}

        try:
            ident = await self._identity(require_manage=False)
            state = await self._get_verified_session_state(ident, sync_sessions=True)

            resolved_conv_id = str(params.conversation_id) if params.conversation_id else None
            conv_match: dict[str, Any] | None = None
            if not resolved_conv_id:
                conv_match = await self._resolve_owned_conversation(
                    ident,
                    chat_id=params.chat_id,
                    phone_number=params.phone_number,
                    session_phone=state["phone_number"],
                )
                if not conv_match and params.sync and state["connected"]:
                    target_sync_chat = params.chat_id or (
                        phone_to_chat_id(params.phone_number) if params.phone_number else None
                    )
                    await self._sync_from_waha_best_effort(days=2, chat_id=target_sync_chat)
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

            if params.sync and state["connected"]:
                await self._sync_from_waha_best_effort(days=2, conversation_id=resolved_conv_id)

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

        conv_raw = data.get("conversation") if isinstance(data.get("conversation"), dict) else (conv_match or {})
        acct_phone = conv_raw.get("account_phone")
        if (
            state["phone_number"]
            and acct_phone
            and normalize_phone_digits(str(acct_phone))
            and not phones_match(state["phone_number"], str(acct_phone))
        ):
            return {"ok": False, "errors": ["Conversation belongs to a different WhatsApp account."]}

        conv_raw, crm_match = await self._ensure_conversation_crm_link(conv_raw)

        raw_msgs = data.get("messages") if isinstance(data.get("messages"), list) else []
        formatted_msgs = [_format_message(m) for m in raw_msgs if isinstance(m, dict)]

        # Deterministic sort by effective timestamp + id
        formatted_msgs.sort(
            key=lambda m: (str(m.get("timestamp") or ""), str(m.get("created_at") or ""), str(m.get("id") or "")),
            reverse=(params.order == "desc"),
        )
        chrono = sorted(
            formatted_msgs,
            key=lambda m: (str(m.get("timestamp") or ""), str(m.get("created_at") or ""), str(m.get("id") or "")),
        )
        latest_msg = chrono[-1] if chrono else None
        latest_inbound = next((m for m in reversed(chrono) if m.get("direction") == "INBOUND"), None)

        total = len(formatted_msgs)
        page = formatted_msgs[offset : offset + params.page_size]
        next_offset = offset + len(page)
        has_more = next_offset < total

        return {
            "ok": True,
            "conversation": _format_conversation(conv_raw) if conv_raw else {"conversation_id": resolved_conv_id},
            "crm_match": crm_match,
            "latest_message": latest_msg,
            "latest_inbound_message": latest_inbound,
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

            crm_match: dict[str, Any] = {}
            if not info.get("is_group") and not info.get("prospect_contact_id"):
                crm_match = await self._match_number_in_market_intel_db(
                    phone_number=params.phone_number or info.get("phoneNumber"),
                    chat_id=info.get("chatId") or target_chat_id,
                )

            return {
                "ok": True,
                "chat_id": info.get("chatId") or target_chat_id,
                "phone_number": (
                    normalize_phone_number(info.get("phoneNumber"))
                    or info.get("phoneNumber")
                    or crm_match.get("phone_number")
                    or ""
                ),
                "name": info.get("name") or crm_match.get("contact_name") or "",
                "contact_name": info.get("contact_name") or crm_match.get("contact_name") or "",
                "company": info.get("company") or crm_match.get("company_name") or "",
                "designation": info.get("designation") or crm_match.get("designation") or "",
                "official_email": info.get("official_email") or crm_match.get("official_email") or "",
                "is_matched": bool(info.get("is_matched") or crm_match.get("matched")),
                "prospect_id": info.get("prospect_id") or crm_match.get("prospect_id"),
                "prospect_contact_id": info.get("prospect_contact_id") or crm_match.get("prospect_contact_id"),
                "prospect": crm_match.get("prospect"),
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
        sync: bool = False,
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
            state = await self._get_verified_session_state(ident, sync_sessions=True)
            if sync and state["connected"]:
                await self._sync_from_waha_best_effort(days=2, chat_id=params.chat_id)

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
    # Messaging helpers & tools
    # ------------------------------------------------------------------

    async def _resolve_outbound_target(
        self,
        ident: Identity,
        state: dict[str, Any],
        *,
        chat_id: str | None = None,
        prospect_contact_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve canonical chat_id, phone, display name, and Market Intel DB contact match."""
        target_chat_id = chat_id
        resolved_contact_name = ""
        resolved_phone = normalize_phone_number(target_chat_id) if target_chat_id else None
        existing_conv: dict[str, Any] | None = None

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

        crm_match: dict[str, Any] = {"matched": False}
        is_group = bool(target_chat_id and (target_chat_id.endswith("@g.us") or target_chat_id.startswith("crm_group_")))
        if not is_group:
            crm_match = await self._match_number_in_market_intel_db(
                phone_number=resolved_phone,
                chat_id=target_chat_id,
            )
            if not resolved_contact_name and crm_match.get("contact_name"):
                resolved_contact_name = str(crm_match["contact_name"])
            if not target_chat_id and crm_match.get("phone_number"):
                resolved_phone = crm_match["phone_number"]
                target_chat_id = phone_to_chat_id(resolved_phone)

        eff_contact_id = (
            prospect_contact_id
            or (str(existing_conv["prospect_contact"]) if existing_conv and existing_conv.get("prospect_contact") else None)
            or crm_match.get("prospect_contact_id")
        )
        return {
            "chat_id": target_chat_id,
            "phone_number": resolved_phone,
            "contact_name": resolved_contact_name,
            "conversation": existing_conv,
            "prospect_contact_id": eff_contact_id,
            "crm_match": crm_match,
        }

    async def _associate_conversation_after_send(
        self,
        ident: Identity,
        state: dict[str, Any],
        *,
        chat_id: str | None = None,
        prospect_contact_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Ensure any newly created conversation is linked to the matched Market Intel contact in DB."""
        if not chat_id or chat_id.endswith("@g.us") or chat_id.startswith("crm_group_"):
            return None
        try:
            conv = await self._resolve_owned_conversation(
                ident,
                chat_id=chat_id,
                phone_number=normalize_phone_number(chat_id),
                session_phone=state["phone_number"],
            )
            if not conv:
                return None
            conv, _ = await self._ensure_conversation_crm_link(conv, prospect_contact_id=prospect_contact_id)
            return _format_conversation(conv) if isinstance(conv, dict) else None
        except ApiError:
            return None

    async def send_whatsapp_message(
        self,
        recipient: str | None = None,
        message_body: str = "",
        chat_id: str | None = None,
        prospect_contact_id: str | None = None,
        reply_to: str | None = None,
        mentions: list[str] | None = None,
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
                state = await self._get_verified_session_state(ident, sync_sessions=True)
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

            api_body = dict(op.payload)
            pending_link_cid = api_body.pop("_link_prospect_contact_id", None)
            try:
                created = await self.client.send_whatsapp_message(api_body)
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
            linked_conv = await self._associate_conversation_after_send(
                ident,
                state,
                chat_id=api_body.get("chat_id"),
                prospect_contact_id=pending_link_cid or api_body.get("prospect_contact_id"),
            )
            out: dict[str, Any] = {
                "sent": True,
                "outcome": "CONFIRMED_SUCCESS",
                "message_id": str(created["id"]),
                "waha_message_id": created.get("waha_message_id") or "",
                "status": created.get("status") or "SENT",
                "sent_at": created.get("sent_at") or created.get("created_at"),
                "recipient": api_body.get("chat_id") or api_body.get("prospect_contact_id"),
                "message": _format_message(created),
            }
            if linked_conv:
                out["conversation"] = linked_conv
            return out

        # Phase 1: Validate & stage
        try:
            params = WhatsAppSendMessageInput.model_validate({
                "recipient": recipient,
                "chat_id": chat_id,
                "prospect_contact_id": prospect_contact_id,
                "message_body": message_body,
                "reply_to": reply_to,
                "mentions": mentions,
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
            state = await self._get_verified_session_state(ident, sync_sessions=True)
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

        resolved = await self._resolve_outbound_target(
            ident,
            state,
            chat_id=params.chat_id,
            prospect_contact_id=str(params.prospect_contact_id) if params.prospect_contact_id else None,
        )
        target_chat_id = resolved["chat_id"]
        resolved_contact_name = resolved["contact_name"]
        resolved_phone = resolved["phone_number"]
        eff_contact_id = resolved["prospect_contact_id"]
        existing_conv = resolved["conversation"]

        resolved_reply_to = await self._resolve_reply_to_waha_id(
            params.reply_to,
            conversation_id=str(existing_conv["id"]) if existing_conv and existing_conv.get("id") else None,
            chat_id=target_chat_id,
            session_phone=state["phone_number"],
        )

        api_payload: dict[str, Any] = {"message_body": params.message_body}
        # Note: Backend WhatsAppSendMessageView drops reply_to/mentions inside its `if contact_id:`
        # branch; when reply_to or mentions are requested and chat_id is known, send via chat_id
        # and persist CRM contact linkage right after send.
        if (resolved_reply_to or params.mentions) and target_chat_id:
            api_payload["chat_id"] = target_chat_id
            if resolved_reply_to:
                api_payload["reply_to"] = resolved_reply_to
            if params.mentions:
                api_payload["mentions"] = params.mentions
            if eff_contact_id:
                api_payload["_link_prospect_contact_id"] = eff_contact_id
        else:
            if eff_contact_id:
                api_payload["prospect_contact_id"] = eff_contact_id
            if target_chat_id:
                api_payload["chat_id"] = target_chat_id
            if resolved_reply_to:
                api_payload["reply_to"] = resolved_reply_to

        token, ttl = self.store.issue(
            ident.key,
            api_payload,
            op_type="whatsapp_send",
            entity_type="whatsapp_message",
            record_id=target_chat_id or eff_contact_id,
        )
        return {
            "sent": False,
            "requires_confirmation": True,
            "preview": {
                "from_user": ident.username,
                "from_phone": state["phone_number"],
                "to_chat_id": target_chat_id,
                "to_phone": resolved_phone,
                "to_name": resolved_contact_name or resolved_phone or str(eff_contact_id or target_chat_id),
                "prospect_contact_id": eff_contact_id,
                "crm_match": resolved["crm_match"],
                "message_body": params.message_body,
                "reply_to": resolved_reply_to,
                "mentions": params.mentions,
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
                state = await self._get_verified_session_state(ident, sync_sessions=True)
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
            linked_conv = await self._associate_conversation_after_send(
                ident,
                state,
                chat_id=op.payload["chat_id"],
                prospect_contact_id=op.payload.get("_link_prospect_contact_id"),
            )
            out_media: dict[str, Any] = {
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
            if linked_conv:
                out_media["conversation"] = linked_conv
            return out_media

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
            state = await self._get_verified_session_state(ident, sync_sessions=True)
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

        resolved = await self._resolve_outbound_target(ident, state, chat_id=params.chat_id)
        target_chat_id = resolved["chat_id"] or params.chat_id
        sha_hex = hashlib.sha256(media_bytes).hexdigest()
        staged_payload: dict[str, Any] = {
            "chat_id": target_chat_id,
            "filename": params.filename,
            "mime_type": params.mime_type,
            "caption": params.caption,
            "file_size_bytes": len(media_bytes),
            "file_sha256": sha_hex,
        }
        if resolved.get("prospect_contact_id"):
            staged_payload["_link_prospect_contact_id"] = resolved["prospect_contact_id"]
        token, ttl = self.store.issue(
            ident.key,
            staged_payload,
            op_type="whatsapp_send_media",
            entity_type="whatsapp_media",
            record_id=target_chat_id,
        )
        self.media_store.put(token, ident.key, sha_hex, media_bytes)

        return {
            "sent": False,
            "requires_confirmation": True,
            "preview": {
                "from_user": ident.username,
                "from_phone": state["phone_number"],
                "to_chat_id": target_chat_id,
                "to_name": resolved["contact_name"] or resolved["phone_number"] or target_chat_id,
                "crm_match": resolved["crm_match"],
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

    async def sync_whatsapp_history(
        self,
        days: int = 2,
        chat_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            params = WhatsAppSyncHistoryInput.model_validate({
                "days": days,
                "chat_id": chat_id,
                "conversation_id": conversation_id,
            })
        except ValidationError as exc:
            return {"ok": False, "errors": format_validation_errors(exc)}

        try:
            ident = await self._identity(require_manage=False)
            state = await self._get_verified_session_state(ident, sync_sessions=True)
            if not state["connected"]:
                return {
                    "ok": False,
                    "errors": [f"WhatsApp session is not connected (status: {state['status']})."],
                }

            if params.conversation_id:
                conv = await self._resolve_owned_conversation(
                    ident,
                    conversation_id=str(params.conversation_id),
                    session_phone=state["phone_number"],
                )
                if not conv:
                    return {"ok": False, "errors": ["Conversation not found among your owned WhatsApp chats."]}
                res = await self.client.sync_whatsapp_conversation(str(params.conversation_id), days=params.days)
            else:
                res = await self.client.sync_whatsapp_history(
                    days=params.days,
                    chat_id=params.chat_id,
                    session_name=state["session_name"] or None,
                )
            return {
                "ok": True,
                "synced": int(res.get("synced", 0)) if isinstance(res, dict) else 0,
                "days": params.days,
                "detail": res.get("detail") if isinstance(res, dict) else "WhatsApp history synchronized.",
            }
        except AuthError as exc:
            return {"ok": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"ok": False, "errors": [f"Failed to synchronize WhatsApp history: {exc}"]}

    async def get_whatsapp_incoming_messages(
        self,
        phone_number: str | None = None,
        chat_id: str | None = None,
        sender_id: str | None = None,
        conversation_id: str | None = None,
        unread_only: bool = False,
        history_limit: int = 20,
        sync: bool = True,
    ) -> dict[str, Any]:
        """Sync recent WhatsApp messages into Market Intel DB, match sender against CRM, and return context."""
        try:
            params = WhatsAppIncomingMessagesInput.model_validate({
                "phone_number": phone_number,
                "chat_id": chat_id,
                "sender_id": sender_id,
                "conversation_id": conversation_id,
                "unread_only": unread_only,
                "history_limit": history_limit,
                "sync": sync,
            })
        except ValidationError as exc:
            return {"ok": False, "errors": format_validation_errors(exc)}

        try:
            ident = await self._identity(require_manage=False)
            state = await self._get_verified_session_state(ident, sync_sessions=True)
            sync_info: dict[str, Any] = {}
            target_jid_hint = params.chat_id or (
                params.sender_id if params.sender_id and "@" in params.sender_id else None
            ) or (phone_to_chat_id(params.phone_number) if params.phone_number else None)
            if params.sync and state["connected"]:
                sync_info = await self._sync_from_waha_best_effort(days=2, chat_id=target_jid_hint)

            convs = await self._list_owned_conversations(ident, session_phone=state["phone_number"])
        except ValueError as exc:
            return {"ok": False, "errors": [str(exc)]}
        except AuthError as exc:
            return {"ok": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"ok": False, "errors": [f"Failed to read incoming WhatsApp messages: {exc}"]}

        # Identify target conversations by conversation_id, chat_id, sender_id, or phone_number
        filtered: list[dict[str, Any]] = []
        has_filter = bool(params.conversation_id or params.chat_id or params.sender_id or params.phone_number)
        if has_filter:
            sender_digits = normalize_phone_digits(params.phone_number or params.sender_id or params.chat_id)
            sender_jid = (params.chat_id or (params.sender_id if params.sender_id and "@" in params.sender_id else "") or "").strip()
            for c in convs:
                cid_str = str(c.get("id") or "")
                c_chat = str(c.get("chat_id") or "")
                c_phone = c.get("phone_number") or c.get("prospect_phone") or c_chat
                if params.conversation_id and cid_str == str(params.conversation_id):
                    filtered.append(c)
                elif sender_jid and (
                    c_chat == sender_jid
                    or c_chat == sender_jid.replace("@s.whatsapp.net", "@c.us")
                    or c_chat == sender_jid.replace("@c.us", "@s.whatsapp.net")
                ):
                    filtered.append(c)
                elif sender_digits and phones_match(sender_digits, c_phone):
                    filtered.append(c)
                elif params.sender_id and params.sender_id.strip() in c_chat:
                    filtered.append(c)
        else:
            filtered = list(convs)

        if params.unread_only:
            filtered = [c for c in filtered if int(c.get("unread_count") or 0) > 0]

        if not filtered:
            # Even if no WhatsApp conversation exists yet, check Market Intel DB for sender phone context
            db_lookup: dict[str, Any] = {"matched": False}
            if params.phone_number or params.chat_id or params.sender_id:
                db_lookup = await self._match_number_in_market_intel_db(
                    phone_number=params.phone_number,
                    chat_id=params.chat_id,
                    sender_id=params.sender_id,
                )
            return {
                "ok": True,
                "connected": state["connected"],
                "session_phone_number": state["phone_number"],
                "sync": sync_info,
                "conversations": [],
                "crm_match": db_lookup,
                "message": "No matching incoming conversations found in Market Intel DB.",
            }

        results: list[dict[str, Any]] = []
        for c in filtered[:10]:
            cid = str(c.get("id") or "")
            if not cid:
                continue
            conv_obj, crm_match = await self._ensure_conversation_crm_link(c)
            conv_obj = conv_obj or c
            try:
                hist = await self.client.get_whatsapp_conversation_messages(cid, limit=max(params.history_limit * 2, 100))
            except ApiError:
                hist = {}
            raw_msgs = hist.get("messages") if isinstance(hist, dict) and isinstance(hist.get("messages"), list) else []
            formatted = [_format_message(m) for m in raw_msgs if isinstance(m, dict)]
            formatted.sort(
                key=lambda m: (str(m.get("timestamp") or ""), str(m.get("created_at") or ""), str(m.get("id") or ""))
            )
            recent_msgs = formatted[-params.history_limit :]
            latest_inbound = next((m for m in reversed(formatted) if m.get("direction") == "INBOUND"), None)
            if not has_filter and not params.unread_only and latest_inbound is None and not recent_msgs:
                continue
            results.append({
                "conversation": _format_conversation(conv_obj),
                "crm_match": crm_match,
                "latest_inbound_message": latest_inbound,
                "recent_messages": recent_msgs,
                "reply_hint": {
                    "chat_id": conv_obj.get("chat_id"),
                    "prospect_contact_id": crm_match.get("prospect_contact_id") or (
                        str(conv_obj["prospect_contact"]) if conv_obj.get("prospect_contact") else None
                    ),
                    "reply_to": latest_inbound.get("waha_message_id") if latest_inbound else None,
                },
            })

        return {
            "ok": True,
            "connected": state["connected"],
            "session_phone_number": state["phone_number"],
            "sync": sync_info,
            "conversations": results,
        }

    async def edit_whatsapp_message(
        self,
        message_id: str,
        new_text: str,
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
                        "edited": False,
                        "outcome": "RATE_LIMITED",
                        "errors": [f"Rate limit exceeded. Retry in {retry_after}s."],
                    }
                op = self.store.consume(confirmation_token, ident.key)
            except ConfirmationError as exc:
                return {"edited": False, "outcome": "NOT_EDITED", "errors": [str(exc)]}
            except AuthError as exc:
                return {"edited": False, "outcome": "NOT_EDITED", "errors": [f"Authentication/permission error: {exc}"]}

            if op.op_type != "whatsapp_edit" or op.entity_type != "whatsapp_message":
                return {"edited": False, "outcome": "NOT_EDITED", "errors": ["Confirmation token is not for editing a message."]}

            try:
                updated = await self.client.edit_whatsapp_message(op.payload["message_id"], op.payload["new_text"])
            except ApiTimeout as exc:
                self.store.mark(op, "UNCERTAIN")
                return {
                    "edited": False,
                    "outcome": "UNCERTAIN",
                    "errors": [f"{exc} Inspect chat history before retrying."],
                }
            except ApiError as exc:
                return {"edited": False, "outcome": "NOT_EDITED", "errors": [f"Market Intel API rejected edit: {exc}"]}

            self.store.mark(op, "UPDATED")
            return {
                "edited": True,
                "outcome": "CONFIRMED_SUCCESS",
                "message_id": op.payload["message_id"],
                "message": _format_message(updated) if isinstance(updated, dict) else {"id": op.payload["message_id"], "body": op.payload["new_text"], "is_edited": True},
            }

        # Phase 1: Validate & stage
        try:
            params = WhatsAppEditMessageInput.model_validate({
                "message_id": message_id,
                "new_text": new_text,
            })
        except ValidationError as exc:
            return {"edited": False, "requires_confirmation": False, "errors": format_validation_errors(exc)}

        if confirmation_token and user_has_explicitly_confirmed is not True:
            return {
                "edited": False,
                "requires_confirmation": True,
                "errors": [
                    "Explicit user confirmation is required (user_has_explicitly_confirmed=true). "
                    "The confirmation token has not been used."
                ],
            }

        try:
            ident = await self._identity(require_manage=True, refresh_from_api=True)
            state = await self._get_verified_session_state(ident, sync_sessions=True)
            if not state["connected"]:
                return {"edited": False, "errors": [f"WhatsApp session is not connected (status: {state['status']})."]}
            msg_obj, conv_obj = await self._locate_owned_message(
                ident,
                message_id=str(params.message_id),
                session_phone=state["phone_number"],
            )
        except AuthError as exc:
            return {"edited": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"edited": False, "errors": [f"Failed to verify WhatsApp message: {exc}"]}

        if not msg_obj:
            return {"edited": False, "errors": ["Message not found among your owned WhatsApp conversations."]}
        if str(msg_obj.get("direction") or "").upper() != "OUTBOUND":
            return {"edited": False, "errors": ["Only outbound messages sent by you can be edited."]}

        original_text = msg_obj.get("full_body") or msg_obj.get("body_preview") or ""
        payload = {"message_id": str(params.message_id), "new_text": params.new_text}
        token, ttl = self.store.issue(
            ident.key,
            payload,
            op_type="whatsapp_edit",
            entity_type="whatsapp_message",
            record_id=str(params.message_id),
        )
        return {
            "edited": False,
            "requires_confirmation": True,
            "preview": {
                "message_id": str(params.message_id),
                "chat_id": conv_obj.get("chat_id") if conv_obj else None,
                "before_text": original_text,
                "after_text": params.new_text,
            },
            "confirmation_token": token,
            "token_expires_in_seconds": ttl,
            "next_step": (
                "NO MESSAGE HAS BEEN EDITED. Show the before/after text to the user and ask for explicit approval. "
                "Then call edit_whatsapp_message with this confirmation_token and user_has_explicitly_confirmed=true."
            ),
        }

    async def forward_whatsapp_message(
        self,
        source_message_id: str,
        source_conversation_id: str | None = None,
        recipient: str | None = None,
        chat_id: str | None = None,
        prospect_contact_id: str | None = None,
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
                        "forwarded": False,
                        "outcome": "RATE_LIMITED",
                        "errors": [f"Rate limit exceeded for outbound WhatsApp messages. Retry in {retry_after}s."],
                    }
                op = self.store.consume(confirmation_token, ident.key)
            except ConfirmationError as exc:
                return {"forwarded": False, "outcome": "NOT_SENT", "errors": [str(exc)]}
            except AuthError as exc:
                return {"forwarded": False, "outcome": "NOT_SENT", "errors": [f"Authentication/permission error: {exc}"]}

            if op.op_type != "whatsapp_forward" or op.entity_type != "whatsapp_message":
                return {"forwarded": False, "outcome": "NOT_SENT", "errors": ["Confirmation token is not for forwarding a message."]}

            try:
                state = await self._get_verified_session_state(ident, sync_sessions=True)
            except AuthError as exc:
                return {"forwarded": False, "outcome": "NOT_SENT", "errors": [f"Session verification failed: {exc}"]}
            except ApiError as exc:
                return {"forwarded": False, "outcome": "NOT_SENT", "errors": [f"Could not verify WhatsApp session: {exc}"]}

            if not state["connected"]:
                return {"forwarded": False, "outcome": "NOT_SENT", "errors": [f"WhatsApp session is not connected (status: {state['status']})."]}
            if not state["outbound_enabled"] or state["safety_stop_active"]:
                return {"forwarded": False, "outcome": "NOT_SENT", "errors": ["Outbound WhatsApp messaging is disabled or safety-stopped."]}

            target_chat_id = op.payload.get("target_chat_id")
            link_contact_id = op.payload.get("link_prospect_contact_id")
            try:
                if op.payload.get("has_media"):
                    content, downloaded_mime = await self.client.download_whatsapp_media(op.payload["source_message_id"])
                    eff_mime = op.payload.get("media_mime_type") or downloaded_mime
                    try:
                        validate_media_mime(eff_mime)
                    except ValueError:
                        eff_mime = validate_media_mime(downloaded_mime)
                    fname = op.payload.get("filename") or "forwarded_media.bin"
                    created = await self.client.send_whatsapp_media(
                        chat_id=target_chat_id,
                        filename=fname,
                        content=content,
                        mime_type=eff_mime,
                        caption=op.payload.get("text_body") or "",
                    )
                else:
                    send_body: dict[str, Any] = {"message_body": op.payload["text_body"]}
                    if op.payload.get("prospect_contact_id"):
                        send_body["prospect_contact_id"] = op.payload["prospect_contact_id"]
                    if target_chat_id:
                        send_body["chat_id"] = target_chat_id
                    created = await self.client.send_whatsapp_message(send_body)
            except ApiTimeout as exc:
                self.store.mark(op, "UNCERTAIN")
                return {
                    "forwarded": False,
                    "outcome": "UNCERTAIN",
                    "errors": [f"{exc} Check target chat history before retrying."],
                }
            except ApiError as exc:
                return {"forwarded": False, "outcome": "NOT_SENT", "errors": [f"Failed to forward WhatsApp message: {exc}"]}

            if not isinstance(created, dict) or not created.get("id"):
                self.store.mark(op, "UNCERTAIN")
                return {"forwarded": False, "outcome": "UNCERTAIN", "errors": ["API did not return a confirmed message ID."]}

            self.store.mark(op, "CREATED")
            linked_conv = await self._associate_conversation_after_send(
                ident,
                state,
                chat_id=target_chat_id,
                prospect_contact_id=link_contact_id,
            )
            out_fwd: dict[str, Any] = {
                "forwarded": True,
                "outcome": "CONFIRMED_SUCCESS",
                "message_id": str(created["id"]),
                "waha_message_id": created.get("waha_message_id") or "",
                "recipient": target_chat_id or op.payload.get("prospect_contact_id"),
                "message": _format_message(created),
            }
            if linked_conv:
                out_fwd["conversation"] = linked_conv
            return out_fwd

        # Phase 1: Validate & stage
        try:
            params = WhatsAppForwardMessageInput.model_validate({
                "source_message_id": source_message_id,
                "source_conversation_id": source_conversation_id,
                "recipient": recipient,
                "chat_id": chat_id,
                "prospect_contact_id": prospect_contact_id,
            })
        except ValidationError as exc:
            return {"forwarded": False, "requires_confirmation": False, "errors": format_validation_errors(exc)}

        if confirmation_token and user_has_explicitly_confirmed is not True:
            return {
                "forwarded": False,
                "requires_confirmation": True,
                "errors": ["Explicit user confirmation is required (user_has_explicitly_confirmed=true)."],
            }

        try:
            ident = await self._identity(require_manage=True, refresh_from_api=True)
            state = await self._get_verified_session_state(ident, sync_sessions=True)
            if not state["connected"]:
                return {"forwarded": False, "errors": [f"WhatsApp session is not connected (status: {state['status']})."]}
            if not state["outbound_enabled"] or state["safety_stop_active"]:
                return {"forwarded": False, "errors": ["Outbound WhatsApp messaging is disabled or safety-stopped."]}

            src_msg, src_conv = await self._locate_owned_message(
                ident,
                message_id=str(params.source_message_id),
                conversation_id=str(params.source_conversation_id) if params.source_conversation_id else None,
                session_phone=state["phone_number"],
            )
            if not src_msg:
                return {"forwarded": False, "errors": ["Source message not found among your owned WhatsApp conversations."]}

            resolved = await self._resolve_outbound_target(
                ident,
                state,
                chat_id=params.chat_id,
                prospect_contact_id=str(params.prospect_contact_id) if params.prospect_contact_id else None,
            )
        except AuthError as exc:
            return {"forwarded": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"forwarded": False, "errors": [f"Failed to prepare forward: {exc}"]}

        has_media = bool(src_msg.get("has_media"))
        text_body = str(src_msg.get("full_body") or src_msg.get("body_preview") or "").strip()
        if not has_media and not text_body:
            return {"forwarded": False, "errors": ["Source message has no text or media content to forward."]}

        raw_mfile = str(src_msg.get("media_file") or "").split("/")[-1]
        safe_fname = "forwarded_file.bin"
        if raw_mfile:
            try:
                safe_fname = validate_safe_filename(raw_mfile)
            except ValueError:
                pass
        elif has_media:
            mime_hint = str(src_msg.get("media_mime_type") or "")
            ext_hint = mime_hint.split("/")[-1].split(";")[0] if "/" in mime_hint else "bin"
            safe_fname = f"forwarded.{ext_hint}"

        staged_payload: dict[str, Any] = {
            "source_message_id": str(params.source_message_id),
            "target_chat_id": resolved["chat_id"],
            "prospect_contact_id": str(params.prospect_contact_id) if params.prospect_contact_id else resolved.get("prospect_contact_id"),
            "link_prospect_contact_id": resolved.get("prospect_contact_id"),
            "has_media": has_media,
            "media_mime_type": src_msg.get("media_mime_type"),
            "filename": safe_fname,
            "text_body": text_body,
        }
        token, ttl = self.store.issue(
            ident.key,
            staged_payload,
            op_type="whatsapp_forward",
            entity_type="whatsapp_message",
            record_id=resolved["chat_id"] or str(params.prospect_contact_id),
        )
        return {
            "forwarded": False,
            "requires_confirmation": True,
            "preview": {
                "source_message_id": str(params.source_message_id),
                "source_chat_id": src_conv.get("chat_id") if src_conv else None,
                "to_chat_id": resolved["chat_id"],
                "to_phone": resolved["phone_number"],
                "to_name": resolved["contact_name"] or resolved["phone_number"] or resolved["chat_id"],
                "crm_match": resolved["crm_match"],
                "has_media": has_media,
                "text_body": text_body,
            },
            "confirmation_token": token,
            "token_expires_in_seconds": ttl,
            "next_step": (
                "NO MESSAGE HAS BEEN FORWARDED. Show the message preview and recipient to the user and ask for explicit approval. "
                "Then call forward_whatsapp_message with this confirmation_token and user_has_explicitly_confirmed=true."
            ),
        }

    async def create_whatsapp_group(
        self,
        group_name: str,
        participants: list[str],
        confirmation_token: str | None = None,
        user_has_explicitly_confirmed: bool = False,
    ) -> dict[str, Any]:
        # Phase 2: Confirmed execution
        if confirmation_token and user_has_explicitly_confirmed is True:
            try:
                ident = await self._identity(require_manage=True, refresh_from_api=True)
                op = self.store.consume(confirmation_token, ident.key)
            except ConfirmationError as exc:
                return {"created": False, "outcome": "NOT_CREATED", "errors": [str(exc)]}
            except AuthError as exc:
                return {"created": False, "outcome": "NOT_CREATED", "errors": [f"Authentication/permission error: {exc}"]}

            if op.op_type != "whatsapp_group_create" or op.entity_type != "whatsapp_group":
                return {"created": False, "outcome": "NOT_CREATED", "errors": ["Confirmation token is not for group creation."]}

            try:
                res = await self.client.create_whatsapp_group(op.payload["name"], op.payload["participants"])
            except ApiTimeout as exc:
                self.store.mark(op, "UNCERTAIN")
                return {"created": False, "outcome": "UNCERTAIN", "errors": [f"{exc} Check list_whatsapp_chats first."]}
            except ApiError as exc:
                return {"created": False, "outcome": "NOT_CREATED", "errors": [f"Failed to create WhatsApp group: {exc}"]}

            self.store.mark(op, "CREATED")
            conv_raw = res.get("conversation") if isinstance(res, dict) and isinstance(res.get("conversation"), dict) else None
            return {
                "created": True,
                "outcome": "CONFIRMED_SUCCESS",
                "group": res.get("group") if isinstance(res, dict) else res,
                "conversation": _format_conversation(conv_raw) if conv_raw else None,
            }

        # Phase 1: Validate & stage
        try:
            params = WhatsAppCreateGroupInput.model_validate({
                "group_name": group_name,
                "participants": participants,
            })
        except ValidationError as exc:
            return {"created": False, "requires_confirmation": False, "errors": format_validation_errors(exc)}

        if confirmation_token and user_has_explicitly_confirmed is not True:
            return {
                "created": False,
                "requires_confirmation": True,
                "errors": ["Explicit user confirmation is required (user_has_explicitly_confirmed=true)."],
            }

        try:
            ident = await self._identity(require_manage=True, refresh_from_api=True)
            state = await self._get_verified_session_state(ident, sync_sessions=True)
            if not state["connected"]:
                return {"created": False, "errors": [f"WhatsApp session is not connected (status: {state['status']})."]}
            if not state["outbound_enabled"] or state["safety_stop_active"]:
                return {"created": False, "errors": ["Outbound WhatsApp actions are disabled or safety-stopped."]}
            matched_participants: list[dict[str, Any]] = []
            for p_digits in params.participants:
                m = await self._match_number_in_market_intel_db(phone_number=f"+{p_digits}")
                matched_participants.append({
                    "phone_number": f"+{p_digits}",
                    "contact_name": m.get("contact_name"),
                    "company_name": m.get("company_name"),
                    "prospect_contact_id": m.get("prospect_contact_id"),
                })
        except AuthError as exc:
            return {"created": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"created": False, "errors": [f"Failed to prepare WhatsApp group: {exc}"]}

        payload = {"name": params.group_name, "participants": params.participants}
        token, ttl = self.store.issue(
            ident.key,
            payload,
            op_type="whatsapp_group_create",
            entity_type="whatsapp_group",
            record_id=params.group_name,
        )
        return {
            "created": False,
            "requires_confirmation": True,
            "preview": {
                "group_name": params.group_name,
                "participants": matched_participants,
            },
            "confirmation_token": token,
            "token_expires_in_seconds": ttl,
            "next_step": (
                "NO GROUP HAS BEEN CREATED. Show the group name and participant list to the user for explicit approval. "
                "Then call create_whatsapp_group with this confirmation_token and user_has_explicitly_confirmed=true."
            ),
        }

    async def manage_whatsapp_group_participants(
        self,
        group_chat_id: str,
        action: str,
        participants: list[str],
        confirmation_token: str | None = None,
        user_has_explicitly_confirmed: bool = False,
    ) -> dict[str, Any]:
        # Phase 2: Confirmed execution
        if confirmation_token and user_has_explicitly_confirmed is True:
            try:
                ident = await self._identity(require_manage=True, refresh_from_api=True)
                op = self.store.consume(confirmation_token, ident.key)
            except ConfirmationError as exc:
                return {"updated": False, "outcome": "NOT_UPDATED", "errors": [str(exc)]}
            except AuthError as exc:
                return {"updated": False, "outcome": "NOT_UPDATED", "errors": [f"Authentication/permission error: {exc}"]}

            if op.op_type != "whatsapp_group_participants" or op.entity_type != "whatsapp_group":
                return {"updated": False, "outcome": "NOT_UPDATED", "errors": ["Confirmation token is not for group participant management."]}

            try:
                if op.payload["action"] == "add":
                    res = await self.client.add_whatsapp_group_participants(
                        op.payload["group_chat_id"], op.payload["participants"]
                    )
                else:
                    res = await self.client.remove_whatsapp_group_participants(
                        op.payload["group_chat_id"], op.payload["participants"]
                    )
            except ApiTimeout as exc:
                self.store.mark(op, "UNCERTAIN")
                return {"updated": False, "outcome": "UNCERTAIN", "errors": [f"{exc} Check group details before retrying."]}
            except ApiError as exc:
                return {"updated": False, "outcome": "NOT_UPDATED", "errors": [f"Group update failed: {exc}"]}

            self.store.mark(op, "UPDATED")
            return {
                "updated": True,
                "outcome": "CONFIRMED_SUCCESS",
                "action": op.payload["action"],
                "group_chat_id": op.payload["group_chat_id"],
                "participants": op.payload["participants"],
                "result": res,
            }

        # Phase 1: Validate & stage
        try:
            params = WhatsAppManageGroupParticipantsInput.model_validate({
                "group_chat_id": group_chat_id,
                "action": action,
                "participants": participants,
            })
        except ValidationError as exc:
            return {"updated": False, "requires_confirmation": False, "errors": format_validation_errors(exc)}

        if confirmation_token and user_has_explicitly_confirmed is not True:
            return {
                "updated": False,
                "requires_confirmation": True,
                "errors": ["Explicit user confirmation is required (user_has_explicitly_confirmed=true)."],
            }

        try:
            ident = await self._identity(require_manage=True, refresh_from_api=True)
            state = await self._get_verified_session_state(ident, sync_sessions=True)
            if not state["connected"]:
                return {"updated": False, "errors": [f"WhatsApp session is not connected (status: {state['status']})."]}
            conv = await self._resolve_owned_conversation(
                ident,
                chat_id=params.group_chat_id,
                session_phone=state["phone_number"],
            )
            if not conv:
                return {"updated": False, "errors": ["Target group was not found among your owned WhatsApp conversations."]}
        except AuthError as exc:
            return {"updated": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"updated": False, "errors": [f"Failed to verify WhatsApp group: {exc}"]}

        payload = {
            "group_chat_id": params.group_chat_id,
            "action": params.action,
            "participants": params.participants,
        }
        token, ttl = self.store.issue(
            ident.key,
            payload,
            op_type="whatsapp_group_participants",
            entity_type="whatsapp_group",
            record_id=params.group_chat_id,
        )
        return {
            "updated": False,
            "requires_confirmation": True,
            "preview": {
                "group_chat_id": params.group_chat_id,
                "group_name": conv.get("contact_name") or conv.get("whatsapp_name") or params.group_chat_id,
                "action": params.action,
                "participants": [f"+{p}" for p in params.participants],
            },
            "confirmation_token": token,
            "token_expires_in_seconds": ttl,
            "next_step": (
                "NO GROUP CHANGE HAS BEEN MADE. Show the group and participant change to the user for explicit approval. "
                "Then call manage_whatsapp_group_participants with this confirmation_token and user_has_explicitly_confirmed=true."
            ),
        }

    async def get_whatsapp_group_invite_link(
        self,
        group_chat_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            params = WhatsAppGroupInviteLinkInput.model_validate({
                "group_chat_id": group_chat_id,
                "conversation_id": conversation_id,
            })
        except ValidationError as exc:
            return {"ok": False, "errors": format_validation_errors(exc)}

        try:
            ident = await self._identity(require_manage=False)
            state = await self._get_verified_session_state(ident, sync_sessions=True)
            conv = await self._resolve_owned_conversation(
                ident,
                conversation_id=str(params.conversation_id) if params.conversation_id else None,
                chat_id=params.group_chat_id,
                session_phone=state["phone_number"],
            )
            if not conv:
                return {"ok": False, "errors": ["Target group was not found among your owned WhatsApp chats."]}
            target_jid = str(conv.get("chat_id") or params.group_chat_id or "")
            res = await self.client.get_whatsapp_group_invite_link(target_jid)
            return {
                "ok": True,
                "group_chat_id": target_jid,
                "code": res.get("code") if isinstance(res, dict) else "",
                "invite_link": res.get("link") if isinstance(res, dict) else "",
            }
        except AuthError as exc:
            return {"ok": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"ok": False, "errors": [f"Failed to retrieve group invite link: {exc}"]}

    async def link_whatsapp_conversation_to_contact(
        self,
        conversation_id: str | None = None,
        chat_id: str | None = None,
        phone_number: str | None = None,
        prospect_contact_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            params = WhatsAppLinkCRMInput.model_validate({
                "conversation_id": conversation_id,
                "chat_id": chat_id,
                "phone_number": phone_number,
                "prospect_contact_id": prospect_contact_id,
            })
        except ValidationError as exc:
            return {"ok": False, "errors": format_validation_errors(exc)}

        try:
            ident = await self._identity(require_manage=False)
            state = await self._get_verified_session_state(ident, sync_sessions=True)
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
            contact_id = str(params.prospect_contact_id) if params.prospect_contact_id else None
            match_info: dict[str, Any] = {"matched": False}
            if not contact_id:
                match_info = await self._match_number_in_market_intel_db(
                    phone_number=params.phone_number or conv.get("phone_number"),
                    chat_id=conv.get("chat_id"),
                )
                contact_id = match_info.get("prospect_contact_id")
                if not contact_id:
                    return {
                        "ok": False,
                        "linked": False,
                        "crm_match": match_info,
                        "errors": ["No matching Market Intel Key Person (ProspectContact) was found for this number."],
                    }

            updated = await self.client.link_whatsapp_conversation_crm(target_conv_id, contact_id)
            return {
                "ok": True,
                "linked": True,
                "conversation": _format_conversation(updated) if isinstance(updated, dict) else {"conversation_id": target_conv_id},
                "prospect_contact_id": contact_id,
                "crm_match": match_info,
            }
        except AuthError as exc:
            return {"ok": False, "errors": [f"Authentication/permission error: {exc}"]}
        except ApiError as exc:
            return {"ok": False, "errors": [f"Failed to link conversation to Market Intel contact: {exc}"]}

