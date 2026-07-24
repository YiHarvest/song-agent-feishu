import sqlite3
import time
from pathlib import Path

import pytest

from song_agent.models import OAuthToken
from song_agent.services.encryption import (
    AesGcmTokenCipher,
    EncryptedValue,
    TokenCipherError,
    token_associated_data,
)
from song_agent.store import SqliteStore


def cipher(version: int = 1, *, include_v1: bool = True) -> AesGcmTokenCipher:
    keys = {version: bytes([version]) * 32}
    if include_v1:
        keys[1] = b"1" * 32
    return AesGcmTokenCipher(keys, version)


def oauth_token() -> OAuthToken:
    return OAuthToken(
        user_id="principal-a",
        tenant_key="tenant-a",
        app_id="app-a",
        open_id="open-a",
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=int(time.time() * 1000) + 3_600_000,
        refresh_expires_at=int(time.time() * 1000) + 86_400_000,
        scope="calendar:calendar",
    )


@pytest.mark.asyncio
async def test_oauth_tokens_are_encrypted_at_rest(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = SqliteStore(path, app_id="app-a", token_cipher=cipher())
    await store.initialize()
    try:
        await store.save_token(oauth_token())
        loaded = await store.get_token(
            "principal-a", tenant_key="tenant-a", app_id="app-a"
        )
        assert loaded is not None
        assert loaded.access_token == "access-secret"
        assert loaded.refresh_token == "refresh-secret"
    finally:
        await store.close()

    connection = sqlite3.connect(path)
    row = connection.execute(
        """
        SELECT access_token, refresh_token, access_token_ciphertext,
               refresh_token_ciphertext, encryption_key_version
        FROM oauth_tokens
        """
    ).fetchone()
    connection.close()
    assert row[0:2] == ("", "")
    assert b"access-secret" not in row[2]
    assert b"refresh-secret" not in row[3]
    assert row[4] == 1


@pytest.mark.asyncio
async def test_plaintext_rows_are_migrated_once(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE oauth_tokens (
            tenant_key TEXT NOT NULL, app_id TEXT NOT NULL, subject_id TEXT NOT NULL,
            open_id TEXT NOT NULL DEFAULT '', tenant_user_id TEXT NOT NULL DEFAULT '',
            union_id TEXT NOT NULL DEFAULT '', access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL, expires_at INTEGER NOT NULL,
            refresh_expires_at INTEGER NOT NULL, scope TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (tenant_key, app_id, subject_id)
        );
        INSERT INTO oauth_tokens VALUES (
            'tenant-a', 'app-a', 'principal-a', 'open-a', '', '',
            'legacy-access', 'legacy-refresh', 9999999999999, 9999999999999,
            'calendar:calendar', 1
        );
        """
    )
    connection.close()

    store = SqliteStore(path, app_id="app-a", token_cipher=cipher())
    await store.initialize()
    try:
        loaded = await store.get_token(
            "principal-a", tenant_key="tenant-a", app_id="app-a"
        )
        assert loaded is not None
        assert loaded.access_token == "legacy-access"
    finally:
        await store.close()

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT access_token, refresh_token, encryption_key_version FROM oauth_tokens"
    ).fetchone()
    connection.close()
    assert row == ("", "", 1)


@pytest.mark.asyncio
async def test_missing_key_disables_installation(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = SqliteStore(path, app_id="app-a", token_cipher=cipher())
    await store.initialize()
    await store.save_token(oauth_token())
    await store.close()

    wrong = AesGcmTokenCipher({2: b"2" * 32}, 2)
    store = SqliteStore(path, app_id="app-a", token_cipher=wrong)
    await store.initialize()
    try:
        assert (
            await store.get_token("principal-a", tenant_key="tenant-a", app_id="app-a")
            is None
        )
        row = await (
            await store.db.execute("SELECT disabled_at FROM oauth_tokens")
        ).fetchone()
        assert row["disabled_at"] is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_key_rotation_reencrypts_with_active_version(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = SqliteStore(path, app_id="app-a", token_cipher=cipher())
    await store.initialize()
    await store.save_token(oauth_token())
    await store.close()

    store = SqliteStore(path, app_id="app-a", token_cipher=cipher(2))
    await store.initialize()
    try:
        assert await store.rotate_token_encryption() == 1
        loaded = await store.get_token(
            "principal-a", tenant_key="tenant-a", app_id="app-a"
        )
        assert loaded is not None and loaded.access_token == "access-secret"
        row = await (
            await store.db.execute("SELECT encryption_key_version FROM oauth_tokens")
        ).fetchone()
        assert row["encryption_key_version"] == 2
    finally:
        await store.close()


def test_cipher_rejects_cross_installation_moves() -> None:
    value = cipher().encrypt(
        b"secret",
        associated_data=token_associated_data("t", "a", "p", "access"),
    )
    with pytest.raises(TokenCipherError):
        cipher().decrypt(
            EncryptedValue(value.ciphertext, value.nonce, value.key_version),
            associated_data=token_associated_data("t", "a", "other", "access"),
        )
