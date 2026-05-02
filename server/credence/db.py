"""asyncpg connection pool + helpers.

We bypass Supabase PostgREST because:
- 10k+ row pulls trigger pagination dance and CORS overhead
- Server-side BFS / fuzzy text needs joins PostgREST can't express well
- Write paths (scoring, enrichment) want transactions
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from .config import get_settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


def _normalize_dsn(url: str) -> str:
    """asyncpg wants a plain `postgres://` or `postgresql://` DSN.

    Our `.env.local` has SQLAlchemy-style `postgresql+asyncpg://...` — strip
    the driver suffix so asyncpg's parser is happy.
    """
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _init_connection(conn: asyncpg.Connection) -> None:
    # Decode JSONB as Python dicts/lists so we don't double-parse downstream.
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


def _select_dsn(s: Any) -> str:
    """Prefer the transaction-pooler DSN when configured.

    The session pooler caps client connections (~15 on Supabase free tier;
    higher on paid). Heavy parallel-agent workloads hit MaxClientsInSessionMode
    and stall. The transaction pooler has no such cap and is safe with
    `statement_cache_size=0` (already set below). Operators opt in by setting
    `DATABASE_URL_TRANSACTION_POOLER`; absent that, we fall back to the
    legacy `DATABASE_URL`.
    """
    pooler = getattr(s, "database_url_transaction_pooler", None)
    return pooler or s.database_url


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        s = get_settings()
        dsn = _select_dsn(s)
        _pool = await asyncpg.create_pool(
            dsn=_normalize_dsn(dsn),
            min_size=s.db_pool_min,
            max_size=s.db_pool_max,
            init=_init_connection,
            statement_cache_size=0,  # Supabase pgbouncer transaction-mode safety
        )
        using_pooler = bool(getattr(s, "database_url_transaction_pooler", None))
        logger.info(
            "DB pool initialized (%s)",
            "transaction pooler" if using_pooler else "session pooler",
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    async with acquire() as conn:
        return await conn.fetch(sql, *args)


async def fetchrow(sql: str, *args: Any) -> asyncpg.Record | None:
    async with acquire() as conn:
        return await conn.fetchrow(sql, *args)


async def execute(sql: str, *args: Any) -> str:
    async with acquire() as conn:
        return await conn.execute(sql, *args)
