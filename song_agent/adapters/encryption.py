from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SingleKeyCipher:
    """One required AEAD key; intentionally has no fallback or key-version logic."""

    def __init__(self, encoded_key: str) -> None:
        try:
            key = base64.urlsafe_b64decode(encoded_key.encode())
        except Exception as error:
            raise ValueError("SONG_AGENT_MASTER_KEY must be URL-safe base64") from error
        if len(key) != 32:
            raise ValueError("SONG_AGENT_MASTER_KEY must decode to exactly 32 bytes")
        self._cipher = AESGCM(key)

    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> tuple[bytes, bytes]:
        nonce = os.urandom(12)
        return nonce, self._cipher.encrypt(nonce, plaintext, associated_data)

    def decrypt(self, nonce: bytes, ciphertext: bytes, *, associated_data: bytes) -> bytes:
        return self._cipher.decrypt(nonce, ciphertext, associated_data)
