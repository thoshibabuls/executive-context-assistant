"""Envelope encryption of refresh tokens (slice 1.2).

Each token gets a fresh 256-bit data key (DEK); the token is encrypted with AES-256-GCM under the
DEK with the connection ID as associated data, and the DEK is wrapped with AES-256-GCM under the
key-encryption key (KEK, ``TOKEN_KEK``, base64 32 bytes; a dev KEK comes from the environment, a
hosted KEK from the secret manager). Blob layout: ``b"v1" | kek_version (2 bytes) | nonce_k (12) |
wrapped_dek (48) | nonce_t (12) | ciphertext``. Tokens are never logged or returned by the API.
"""

from __future__ import annotations

import base64
import os
import struct
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from eca.platform.errors import ValidationFailed

MAGIC = b"v1"


class TokenCrypto:
    def __init__(self, kek_b64: str, *, version: int) -> None:
        try:
            kek = base64.b64decode(kek_b64, validate=True)
        except ValueError as exc:
            raise ValidationFailed("TOKEN_KEK must be base64") from exc
        if len(kek) != 32:
            raise ValidationFailed("TOKEN_KEK must decode to 32 bytes")
        if not 0 < version < 65536:
            raise ValidationFailed("token key version out of range")
        self._kek = AESGCM(kek)
        self.version = version

    def encrypt(self, token: str, *, connection_id: UUID) -> bytes:
        dek = AESGCM.generate_key(bit_length=256)
        nonce_k, nonce_t = os.urandom(12), os.urandom(12)
        wrapped = self._kek.encrypt(nonce_k, dek, connection_id.bytes)
        ciphertext = AESGCM(dek).encrypt(nonce_t, token.encode("utf-8"), connection_id.bytes)
        return MAGIC + struct.pack(">H", self.version) + nonce_k + wrapped + nonce_t + ciphertext

    def decrypt(self, blob: bytes, *, connection_id: UUID) -> str:
        if blob[:2] != MAGIC:
            raise ValidationFailed("unknown token blob format")
        (version,) = struct.unpack(">H", blob[2:4])
        if version != self.version:
            raise ValidationFailed(f"token encrypted with key version {version}; current is {self.version}")
        nonce_k, wrapped, nonce_t, ciphertext = blob[4:16], blob[16:64], blob[64:76], blob[76:]
        try:
            dek = self._kek.decrypt(nonce_k, wrapped, connection_id.bytes)
            return AESGCM(dek).decrypt(nonce_t, ciphertext, connection_id.bytes).decode("utf-8")
        except InvalidTag as exc:
            raise ValidationFailed("token blob does not authenticate") from exc
