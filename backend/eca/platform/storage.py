"""Object storage port (TECHNICAL_DESIGN.md §10.6, BACKEND_DESIGN.md §11.4).

Uploaded media go straight from the browser to storage through a short-lived pre-signed PUT URL;
the API never proxies a hosted upload. The port is provider-neutral and has no listing call: a
process reaches only keys it read from the user's own ``recordings`` rows.

The MVP builds one adapter, :class:`LocalObjectStorage` (development only; refused in
production). Its "pre-signed URL" is the API route ``PUT /api/v1/uploads/{token}``: the token
carries the key, content type, byte limit and expiry, signed with HMAC-SHA256, so the route needs
no session, exactly like a cloud pre-signed URL. The hosted adapter (GCS or S3, Q1) implements the
same protocol.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from eca.platform.config import REPO_ROOT, Settings
from eca.platform.errors import PermissionDenied, ValidationFailed

UPLOAD_URL_TTL = datetime.timedelta(hours=1)
UPLOAD_ROUTE = "/api/v1/uploads/"
_KEY = re.compile(r"^[a-z0-9_]+(?:/[0-9a-f-]{36})+(?:/[a-z0-9_.]{1,40})?$")
_CHUNK = 1024 * 1024


class StorageError(Exception):
    """A storage operation failed (missing object, I/O). Messages never contain content."""


@dataclass(frozen=True)
class PresignedUpload:
    url: str
    method: str
    headers: dict[str, str]
    expires_at: datetime.datetime


@dataclass(frozen=True)
class ObjectInfo:
    size: int


class ObjectStorage:
    """The port. A class rather than a protocol so the worker can hand it to handlers through
    ``Resources`` (looked up by type)."""

    def presign_put(
        self, key: str, *, content_type: str, max_bytes: int, now: datetime.datetime
    ) -> PresignedUpload:
        raise NotImplementedError

    async def head(self, key: str) -> ObjectInfo | None:
        raise NotImplementedError

    async def download(self, key: str, target: Path) -> None:
        raise NotImplementedError

    async def put_file(self, key: str, source: Path, *, content_type: str) -> None:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        """Idempotent: deleting a missing object is not an error."""
        raise NotImplementedError


def check_key(key: str) -> str:
    """Object keys are built by code from IDs only (``recordings/<user>/<recording>[/name]``)."""
    if not _KEY.fullmatch(key):
        raise ValueError("invalid object key")
    return key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class UploadGrant:
    """What a valid upload token allows: one object, one content type, at most ``max_bytes``."""

    key: str
    content_type: str
    max_bytes: int
    expires_at: datetime.datetime


class UploadSigner:
    """HMAC-SHA256 upload tokens: ``base64url(payload).base64url(signature)``."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("the storage signing key needs at least 32 bytes")
        self._key = key

    def sign(self, grant: UploadGrant) -> str:
        payload = json.dumps(
            {
                "k": grant.key,
                "ct": grant.content_type,
                "max": grant.max_bytes,
                "exp": int(grant.expires_at.timestamp()),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        mac = hmac.new(self._key, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(mac)}"

    def verify(self, token: str, *, now: datetime.datetime) -> UploadGrant:
        try:
            body, mac = token.split(".", 1)
            payload = _unb64(body)
            expected = hmac.new(self._key, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _unb64(mac)):
                raise PermissionDenied("invalid upload token")
            data = json.loads(payload)
            grant = UploadGrant(
                key=check_key(str(data["k"])),
                content_type=str(data["ct"]),
                max_bytes=int(data["max"]),
                expires_at=datetime.datetime.fromtimestamp(int(data["exp"]), datetime.UTC),
            )
        except PermissionDenied:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise PermissionDenied("invalid upload token") from exc
        if grant.expires_at <= now:
            raise PermissionDenied("the upload URL has expired")
        return grant


class LocalObjectStorage(ObjectStorage):
    """Objects as files under ``root``; uploads through the signed API route (development)."""

    def __init__(self, root: Path, signer: UploadSigner) -> None:
        self.root = root
        self.signer = signer

    def path(self, key: str) -> Path:
        return self.root / check_key(key)

    def presign_put(
        self, key: str, *, content_type: str, max_bytes: int, now: datetime.datetime
    ) -> PresignedUpload:
        expires_at = now + UPLOAD_URL_TTL
        token = self.signer.sign(UploadGrant(check_key(key), content_type, max_bytes, expires_at))
        return PresignedUpload(
            url=f"{UPLOAD_ROUTE}{token}",
            method="PUT",
            headers={"Content-Type": content_type},
            expires_at=expires_at,
        )

    async def receive(
        self,
        token: str,
        *,
        content_type: str | None,
        chunks: AsyncIterator[bytes],
        now: datetime.datetime,
    ) -> ObjectInfo:
        """Store one upload body for a valid token (the local adapter's PUT target)."""
        grant = self.signer.verify(token, now=now)
        if (content_type or "").split(";")[0].strip().lower() != grant.content_type:
            raise ValidationFailed("the Content-Type differs from the one the upload URL was made for")
        target = self.path(grant.key)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        size = 0
        try:
            with partial.open("wb") as fh:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > grant.max_bytes:
                        raise ValidationFailed(
                            "the upload is larger than declared", details={"reason": "too_large"}
                        )
                    fh.write(chunk)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return ObjectInfo(size)

    async def head(self, key: str) -> ObjectInfo | None:
        p = self.path(key)
        return ObjectInfo(p.stat().st_size) if p.is_file() else None

    def local_path(self, key: str) -> Path | None:
        p = self.path(key)
        return p if p.is_file() else None

    async def download(self, key: str, target: Path) -> None:
        source = self.path(key)
        if not source.is_file():
            raise StorageError("object not found")
        shutil.copyfile(source, target)

    async def put_file(self, key: str, source: Path, *, content_type: str) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        shutil.copyfile(source, partial)
        os.replace(partial, target)

    async def delete(self, key: str) -> None:
        p = self.path(key)
        p.unlink(missing_ok=True)
        p.with_name(p.name + ".part").unlink(missing_ok=True)


def sha256_of(path: Path) -> bytes:
    """Streaming SHA-256 of a file (the upload checksum check, BACKEND_DESIGN.md §11.4)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.digest()


def _signing_key(secret: SecretStr | None) -> bytes:
    if secret is not None and secret.get_secret_value():
        return hashlib.sha256(secret.get_secret_value().encode()).digest()
    # Unset: a random per-process key, so upload URLs stop working across restarts (dev only).
    return secrets.token_bytes(32)


def build_storage(settings: Settings) -> ObjectStorage:
    """The process's storage adapter from ``API_STORAGE_BACKEND`` (``local`` in the MVP)."""
    backend = settings.api_storage_backend
    if backend != "local":
        raise ValueError(f"unknown storage backend {backend!r}")
    if settings.is_production:
        raise ValueError("the local storage adapter is for development only")
    root = (
        Path(settings.api_storage_local_dir)
        if settings.api_storage_local_dir
        else REPO_ROOT / ".local" / "objects"
    )
    return LocalObjectStorage(root, UploadSigner(_signing_key(settings.storage_signing_key)))
