"""显式的一次性旧 JSON 数据迁移工具。

用法：

    uv run python -m song_agent.maintenance.legacy_json_import \\
        --input .data/state.json \\
        --database .data/song-agent.db

正常应用启动流程不再自动导入旧 JSON 文件；尚未迁移旧数据的部署
必须先运行本工具，导入完成后建议人工备份并移走旧文件。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ..config import Settings
from ..services.encryption import AesGcmTokenCipher
from ..store import SqliteStore


async def _run(input_path: Path, database_path: Path) -> int:
    settings = Settings()
    token_cipher = AesGcmTokenCipher.from_base64_keys(
        settings.token_encryption_keys,
        settings.song_agent_token_active_key_version,
        bootstrap_secret=settings.feishu_app_secret,
        bootstrap_context=settings.feishu_app_id,
    )
    store = SqliteStore(
        database_path,
        app_id=settings.feishu_app_id,
        token_cipher=token_cipher,
        legacy_json_path=input_path,
        event_retention_days=settings.processed_event_retention_days,
    )
    await store.initialize()
    try:
        imported = await store.import_legacy_json_once()
    finally:
        await store.close()

    print(f"旧 JSON 导入完成（来源：{input_path}）：")
    for kind, count in imported.items():
        print(f"  {kind}: {count}")
    print("导入结果已写入 SQLite；建议备份并移走旧 JSON 文件。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将旧版 JSON 数据文件一次性导入 SQLite。"
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="旧版 JSON 数据文件路径（如 .data/state.json）",
    )
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="目标 SQLite 数据库路径",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.input.resolve(), args.database.resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
