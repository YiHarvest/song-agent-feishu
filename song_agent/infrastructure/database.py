from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA_REVISION = "0002_oauth"


class SchemaMismatchError(RuntimeError):
    pass


class Database:
    """Connection factory. Each repository operation owns a short connection/transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    async def verify_schema(self) -> None:
        if not self.path.exists():
            raise SchemaMismatchError(
                f"database does not exist: {self.path}; run `song-agent migrate` first"
            )
        async with self.connect() as connection:
            try:
                row = await (
                    await connection.execute("SELECT version_num FROM alembic_version")
                ).fetchone()
            except aiosqlite.Error as error:
                raise SchemaMismatchError(
                    "database is not a song-agent v1 schema; run migration against a fresh database"
                ) from error
        if row is None or row["version_num"] != SCHEMA_REVISION:
            actual = row["version_num"] if row else "missing"
            raise SchemaMismatchError(
                f"database revision {actual!r} does not match required {SCHEMA_REVISION!r}"
            )

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
        finally:
            await connection.close()

    @asynccontextmanager
    async def transaction(self, *, immediate: bool = False) -> AsyncIterator[aiosqlite.Connection]:
        async with self.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()
