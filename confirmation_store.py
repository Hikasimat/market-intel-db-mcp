"""In-memory, single-use, short-lived confirmation tokens bound to payload + identity."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


class ConfirmationError(Exception):
    """Token missing, unknown, expired, consumed, tampered, or for another identity."""


def canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


@dataclass
class StagedOperation:
    identity: str
    payload_json: str          # immutable snapshot of the reviewed payload
    mac: str                   # HMAC over identity + metadata + payload (tamper evidence)
    expires_at: float
    status: str = "PENDING"    # PENDING -> CONSUMED | UNCERTAIN | CREATED | UPDATED
    payload_digest: str = field(default="")
    op_type: str = "create"    # "create" | "update"
    entity_type: str = "prospect"  # "prospect" | "government_entity" | "market_event"
    record_id: str | None = None
    original_snapshot_json: str | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)  # fresh copy every time

    @property
    def original_snapshot(self) -> dict[str, Any] | None:
        return json.loads(self.original_snapshot_json) if self.original_snapshot_json else None


class ConfirmationStore:
    def __init__(self, ttl_seconds: int = 600, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._key = secrets.token_bytes(32)       # per-process; tokens die with the process
        self._ops: dict[str, StagedOperation] = {}
        self._created_digests: set[str] = set()
        self._lock = threading.Lock()

    def _mac(self, identity: str, op_type: str, entity_type: str, record_id: str | None, payload_json: str) -> str:
        msg = f"{identity}\n{op_type}\n{entity_type}\n{record_id or ''}\n{payload_json}"
        return hmac.new(self._key, msg.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _slot(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()  # plaintext token is never stored

    def issue(
        self,
        identity: str,
        payload: dict[str, Any],
        op_type: str = "create",
        entity_type: str = "prospect",
        record_id: str | None = None,
        original_snapshot: dict[str, Any] | None = None,
    ) -> tuple[str, float]:
        token = secrets.token_urlsafe(32)
        pj = canonical(payload)
        snap_json = canonical(original_snapshot) if original_snapshot is not None else None
        mac = self._mac(identity, op_type, entity_type, record_id, pj)
        op = StagedOperation(
            identity=identity,
            payload_json=pj,
            mac=mac,
            expires_at=self._clock() + self._ttl,
            payload_digest=payload_hash(payload),
            op_type=op_type,
            entity_type=entity_type,
            record_id=record_id,
            original_snapshot_json=snap_json,
        )
        with self._lock:
            now = self._clock()
            self._ops = {k: v for k, v in self._ops.items() if v.expires_at > now}
            self._ops[self._slot(token)] = op
        return token, self._ttl

    def consume(self, token: str, identity: str) -> StagedOperation:
        """Atomically claim a token for ONE operation attempt. It can never be claimed again."""
        if not isinstance(token, str) or not token.strip():
            raise ConfirmationError("A confirmation_token is required.")
        with self._lock:
            op = self._ops.get(self._slot(token.strip()))
            if op is None:
                raise ConfirmationError("Unknown confirmation token. Run the validation tool again.")
            if op.status != "PENDING":
                raise ConfirmationError("This confirmation token was already used. No second operation will be executed.")
            if self._clock() >= op.expires_at:
                op.status = "CONSUMED"
                raise ConfirmationError("Confirmation token expired. Run the validation tool again.")
            if op.identity != identity:
                raise ConfirmationError("Token was issued for a different authenticated account.")
            expected_mac = self._mac(op.identity, op.op_type, op.entity_type, op.record_id, op.payload_json)
            if not hmac.compare_digest(op.mac, expected_mac):
                op.status = "CONSUMED"
                raise ConfirmationError("Staged payload failed its integrity check; operation refused.")
            if op.op_type == "create" and op.payload_digest in self._created_digests:
                op.status = "CONSUMED"
                raise ConfirmationError(f"An identical {op.entity_type} was already created in this session.")
            op.status = "CONSUMED"
            return op

    def mark(self, op: StagedOperation, status: str) -> None:
        with self._lock:
            op.status = status
            if status in ("CREATED", "UPDATED", "UNCERTAIN") and op.op_type == "create":
                self._created_digests.add(op.payload_digest)
