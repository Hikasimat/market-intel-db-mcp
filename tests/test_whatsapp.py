"""Comprehensive automated tests for Market Intel WhatsApp MCP integration."""
from __future__ import annotations

import asyncio
import base64
import copy
import logging
import uuid
from typing import Any

import httpx
import pytest
import respx
from starlette.requests import Request

from api_client import ApiError, ApiTimeout, AuthError, Identity, MarketIntelClient
from config import Settings
from confirmation_store import ConfirmationStore
from login_views import create_login_handler
from oauth_store import OAuthStore
from server import create_mcp
from whatsapp_schemas import (
    normalize_phone_digits,
    normalize_phone_number,
    phones_match,
    validate_chat_id,
)
from whatsapp_workflow import SlidingWindowRateLimiter, StagedMediaStore, WhatsAppWorkflow
from workflow import Workflow

BASE = "https://api.test/api"


class FakeWhatsAppClient:
    """In-memory fake of MarketIntelClient's WhatsApp + auth surface for isolated tests."""

    def __init__(
        self,
        *,
        user_id: str = "10",
        username: str = "pre_alice",
        role: str = "PRE",
        is_superuser: bool = False,
        session_name: str = "wa_u10_abcdef123456",
        phone_number: str | None = "+919876543210",
        status: str = "READY",
    ):
        self.identity: Identity | None = None
        self.user_id = user_id
        self.username = username
        self.role = role
        self.is_superuser = is_superuser
        self.session_name = session_name
        self.owner_username = username
        self.phone_number = phone_number
        self.status = status
        self.outbound_enabled = True
        self.safety_stop_active = False
        self.qr_responses: list[dict[str, Any]] = []
        self.status_sequence: list[dict[str, Any]] = []
        self.conversations: list[dict[str, Any]] = []
        self.messages_by_conv: dict[str, list[dict[str, Any]]] = {}
        self.contact_info_by_chat: dict[str, dict[str, Any]] = {}
        self.crm_contacts: list[dict[str, Any]] = []
        self.prospects: list[dict[str, Any]] = []
        self.media_bytes_by_msg: dict[str, tuple[bytes, str]] = {}
        self.pending_sync_conversations: list[dict[str, Any]] = []
        self.pending_sync_messages: dict[str, list[dict[str, Any]]] = {}
        self.waha_status_override: str | None = None
        self.send_exc: Exception | None = None
        self.send_media_exc: Exception | None = None
        self.status_exc: Exception | None = None
        self.calls: list[tuple[Any, ...]] = []

    async def authenticate(self) -> Identity:
        if self.role != "PRE":
            raise AuthError(f"Account role is {self.role!r}; only PRE accounts are permitted.")
        self.identity = Identity(self.user_id, self.username, self.role, is_superuser=self.is_superuser)
        return self.identity

    async def authenticate_whatsapp(self, allowed_roles=None) -> Identity:
        roles = set(allowed_roles or ("PRE", "LQ", "MANAGER", "SUPERUSER"))
        if self.role not in roles and not (self.is_superuser and "SUPERUSER" in roles):
            raise AuthError(f"Account role is {self.role!r}; not permitted for this WhatsApp operation.")
        sessions = (
            {"id": 1, "session_name": self.session_name, "status": self.status, "phone_number": self.phone_number},
        )
        self.identity = Identity(
            self.user_id,
            self.username,
            self.role,
            is_superuser=self.is_superuser,
            whatsapp_sessions=sessions,
        )
        return self.identity

    async def get_whatsapp_status(self) -> dict[str, Any]:
        self.calls.append(("get_whatsapp_status",))
        if self.status_exc:
            raise self.status_exc
        if self.status_sequence:
            nxt = self.status_sequence.pop(0)
            self.status = nxt.get("status", self.status)
            if "phone_number" in nxt:
                self.phone_number = nxt["phone_number"]
        out = {
            "session": {
                "id": 1,
                "session_name": self.session_name,
                "status": self.status,
                "phone_number": self.phone_number,
                "owner_username": self.owner_username,
                "is_active": True,
            },
            "historical_sessions": [],
            "status": self.status,
            "phone_number": self.phone_number,
            "config": {
                "outbound_enabled": self.outbound_enabled,
                "safety_stop_active": self.safety_stop_active,
            },
        }
        if self.waha_status_override:
            out["waha_status"] = {"name": self.session_name, "status": self.waha_status_override}
        return out

    async def list_whatsapp_sessions(self) -> list[dict[str, Any]]:
        self.calls.append(("list_whatsapp_sessions",))
        return [{
            "id": 1,
            "session_name": self.session_name,
            "status": self.status,
            "phone_number": self.phone_number,
            "owner_username": self.owner_username,
            "is_active": True,
        }]

    async def start_whatsapp_session(self, session_name: str | None = None) -> dict[str, Any]:
        self.calls.append(("start_whatsapp_session", session_name))
        if self.status in ("DISCONNECTED", "STOPPED", "FAILED"):
            self.status = "STARTING"
        return {"name": session_name or self.session_name, "status": self.status}

    async def logout_whatsapp_session(self, session_name: str | None = None) -> dict[str, Any]:
        self.calls.append(("logout_whatsapp_session", session_name))
        self.status = "DISCONNECTED"
        return {"status": "DISCONNECTED", "detail": "Logged out cleanly."}

    async def get_whatsapp_qr(self, force_restart: bool = False) -> dict[str, Any]:
        self.calls.append(("get_whatsapp_qr", force_restart))
        if self.qr_responses:
            return copy.deepcopy(self.qr_responses.pop(0))
        if self.status == "READY":
            return {
                "status": "WORKING",
                "connected": True,
                "session_name": self.session_name,
                "phone_number": self.phone_number,
            }
        return {
            "status": "SCAN_QR_CODE",
            "connected": False,
            "session_name": self.session_name,
            "phone_number": self.phone_number,
            "qr": "raw-secret-qr-token",
            "qrBase64": "data:image/png;base64,ZmFrZV9xcl9pbWFnZQ==",
        }

    async def list_whatsapp_conversations(self) -> list[dict[str, Any]]:
        self.calls.append(("list_whatsapp_conversations",))
        return copy.deepcopy(self.conversations)

    async def get_whatsapp_conversation_messages(self, conversation_id: str, limit: int = 2000) -> dict[str, Any]:
        self.calls.append(("get_whatsapp_conversation_messages", conversation_id, limit))
        conv = next((c for c in self.conversations if str(c.get("id")) == str(conversation_id)), None)
        if not conv:
            raise ApiError("Conversation not found or not owned by you.", status=404)
        msgs = copy.deepcopy(self.messages_by_conv.get(str(conversation_id), []))
        return {"conversation": copy.deepcopy(conv), "messages": msgs[:limit]}

    async def mark_whatsapp_conversation_read(self, conversation_id: str) -> dict[str, Any]:
        self.calls.append(("mark_whatsapp_conversation_read", conversation_id))
        conv = next((c for c in self.conversations if str(c.get("id")) == str(conversation_id)), None)
        if not conv:
            raise ApiError("Conversation not found.", status=404)
        conv["unread_count"] = 0
        return {"detail": "Marked as read."}

    async def get_whatsapp_contact_info(self, chat_id: str) -> dict[str, Any]:
        self.calls.append(("get_whatsapp_contact_info", chat_id))
        if chat_id in self.contact_info_by_chat:
            return copy.deepcopy(self.contact_info_by_chat[chat_id])
        return {
            "chatId": chat_id,
            "phoneNumber": normalize_phone_number(chat_id) or "",
            "name": "Contact",
            "company": "",
            "contact_name": "",
            "designation": "",
            "official_email": "",
            "is_matched": False,
            "prospect_id": None,
            "prospect_contact_id": None,
            "is_group": chat_id.endswith("@g.us"),
            "members": [],
        }

    async def list_whatsapp_crm_contacts(self, search: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_whatsapp_crm_contacts", search))
        if not search:
            return copy.deepcopy(self.crm_contacts)
        q = search.casefold()
        return [
            copy.deepcopy(c)
            for c in self.crm_contacts
            if any(q in str(c.get(k) or "").casefold() for k in ("contact_name", "phone_number", "company_name", "official_email"))
        ]

    async def list_prospects(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("list_prospects", params))
        q = str((params or {}).get("search") or "").casefold()
        if not q:
            return {"results": copy.deepcopy(self.prospects)}
        out = []
        for p in self.prospects:
            hay = " ".join(str(p.get(k) or "") for k in ("company_name", "official_phone_number", "official_email_address"))
            kc_phones = " ".join(str(kc.get("phone_number") or "") for kc in p.get("key_contacts", []))
            if q in (hay + " " + kc_phones).casefold():
                out.append(copy.deepcopy(p))
        return {"results": out}

    async def sync_whatsapp_history(
        self, days: int = 2, chat_id: str | None = None, session_name: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("sync_whatsapp_history", days, chat_id, session_name))
        synced = 0
        for conv in self.pending_sync_conversations:
            if not any(existing.get("id") == conv.get("id") for existing in self.conversations):
                self.conversations.append(copy.deepcopy(conv))
                synced += 1
        for cid, msgs in self.pending_sync_messages.items():
            target_list = self.messages_by_conv.setdefault(cid, [])
            for m in msgs:
                if not any(ex.get("id") == m.get("id") for ex in target_list):
                    target_list.append(copy.deepcopy(m))
                    synced += 1
        return {"detail": f"Synchronized {synced} messages.", "synced": synced, "days": days}

    async def sync_whatsapp_conversation(self, conversation_id: str, days: int = 2) -> dict[str, Any]:
        self.calls.append(("sync_whatsapp_conversation", conversation_id, days))
        synced = 0
        if conversation_id in self.pending_sync_messages:
            target_list = self.messages_by_conv.setdefault(conversation_id, [])
            for m in self.pending_sync_messages[conversation_id]:
                if not any(ex.get("id") == m.get("id") for ex in target_list):
                    target_list.append(copy.deepcopy(m))
                    synced += 1
        return {"detail": f"Synced {synced} messages.", "synced": synced, "days": days}

    async def link_whatsapp_conversation_crm(
        self, conversation_id: str, prospect_contact_id: str | None
    ) -> dict[str, Any]:
        self.calls.append(("link_whatsapp_conversation_crm", conversation_id, prospect_contact_id))
        contact = next((c for c in self.crm_contacts if str(c.get("id")) == str(prospect_contact_id)), None)
        for conv in self.conversations:
            if str(conv.get("id")) == str(conversation_id):
                conv["prospect_contact"] = prospect_contact_id
                conv["is_matched"] = bool(prospect_contact_id)
                if contact:
                    conv["contact_name"] = contact.get("contact_name") or conv.get("contact_name")
                    conv["prospect_name"] = contact.get("company_name")
                    conv["prospect_id"] = contact.get("prospect_id")
                return copy.deepcopy(conv)
        raise ApiError("Conversation not found.", 404)

    async def edit_whatsapp_message(self, message_id: str, text: str) -> dict[str, Any]:
        self.calls.append(("edit_whatsapp_message", message_id, text))
        for msgs in self.messages_by_conv.values():
            for m in msgs:
                if str(m.get("id")) == str(message_id):
                    m["full_body"] = text
                    m["body_preview"] = text[:200]
                    m["is_edited"] = True
                    return copy.deepcopy(m)
        raise ApiError("Message not found.", 404)

    async def create_whatsapp_group(self, name: str, participants: list[str]) -> dict[str, Any]:
        self.calls.append(("create_whatsapp_group", name, list(participants)))
        group_jid = f"120363{len(self.conversations) + 1}@g.us"
        conv_id = str(uuid.uuid4())
        conv = {
            "id": conv_id,
            "chat_id": group_jid,
            "is_group": True,
            "is_matched": True,
            "contact_name": name,
            "whatsapp_name": name,
            "account_phone": self.phone_number,
            "unread_count": 0,
            "last_message_at": "2026-11-17T09:00:00Z",
            "last_message_preview": f"Group created: {name}",
            "last_message_from_me": True,
        }
        self.conversations.append(conv)
        return {"group": {"id": group_jid, "name": name, "participants": participants}, "conversation": copy.deepcopy(conv)}

    async def add_whatsapp_group_participants(self, chat_id: str, participants: list[str]) -> dict[str, Any]:
        self.calls.append(("add_whatsapp_group_participants", chat_id, list(participants)))
        return {"status": "success", "added": participants}

    async def remove_whatsapp_group_participants(self, chat_id: str, participants: list[str]) -> dict[str, Any]:
        self.calls.append(("remove_whatsapp_group_participants", chat_id, list(participants)))
        return {"status": "success", "removed": participants}

    async def get_whatsapp_group_invite_link(self, chat_id: str) -> dict[str, Any]:
        self.calls.append(("get_whatsapp_group_invite_link", chat_id))
        return {"code": "AbCdEf123", "link": "https://chat.whatsapp.com/AbCdEf123"}

    async def download_whatsapp_media(self, message_id: str) -> tuple[bytes, str]:
        self.calls.append(("download_whatsapp_media", message_id))
        if message_id in self.media_bytes_by_msg:
            return self.media_bytes_by_msg[message_id]
        raise ApiError("Media not found.", 404)

    async def send_whatsapp_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("send_whatsapp_message", copy.deepcopy(payload)))
        if self.send_exc:
            raise self.send_exc
        msg_id = str(uuid.uuid4())
        chat_id = payload.get("chat_id")
        contact_id = payload.get("prospect_contact_id")
        matched_contact = next((c for c in self.crm_contacts if str(c.get("id")) == str(contact_id)), None) if contact_id else None
        if not chat_id and matched_contact and matched_contact.get("phone_number"):
            digits = normalize_phone_digits(matched_contact["phone_number"])
            chat_id = f"{digits}@c.us"
        conv = next(
            (c for c in self.conversations if (chat_id and c.get("chat_id") == chat_id) or (contact_id and str(c.get("prospect_contact")) == str(contact_id))),
            None,
        )
        if not conv and chat_id:
            conv = {
                "id": str(uuid.uuid4()),
                "chat_id": chat_id,
                "phone_number": normalize_phone_number(chat_id),
                "is_group": chat_id.endswith("@g.us"),
                "is_matched": bool(matched_contact),
                "prospect_contact": str(matched_contact["id"]) if matched_contact else None,
                "prospect_id": matched_contact.get("prospect_id") if matched_contact else None,
                "prospect_name": matched_contact.get("company_name") if matched_contact else None,
                "contact_name": matched_contact.get("contact_name") if matched_contact else normalize_phone_number(chat_id),
                "whatsapp_name": matched_contact.get("contact_name") if matched_contact else "",
                "account_phone": self.phone_number,
                "unread_count": 0,
                "last_message_at": "2026-11-16T12:00:00Z",
                "last_message_preview": payload["message_body"][:200],
                "last_message_from_me": True,
            }
            self.conversations.append(conv)
        msg_obj = {
            "id": msg_id,
            "waha_message_id": f"true_{chat_id or 'x'}_MSG1",
            "direction": "OUTBOUND",
            "status": "SENT",
            "body_preview": payload["message_body"][:200],
            "full_body": payload["message_body"],
            "message_type": "chat",
            "performed_by_name": self.username,
            "waha_timestamp": "2026-11-16T12:00:00Z",
            "sent_at": "2026-11-16T12:00:00Z",
            "created_at": "2026-11-16T12:00:00Z",
            "has_media": False,
            "is_edited": False,
            "reply_context": {"id": payload["reply_to"]} if payload.get("reply_to") else None,
        }
        if conv:
            self.messages_by_conv.setdefault(conv["id"], []).append(copy.deepcopy(msg_obj))
        return msg_obj

    async def send_whatsapp_media(
        self, chat_id: str, filename: str, content: bytes, mime_type: str, caption: str = ""
    ) -> dict[str, Any]:
        self.calls.append(("send_whatsapp_media", chat_id, filename, len(content), mime_type, caption))
        if self.send_media_exc:
            raise self.send_media_exc
        return {
            "id": str(uuid.uuid4()),
            "waha_message_id": f"true_{chat_id}_MEDIA1",
            "direction": "OUTBOUND",
            "status": "SENT",
            "body_preview": caption[:200] if caption else f"[{mime_type}]",
            "full_body": caption,
            "message_type": "image" if mime_type.startswith("image/") else "document",
            "has_media": True,
            "media_mime_type": mime_type,
            "sent_at": "2026-11-16T12:05:00Z",
            "created_at": "2026-11-16T12:05:00Z",
        }


