"""Opaque, signed keyset cursors (BACKEND_DESIGN.md §16.4).

A cursor carries the sort key and ``id`` of the last item of a page, plus the user and the list
it belongs to, so it cannot be replayed against another user or collection. Format:
``base64url(json) "." base64url(hmac_sha256)``. Not encrypted: it holds sort values and IDs only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any
from uuid import UUID

from eca.platform.errors import ValidationFailed

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class CursorCodec:
    def __init__(self, key: str | None) -> None:
        self._key = key.encode() if key else secrets.token_bytes(32)

    def _sig(self, body: bytes) -> str:
        return _b64(hmac.new(self._key, body, hashlib.sha256).digest()[:16])

    def encode(self, *, user_id: UUID, scope: str, key: list[Any]) -> str:
        body = json.dumps({"u": str(user_id), "s": scope, "k": key}, separators=(",", ":")).encode()
        return f"{_b64(body)}.{self._sig(body)}"

    def decode(self, cursor: str | None, *, user_id: UUID, scope: str) -> list[Any] | None:
        if not cursor:
            return None
        try:
            body_b64, sig = cursor.split(".", 1)
            body = _unb64(body_b64)
            if not hmac.compare_digest(sig, self._sig(body)):
                raise ValueError("signature")
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValidationFailed("invalid cursor") from exc
        if data.get("u") != str(user_id) or data.get("s") != scope or not isinstance(data.get("k"), list):
            raise ValidationFailed("cursor does not belong to this list")
        key: list[Any] = data["k"]
        return key


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1 or limit > MAX_LIMIT:
        raise ValidationFailed(f"limit must be between 1 and {MAX_LIMIT}")
    return limit
