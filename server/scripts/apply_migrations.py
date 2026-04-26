"""Apply every .sql file in server/migrations/ in lexical order.

Idempotent: each migration uses CREATE OR REPLACE / IF NOT EXISTS so re-running
is a no-op. We don't track a migrations table for v0 — the SQL files are the
source of truth and they're checked into git.

Usage: `uv run python scripts/apply_migrations.py`
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Allow running as `python scripts/apply_migrations.py` from server/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from credence.db import acquire, close_pool

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


async def main() -> None:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        log.warning("no migrations found in %s", MIGRATIONS_DIR)
        return

    async with acquire() as conn:
        for f in files:
            log.info("applying %s", f.name)
            sql = f.read_text()
            await conn.execute(sql)
        log.info("done — %d migrations applied", len(files))

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
