from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config


def main() -> None:
    parser = argparse.ArgumentParser(prog="song-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="start the HTTP service")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=int(os.getenv("SONG_AGENT_PORT", "45837")))
    serve.add_argument("--reload", action="store_true")
    migrate = subparsers.add_parser("migrate", help="explicitly upgrade the database schema")
    migrate.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("SONG_AGENT_DATABASE_PATH", ".data/song-agent-v1.db")),
    )
    args = parser.parse_args()
    if args.command == "migrate":
        args.database.parent.mkdir(parents=True, exist_ok=True)
        os.environ["SONG_AGENT_DATABASE_PATH"] = str(args.database)
        config = Config("alembic.ini")
        command.upgrade(config, "head")
        args.database.chmod(0o600)
        return
    uvicorn.run(
        "song_agent.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=["song_agent"] if args.reload else None,
    )


if __name__ == "__main__":
    main()