@pytest.fixture
def wa_settings():
    return Settings(
        api_base_url=BASE,
        username="pre_alice",
        password="secret-pw",
        whatsapp_connect_poll_interval=0.05,
        whatsapp_rate_limit_per_minute=5,
    )


def _make_wa(client: FakeWhatsAppClient, settings: Settings, clock=None):
    c_fn = clock if clock is not None else (lambda: 1000.0)
    store = ConfirmationStore(600, c_fn)
    limiter = SlidingWindowRateLimiter(settings.whatsapp_rate_limit_per_minute, c_fn)
    media_store = StagedMediaStore(600, clock=c_fn)

    async def _no_sleep(_dt: float) -> None:
        return None

    return WhatsAppWorkflow(
        client,
        store,
        settings,
        rate_limiter=limiter,
        media_store=media_store,
        sleep_fn=_no_sleep,
        clock=c_fn,
    )


# ---------------------------------------------------------------------------
# 1. Connection initiation, QR retrieval, polling auth, disconnect, reconnect
# ---------------------------------------------------------------------------
async def test_whatsapp_connection_initiation_and_qr_retrieval(wa_settings, caplog):
    caplog.set_level(logging.DEBUG)
    c = FakeWhatsAppClient(status="DISCONNECTED", phone_number=None)
    wa = _make_wa(c, wa_settings)

    res = await wa.connect_whatsapp(wait_for_connection=False)
    assert res["ok"] is True
    assert res["connected"] is False
    assert res["qr_required"] is True
    assert res["qr_base64"].startswith("data:image/png;base64,")
    assert ("start_whatsapp_session", "wa_u10_abcdef123456") in c.calls

    # Direct QR retrieval
    qr = await wa.get_whatsapp_qr_code()
    assert qr["ok"] is True
    assert qr["status"] == "SCAN_QR_CODE"
    assert "qr_base64" in qr

    # QR secrets must NEVER appear in logs
    assert "ZmFrZV9xcl9pbWFnZQ==" not in caplog.text
    assert "raw-secret-qr-token" not in caplog.text


