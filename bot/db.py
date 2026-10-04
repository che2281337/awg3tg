from __future__ import annotations

import os
import time
from dataclasses import dataclass

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id      INTEGER PRIMARY KEY,
    username   TEXT,
    full_name  TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | banned
    key_limit  INTEGER,                          -- NULL = значение по умолчанию
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER,                         -- NULL = выдан админом вручную
    name        TEXT NOT NULL,
    public_key  TEXT NOT NULL UNIQUE,
    private_key TEXT NOT NULL,
    ip          TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS keys_tg_id ON keys(tg_id);
"""


@dataclass
class User:
    tg_id: int
    username: str | None
    full_name: str | None
    status: str
    key_limit: int | None
    created_at: int

    @property
    def title(self) -> str:
        return f"@{self.username}" if self.username else (self.full_name or str(self.tg_id))


@dataclass
class Key:
    id: int
    tg_id: int | None
    name: str
    public_key: str
    private_key: str
    ip: str
    created_at: int


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    @property
    def c(self) -> aiosqlite.Connection:
        assert self.conn is not None
        return self.conn

    # ---------- users ----------

    async def get_user(self, tg_id: int) -> User | None:
        async with self.c.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cur:
            row = await cur.fetchone()
        return User(**dict(row)) if row else None

    async def upsert_user(self, tg_id: int, username: str | None, full_name: str | None, status: str) -> User:
        await self.c.execute(
            """INSERT INTO users (tg_id, username, full_name, status, created_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username, full_name = excluded.full_name""",
            (tg_id, username, full_name, status, int(time.time())),
        )
        await self.c.commit()
        user = await self.get_user(tg_id)
        assert user
        return user

    async def set_status(self, tg_id: int, status: str) -> bool:
        cur = await self.c.execute("UPDATE users SET status = ? WHERE tg_id = ?", (status, tg_id))
        await self.c.commit()
        return cur.rowcount > 0

    async def set_limit(self, tg_id: int, limit: int | None) -> bool:
        cur = await self.c.execute("UPDATE users SET key_limit = ? WHERE tg_id = ?", (limit, tg_id))
        await self.c.commit()
        return cur.rowcount > 0

    async def list_users(self) -> list[User]:
        async with self.c.execute("SELECT * FROM users ORDER BY created_at") as cur:
            return [User(**dict(r)) for r in await cur.fetchall()]

    # ---------- keys ----------

    async def add_key(self, tg_id: int | None, name: str, public_key: str, private_key: str, ip: str) -> Key:
        cur = await self.c.execute(
            "INSERT INTO keys (tg_id, name, public_key, private_key, ip, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (tg_id, name, public_key, private_key, ip, int(time.time())),
        )
        await self.c.commit()
        key = await self.get_key(cur.lastrowid)
        assert key
        return key

    async def get_key(self, key_id: int) -> Key | None:
        async with self.c.execute("SELECT * FROM keys WHERE id = ?", (key_id,)) as cur:
            row = await cur.fetchone()
        return Key(**dict(row)) if row else None

    async def user_keys(self, tg_id: int) -> list[Key]:
        async with self.c.execute("SELECT * FROM keys WHERE tg_id = ? ORDER BY id", (tg_id,)) as cur:
            return [Key(**dict(r)) for r in await cur.fetchall()]

    async def all_keys(self) -> list[Key]:
        async with self.c.execute("SELECT * FROM keys ORDER BY id") as cur:
            return [Key(**dict(r)) for r in await cur.fetchall()]

    async def delete_key(self, key_id: int) -> None:
        await self.c.execute("DELETE FROM keys WHERE id = ?", (key_id,))
        await self.c.commit()
