"""Scheduled refresh of company enrichment — COMPANY_ENRICHMENT_PLAN.md Step 6.

Re-fires `bulk_company_enrichment.run_bulk` for any company whose
`enrichment_last_run` is older than the staleness cutoff. Designed to run
from cron (suggested daily 03:00 UTC) or as a FastAPI background task
triggered by `POST /admin/refresh-company-enrichment`.

## Staleness model

Single threshold for now (`PRESS_STALENESS_DAYS = 30`). Press releases
are the most volatile signal — leadership pages drift much more slowly,
so refreshing the whole company is wasteful but safe and cheap. A future
iteration could split into "press refresh weekly" + "leadership refresh
monthly" but the cost saving is ~$5/run, not worth the complexity.

## How it interacts with `bulk_company_enrichment`

The refresh resets stale rows' `enrichment_status` to `'pending'` and
then calls `run_bulk`. `bulk_company_enrichment` already filters on
`enrichment_status IN ('pending', 'error')`, so the reset is the
trigger. No double-write — running this against a freshly-enriched
universe is a no-op.

## Idempotency + safety

- Re-running within the staleness window writes nothing new (no rows
  cross the cutoff).
- The reset is wrapped in a single transaction so a crash mid-reset
  doesn't leave the table half-flipped.
- `--dry-run` reports who'd be refreshed without flipping any rows.

## Usage

    cd server
    DATABASE_URL=...  FIRECRAWL_API_KEY=...  uv run python -m \\
      credence.enrichment.refresh_company_enrichment
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime, timedelta

if __package__ in (None, ""):
    _SERVER_DIR = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    if _SERVER_DIR not in sys.path:
        sys.path.insert(0, _SERVER_DIR)

from credence.db import close_pool, fetch  # noqa: E402
from credence.enrichment.bulk_company_enrichment import run_bulk  # noqa: E402

log = logging.getLogger(__name__)


# Days after which we consider a company's signals stale enough to re-fetch.
# 30 days matches the press-release volatility band; leadership churn is
# slower but a single threshold keeps the job simple.
DEFAULT_STALENESS_DAYS: int = 30


async def find_stale_company_ids(*, staleness_days: int) -> list[str]:
    """Return UUIDs of companies whose enrichment is older than the cutoff.

    Pure read query — safe to call without locking. The bulk runner picks
    these up after we flip them to `'pending'`.
    """
    cutoff = datetime.now(UTC) - timedelta(days=staleness_days)
    rows = await fetch(
        """
        SELECT id
        FROM companies
        WHERE enrichment_status   = 'done'
          AND enrichment_last_run IS NOT NULL
          AND enrichment_last_run < $1
        """,
        cutoff,
    )
    return [str(row["id"]) for row in rows]


async def reset_to_pending(company_ids: list[str]) -> int:
    """Flip the given company IDs back to `enrichment_status='pending'`.

    Returns the number of rows actually updated. Used by `run_refresh`
    before it hands off to `run_bulk`. Wrapped in `WHERE enrichment_status
    = 'done'` to avoid stomping on rows that another agent moved to
    `'running'` between our SELECT and UPDATE.
    """
    if not company_ids:
        return 0
    rows = await fetch(
        """
        UPDATE companies
           SET enrichment_status = 'pending',
               updated_at        = now()
         WHERE id = ANY($1::uuid[])
           AND enrichment_status = 'done'
        RETURNING id
        """,
        company_ids,
    )
    return len(rows)


async def run_refresh(
    *,
    staleness_days: int = DEFAULT_STALENESS_DAYS,
    concurrency: int = 10,
    dry_run: bool = False,
) -> dict:
    """End-to-end: find stale → reset → re-bulk. Returns a counter dict."""
    stale_ids = await find_stale_company_ids(staleness_days=staleness_days)
    log.info(
        "refresh: %d stale companies (older than %d days)",
        len(stale_ids), staleness_days,
    )
    if not stale_ids:
        return {"stale": 0, "reset": 0, "enriched": 0, "errors": 0}

    if dry_run:
        return {"stale": len(stale_ids), "reset": 0, "enriched": 0, "errors": 0}

    reset_count = await reset_to_pending(stale_ids)
    rollup = await run_bulk(
        limit=len(stale_ids) + 10,
        concurrency=concurrency,
        dry_run=False,
    )
    return {
        "stale": len(stale_ids),
        "reset": reset_count,
        "enriched": rollup.enriched,
        "errors": rollup.errors,
        "signals_written": rollup.signals_written,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if not args.dry_run and not os.environ.get("FIRECRAWL_API_KEY"):
        log.error("FIRECRAWL_API_KEY not set — aborting (use --dry-run for plan-only)")
        return 2
    try:
        rollup = await run_refresh(
            staleness_days=args.staleness_days,
            concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
    finally:
        await close_pool()
    print(
        f"refresh_company_enrichment: stale={rollup['stale']} "
        f"reset={rollup['reset']} enriched={rollup.get('enriched', 0)} "
        f"errors={rollup.get('errors', 0)} (dry_run={args.dry_run})"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh stale company enrichment via Firecrawl."
    )
    parser.add_argument("--staleness-days", type=int, default=DEFAULT_STALENESS_DAYS)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_STALENESS_DAYS",
    "find_stale_company_ids",
    "reset_to_pending",
    "run_refresh",
]