async def test_whatsapp_polling_until_authenticated(wa_settings):
    t = [0.0]

    def clk():
        t[0] += 0.5
        return t[0]

    c = FakeWhatsAppClient(status="DISCONNECTED", phone_number=None)
    c.status_sequence = [
        {"status": "DISCONNECTED", "phone_number": None},
        {"status": "QR_REQUIRED", "phone_number": None},
        {"status": "READY", "phone_number": "+91 98765-43210"},
    ]
    c.qr_responses = [
        {"status": "SCAN_QR_CODE", "connected": False, "qrBase64": "data:image/png;base64,AAA="},
        {"status": "SCAN_QR_CODE", "connected": False, "qrBase64": "data:image/png;base64,AAA="},
    ]
    wa = _make_wa(c, wa_settings, clock=clk)

    res = await wa.connect_whatsapp(wait_for_connection=True, timeout_seconds=10)
    assert res["ok"] is True
    assert res["connected"] is True
    assert res["phone_number"] == "+919876543210"
    assert res["user_id"] == "10"
    assert res["identity_matched"] is True


async def test_whatsapp_disconnect_requires_confirmation_and_reconnects(wa_settings):
    c = FakeWhatsAppClient(status="READY", phone_number="+919876543210")
    wa = _make_wa(c, wa_settings)

    # Phase 1: requires confirmation
    stage = await wa.disconnect_whatsapp()
    assert stage["disconnected"] is False
    assert stage["requires_confirmation"] is True
    tok = stage["confirmation_token"]

    # Refused when user_has_explicitly_confirmed=False
    refused = await wa.disconnect_whatsapp(confirmation_token=tok, user_has_explicitly_confirmed=False)
    assert refused["disconnected"] is False

    # Phase 2: confirmed disconnect
    done = await wa.disconnect_whatsapp(confirmation_token=tok, user_has_explicitly_confirmed=True)
    assert done["disconnected"] is True
    assert done["history_preserved"] is True

    status_after = await wa.get_whatsapp_connection_status()
    assert status_after["connected"] is False
    assert status_after["status"] == "DISCONNECTED"

    # Reconnect same number
    c.status_sequence = [
        {"status": "DISCONNECTED", "phone_number": "+919876543210"},
        {"status": "READY", "phone_number": "+919876543210"},
    ]
    c.qr_responses = [
        {"status": "WORKING", "connected": True, "phone_number": "+919876543210"},
    ]
    reconn = await wa.connect_whatsapp()
    assert reconn["ok"] is True
    assert reconn["connected"] is True
    assert reconn["phone_number"] == "+919876543210"


# ---------------------------------------------------------------------------
# 2. Phone-number normalization & identity matching
# ---------------------------------------------------------------------------
def test_phone_normalization_and_equivalence():
    assert normalize_phone_digits("+91 (98765)-43210") == "919876543210"
    assert normalize_phone_digits("0091 9876543210") == "919876543210"
    assert normalize_phone_digits("919876543210:2@s.whatsapp.net") == "919876543210"
    assert normalize_phone_digits("226212345678901@lid") == ""
    assert normalize_phone_digits("12036304123456@g.us") == ""
    assert normalize_phone_number("919876543210@c.us") == "+919876543210"

    assert phones_match("+91 98765-43210", "919876543210@c.us") is True
    assert phones_match("00919876543210", "+919876543210") is True
    assert phones_match("09876543210", "+919876543210") is True
    assert phones_match("9876543210", "+919876543210") is True
    assert phones_match("+919876543210", "+919999999999") is False
    assert phones_match("12345", "12345") is False  # too short


