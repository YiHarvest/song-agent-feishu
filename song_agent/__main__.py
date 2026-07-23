"""
Song Agent 入口点。

启动 FastAPI 服务，监听飞书 WebSocket 长连接消息。
"""

import argparse

import uvicorn

from .config import Settings


def main() -> None:
    """启动 Song Agent 服务。"""
    parser = argparse.ArgumentParser(description="启动 Song Agent")
    parser.add_argument("--reload", action="store_true", help="代码变更后自动重启（仅用于开发环境）")
    args = parser.parse_args()
    settings = Settings()
    uvicorn.run(
        "song_agent.app:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=args.reload,
        reload_dirs=["song_agent"] if args.reload else None,
    )


if __name__ == "__main__":
    main()
