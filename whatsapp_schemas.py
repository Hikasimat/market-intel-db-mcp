"""WhatsApp input schemas, phone normalization, chat-ID validation, and cursor helpers."""
from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import PurePosixPath
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

VALID_CHAT_ID_RE = re.compile(
    r"^(?:[0-9a-zA-Z_\-\.]+@(?:c\.us|g\.us|lid|s\.whatsapp\.net)|crm_group_[0-9a-zA-Z_\-]+)$"
)
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ \-]{0,199}$")

ALLOWED_MEDIA_MIME_PREFIXES = ("image/", "audio/", "video/")
ALLOWED_MEDIA_EXACT_MIMES = frozenset({
    "application/pdf",
    "text/plain",
    "text/csv",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/zip",
    "application/octet-stream",
})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _blank_to_none(v: Any) -> Any:
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _strip_idd_prefix(raw: str, digits: str) -> str:
    if not raw.startswith("+") and digits.startswith("00") and len(digits) > 10:
        return digits[2:]
    return digits


def is_lid_identifier(value: str | None) -> bool:
    if not value:
        return False
    raw = str(value).strip()
    if raw.endswith("@lid"):
        return True
    head = raw.split("@")[0].split(":")[0]
    digits = _strip_idd_prefix(head, re.sub(r"\D+", "", head))
    return len(digits) > 13 or digits.startswith(("7658", "2262", "2167"))


def normalize_phone_digits(value: str | None) -> str:
    """Extract normalized E.164-compatible digits (7-15 digits) from phone or JID.

    Handles formatting chars, `00` IDD prefixes, `@c.us`/`@s.whatsapp.net` suffixes,
    and `:device` suffixes. Returns '' for group/LID IDs or invalid lengths.
    """
    if not value:
        return ""
    raw = str(value).strip()
    if not raw or "@g.us" in raw or is_lid_identifier(raw):
        return ""
    if "@" in raw:
        raw = raw.split("@")[0].split(":")[0]
    elif ":" in raw:
        raw = raw.split(":")[0]
    digits = _strip_idd_prefix(raw, re.sub(r"\D+", "", raw))
    if 7 <= len(digits) <= 15:
        return digits
    return ""


def normalize_phone_number(value: str | None) -> Optional[str]:
    """Normalize a phone number or JID to canonical '+<digits>' format."""
    digits = normalize_phone_digits(value)
    return f"+{digits}" if digits else None


def phones_match(phone_a: str | None, phone_b: str | None) -> bool:
    """Safely compare two phone numbers across formatting and national-prefix variations.

    Never returns True on empty, non-phone, or short (<7 digit) inputs.
    """
    da = normalize_phone_digits(phone_a)
    db = normalize_phone_digits(phone_b)
    if not da or not db:
        return False
    if da == db:
        return True
    # Handle national trunk '0' prefix (e.g. 09876543210 vs 919876543210)
    sa = da.lstrip("0")
    sb = db.lstrip("0")
    if sa and sb and sa == sb:
        return True
    # Match 10-digit subscriber tail ONLY if one number is a bare 10-digit local number
    # (or 0 + 10-digit local number) and the other includes a 1-3 digit country code.
    if len(sa) == 10 and 11 <= len(sb) <= 13 and sb.endswith(sa):
        return True
    if len(sb) == 10 and 11 <= len(sa) <= 13 and sa.endswith(sb):
        return True
    return False


def validate_chat_id(chat_id: str) -> str:
    """Validate a WhatsApp chat JID and reject path traversal or control characters."""
    cleaned = (chat_id or "").strip()
    if not cleaned or len(cleaned) > 100:
        raise ValueError("chat_id must be 1-100 characters.")
    if ".." in cleaned or "/" in cleaned or "\\" in cleaned or any(ord(c) < 32 for c in cleaned):
        raise ValueError("chat_id contains invalid characters.")
    # Strip device index if present, e.g. 919876543210:1@s.whatsapp.net -> 919876543210@s.whatsapp.net
    cleaned = re.sub(r":[0-9]+@", "@", cleaned)
    if not VALID_CHAT_ID_RE.match(cleaned):
        raise ValueError("chat_id must be a valid WhatsApp JID (e.g. '919876543210@c.us' or '...g.us').")
    return cleaned


def phone_to_chat_id(phone: str) -> str:
    """Convert a raw phone number into a validated '<digits>@c.us' chat ID."""
    digits = normalize_phone_digits(phone)
    if not digits:
        raise ValueError("phone_number must contain 7-15 digits (e.g. '+919876543210').")
    return validate_chat_id(f"{digits}@c.us")


def resolve_chat_id_input(chat_id: str | None = None, phone_number: str | None = None) -> str:
    """Resolve and validate either a chat_id or a phone_number (or recipient string)."""
    if chat_id and "@" in chat_id:
        return validate_chat_id(chat_id)
    if chat_id and chat_id.startswith("crm_group_"):
        return validate_chat_id(chat_id)
    candidate = phone_number or chat_id or ""
    if not candidate.strip():
        raise ValueError("A valid chat_id or phone_number is required.")
    return phone_to_chat_id(candidate)