# ---------------------------------------------------------------------------
# 3. Conversations, full paginated history, contact details, search, mark read
# ---------------------------------------------------------------------------
async def test_conversation_list_paginated_history_and_search(wa_settings):
    conv1_id = str(uuid.uuid4())
    conv2_id = str(uuid.uuid4())
    c = FakeWhatsAppClient(status="READY", phone_number="+919876543210")
    c.conversations = [
        {
            "id": conv1_id,
            "chat_id": "919000011111@c.us",
            "is_group": False,
            "phone_number": "+919000011111",
            "is_matched": True,
            "contact_name": "Ravi Menon",
            "whatsapp_name": "Ravi",
            "prospect_name": "Acme Solar",
            "prospect_id": str(uuid.uuid4()),
            "prospect_contact": str(uuid.uuid4()),
            "account_phone": "+919876543210",
            "unread_count": 2,
            "last_message_at": "2026-11-16T10:05:00Z",
            "last_message_preview": "Msg 5",
            "last_message_from_me": False,
        },
        {
            "id": conv2_id,
            "chat_id": "12036304123456@g.us",
            "is_group": True,
            "phone_number": None,
            "is_matched": True,
            "contact_name": "Project Alpha Group",
            "whatsapp_name": "Project Alpha Group",
            "account_phone": "+919876543210",
            "unread_count": 0,
            "last_message_at": "2026-11-16T09:00:00Z",
            "last_message_preview": "Invoice attached",
            "last_message_from_me": True,
        },
    ]
    c.messages_by_conv[conv1_id] = [
        {
            "id": f"m-{i}",
            "waha_message_id": f"waha-{i}",
            "direction": "INBOUND" if i % 2 else "OUTBOUND",
            "status": "READ" if i % 2 else "DELIVERED",
            "body_preview": f"Message number {i}",
            "full_body": f"Message number {i} full text",
            "message_type": "image" if i == 3 else "chat",
            "waha_timestamp": f"2026-11-16T10:0{i}:00Z",
            "created_at": f"2026-11-16T10:0{i}:00Z",
            "has_media": i == 3,
            "media_mime_type": "image/png" if i == 3 else None,
            "reply_context": {"id": "waha-1", "body": "Message number 1", "sender": "Ravi", "from_me": False} if i == 4 else None,
        }
        for i in range(1, 6)
    ]
    c.contact_info_by_chat["919000011111@c.us"] = {
        "chatId": "919000011111@c.us",
        "phoneNumber": "+919000011111",
        "name": "Ravi Menon",
        "contact_name": "Ravi Menon",
        "company": "Acme Solar",
        "designation": "VP Procurement",
        "official_email": "ravi@acmesolar.example",
        "is_matched": True,
        "prospect_id": c.conversations[0]["prospect_id"],
        "prospect_contact_id": c.conversations[0]["prospect_contact"],
        "is_group": False,
        "members": [],
    }
    wa = _make_wa(c, wa_settings)

    # List chats with filter + pagination
    chats_p1 = await wa.list_whatsapp_chats(limit=1)
    assert chats_p1["ok"] is True
    assert len(chats_p1["chats"]) == 1
    assert chats_p1["pagination"]["has_more"] is True

    chats_p2 = await wa.list_whatsapp_chats(limit=1, cursor=chats_p1["pagination"]["next_cursor"])
    assert chats_p2["ok"] is True
    assert chats_p2["chats"][0]["is_group"] is True

    # Paginated complete history retrieval (2 + 2 + 1 = 5 messages)
    collected = []
    cursor = None
    while True:
        hist = await wa.get_whatsapp_chat_history(
            phone_number="+91 90000 11111",
            page_size=2,
            cursor=cursor,
            order="asc",
        )
        assert hist["ok"] is True
        assert hist["conversation"]["contact_name"] == "Ravi Menon"
        collected.extend(hist["messages"])
        if not hist["pagination"]["has_more"]:
            break
        cursor = hist["pagination"]["next_cursor"]

    assert [m["id"] for m in collected] == ["m-1", "m-2", "m-3", "m-4", "m-5"]
    assert collected[2]["has_media"] is True and collected[2]["media_mime_type"] == "image/png"
    assert collected[3]["reply_context"]["id"] == "waha-1"

    # Contact details
    details = await wa.get_whatsapp_contact_details(phone_number="+919000011111")
    assert details["ok"] is True
    assert details["company"] == "Acme Solar"
    assert details["designation"] == "VP Procurement"

    # Search messages
    search = await wa.search_whatsapp_messages(query="number 3", has_media=True)
    assert search["ok"] is True
    assert len(search["matches"]) == 1
    assert search["matches"][0]["message"]["id"] == "m-3"

    # Mark chat read
    read_res = await wa.mark_whatsapp_chat_read(conversation_id=conv1_id)
    assert read_res["ok"] is True
    assert c.conversations[0]["unread_count"] == 0


# ---------------------------------------------------------------------------
# 4. Sending text & media messages, timeouts, and confirmations
# ---------------------------------------------------------------------------
async def test_send_text_message_with_confirmation_and_timeout(wa_settings):
    c = FakeWhatsAppClient(status="READY", phone_number="+919876543210")
    wa = _make_wa(c, wa_settings)

    # Step 1: Preview + confirmation token (nothing sent yet)
    staged = await wa.send_whatsapp_message(recipient="+91 91234-56789", message_body="Hello")
    assert staged["sent"] is False
    assert staged["requires_confirmation"] is True
    assert staged["preview"]["to_chat_id"] == "919123456789@c.us"
    assert len([x for x in c.calls if x[0] == "send_whatsapp_message"]) == 0

    tok = staged["confirmation_token"]

    # Step 2: Confirm and send
    sent = await wa.send_whatsapp_message(confirmation_token=tok, user_has_explicitly_confirmed=True)
    assert sent["sent"] is True
    assert sent["outcome"] == "CONFIRMED_SUCCESS"
    assert sent["message_id"]
    assert sent["waha_message_id"]

    # Token reuse blocked
    reused = await wa.send_whatsapp_message(confirmation_token=tok, user_has_explicitly_confirmed=True)
    assert reused["sent"] is False

    # Timeout -> UNCERTAIN, never retried
    staged2 = await wa.send_whatsapp_message(recipient="+919123456789", message_body="Second")
    c.send_exc = ApiTimeout("ReadTimeout")
    uncertain = await wa.send_whatsapp_message(
        confirmation_token=staged2["confirmation_token"], user_has_explicitly_confirmed=True
    )
    assert uncertain["sent"] is False
    assert uncertain["outcome"] == "UNCERTAIN"


async def test_send_media_with_confirmation_and_validation(wa_settings):
    c = FakeWhatsAppClient(status="READY", phone_number="+919876543210")
    wa = _make_wa(c, wa_settings)

    b64 = base64.b64encode(b"%PDF-1.4 fake pdf bytes").decode("ascii")

    # Bad mime rejected
    bad_mime = await wa.send_whatsapp_media(
        recipient="+919123456789",
        filename="evil.exe",
        mime_type="application/x-msdownload",
        file_base64=b64,
    )
    assert bad_mime["sent"] is False

    # Valid staging
    staged = await wa.send_whatsapp_media(
        recipient="+919123456789",
        filename="brochure.pdf",
        mime_type="application/pdf",
        file_base64=b64,
        caption="Please review",
    )
    assert staged["requires_confirmation"] is True
    tok = staged["confirmation_token"]

    sent = await wa.send_whatsapp_media(confirmation_token=tok, user_has_explicitly_confirmed=True)
    assert sent["sent"] is True
    assert sent["outcome"] == "CONFIRMED_SUCCESS"
    assert sent["media_mime_type"] == "application/pdf"


# ---------------------------------------------------------------------------
# 5. Security: Unauthorized roles, cross-user isolation, invalid IDs, rate limit
# ---------------------------------------------------------------------------
async def test_role_isolation_and_cross_user_protection(wa_settings):
    # MANAGER can read WhatsApp, but CANNOT send/connect/disconnect
    mgr_client = FakeWhatsAppClient(user_id="20", username="mgr_bob", role="MANAGER")
    mgr_wa = _make_wa(mgr_client, wa_settings)
    assert (await mgr_wa.get_whatsapp_connection_status())["ok"] is True
    assert (await mgr_wa.list_whatsapp_chats())["ok"] is True
    send_blocked = await mgr_wa.send_whatsapp_message(recipient="+919123456789", message_body="Hi")
    assert send_blocked["sent"] is False and "not permitted" in send_blocked["errors"][0]
    conn_blocked = await mgr_wa.connect_whatsapp()
    assert conn_blocked["ok"] is False

    # Unknown / ANALYST role blocked from all WhatsApp operations
    analyst = FakeWhatsAppClient(user_id="21", username="ana", role="ANALYST")
    ana_wa = _make_wa(analyst, wa_settings)
    assert (await ana_wa.get_whatsapp_connection_status())["ok"] is False

    # LQ allowed WhatsApp, but STILL blocked from Prospect Workflow!
    lq_client = FakeWhatsAppClient(user_id="22", username="lq_sam", role="LQ")
    lq_wa = _make_wa(lq_client, wa_settings)
    assert (await lq_wa.connect_whatsapp())["ok"] is True
    lq_prospect_wf = Workflow(lq_client, ConfirmationStore(600), wa_settings)
    prospect_res = await lq_prospect_wf.search("Acme")
    assert prospect_res["ok"] is False and "PRE" in prospect_res["errors"][0]


