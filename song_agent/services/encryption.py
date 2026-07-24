"""用户 OAuth 凭据的认证加密。"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class TokenCipherError(RuntimeError):
    """凭据无法加密或认证时抛出。"""


@dataclass(frozen=True, slots=True)
class EncryptedValue:
    ciphertext: bytes
    nonce: bytes
    key_version: int


class AesGcmTokenCipher:
    """版本化的 AES-256-GCM 密钥环。

    调用者提供关联数据，确保密文不能在安装之间
    或 access/refresh token 字段之间移动。
    """

    def __init__(self, keys: Mapping[int, bytes], active_key_version: int) -> None:
        normalized = dict(keys)
        if active_key_version not in normalized:
            raise TokenCipherError("active token encryption key is not configured")
        if any(version < 1 or len(key) != 32 for version, key in normalized.items()):
            raise TokenCipherError("token encryption keys must be versioned 256-bit keys")
        self._keys = normalized
        self.active_key_version = active_key_version

    @classmethod
    def from_base64_keys(
        cls,
        keys: Mapping[int, str],
        active_key_version: int,
        *,
        bootstrap_secret: str = "",
        bootstrap_context: str = "",
    ) -> AesGcmTokenCipher:
        decoded: dict[int, bytes] = {}
        for version, encoded in keys.items():
            try:
                padded = encoded + "=" * (-len(encoded) % 4)
                value = base64.urlsafe_b64decode(padded.encode("ascii"))
            except (ValueError, UnicodeEncodeError, binascii.Error) as error:
                raise TokenCipherError(f"token encryption key V{version} is not valid base64") from error
            decoded[version] = value
        if not decoded:
            if not bootstrap_secret:
                raise TokenCipherError("no token encryption key is configured")
            decoded[1] = _derive_bootstrap_key(bootstrap_secret, bootstrap_context)
            active_key_version = 1
        return cls(decoded, active_key_version)

    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> EncryptedValue:
        if not plaintext:
            return EncryptedValue(b"", b"", self.active_key_version)
        nonce = os.urandom(12)
        try:
            ciphertext = AESGCM(self._keys[self.active_key_version]).encrypt(
                nonce,
                plaintext,
                associated_data,
            )
        except Exception as error:
            raise TokenCipherError("failed to encrypt OAuth credential") from error
        return EncryptedValue(ciphertext, nonce, self.active_key_version)

    def decrypt(self, value: EncryptedValue, *, associated_data: bytes) -> bytes:
        if not value.ciphertext:
            return b""
        key = self._keys.get(value.key_version)
        if key is None:
            raise TokenCipherError(
                f"token encryption key V{value.key_version} is not available"
            )
        try:
            return AESGCM(key).decrypt(value.nonce, value.ciphertext, associated_data)
        except Exception as error:
            raise TokenCipherError("OAuth credential authentication failed") from error

    def rotate(self, value: EncryptedValue, *, associated_data: bytes) -> EncryptedValue:
        plaintext = self.decrypt(value, associated_data=associated_data)
        return self.encrypt(plaintext, associated_data=associated_data)


def token_associated_data(
    tenant_key: str,
    app_id: str,
    principal_id: str,
    token_kind: str,
) -> bytes:
    if token_kind not in {"access", "refresh"}:
        raise ValueError("unsupported token kind")
    return "\x1f".join(
        ("song-agent.oauth.v1", tenant_key, app_id, principal_id, token_kind)
    ).encode()


def _derive_bootstrap_key(secret: str, context: str) -> bytes:
    """在未配置专用密钥时派生稳定的开发密钥。

    生产环境应配置 SONG_AGENT_TOKEN_KEY_V1。此回退仍可防止明文存储，
    并与飞书 App Secret 的其他用途域分离。
    """

    salt = sha256(("song-agent:" + context).encode()).digest()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"song-agent/token-encryption/bootstrap/v1",
    ).derive(secret.encode())
