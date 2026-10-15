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
    mac: str                   # HMAC over identity + payload (tamper evidence)
    expires_at: float
    status: str = "PENDING"    # PENDING -> CONSUMED | UNCERTAIN | CREATED
    payload_digest: str = field(default="")

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)  # fresh copy every time


class ConfirmationStore:
    def __init__(self, ttl_seconds: int = 600, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._key = secrets.token_bytes(32)       # per-process; tokens die with the process
        self._ops: dict[str, StagedOperation] = {}
        self._created_digests: set[str] = set()
        self._lock = threading.Lock()

    def _mac(self, identity: str, payload_json: str) -> str:
        return hmac.new(self._key, f"{identity}\n{payload_json}".encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _slot(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()  # plaintext token is never stored

    def issue(self, identity: str, payload: dict[str, Any]) -> tuple[str, float]:
        token = secrets.token_urlsafe(32)
        pj = canonical(payload)
        op = StagedOperation(identity, pj, self._mac(identity, pj),
                             self._clock() + self._ttl, payload_digest=payload_hash(payload))
        with self._lock:
            now = self._clock()
            self._ops = {k: v for k, v in self._ops.items() if v.expires_at > now}
            self._ops[self._slot(token)] = op
        return token, self._ttl

    def consume(self, token: str, identity: str) -> StagedOperation:
        """Atomically claim a token for ONE creation attempt. It can never be claimed again."""
        if not isinstance(token, str) or not token.strip():
            raise ConfirmationError("A confirmation_token is required.")
        with self._lock:
            op = self._ops.get(self._slot(token.strip()))
            if op is None:
                raise ConfirmationError("Unknown confirmation token. Run validate_prospect_data again.")
            if op.status != "PENDING":
                raise ConfirmationError("This confirmation token was already used. No second prospect will be created.")
            if self._clock() >= op.expires_at:
                op.status = "CONSUMED"
                raise ConfirmationError("Confirmation token expired. Run validate_prospect_data again.")
            if op.identity != identity:
                raise ConfirmationError("Token was issued for a different authenticated account.")
            if not hmac.compare_digest(op.mac, self._mac(op.identity, op.payload_json)):
                op.status = "CONSUMED"
                raise ConfirmationError("Staged payload failed its integrity check; creation refused.")
            if op.payload_digest in self._created_digests:
                op.status = "CONSUMED"
                raise ConfirmationError("An identical prospect was already created in this session.")
            op.status = "CONSUMED"
            return op

    def mark(self, op: StagedOperation, status: str) -> None:
        with self._lock:
            op.status = status
            if status in ("CREATED", "UNCERTAIN"):  # uncertain: block identical blind retries too
                self._created_digests.add(op.payload_digest)
