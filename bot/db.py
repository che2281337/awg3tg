"""SQLite-хранилище: аккаунты, устройства (ключи), тарифы, платежи, трафик.

Для VPN-сервиса на один сервер SQLite с WAL более чем достаточно: тысячи
пользователей и запись статистики раз в несколько минут. Файл базы лежит в
data/ — его достаточно копировать для резервной копии.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id        INTEGER PRIMARY KEY,
    username     TEXT,
    full_name    TEXT,
    created_at   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL DEFAULT 0,
    banned       INTEGER NOT NULL DEFAULT 0,
    referrer_id  INTEGER,
    trial_used   INTEGER NOT NULL DEFAULT 0,
    sub_until    INTEGER,                 -- unix-время окончания подписки
    device_limit INTEGER NOT NULL DEFAULT 0,
    notified     INTEGER NOT NULL DEFAULT 0,  -- этап напоминаний об окончании
    ref_rewarded INTEGER NOT NULL DEFAULT 0   -- пригласивший уже получил бонус
);
CREATE TABLE IF NOT EXISTS servers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,                -- «Германия»
    flag       TEXT NOT NULL DEFAULT '🌐',
    host       TEXT NOT NULL,                -- публичный IP/домен для Endpoint
    conn       TEXT NOT NULL,                -- local | user@host:port | mock:<папка>
    container  TEXT,                         -- NULL = определить автоматически
    active     INTEGER NOT NULL DEFAULT 1,   -- 0 = скрыт для новых устройств
    sort       INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    status_ok  INTEGER NOT NULL DEFAULT 1,   -- результат последней проверки
    last_error TEXT,
    last_check INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS keys (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id          INTEGER,
    server_id      INTEGER,
    name           TEXT NOT NULL,
    public_key     TEXT NOT NULL UNIQUE,
    private_key    TEXT NOT NULL,
    ip             TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,  -- 0 = пир снят с сервера (подписка кончилась)
    last_handshake INTEGER NOT NULL DEFAULT 0,
    last_rx        INTEGER NOT NULL DEFAULT 0,  -- последние показания счётчиков awg
    last_tx        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS keys_tg_id ON keys(tg_id);
CREATE TABLE IF NOT EXISTS traffic_daily (
    key_id    INTEGER NOT NULL,
    tg_id     INTEGER,
    server_id INTEGER,
    day       TEXT NOT NULL,               -- YYYY-MM-DD в часовом поясе бота
    rx     INTEGER NOT NULL DEFAULT 0,   -- от клиента к серверу (отдано клиентом)
    tx     INTEGER NOT NULL DEFAULT 0,   -- от сервера к клиенту (скачано клиентом)
    PRIMARY KEY (key_id, day)
);
CREATE INDEX IF NOT EXISTS traffic_tg_day ON traffic_daily(tg_id, day);
CREATE TABLE IF NOT EXISTS plans (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title   TEXT NOT NULL,
    days    INTEGER NOT NULL,
    devices INTEGER NOT NULL,
    price   INTEGER NOT NULL,
    active  INTEGER NOT NULL DEFAULT 1,
    sort    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id        INTEGER NOT NULL,
    plan_id      INTEGER,
    title        TEXT NOT NULL,
    days         INTEGER NOT NULL,
    devices      INTEGER NOT NULL,
    amount       INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | paid | rejected
    receipt_type TEXT,                              -- photo | document | text
    receipt      TEXT,                              -- file_id или текст
    created_at   INTEGER NOT NULL,
    decided_at   INTEGER,
    admin_id     INTEGER
);
CREATE INDEX IF NOT EXISTS payments_status ON payments(status);
"""

DEFAULT_PLANS = [
    ("1 месяц", 30, 2, 100),
    ("3 месяца", 90, 2, 300),
    ("6 месяцев", 180, 2, 600),
    ("12 месяцев", 365, 2, 1200),
]