async def test_cross_user_session_and_token_isolation(wa_settings):
    shared_store = ConfirmationStore(600)
    shared_limiter = SlidingWindowRateLimiter(5)
    shared_media = StagedMediaStore(600)

    c1 = FakeWhatsAppClient(user_id="1", username="alice", phone_number="+911111111111", session_name="wa_u1")
    c2 = FakeWhatsAppClient(user_id="2", username="bob", phone_number="+912222222222", session_name="wa_u2")
    c1.conversations = [{"id": str(uuid.uuid4()), "chat_id": "919999900001@c.us", "account_phone": "+911111111111"}]
    c2.conversations = [{"id": str(uuid.uuid4()), "chat_id": "919999900002@c.us", "account_phone": "+912222222222"}]

    wa1 = WhatsAppWorkflow(c1, shared_store, wa_settings, rate_limiter=shared_limiter, media_store=shared_media)
    wa2 = WhatsAppWorkflow(c2, shared_store, wa_settings, rate_limiter=shared_limiter, media_store=shared_media)

    # User 1 stages a message; User 2 tries to use User 1's confirmation token -> rejected!
    staged = await wa1.send_whatsapp_message(recipient="+919999900001", message_body="Private to Alice")
    hijack = await wa2.send_whatsapp_message(
        confirmation_token=staged["confirmation_token"],
        user_has_explicitly_confirmed=True,
    )
    assert hijack["sent"] is False
    assert "different authenticated account" in hijack["errors"][0]

    # Backend returning a session owned by another user is rejected
    c2.owner_username = "alice"  # mismatch for bob!
    mismatch = await wa2.get_whatsapp_connection_status()
    assert mismatch["ok"] is False
    assert "ownership mismatch" in mismatch["errors"][0].lower()

    # Cross-user conversation account_phone mismatch is filtered out
    c2.owner_username = "bob"
    c2.conversations.append({"id": str(uuid.uuid4()), "chat_id": "919999900001@c.us", "account_phone": "+911111111111"})
    bob_chats = await wa2.list_whatsapp_chats()
    assert all(ch["chat_id"] != "919999900001@c.us" for ch in bob_chats["chats"])


async def test_invalid_chat_ids_and_rate_limiting(wa_settings):
    for bad in ("", "../users/", "abc/def@c.us", "not-a-jid", "12345"):
        with pytest.raises(ValueError):
            validate_chat_id(bad)

    c = FakeWhatsAppClient(status="READY", phone_number="+919876543210")
    wa = _make_wa(c, wa_settings)

    hist_bad = await wa.get_whatsapp_chat_history(chat_id="../secret@c.us")
    assert hist_bad["ok"] is False

    # Rate limit: max_per_minute=5 in wa_settings
    for i in range(5):
        s = await wa.send_whatsapp_message(recipient="+919123456789", message_body=f"msg {i}")
        r = await wa.send_whatsapp_message(confirmation_token=s["confirmation_token"], user_has_explicitly_confirmed=True)
        assert r["sent"] is True

    s6 = await wa.send_whatsapp_message(recipient="+919123456789", message_body="msg 6")
    r6 = await wa.send_whatsapp_message(confirmation_token=s6["confirmation_token"], user_has_explicitly_confirmed=True)
    assert r6["sent"] is False and r6["outcome"] == "RATE_LIMITED"


# ---------------------------------------------------------------------------
# 6. Expired JWT, WAHA/API failure, Client Disconnection, Simultaneous Users
# ---------------------------------------------------------------------------
@respx.mock
async def test_expired_jwt_refreshes_and_token_only_never_falls_back(wa_settings):
    refreshed_calls = []

    async def on_refresh(new_acc, new_ref=None):
        refreshed_calls.append((new_acc, new_ref))

    client = MarketIntelClient(
        wa_settings,
        access_token="expired-jwt",
        refresh_token="valid-refresh",
        identity=Identity("10", "pre_alice", "PRE"),
        on_token_refreshed=on_refresh,
    )

    respx.get(f"{BASE}/whatsapp/status/").mock(side_effect=[
        httpx.Response(401, json={"detail": "Token expired"}),
        httpx.Response(200, json={"status": "READY", "phone_number": "+919876543210", "session": {"session_name": "wa_u10", "owner_username": "pre_alice"}}),
    ])
    respx.post(f"{BASE}/auth/refresh/").mock(
        return_value=httpx.Response(200, json={"access": "fresh-jwt", "refresh": "fresh-ref"})
    )

    data = await client.get_whatsapp_status()
    assert data["status"] == "READY"
    assert refreshed_calls == [("fresh-jwt", "fresh-ref")]

    # When refresh also fails, token-only OAuth client MUST NOT fall back to settings.username login
    login_route = respx.post(f"{BASE}/auth/login/").mock(return_value=httpx.Response(200, json={"access": "global-acc"}))
    respx.get(f"{BASE}/whatsapp/status/").mock(return_value=httpx.Response(401, json={}))
    respx.post(f"{BASE}/auth/refresh/").mock(return_value=httpx.Response(401, json={}))

    with pytest.raises(AuthError, match="Session expired"):
        await client.get_whatsapp_status()
    assert login_route.call_count == 0
    await client.aclose()


async def test_waha_api_failure_handled_gracefully(wa_settings):
    c = FakeWhatsAppClient(status="READY")
    c.status_exc = ApiError("WAHA gateway unavailable", status=503)
    wa = _make_wa(c, wa_settings)
    res = await wa.get_whatsapp_connection_status()
    assert res["ok"] is False
    assert "503" in res["errors"][0] or "WAHA" in res["errors"][0]


async def test_mcp_client_disconnection_cancels_polling_cleanly(wa_settings):
    c = FakeWhatsAppClient(status="DISCONNECTED", phone_number=None)
    entered_sleep = asyncio.Event()

    async def slow_sleep(_dt: float):
        entered_sleep.set()
        await asyncio.sleep(10)

    wa = WhatsAppWorkflow(c, ConfirmationStore(600), wa_settings, sleep_fn=slow_sleep)
    task = asyncio.create_task(wa.connect_whatsapp(wait_for_connection=True, timeout_seconds=30))
    await entered_sleep.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_multiple_simultaneous_oauth_users_via_mcp_server(wa_settings):
    c_alice = FakeWhatsAppClient(user_id="1", username="alice", phone_number="+911111111111", session_name="wa_u1")
    c_bob = FakeWhatsAppClient(user_id="2", username="bob", phone_number="+912222222222", session_name="wa_u2")
    c_alice.conversations = [{"id": str(uuid.uuid4()), "chat_id": "910000000001@c.us", "whatsapp_name": "Alice Lead", "account_phone": "+911111111111"}]
    c_bob.conversations = [{"id": str(uuid.uuid4()), "chat_id": "910000000002@c.us", "whatsapp_name": "Bob Lead", "account_phone": "+912222222222"}]

    active_user = ["alice"]
    store = ConfirmationStore(600)
    limiter = SlidingWindowRateLimiter(20)
    media = StagedMediaStore(600)

    def get_wf():
        return Workflow(c_alice if active_user[0] == "alice" else c_bob, store, wa_settings)

    def get_wa():
        return WhatsAppWorkflow(
            c_alice if active_user[0] == "alice" else c_bob,
            store,
            wa_settings,
            rate_limiter=limiter,
            media_store=media,
        )

    mcp = create_mcp(get_wf, get_whatsapp_workflow=get_wa)

    def unwrap(res):
        s = res.structured_content
        assert not res.is_error, res
        return s.get("result", s) if set(s) == {"result"} else s

    active_user[0] = "alice"
    alice_chats = unwrap(await mcp.call_tool("list_whatsapp_chats", {}))
    assert alice_chats["session_phone_number"] == "+911111111111"
    assert [ch["whatsapp_name"] for ch in alice_chats["chats"]] == ["Alice Lead"]

    active_user[0] = "bob"
    bob_chats = unwrap(await mcp.call_tool("list_whatsapp_chats", {}))
    assert bob_chats["session_phone_number"] == "+912222222222"
    assert [ch["whatsapp_name"] for ch in bob_chats["chats"]] == ["Bob Lead"]


