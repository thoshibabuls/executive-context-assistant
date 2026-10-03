"""Web Push without payload (RFC 8030, VAPID RFC 8292; TECHNICAL_DESIGN.md §15.4).

Pushes carry no payload, so no content passes through the push service: the browser's service
worker fetches the notification center when woken. Each push sends ``TTL``, ``Urgency`` and
``Topic`` (the reminder ID without dashes, 32 characters) so undelivered duplicates collapse.
The VAPID keys come only from the environment (``WEB_PUSH_VAPID_*``); unset keys disable Web
Push. Endpoints are accepted only on known push-service hosts, so a subscription cannot make the
worker call an arbitrary URL.
"""

from __future__ import annotations

import base64
import datetime
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr

from eca.platform.config import Settings

TTL_S = 4 * 3600
JWT_LIFETIME = datetime.timedelta(hours=12)
PUSH_HOSTS = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
)
PUSH_HOST_SUFFIXES = (".notify.windows.com", ".push.apple.com")
MAX_ENDPOINT_LENGTH = 1000


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def allowed_endpoint(endpoint: str) -> bool:
    """https on a known push service host (SSRF guard), at most 1,000 characters."""
    if len(endpoint) > MAX_ENDPOINT_LENGTH:
        return False
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    return host in PUSH_HOSTS or host.endswith(PUSH_HOST_SUFFIXES)


def topic_for(reminder_id: UUID) -> str:
    return reminder_id.hex  # 32 characters of the URL-safe alphabet (RFC 8030 §5.4)


@dataclass(frozen=True)
class VapidKeys:
    public_key: str  # base64url uncompressed P-256 point (given to PushManager.subscribe)
    private_key: SecretStr  # base64url 32-byte private scalar
    subject: str  # mailto: or https: contact

    @classmethod
    def from_settings(cls, settings: Settings) -> VapidKeys | None:
        public = settings.web_push_vapid_public_key
        private = settings.web_push_vapid_private_key
        subject = settings.web_push_vapid_subject
        if not public or private is None or not private.get_secret_value() or not subject:
            return None
        return cls(public, private, subject)

    def signing_key(self) -> ec.EllipticCurvePrivateKey:
        raw = b64url_decode(self.private_key.get_secret_value())
        return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def generate_keys() -> tuple[str, str]:
    """A new VAPID key pair (public, private), base64url; the operator stores them as secrets."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64url(public), b64url(private)


def vapid_headers(keys: VapidKeys, endpoint: str, *, now: datetime.datetime, topic: str) -> dict[str, str]:
    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int((now + JWT_LIFETIME).timestamp()),
        "sub": keys.subject,
    }
    token = jwt.encode(claims, keys.signing_key(), algorithm="ES256")
    return {
        "Authorization": f"vapid t={token}, k={keys.public_key}",
        "TTL": str(TTL_S),
        "Urgency": "normal",
        "Topic": topic,
        "Content-Length": "0",
    }


PushStatus = Literal["sent", "gone", "retry"]


@dataclass(frozen=True)
class PushResult:
    status: PushStatus
    code: int | None


class WebPushSender:
    """Process-level resource built by the worker composition when the VAPID keys are set."""

    def __init__(self, keys: VapidKeys, http: httpx.AsyncClient) -> None:
        self.keys = keys
        self._http = http

    async def send(self, endpoint: str, *, topic: str, now: datetime.datetime) -> PushResult:
        if not allowed_endpoint(endpoint):
            return PushResult("gone", None)
        try:
            response = await self._http.post(
                endpoint,
                headers=vapid_headers(self.keys, endpoint, now=now, topic=topic),
                content=b"",
                timeout=15.0,
            )
        except httpx.HTTPError:
            return PushResult("retry", None)
        if response.status_code in (200, 201, 202):
            return PushResult("sent", response.status_code)
        if response.status_code in (404, 410):
            return PushResult("gone", response.status_code)
        return PushResult("retry", response.status_code)