# Колонки, появившиеся после первой версии бота (миграция старой базы).
_MIGRATIONS = {
    "users": {
        "last_seen": "INTEGER NOT NULL DEFAULT 0",
        "banned": "INTEGER NOT NULL DEFAULT 0",
        "referrer_id": "INTEGER",
        "trial_used": "INTEGER NOT NULL DEFAULT 0",
        "sub_until": "INTEGER",
        "device_limit": "INTEGER NOT NULL DEFAULT 0",
        "notified": "INTEGER NOT NULL DEFAULT 0",
        "ref_rewarded": "INTEGER NOT NULL DEFAULT 0",
    },
    "keys": {
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "last_handshake": "INTEGER NOT NULL DEFAULT 0",
        "last_rx": "INTEGER NOT NULL DEFAULT 0",
        "last_tx": "INTEGER NOT NULL DEFAULT 0",
        "server_id": "INTEGER",
    },
    "traffic_daily": {
        "server_id": "INTEGER",
    },
}

# Индексы по колонкам, которые могли появиться только после миграции.
POST_MIGRATION = """
CREATE INDEX IF NOT EXISTS keys_server ON keys(server_id);
CREATE INDEX IF NOT EXISTS traffic_server_day ON traffic_daily(server_id, day);
"""


def now() -> int:
    return int(time.time())


@dataclass
class User:
    tg_id: int
    username: str | None
    full_name: str | None
    created_at: int
    last_seen: int
    banned: int
    referrer_id: int | None
    trial_used: int
    sub_until: int | None
    device_limit: int
    notified: int
    ref_rewarded: int

    @property
    def title(self) -> str:
        return f"@{self.username}" if self.username else (self.full_name or str(self.tg_id))

    @property
    def active(self) -> bool:
        return bool(self.sub_until and self.sub_until > now())


@dataclass
class Server:
    id: int
    name: str
    flag: str
    host: str
    conn: str
    container: str | None
    active: int
    sort: int
    created_at: int
    status_ok: int
    last_error: str | None
    last_check: int

    @property
    def title(self) -> str:
        return f"{self.flag} {self.name}"


@dataclass
class Key:
    id: int
    tg_id: int | None
    server_id: int | None
    name: str
    public_key: str
    private_key: str
    ip: str
    created_at: int
    enabled: int
    last_handshake: int
    last_rx: int
    last_tx: int


@dataclass
class Plan:
    id: int
    title: str
    days: int
    devices: int
    price: int
    active: int
    sort: int


@dataclass
class Payment:
    id: int
    tg_id: int
    plan_id: int | None
    title: str
    days: int
    devices: int
    amount: int
    status: str
    receipt_type: str | None
    receipt: str | None
    created_at: int
    decided_at: int | None
    admin_id: int | None


@dataclass
class Traffic:
    rx: int = 0
    tx: int = 0

    @property
    def total(self) -> int:
        return self.rx + self.tx