def validate_safe_filename(filename: str) -> str:
    base = PurePosixPath((filename or "").replace("\\", "/")).name.strip()
    if not base or base in (".", "..") or not SAFE_FILENAME_RE.match(base):
        raise ValueError("filename must be a safe basename (1-200 alphanumeric/dot/dash/underscore/space chars).")
    return base


def validate_media_mime(mime_type: str) -> str:
    mime = (mime_type or "").split(";")[0].strip().lower()
    if not mime or "/" not in mime:
        raise ValueError("mime_type must be a valid MIME type (e.g. 'image/png', 'application/pdf').")
    if mime.startswith(ALLOWED_MEDIA_MIME_PREFIXES) or mime in ALLOWED_MEDIA_EXACT_MIMES:
        return mime
    raise ValueError(f"Unsupported or disallowed mime_type: '{mime}'.")


def decode_base64_media(file_base64: str, max_bytes: int) -> bytes:
    raw = (file_base64 or "").strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1].strip()
    if not raw:
        raise ValueError("file_base64 cannot be empty.")
    try:
        content = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("file_base64 is not valid base64 data.") from None
    if not content:
        raise ValueError("Decoded media file cannot be empty.")
    if len(content) > max_bytes:
        raise ValueError(f"Media file exceeds maximum allowed size ({max_bytes} bytes).")
    return content


