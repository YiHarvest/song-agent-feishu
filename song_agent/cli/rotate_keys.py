"""将加密的 OAuth 凭据轮换到配置的活跃密钥。"""

from __future__ import annotations

import asyncio

from ..config import Settings
from ..services.encryption import AesGcmTokenCipher
from ..store import SqliteStore


async def _rotate() -> int:
    settings = Settings()
    cipher = AesGcmTokenCipher.from_base64_keys(
        settings.token_encryption_keys,
        settings.song_agent_token_active_key_version,
        bootstrap_secret=settings.feishu_app_secret,
        bootstrap_context=settings.feishu_app_id,
    )
    store = SqliteStore(
        settings.database_path,
        app_id=settings.feishu_app_id,
        token_cipher=cipher,
        legacy_json_path=settings.data_file,
        event_retention_days=settings.processed_event_retention_days,
    )
    await store.initialize()
    try:
        return await store.rotate_token_encryption()
    finally:
        await store.close()


def main() -> None:
    rotated = asyncio.run(_rotate())
    print(f"Rotated {rotated} OAuth installation(s).")


if __name__ == "__main__":
    main()
