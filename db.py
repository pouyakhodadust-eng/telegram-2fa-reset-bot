import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from config import settings


_db_lock = asyncio.Lock()


@asynccontextmanager
async def get_db():
    async with _db_lock:
        conn = await aiosqlite.connect(settings.db_path)
        try:
            conn.row_factory = aiosqlite.Row
            yield conn
        finally:
            await conn.close()


async def init_db() -> None:
    async with get_db() as db:
        await db.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_whitelisted INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                username TEXT,
                password TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS stats (
                user_id INTEGER PRIMARY KEY,
                total_processed INTEGER NOT NULL DEFAULT 0,
                no_2fa INTEGER NOT NULL DEFAULT 0,
                reset_success INTEGER NOT NULL DEFAULT 0,
                reset_timer INTEGER NOT NULL DEFAULT 0,
                reset_failed INTEGER NOT NULL DEFAULT 0,
                email_required INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            """
        )

        # Ensure main admin exists and is whitelisted
        await db.execute(
            """
            INSERT INTO users (user_id, is_whitelisted, is_admin)
            VALUES (?, 1, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                is_whitelisted=1,
                is_admin=1
            """,
            (settings.main_admin_id,),
        )
        await db.commit()


async def is_whitelisted(user_id: int) -> bool:
    async with get_db() as db:
        async with db.execute(
            "SELECT is_whitelisted FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row["is_whitelisted"])


async def is_admin(user_id: int) -> bool:
    async with get_db() as db:
        async with db.execute(
            "SELECT is_admin FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row["is_admin"])


async def add_user(user_id: int, admin: bool = False) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO users (user_id, is_whitelisted, is_admin)
            VALUES (?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                is_whitelisted=1,
                is_admin=MAX(is_admin, excluded.is_admin)
            """,
            (user_id, int(admin)),
        )
        await db.commit()


async def remove_user(user_id: int) -> None:
    async with get_db() as db:
        await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await db.commit()


async def list_users() -> List[Dict[str, Any]]:
    async with get_db() as db:
        async with db.execute(
            "SELECT user_id, is_whitelisted, is_admin FROM users ORDER BY user_id"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]


async def add_proxy(
    user_id: int, host: str, port: int, username: Optional[str], password: Optional[str]
) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO proxies (user_id, host, port, username, password)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, host, port, username, password),
        )
        await db.commit()


async def remove_proxy(user_id: int, proxy_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM proxies WHERE user_id = ? AND id = ?", (user_id, proxy_id)
        )
        await db.commit()


async def list_proxies(user_id: int) -> List[Dict[str, Any]]:
    async with get_db() as db:
        async with db.execute(
            "SELECT id, host, port, username, password FROM proxies WHERE user_id = ? ORDER BY id",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]


async def count_proxies(user_id: int) -> int:
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*) AS c FROM proxies WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return int(row["c"]) if row else 0


async def get_proxies_for_user(user_id: int) -> List[Tuple[int, str, int, Optional[str], Optional[str]]]:
    async with get_db() as db:
        async with db.execute(
            "SELECT id, host, port, username, password FROM proxies WHERE user_id = ? ORDER BY id",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [
                (
                    int(row["id"]),
                    str(row["host"]),
                    int(row["port"]),
                    row["username"],
                    row["password"],
                )
                for row in rows
            ]


async def ensure_stats_row(user_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO stats (user_id)
            VALUES (?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id,),
        )
        await db.commit()


async def update_stats(
    user_id: int,
    total_delta: int,
    no_2fa: int,
    reset_success: int,
    reset_timer: int,
    reset_failed: int,
    email_required: int,
) -> None:
    await ensure_stats_row(user_id)
    async with get_db() as db:
        await db.execute(
            """
            UPDATE stats
            SET
                total_processed = total_processed + ?,
                no_2fa = no_2fa + ?,
                reset_success = reset_success + ?,
                reset_timer = reset_timer + ?,
                reset_failed = reset_failed + ?,
                email_required = email_required + ?
            WHERE user_id = ?
            """,
            (
                total_delta,
                no_2fa,
                reset_success,
                reset_timer,
                reset_failed,
                email_required,
                user_id,
            ),
        )
        await db.commit()


async def get_stats(user_id: int) -> Optional[Dict[str, Any]]:
    async with get_db() as db:
        async with db.execute(
            """
            SELECT total_processed, no_2fa, reset_success, reset_timer,
                   reset_failed, email_required
            FROM stats
            WHERE user_id = ?
            """,
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