@pytest.mark.asyncio
async def test_login_allows_additional_whatsapp_roles_only_when_explicitly_configured(tmp_path):
    store = OAuthStore(tmp_path / "oauth_roles.db")
    store.save_auth_session(
        session_id="sess_lq",
        client_id="cid",
        redirect_uri="https://claude.ai/callback",
        state="st",
        code_challenge="chal",
        scopes=[],
        resource=None,
    )
    settings_with_lq = Settings(
        api_base_url=BASE,
        username="svc",
        password="pw",
        oauth_enabled=True,
        oauth_issuer_url="https://mcp.example.com",
        whatsapp_allowed_roles=("PRE", "LQ"),
    )
    handler = create_login_handler(store, settings_with_lq)

    with respx.mock(base_url=BASE) as rm:
        rm.post("/auth/login/").respond(200, json={"access": "acc_lq", "refresh": "ref_lq"})
        rm.get("/auth/me/").respond(200, json={"id": 99, "username": "lq_user", "role": "LQ"})

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "query_string": b"",
        }
        async def receive():
            return {"type": "http.request", "body": b"session_id=sess_lq&username=lq_user&password=pw"}

        resp = await handler(Request(scope, receive=receive))
        assert resp.status_code == 302
        code = resp.headers["location"].split("code=")[1].split("&")[0]
        stored = store.get_auth_code(code)
        assert stored is not None
        assert stored.role == "LQ"


@respx.mock
async def test_http_client_whatsapp_endpoints_and_multipart_media(wa_settings):
    client = MarketIntelClient(wa_settings)
    respx.post(f"{BASE}/auth/login/").mock(return_value=httpx.Response(200, json={"access": "jwt-1"}))
    respx.get(f"{BASE}/auth/me/").mock(return_value=httpx.Response(
        200, json={"id": 7, "username": "pre_alice", "role": "PRE", "is_superuser": False}
    ))
    send_route = respx.post(f"{BASE}/whatsapp/send/").mock(return_value=httpx.Response(
        201, json={"id": "msg-uuid-1", "waha_message_id": "waha-1", "status": "SENT"}
    ))
    media_route = respx.post(f"{BASE}/whatsapp/send-media/").mock(return_value=httpx.Response(
        200, json={"id": "msg-uuid-2", "waha_message_id": "waha-2", "status": "SENT", "media_mime_type": "image/png"}
    ))

    ident = await client.authenticate_whatsapp()
    assert ident.user_id == "7" and ident.role == "PRE"

    txt_res = await client.send_whatsapp_message({"chat_id": "919876543210@c.us", "message_body": "Hi"})
    assert txt_res["id"] == "msg-uuid-1"
    assert send_route.call_count == 1

    med_res = await client.send_whatsapp_media("919876543210@c.us", "pic.png", b"\x89PNG", "image/png", "Look")
    assert med_res["id"] == "msg-uuid-2"
    assert media_route.call_count == 1
    assert b"multipart/form-data" in media_route.calls[0].request.headers["content-type"].encode()
    assert b"pic.png" in media_route.calls[0].request.content
    await client.aclose()


async def test_hosted_qr_url_and_http_view_handlers():
    from starlette.requests import Request
    from server import TokenAuthMiddleware
    from whatsapp_qr_views import create_qr_image_handler, create_qr_page_handler
    from whatsapp_workflow import EphemeralQRStore, parse_qr_image_data

    # Unsafe SVG data URI must never be accepted for hosted QR serving
    assert parse_qr_image_data("data:image/svg+xml;base64,PHN2Zy8+") is None

    now = [1000.0]
    qr_store = EphemeralQRStore(ttl_seconds=120, clock=lambda: now[0])
    settings = Settings(
        api_base_url=BASE,
        username="pre_alice",
        password="pw",
        oauth_enabled=True,
        oauth_issuer_url="https://mcp.example.com",
    )
    c = FakeWhatsAppClient(session_name="sess_<xss>", phone_number=None, status="SCAN_QR_CODE")
    c.status_sequence = [
        {"status": "SCAN_QR_CODE", "phone_number": None},
        {"status": "READY", "phone_number": "+919876500000"},
    ]
    wa = WhatsAppWorkflow(
        c,
        ConfirmationStore(),
        settings,
        clock=lambda: now[0],
        qr_store=qr_store,
        qr_public_base_url="https://mcp.example.com",
    )

    res = await wa.connect_whatsapp(wait_for_connection=False)
    assert res["ok"] is True
    assert res["qr_required"] is True
    assert res["qr_omitted"] is True
    assert "qr_base64" not in res
    assert "qr" not in res
    assert res["qr_expires_in_seconds"] == 120
    assert res["qr_url"].startswith("https://mcp.example.com/whatsapp/qr/")
    token = res["qr_url"].rsplit("/", 1)[-1]

    page_handler = create_qr_page_handler(qr_store)
    img_handler = create_qr_image_handler(qr_store)

    page_req = Request({"type": "http", "method": "GET", "path": f"/whatsapp/qr/{token}", "path_params": {"token": token}, "headers": [], "query_string": b""})
    page_resp = await page_handler(page_req)
    assert page_resp.status_code == 200
    assert page_resp.headers["cache-control"].startswith("no-store")
    assert page_resp.headers["x-content-type-options"] == "nosniff"
    body_text = page_resp.body.decode("utf-8")
    assert f"/whatsapp/qr/{token}/image" in body_text
    assert "sess_&lt;xss&gt;" in body_text
    assert "<xss>" not in body_text

    img_req = Request({"type": "http", "method": "GET", "path": f"/whatsapp/qr/{token}/image", "path_params": {"token": token}, "headers": [], "query_string": b""})
    img_resp = await img_handler(img_req)
    assert img_resp.status_code == 200
    assert img_resp.media_type == "image/png"
    assert img_resp.body == b"fake_qr_image"

    # Also test direct get_whatsapp_qr_code returns qr_url
    qr_res = await wa.get_whatsapp_qr_code()
    assert qr_res["ok"] is True
    assert qr_res["qr_url"].startswith("https://mcp.example.com/whatsapp/qr/")
    assert "qr_base64" not in qr_res
    token2 = qr_res["qr_url"].rsplit("/", 1)[-1]

    # Once WhatsApp connects, outstanding QR tokens for that user are purged immediately
    status_res = await wa.get_whatsapp_connection_status()
    assert status_res["connected"] is True
    assert qr_store.get(token) is None
    assert qr_store.get(token2) is None

    expired_page = await page_handler(page_req)
    assert expired_page.status_code == 410
    expired_img = await img_handler(img_req)
    assert expired_img.status_code == 410

    # Verify TTL expiry cleanup
    token3, _ = qr_store.put("user-1", "alice", "sess_1", b"pngbytes", "image/png")
    assert qr_store.get(token3) is not None
    now[0] += 121.0
    assert qr_store.get(token3) is None

    # Verify TokenAuthMiddleware allows GET /whatsapp/qr/... without MCP_AUTH_TOKEN, blocks /mcp and POST
    async def dummy_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = TokenAuthMiddleware(dummy_app, "secret123")

    async def run_mw(method: str, path: str) -> int:
        status_holder: list[int] = []
        async def send(msg):
            if msg["type"] == "http.response.start":
                status_holder.append(msg["status"])
        async def receive():
            return {"type": "http.request", "body": b""}
        await mw({"type": "http", "method": method, "path": path, "headers": [], "query_string": b""}, receive, send)
        return status_holder[0]

    assert await run_mw("GET", f"/whatsapp/qr/{token}") == 200
    assert await run_mw("POST", f"/whatsapp/qr/{token}") == 401
    assert await run_mw("GET", "/mcp") == 401


