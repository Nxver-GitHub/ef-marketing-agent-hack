"""Recompute scores for prospects whose latest signal is newer than their latest score.

Usage:
  uv run python scripts/score_all.py            # incremental (default)
  uv run python scripts/score_all.py --all      # force every prospect
  uv run python scripts/score_all.py --limit 50 # cap (sanity check before full run)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from credence.db import close_pool, fetch
from credence.score_runner import load_weights, score_prospect

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("score_all")

CONCURRENCY = 16


async def find_targets(force: bool, limit: int | None) -> list[str]:
    if force:
        sql = "SELECT id FROM prospects ORDER BY updated_at DESC"
    else:
        # prospects whose newest signal is newer than their newest score (or no score yet)
        sql = """
        SELECT p.id
        FROM prospects p
        LEFT JOIN LATERAL (SELECT max(collected_at) AS t FROM signals WHERE prospect_id = p.id) sig ON TRUE
        LEFT JOIN LATERAL (SELECT max(computed_at)  AS t FROM scores  WHERE prospect_id = p.id) sc  ON TRUE
        WHERE sig.t IS NOT NULL AND (sc.t IS NULL OR sig.t > sc.t)
        ORDER BY sig.t DESC
        """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = await fetch(sql)
    return [r["id"] for r in rows]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="rescore every prospect, not just stale ones")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    weights = await load_weights()
    log.info("loaded %d signal_weights", len(weights))

    ids = await find_targets(force=args.all, limit=args.limit)
    log.info("scoring %d prospects (concurrency=%d)", len(ids), CONCURRENCY)

    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0
    t0 = time.monotonic()

    async def run(pid: str) -> None:
        nonlocal done
        async with sem:
            try:
                await score_prospect(pid, weights)
            except Exception as e:
                log.warning("failed %s: %s", pid, e)
            done += 1
            if done % 100 == 0:
                rate = done / (time.monotonic() - t0)
                log.info("  %d / %d (%.1f/s)", done, len(ids), rate)

    await asyncio.gather(*(run(pid) for pid in ids))

    log.info("done — %d scored in %.1fs", done, time.monotonic() - t0)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