def encode_cursor(offset: int, order: str = "asc", scope: str = "") -> str:
    payload = json.dumps({"o": offset, "d": order, "s": scope}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None, expected_order: str | None = None, expected_scope: str = "") -> int:
    if not cursor:
        return 0
    raw = cursor.strip()
    if not raw:
        return 0
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid cursor format.")
        offset = int(data["o"])
        if offset < 0:
            raise ValueError("Cursor offset cannot be negative.")
        if expected_order and data.get("d") != expected_order:
            raise ValueError("Cursor order does not match requested sort order.")
        if expected_scope and data.get("s") != expected_scope:
            raise ValueError("Cursor does not belong to this conversation or query.")
        return offset
    except (binascii.Error, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid pagination cursor: {exc}") from None


# ---------------------------------------------------------------------------
# Strict Pydantic Input Models
# ---------------------------------------------------------------------------

class WhatsAppConnectInput(_Strict):
    wait_for_connection: bool = False
    timeout_seconds: int = Field(default=30, ge=1, le=60)
    force_restart: bool = False


class WhatsAppQRInput(_Strict):
    force_restart: bool = False


class WhatsAppListChatsInput(_Strict):
    search: Optional[str] = Field(default=None, max_length=200)
    is_group: Optional[bool] = None
    matched_only: bool = False
    limit: int = Field(default=25, ge=1, le=100)
    cursor: Optional[str] = Field(default=None, max_length=256)

    _search = field_validator("search", mode="before")(_blank_to_none)
    _cursor = field_validator("cursor", mode="before")(_blank_to_none)


class WhatsAppChatHistoryInput(_Strict):
    conversation_id: Optional[UUID] = None
    chat_id: Optional[str] = Field(default=None, max_length=100)
    phone_number: Optional[str] = Field(default=None, max_length=40)
    page_size: int = Field(default=50, ge=1, le=200)
    cursor: Optional[str] = Field(default=None, max_length=256)
    order: Literal["asc", "desc"] = "asc"

    _chat_id = field_validator("chat_id", mode="before")(_blank_to_none)
    _phone = field_validator("phone_number", mode="before")(_blank_to_none)
    _cursor = field_validator("cursor", mode="before")(_blank_to_none)

    @field_validator("chat_id")
    @classmethod
    def _val_chat_id(cls, v: Optional[str]) -> Optional[str]:
        return validate_chat_id(v) if v is not None else None

    @field_validator("phone_number")
    @classmethod
    def _val_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not normalize_phone_digits(v):
            raise ValueError("must be a valid phone number (7-15 digits).")
        return normalize_phone_number(v)

    @model_validator(mode="after")
    def _require_target(self):
        if not (self.conversation_id or self.chat_id or self.phone_number):
            raise ValueError("Provide at least one of conversation_id, chat_id, or phone_number.")
        return self


class WhatsAppContactDetailsInput(_Strict):
    chat_id: Optional[str] = Field(default=None, max_length=100)
    phone_number: Optional[str] = Field(default=None, max_length=40)
    conversation_id: Optional[UUID] = None

    _chat_id = field_validator("chat_id", mode="before")(_blank_to_none)
    _phone = field_validator("phone_number", mode="before")(_blank_to_none)

    @field_validator("chat_id")
    @classmethod
    def _val_chat_id(cls, v: Optional[str]) -> Optional[str]:
        return validate_chat_id(v) if v is not None else None

    @field_validator("phone_number")
    @classmethod
    def _val_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not normalize_phone_digits(v):
            raise ValueError("must be a valid phone number (7-15 digits).")
        return normalize_phone_number(v)

    @model_validator(mode="after")
    def _require_target(self):
        if not (self.chat_id or self.phone_number or self.conversation_id):
            raise ValueError("Provide at least one of chat_id, phone_number, or conversation_id.")
        return self


class WhatsAppSearchMessagesInput(_Strict):
    query: str = Field(min_length=1, max_length=200)
    conversation_id: Optional[UUID] = None
    chat_id: Optional[str] = Field(default=None, max_length=100)
    direction: Optional[Literal["INBOUND", "OUTBOUND"]] = None
    has_media: Optional[bool] = None
    limit: int = Field(default=25, ge=1, le=100)
    cursor: Optional[str] = Field(default=None, max_length=256)

    _chat_id = field_validator("chat_id", mode="before")(_blank_to_none)
    _cursor = field_validator("cursor", mode="before")(_blank_to_none)

    @field_validator("chat_id")
    @classmethod
    def _val_chat_id(cls, v: Optional[str]) -> Optional[str]:
        return validate_chat_id(v) if v is not None else None


class WhatsAppSendMessageInput(_Strict):
    recipient: Optional[str] = Field(default=None, max_length=100)
    chat_id: Optional[str] = Field(default=None, max_length=100)
    prospect_contact_id: Optional[UUID] = None
    message_body: str = Field(min_length=1, max_length=4096)
    reply_to: Optional[str] = Field(default=None, max_length=255)

    _recipient = field_validator("recipient", mode="before")(_blank_to_none)
    _chat_id = field_validator("chat_id", mode="before")(_blank_to_none)
    _reply_to = field_validator("reply_to", mode="before")(_blank_to_none)

    @field_validator("chat_id")
    @classmethod
    def _val_chat_id(cls, v: Optional[str]) -> Optional[str]:
        return validate_chat_id(v) if v is not None else None

    @field_validator("reply_to")
    @classmethod
    def _val_reply_to(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if any(c.isspace() or ord(c) < 32 for c in v):
            raise ValueError("reply_to must not contain whitespace or control characters.")
        return v

    @model_validator(mode="after")
    def _validate_target(self):
        if not (self.recipient or self.chat_id or self.prospect_contact_id):
            raise ValueError("Provide recipient (phone number or chat JID), chat_id, or prospect_contact_id.")
        if self.recipient and not self.chat_id:
            self.chat_id = resolve_chat_id_input(chat_id=self.recipient)
        return self


class WhatsAppSendMediaInput(_Strict):
    recipient: Optional[str] = Field(default=None, max_length=100)
    chat_id: Optional[str] = Field(default=None, max_length=100)
    filename: str = Field(min_length=1, max_length=200)
    mime_type: str = Field(min_length=3, max_length=120)
    file_base64: str = Field(min_length=4)
    caption: str = Field(default="", max_length=4096)

    _recipient = field_validator("recipient", mode="before")(_blank_to_none)
    _chat_id = field_validator("chat_id", mode="before")(_blank_to_none)

    @field_validator("chat_id")
    @classmethod
    def _val_chat_id(cls, v: Optional[str]) -> Optional[str]:
        return validate_chat_id(v) if v is not None else None

    @field_validator("filename")
    @classmethod
    def _val_filename(cls, v: str) -> str:
        return validate_safe_filename(v)

    @field_validator("mime_type")
    @classmethod
    def _val_mime(cls, v: str) -> str:
        return validate_media_mime(v)

    @model_validator(mode="after")
    def _validate_target(self):
        if not (self.recipient or self.chat_id):
            raise ValueError("Provide recipient (phone number or chat JID) or chat_id.")
        if self.recipient and not self.chat_id:
            self.chat_id = resolve_chat_id_input(chat_id=self.recipient)
        return self


class WhatsAppMarkReadInput(_Strict):
    conversation_id: Optional[UUID] = None
    chat_id: Optional[str] = Field(default=None, max_length=100)
    phone_number: Optional[str] = Field(default=None, max_length=40)

    _chat_id = field_validator("chat_id", mode="before")(_blank_to_none)
    _phone = field_validator("phone_number", mode="before")(_blank_to_none)

    @field_validator("chat_id")
    @classmethod
    def _val_chat_id(cls, v: Optional[str]) -> Optional[str]:
        return validate_chat_id(v) if v is not None else None

    @field_validator("phone_number")
    @classmethod
    def _val_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not normalize_phone_digits(v):
            raise ValueError("must be a valid phone number (7-15 digits).")
        return normalize_phone_number(v)

    @model_validator(mode="after")
    def _require_target(self):
        if not (self.conversation_id or self.chat_id or self.phone_number):
            raise ValueError("Provide at least one of conversation_id, chat_id, or phone_number.")
        return self


def format_validation_errors(exc: ValidationError, root_name: str = "input") -> list[str]:
    errors: list[str] = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"]) or root_name
        msg = e["msg"].removeprefix("Value error, ")
        if e["type"] == "missing":
            msg = "is required"
        elif e["type"] == "extra_forbidden":
            msg = "is not a supported field"
        elif e["type"] == "literal_error":
            msg = f"has an invalid value ({e['msg'].removeprefix('Input should be ')})"
        errors.append(f"{loc}: {msg}")
    return errors