async def test_disconnect_works_when_db_status_stale_but_waha_working(wa_settings):
    c = FakeWhatsAppClient(status="DISCONNECTED")
    c.waha_status_override = "WORKING"
    wa = _make_wa(c, wa_settings)

    stage = await wa.disconnect_whatsapp()
    assert stage["requires_confirmation"] is True
    assert ("list_whatsapp_sessions",) in c.calls

    done = await wa.disconnect_whatsapp(confirmation_token=stage["confirmation_token"], user_has_explicitly_confirmed=True)
    assert done["disconnected"] is True


async def test_incoming_hi_from_2356897845_syncs_matches_crm_and_replies_with_reply_to(wa_settings):
    conv_id = str(uuid.uuid4())
    in_msg_id = str(uuid.uuid4())
    contact_id = str(uuid.uuid4())
    prospect_id = str(uuid.uuid4())

    c = FakeWhatsAppClient(status="READY")
    c.crm_contacts = [
        {
            "id": contact_id,
            "contact_name": "Kiran Rao",
            "designation": "Head of Operations",
            "phone_number": "+91 23568-97845",
            "official_email": "kiran@vortexen.example",
            "company_name": "Vortexen Corp",
            "prospect_id": prospect_id,
        }
    ]
    c.prospects = [
        {
            "id": prospect_id,
            "company_name": "Vortexen Corp",
            "country_head_office": "India",
            "primary_industries": "Renewable Energy",
            "official_phone_number": "+91 23568-97845",
            "official_email_address": "info@vortexen.example",
            "operational_status": "Active",
        }
    ]
    # Simulate an incoming message waiting in WAHA sync buffer (not in DB yet until synced)
    c.pending_sync_conversations = [
        {
            "id": conv_id,
            "chat_id": "912356897845@c.us",
            "phone_number": "+912356897845",
            "is_group": False,
            "is_matched": False,
            "prospect_contact": None,
            "contact_name": "+912356897845",
            "whatsapp_name": "Kiran",
            "account_phone": "+919876543210",
            "unread_count": 1,
            "last_message_at": "2026-11-17T08:30:00Z",
            "last_message_preview": "Hi",
            "last_message_from_me": False,
        }
    ]
    c.pending_sync_messages[conv_id] = [
        {
            "id": in_msg_id,
            "waha_message_id": "false_912356897845@c.us_INBOUND_HI",
            "direction": "INBOUND",
            "status": "RECEIVED",
            "body_preview": "Hi",
            "full_body": "Hi",
            "message_type": "chat",
            "waha_timestamp": "2026-11-17T08:30:00Z",
            "created_at": "2026-11-17T08:30:00Z",
            "has_media": False,
            "is_edited": False,
        }
    ]
    wa = _make_wa(c, wa_settings)

    incoming = await wa.get_whatsapp_incoming_messages(phone_number="2356897845")
    assert incoming["ok"] is True
    assert len(incoming["conversations"]) == 1
    entry = incoming["conversations"][0]
    assert entry["latest_inbound_message"]["body"] == "Hi"
    assert entry["crm_match"]["matched"] is True
    assert entry["crm_match"]["contact_name"] == "Kiran Rao"
    assert entry["crm_match"]["company_name"] == "Vortexen Corp"
    # Auto-linked in Market Intel DB
    assert entry["conversation"]["is_matched"] is True
    assert entry["conversation"]["prospect_contact_id"] == contact_id
    assert entry["reply_hint"]["reply_to"] == "false_912356897845@c.us_INBOUND_HI"

    # Reply referencing the DB message UUID -> resolves to waha_message_id and preserves reply_to!
    staged = await wa.send_whatsapp_message(
        recipient="2356897845",
        message_body="Hi Kiran! Happy to help you with Vortexen Corp details.",
        reply_to=in_msg_id,
    )
    assert staged["requires_confirmation"] is True
    assert staged["preview"]["reply_to"] == "false_912356897845@c.us_INBOUND_HI"
    assert staged["preview"]["crm_match"]["contact_name"] == "Kiran Rao"

    sent = await wa.send_whatsapp_message(
        confirmation_token=staged["confirmation_token"],
        user_has_explicitly_confirmed=True,
    )
    assert sent["sent"] is True
    assert sent["message"]["reply_context"]["id"] == "false_912356897845@c.us_INBOUND_HI"
    # Check sent payload did NOT pass prospect_contact_id when reply_to is set (so Django sends quoted reply)
    send_calls = [call[1] for call in c.calls if call[0] == "send_whatsapp_message"]
    assert send_calls[-1]["reply_to"] == "false_912356897845@c.us_INBOUND_HI"
    assert "prospect_contact_id" not in send_calls[-1]


async def test_new_number_first_message_checks_market_intel_db_and_persists_conversation(wa_settings):
    contact_id = str(uuid.uuid4())
    prospect_id = str(uuid.uuid4())
    c = FakeWhatsAppClient(status="READY")
    c.crm_contacts = [
        {
            "id": contact_id,
            "contact_name": "Meera Nair",
            "designation": "Director",
            "phone_number": "+91 23568-97845",
            "official_email": "meera@acme.example",
            "company_name": "Acme Solar",
            "prospect_id": prospect_id,
        }
    ]
    wa = _make_wa(c, wa_settings)

    staged = await wa.send_whatsapp_message(
        recipient="2356897845",
        message_body="Hello Meera, following up from Market Intel.",
    )
    assert staged["requires_confirmation"] is True
    assert staged["preview"]["prospect_contact_id"] == contact_id
    assert staged["preview"]["crm_match"]["company_name"] == "Acme Solar"

    sent = await wa.send_whatsapp_message(
        confirmation_token=staged["confirmation_token"],
        user_has_explicitly_confirmed=True,
    )
    assert sent["sent"] is True
    assert sent["conversation"]["is_matched"] is True
    assert sent["conversation"]["prospect_contact_id"] == contact_id
    # Subsequent history read finds the new persisted conversation and message
    hist = await wa.get_whatsapp_chat_history(phone_number="2356897845")
    assert hist["ok"] is True
    assert hist["latest_message"]["body"] == "Hello Meera, following up from Market Intel."