def _fields(cls) -> list[str]:
    return list(cls.__dataclass_fields__)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.executescript(POST_MIGRATION)
        async with self.c.execute("SELECT COUNT(*) FROM plans") as cur:
            if (await cur.fetchone())[0] == 0:
                for i, (title, days, devices, price) in enumerate(DEFAULT_PLANS):
                    await self.c.execute(
                        "INSERT INTO plans (title, days, devices, price, sort) VALUES (?, ?, ?, ?, ?)",
                        (title, days, devices, price, i),
                    )
        await self.conn.commit()

    async def _migrate(self) -> None:
        for table, columns in _MIGRATIONS.items():
            async with self.c.execute(f"PRAGMA table_info({table})") as cur:
                existing = {r["name"] for r in await cur.fetchall()}
            for name, ddl in columns.items():
                if name not in existing:
                    await self.c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            if table == "users" and "status" in existing:
                # Первая версия бота: status = pending/approved/banned.
                await self.c.execute("UPDATE users SET banned = 1 WHERE status = 'banned'")

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    @property
    def c(self) -> aiosqlite.Connection:
        assert self.conn is not None
        return self.conn

    async def _one(self, cls, sql: str, args: tuple = ()):
        if any(isinstance(a, int) and not -(2**63) <= a < 2**63 for a in args):
            return None  # подделанный гигантский ID из кнопки — такой записи быть не может
        async with self.c.execute(sql, args) as cur:
            row = await cur.fetchone()
        return cls(**{k: row[k] for k in _fields(cls)}) if row else None

    async def _all(self, cls, sql: str, args: tuple = ()) -> list:
        async with self.c.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [cls(**{k: r[k] for k in _fields(cls)}) for r in rows]

    async def _scalar(self, sql: str, args: tuple = ()):
        async with self.c.execute(sql, args) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def _exec(self, sql: str, args: tuple = ()) -> int:
        cur = await self.c.execute(sql, args)
        await self.c.commit()
        return cur.rowcount

    # ---------- users ----------

    async def get_user(self, tg_id: int) -> User | None:
        return await self._one(User, "SELECT * FROM users WHERE tg_id = ?", (tg_id,))

    async def touch_user(self, tg_id: int, username: str | None, full_name: str | None) -> tuple[User, bool]:
        """Создаёт аккаунт при первом обращении, обновляет имя и last_seen."""
        ts = now()
        cur = await self.c.execute(
            "INSERT OR IGNORE INTO users (tg_id, username, full_name, created_at, last_seen) VALUES (?, ?, ?, ?, ?)",
            (tg_id, username, full_name, ts, ts),
        )
        created = cur.rowcount > 0
        if not created:
            await self.c.execute(
                "UPDATE users SET username = ?, full_name = ?, last_seen = ? WHERE tg_id = ?",
                (username, full_name, ts, tg_id),
            )
        await self.c.commit()
        user = await self.get_user(tg_id)
        assert user
        return user, created

    async def update_user(self, tg_id: int, **fields) -> bool:
        if not fields:
            return False
        sets = ", ".join(f"{k} = ?" for k in fields)
        return await self._exec(f"UPDATE users SET {sets} WHERE tg_id = ?", (*fields.values(), tg_id)) > 0

    async def delete_user(self, tg_id: int) -> None:
        await self.c.execute("DELETE FROM keys WHERE tg_id = ?", (tg_id,))
        await self.c.execute("DELETE FROM traffic_daily WHERE tg_id = ?", (tg_id,))
        await self.c.execute("UPDATE users SET referrer_id = NULL WHERE referrer_id = ?", (tg_id,))
        await self.c.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
        await self.c.commit()

    async def list_users(self, flt: str = "all", offset: int = 0, limit: int = 10) -> tuple[list[User], int]:
        where = {
            "all": "1",
            "active": "sub_until > :now AND banned = 0",
            "expired": "(sub_until IS NULL OR sub_until <= :now) AND banned = 0",
            "banned": "banned = 1",
        }[flt]
        args = {"now": now(), "limit": limit, "offset": offset}
        total = await self._scalar(f"SELECT COUNT(*) FROM users WHERE {where}", args)
        async with self.c.execute(
            f"SELECT * FROM users WHERE {where} ORDER BY created_at DESC LIMIT :limit OFFSET :offset", args
        ) as cur:
            rows = await cur.fetchall()
        return [User(**{k: r[k] for k in _fields(User)}) for r in rows], total

    async def find_users(self, query: str) -> list[User]:
        q = query.strip().lstrip("@")
        if q.isdigit():
            return await self._all(User, "SELECT * FROM users WHERE tg_id = ?", (int(q),))
        like = f"%{q}%"
        return await self._all(
            User,
            "SELECT * FROM users WHERE username LIKE ? OR full_name LIKE ? ORDER BY created_at DESC LIMIT 20",
            (like, like),
        )

    async def all_user_ids(self, flt: str = "all") -> list[int]:
        where = {
            "all": "banned = 0",
            "active": "banned = 0 AND sub_until > ?",
            "expired": "banned = 0 AND (sub_until IS NULL OR sub_until <= ?)",
        }[flt]
        args = () if flt == "all" else (now(),)
        async with self.c.execute(f"SELECT tg_id FROM users WHERE {where}", args) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def users_to_expire(self) -> list[User]:
        """Подписка кончилась, а включённые ключи ещё есть."""
        return await self._all(
            User,
            """SELECT * FROM users u WHERE (u.sub_until IS NULL OR u.sub_until <= ?)
               AND EXISTS (SELECT 1 FROM keys k WHERE k.tg_id = u.tg_id AND k.enabled = 1)""",
            (now(),),
        )

    async def users_expiring(self, before: int) -> list[User]:
        return await self._all(
            User, "SELECT * FROM users WHERE banned = 0 AND sub_until > ? AND sub_until <= ?", (now(), before)
        )

    async def expired_unnotified(self) -> list[User]:
        return await self._all(
            User,
            "SELECT * FROM users WHERE banned = 0 AND sub_until IS NOT NULL AND sub_until <= ? AND notified < 3",
            (now(),),
        )

    async def inactive_users(self, days: int) -> list[User]:
        """Заброшенные аккаунты: нет подписки, давно не заходили в бота и не подключались."""
        border = now() - days * 86400
        return await self._all(
            User,
            """SELECT * FROM users u
               WHERE (u.sub_until IS NULL OR u.sub_until <= ?) AND u.last_seen < ?
               AND NOT EXISTS (SELECT 1 FROM keys k WHERE k.tg_id = u.tg_id AND k.last_handshake >= ?)
               ORDER BY u.last_seen""",
            (now(), border, border),
        )

    async def referrals_count(self, tg_id: int) -> tuple[int, int]:
        total = await self._scalar("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (tg_id,))
        paid = await self._scalar("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND ref_rewarded = 1", (tg_id,))
        return total or 0, paid or 0

    # ---------- servers ----------

    async def servers(self, only_active: bool = False) -> list[Server]:
        where = "WHERE active = 1" if only_active else ""
        return await self._all(Server, f"SELECT * FROM servers {where} ORDER BY sort, id")

    async def get_server(self, server_id: int) -> Server | None:
        return await self._one(Server, "SELECT * FROM servers WHERE id = ?", (server_id,))

    async def add_server(self, name: str, flag: str, host: str, conn: str, container: str | None = None) -> Server:
        sort = (await self._scalar("SELECT COALESCE(MAX(sort), 0) + 1 FROM servers")) or 0
        cur = await self.c.execute(
            "INSERT INTO servers (name, flag, host, conn, container, sort, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, flag, host, conn, container, sort, now()),
        )
        await self.c.commit()
        server = await self.get_server(cur.lastrowid)
        assert server
        return server

    async def update_server(self, server_id: int, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self._exec(f"UPDATE servers SET {sets} WHERE id = ?", (*fields.values(), server_id))

    async def delete_server(self, server_id: int) -> None:
        await self._exec("DELETE FROM servers WHERE id = ?", (server_id,))

    async def server_key_counts(self) -> dict[int, tuple[int, int, int]]:
        """server_id -> (всего ключей, включённых, онлайн за 3 мин)."""
        async with self.c.execute(
            """SELECT server_id, COUNT(*), SUM(enabled), SUM(enabled = 1 AND last_handshake >= ?)
               FROM keys GROUP BY server_id""",
            (now() - 180,),
        ) as cur:
            return {r[0]: (r[1], r[2] or 0, r[3] or 0) for r in await cur.fetchall()}

    async def assign_orphan_keys(self, server_id: int) -> int:
        """Ключи из версии бота без мультисерверности привязываются к первому серверу."""
        n = await self._exec("UPDATE keys SET server_id = ? WHERE server_id IS NULL", (server_id,))
        await self._exec(
            """UPDATE traffic_daily SET server_id = (SELECT server_id FROM keys WHERE keys.id = traffic_daily.key_id)
               WHERE server_id IS NULL"""
        )
        return n

    # ---------- keys ----------

    async def add_key(
        self, tg_id: int | None, name: str, public_key: str, private_key: str, ip: str, server_id: int | None = None
    ) -> Key:
        cur = await self.c.execute(
            """INSERT INTO keys (tg_id, server_id, name, public_key, private_key, ip, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (tg_id, server_id, name, public_key, private_key, ip, now()),
        )
        await self.c.commit()
        key = await self.get_key(cur.lastrowid)
        assert key
        return key

    async def get_key(self, key_id: int) -> Key | None:
        return await self._one(Key, "SELECT * FROM keys WHERE id = ?", (key_id,))

    async def user_keys(self, tg_id: int) -> list[Key]:
        return await self._all(Key, "SELECT * FROM keys WHERE tg_id = ? ORDER BY id", (tg_id,))

    async def all_keys(self) -> list[Key]:
        return await self._all(Key, "SELECT * FROM keys ORDER BY id")

    async def enabled_keys(self, server_id: int | None = None) -> list[Key]:
        if server_id is None:
            return await self._all(Key, "SELECT * FROM keys WHERE enabled = 1")
        return await self._all(Key, "SELECT * FROM keys WHERE enabled = 1 AND server_id = ?", (server_id,))

    async def server_keys(self, server_id: int) -> list[Key]:
        return await self._all(Key, "SELECT * FROM keys WHERE server_id = ? ORDER BY id", (server_id,))

    async def reserved_ips(self, server_id: int | None = None) -> set[str]:
        """IP, занятые ключами из БД на данном сервере (в т.ч. отключёнными)."""
        if server_id is None:
            sql, args = "SELECT ip FROM keys", ()
        else:
            sql, args = "SELECT ip FROM keys WHERE server_id = ?", (server_id,)
        async with self.c.execute(sql, args) as cur:
            return {r[0] for r in await cur.fetchall()}

    async def other_ips(self, server_id: int, key_id: int) -> set[str]:
        """IP остальных ключей этого сервера — их нельзя отдавать восстанавливаемому ключу."""
        async with self.c.execute(
            "SELECT ip FROM keys WHERE server_id = ? AND id != ? AND ip != ''", (server_id, key_id)
        ) as cur:
            return {r[0] for r in await cur.fetchall()}

    async def key_by_public(self, public_key: str) -> Key | None:
        return await self._one(Key, "SELECT * FROM keys WHERE public_key = ?", (public_key,))

    async def update_key(self, key_id: int, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self._exec(f"UPDATE keys SET {sets} WHERE id = ?", (*fields.values(), key_id))

    async def delete_key(self, key_id: int) -> None:
        await self._exec("DELETE FROM keys WHERE id = ?", (key_id,))

    # ---------- traffic ----------

    async def add_traffic(self, key: Key, day: str, rx: int, tx: int, cur_rx: int, cur_tx: int, hs: int) -> None:
        if rx or tx:
            await self.c.execute(
                """INSERT INTO traffic_daily (key_id, tg_id, server_id, day, rx, tx) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key_id, day) DO UPDATE SET rx = rx + excluded.rx, tx = tx + excluded.tx""",
                (key.id, key.tg_id, key.server_id, day, rx, tx),
            )
        await self.c.execute(
            "UPDATE keys SET last_rx = ?, last_tx = ?, last_handshake = MAX(last_handshake, ?) WHERE id = ?",
            (cur_rx, cur_tx, hs, key.id),
        )

    async def commit(self) -> None:
        await self.c.commit()

    async def traffic(
        self, *, tg_id: int | None = None, key_id: int | None = None, server_id: int | None = None, since: str = ""
    ) -> Traffic:
        cond, args = ["day >= ?"], [since]
        if server_id is not None:
            cond.append("server_id = ?")
            args.append(server_id)
        if tg_id is not None:
            cond.append("tg_id = ?")
            args.append(tg_id)
        if key_id is not None:
            cond.append("key_id = ?")
            args.append(key_id)
        async with self.c.execute(
            f"SELECT COALESCE(SUM(rx), 0), COALESCE(SUM(tx), 0) FROM traffic_daily WHERE {' AND '.join(cond)}",
            tuple(args),
        ) as cur:
            rx, tx = await cur.fetchone()
        return Traffic(rx, tx)

    async def top_traffic(self, since: str, limit: int = 10) -> list[tuple[int, Traffic]]:
        async with self.c.execute(
            """SELECT tg_id, SUM(rx), SUM(tx) FROM traffic_daily WHERE day >= ? AND tg_id IS NOT NULL
               GROUP BY tg_id ORDER BY SUM(rx) + SUM(tx) DESC LIMIT ?""",
            (since, limit),
        ) as cur:
            return [(r[0], Traffic(r[1], r[2])) for r in await cur.fetchall()]

    # ---------- plans ----------

    async def plans(self, only_active: bool = True) -> list[Plan]:
        where = "WHERE active = 1" if only_active else ""
        return await self._all(Plan, f"SELECT * FROM plans {where} ORDER BY sort, days")

    async def get_plan(self, plan_id: int) -> Plan | None:
        return await self._one(Plan, "SELECT * FROM plans WHERE id = ?", (plan_id,))

    async def add_plan(self, title: str, days: int, devices: int, price: int) -> None:
        sort = (await self._scalar("SELECT COALESCE(MAX(sort), 0) + 1 FROM plans")) or 0
        await self._exec(
            "INSERT INTO plans (title, days, devices, price, sort) VALUES (?, ?, ?, ?, ?)",
            (title, days, devices, price, sort),
        )

    async def update_plan(self, plan_id: int, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self._exec(f"UPDATE plans SET {sets} WHERE id = ?", (*fields.values(), plan_id))

    async def delete_plan(self, plan_id: int) -> None:
        await self._exec("DELETE FROM plans WHERE id = ?", (plan_id,))

    # ---------- payments ----------

    async def create_payment(
        self, tg_id: int, plan: Plan, receipt_type: str, receipt: str, *, amount: int | None = None, title: str | None = None
    ) -> Payment:
        cur = await self.c.execute(
            """INSERT INTO payments (tg_id, plan_id, title, days, devices, amount, receipt_type, receipt, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tg_id,
                plan.id,
                title or plan.title,
                plan.days,
                plan.devices,
                plan.price if amount is None else amount,
                receipt_type,
                receipt,
                now(),
            ),
        )
        await self.c.commit()
        p = await self.get_payment(cur.lastrowid)
        assert p
        return p

    async def get_payment(self, payment_id: int) -> Payment | None:
        return await self._one(Payment, "SELECT * FROM payments WHERE id = ?", (payment_id,))

    async def decide_payment(self, payment_id: int, status: str, admin_id: int) -> bool:
        """Атомарно меняет статус pending -> paid/rejected (защита от двойного нажатия)."""
        return (
            await self._exec(
                "UPDATE payments SET status = ?, decided_at = ?, admin_id = ? WHERE id = ? AND status = 'pending'",
                (status, now(), admin_id, payment_id),
            )
            > 0
        )

    async def pending_payments(self) -> list[Payment]:
        return await self._all(Payment, "SELECT * FROM payments WHERE status = 'pending' ORDER BY id")

    async def pending_count(self, tg_id: int) -> int:
        return await self._scalar("SELECT COUNT(*) FROM payments WHERE tg_id = ? AND status = 'pending'", (tg_id,)) or 0

    async def user_payments(self, tg_id: int, limit: int = 10) -> list[Payment]:
        return await self._all(
            Payment, "SELECT * FROM payments WHERE tg_id = ? ORDER BY id DESC LIMIT ?", (tg_id, limit)
        )

    async def has_paid(self, tg_id: int) -> bool:
        return bool(await self._scalar("SELECT 1 FROM payments WHERE tg_id = ? AND status = 'paid'", (tg_id,)))

    # ---------- статистика ----------

    async def stats(self, day_start: int, month_start: int) -> dict:
        n = now()
        q = self._scalar
        return {
            "users": await q("SELECT COUNT(*) FROM users"),
            "new_today": await q("SELECT COUNT(*) FROM users WHERE created_at >= ?", (day_start,)),
            "new_month": await q("SELECT COUNT(*) FROM users WHERE created_at >= ?", (month_start,)),
            "active": await q("SELECT COUNT(*) FROM users WHERE sub_until > ? AND banned = 0", (n,)),
            "paying": await q(
                "SELECT COUNT(DISTINCT p.tg_id) FROM payments p JOIN users u ON u.tg_id = p.tg_id "
                "WHERE p.status = 'paid' AND u.sub_until > ?",
                (n,),
            ),
            "banned": await q("SELECT COUNT(*) FROM users WHERE banned = 1"),
            "keys": await q("SELECT COUNT(*) FROM keys"),
            "keys_enabled": await q("SELECT COUNT(*) FROM keys WHERE enabled = 1"),
            "online": await q("SELECT COUNT(*) FROM keys WHERE enabled = 1 AND last_handshake >= ?", (n - 180,)),
            "revenue_total": await q("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid'"),
            "revenue_month": await q(
                "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid' AND decided_at >= ?",
                (month_start,),
            ),
            "payments_month": await q(
                "SELECT COUNT(*) FROM payments WHERE status = 'paid' AND decided_at >= ?", (month_start,)
            ),
            "pending": await q("SELECT COUNT(*) FROM payments WHERE status = 'pending'"),
        }