async def test_edit_forward_and_group_actions_and_delete_blocked(wa_settings):
    from api_client import EndpointNotAllowed, MarketIntelClient

    conv1_id = str(uuid.uuid4())
    out_msg_id = str(uuid.uuid4())
    in_msg_id = str(uuid.uuid4())
    media_msg_id = str(uuid.uuid4())
    c = FakeWhatsAppClient(status="READY")
    c.conversations = [
        {
            "id": conv1_id,
            "chat_id": "919000011111@c.us",
            "phone_number": "+919000011111",
            "is_group": False,
            "is_matched": False,
            "contact_name": "Ravi",
            "account_phone": "+919876543210",
            "last_message_at": "2026-11-17T08:00:00Z",
        }
    ]
    c.messages_by_conv[conv1_id] = [
        {
            "id": out_msg_id,
            "waha_message_id": "true_919000011111@c.us_OUT1",
            "direction": "OUTBOUND",
            "status": "SENT",
            "body_preview": "Draft proposal",
            "full_body": "Draft proposal",
            "waha_timestamp": "2026-11-17T08:00:00Z",
            "created_at": "2026-11-17T08:00:00Z",
            "has_media": False,
        },
        {
            "id": in_msg_id,
            "waha_message_id": "false_919000011111@c.us_IN1",
            "direction": "INBOUND",
            "status": "RECEIVED",
            "body_preview": "Looks good!",
            "full_body": "Looks good!",
            "waha_timestamp": "2026-11-17T08:05:00Z",
            "created_at": "2026-11-17T08:05:00Z",
            "has_media": False,
        },
        {
            "id": media_msg_id,
            "waha_message_id": "false_919000011111@c.us_MED1",
            "direction": "INBOUND",
            "status": "RECEIVED",
            "body_preview": "Brochure",
            "full_body": "Brochure",
            "waha_timestamp": "2026-11-17T08:06:00Z",
            "created_at": "2026-11-17T08:06:00Z",
            "has_media": True,
            "media_mime_type": "application/pdf",
            "media_file": "media/brochure.pdf",
        },
    ]
    c.media_bytes_by_msg[media_msg_id] = (b"%PDF-1.4 fake pdf", "application/pdf")
    wa = _make_wa(c, wa_settings)

    # 1. Edit outbound message (confirmed); reject editing inbound messages
    bad_edit = await wa.edit_whatsapp_message(in_msg_id, "Tamper")
    assert bad_edit["edited"] is False and "outbound" in bad_edit["errors"][0].lower()

    edit_stage = await wa.edit_whatsapp_message(out_msg_id, "Final proposal")
    assert edit_stage["requires_confirmation"] is True
    assert edit_stage["preview"]["before_text"] == "Draft proposal"
    assert edit_stage["preview"]["after_text"] == "Final proposal"
    edit_done = await wa.edit_whatsapp_message(
        out_msg_id, "Final proposal",
        confirmation_token=edit_stage["confirmation_token"],
        user_has_explicitly_confirmed=True,
    )
    assert edit_done["edited"] is True and edit_done["message"]["body"] == "Final proposal"

    # 2. Forward text message AND media message
    fwd_txt_stage = await wa.forward_whatsapp_message(
        source_message_id=in_msg_id,
        source_conversation_id=conv1_id,
        recipient="919888877777",
    )
    assert fwd_txt_stage["requires_confirmation"] is True
    fwd_txt_done = await wa.forward_whatsapp_message(
        source_message_id=in_msg_id,
        confirmation_token=fwd_txt_stage["confirmation_token"],
        user_has_explicitly_confirmed=True,
    )
    assert fwd_txt_done["forwarded"] is True and fwd_txt_done["message"]["body"] == "Looks good!"

    fwd_med_stage = await wa.forward_whatsapp_message(
        source_message_id=media_msg_id,
        source_conversation_id=conv1_id,
        recipient="919888877777",
    )
    assert fwd_med_stage["requires_confirmation"] is True and fwd_med_stage["preview"]["has_media"] is True
    fwd_med_done = await wa.forward_whatsapp_message(
        source_message_id=media_msg_id,
        confirmation_token=fwd_med_stage["confirmation_token"],
        user_has_explicitly_confirmed=True,
    )
    assert fwd_med_done["forwarded"] is True
    assert any(call[0] == "send_whatsapp_media" and call[2] == "brochure.pdf" for call in c.calls)

    # 3. Create group, add/remove participants, invite link
    grp_stage = await wa.create_whatsapp_group("Sales Ops", ["+919000011111", "919888877777"])
    assert grp_stage["requires_confirmation"] is True
    grp_done = await wa.create_whatsapp_group(
        "Sales Ops", ["+919000011111"],
        confirmation_token=grp_stage["confirmation_token"],
        user_has_explicitly_confirmed=True,
    )
    assert grp_done["created"] is True
    group_jid = grp_done["conversation"]["chat_id"]

    add_stage = await wa.manage_whatsapp_group_participants(group_jid, "add", ["+919111122222"])
    add_done = await wa.manage_whatsapp_group_participants(
        group_jid, "add", ["+919111122222"],
        confirmation_token=add_stage["confirmation_token"],
        user_has_explicitly_confirmed=True,
    )
    assert add_done["updated"] is True and add_done["action"] == "add"

    rem_stage = await wa.manage_whatsapp_group_participants(group_jid, "remove", ["+919111122222"])
    rem_done = await wa.manage_whatsapp_group_participants(
        group_jid, "remove", ["+919111122222"],
        confirmation_token=rem_stage["confirmation_token"],
        user_has_explicitly_confirmed=True,
    )
    assert rem_done["updated"] is True and rem_done["action"] == "remove"

    inv = await wa.get_whatsapp_group_invite_link(group_chat_id=group_jid)
    assert inv["ok"] is True and inv["invite_link"].startswith("https://chat.whatsapp.com/")

    # 4. Sync history tool & strict NO DELETE enforcement
    sync_res = await wa.sync_whatsapp_history(days=2)
    assert sync_res["ok"] is True

    raw_client = MarketIntelClient(wa_settings, access_token="tok")
    with pytest.raises(EndpointNotAllowed):
        await raw_client._send("DELETE", f"/whatsapp/conversations/{conv1_id}/")
    with pytest.raises(EndpointNotAllowed):
        await raw_client._send("DELETE", f"/whatsapp/messages/{out_msg_id}/")
    await raw_client.aclose()


@respx.mock
async def test_email_login_canonical_subject_and_incoming_messages_identity_refresh(wa_settings, tmp_path):
    from oauth_store import OAuthStore

    # 1. OAuth login with email stores canonical Django username in subject
    store = OAuthStore(str(tmp_path / "oauth_email.db"))
    store.save_auth_session("sess_email", "cid_1", "http://localhost/cb", "st1", "ch1", ["read"], None)
    handler = create_login_handler(store, wa_settings)

    respx.post(f"{BASE}/auth/login/").mock(return_value=httpx.Response(200, json={"access": "acc-email"}))
    respx.get(f"{BASE}/auth/me/").mock(
        return_value=httpx.Response(200, json={"id": 42, "username": "MohanRaj", "role": "PRE", "is_superuser": False})
    )

    class _PostReq:
        method = "POST"
        query_params = {}
        async def form(self):
            return {"session_id": "sess_email", "username": "mohanrajvmvm221023@gmail.com", "password": "pw"}

    login_resp = await handler(_PostReq())
    assert login_resp.status_code == 302
    loc = login_resp.headers["location"]
    code = loc.split("code=")[1].split("&")[0]
    auth_code = store.get_auth_code(code)
    assert auth_code is not None
    assert auth_code.subject == "42:MohanRaj"

    # 2. Stale cached Identity with email in username refreshes dynamically on incoming messages
    client = MarketIntelClient(
        wa_settings,
        access_token="acc-email",
        identity=Identity("42", "mohanrajvmvm221023@gmail.com", "PRE"),
    )
    respx.get(f"{BASE}/whatsapp/status/").mock(return_value=httpx.Response(200, json={
        "status": "READY",
        "phone_number": "+919003619221",
        "session": {"session_name": "wa_u42_abc", "owner_username": "MohanRaj", "status": "READY"},
    }))
    respx.get(f"{BASE}/whatsapp/session/list/").mock(return_value=httpx.Response(200, json=[
        {"id": 1, "session_name": "wa_u42_abc", "status": "READY", "phone_number": "+919003619221", "owner_username": "MohanRaj"}
    ]))
    respx.get(f"{BASE}/auth/me/").mock(return_value=httpx.Response(200, json={
        "id": 42, "username": "MohanRaj", "role": "PRE", "is_superuser": False,
        "whatsapp_sessions": [{"id": 1, "session_name": "wa_u42_abc", "status": "READY", "phone_number": "+919003619221"}],
    }))
    respx.post(f"{BASE}/whatsapp/sync-history/").mock(return_value=httpx.Response(200, json={"synced": 0}))
    respx.get(f"{BASE}/whatsapp/conversations/").mock(return_value=httpx.Response(200, json=[]))

    wa = _make_wa(client, wa_settings)
    res = await wa.get_whatsapp_incoming_messages(sync=False)
    assert res["ok"] is True
    assert client.identity.username == "MohanRaj"
    await client.aclose()




