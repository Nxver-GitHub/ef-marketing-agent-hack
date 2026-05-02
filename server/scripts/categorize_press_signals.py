"""Backfill `category` into the JSONB blob of every press_release signal row.

The frontend mockup tags each press item with one of six categories (earnings,
product_launch, partnership, research, co_mention_signal, general). Rather
than recompute the tag at render time, we materialize it once into the
existing `company_signals.structured_value` JSONB blob — no schema change
required.

## Idempotency

The SELECT filters out rows whose `structured_value` already has a `category`
key, so re-running this script is safe. Only newly-inserted press releases
(or rows whose category was deliberately stripped) get reclassified.

## Why jsonb_set instead of overwrite

`UPDATE … SET structured_value = $blob` would replace the entire JSONB
payload, racing with any other writer that mutated the row in between. Using
`jsonb_set(structured_value, '{category}', $val::jsonb)` patches a single
key in-place, which the planner can do as a single tuple update.

## Usage

    cd server
    DATABASE_URL=...  uv run python -m scripts.categorize_press_signals --dry-run
    DATABASE_URL=...  uv run python -m scripts.categorize_press_signals --limit 500
    DATABASE_URL=...  uv run python -m scripts.categorize_press_signals
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter
from typing import Any

# Allow `python server/scripts/categorize_press_signals.py` invocation in
# addition to the documented `python -m scripts.categorize_press_signals`.
if __package__ in (None, ""):
    _SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _SERVER_DIR not in sys.path:
        sys.path.insert(0, _SERVER_DIR)

from credence.db import close_pool, execute, fetch  # noqa: E402
from credence.enrichment.press_classifier import classify_press_release  # noqa: E402

log = logging.getLogger(__name__)


# ── Core routine ────────────────────────────────────────────────────────────


async def categorize_press_signals(
    *, dry_run: bool = False, limit: int | None = None
) -> dict[str, Any]:
    """Classify every un-categorized press_release row and patch its JSONB.

    Args:
        dry_run: When True, report intent and per-category breakdown without
            issuing any UPDATEs.
        limit: Optional cap on rows pulled from the DB for this run. None =
            process every unclassified row.

    Returns:
        Counter rollup: {processed, classified, skipped_already_done,
        errors, by_category: {<cat>: <count>}}.
    """
    # The `NOT (structured_value ? 'category')` clause is the idempotency
    # gate — it skips rows that already carry a category, so a fresh run
    # only touches new arrivals.
    base_sql = """
        SELECT id, structured_value
        FROM company_signals
        WHERE signal_type = 'press_release'
          AND structured_value IS NOT NULL
          AND NOT (structured_value ? 'category')
        ORDER BY id
    """
    if limit is not None:
        rows = await fetch(base_sql + " LIMIT $1", limit)
    else:
        rows = await fetch(base_sql)

    log.info("categorize: fetched %d unclassified press_release rows", len(rows))

    counters: dict[str, Any] = {
        "processed": 0,
        "classified": 0,
        "skipped_already_done": 0,  # surfaced for symmetry; SELECT filters them out upfront
        "errors": 0,
        "by_category": Counter(),
    }

    for row in rows:
        counters["processed"] += 1
        row_id = row["id"]
        payload_raw = row["structured_value"]

        # asyncpg returns JSONB as already-parsed dict by default, but some
        # connection configs hand back the raw text. Normalize defensively.
        try:
            payload = (
                json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
            )
        except (TypeError, ValueError) as exc:
            log.warning("categorize: row %s — un-parseable structured_value: %s", row_id, exc)
            counters["errors"] += 1
            continue

        try:
            category = classify_press_release(payload)
        except Exception as exc:  # noqa: BLE001 — never let one bad row halt the batch
            log.warning("categorize: row %s — classifier raised: %s", row_id, exc)
            counters["errors"] += 1
            continue

        counters["by_category"][category] += 1

        if dry_run:
            counters["classified"] += 1
            continue

        try:
            # `to_jsonb($2::text)` would wrap the category as `"earnings"` —
            # exactly what we want to store as the JSONB scalar value.
            await execute(
                """
                UPDATE company_signals
                   SET structured_value = jsonb_set(
                           structured_value, '{category}', to_jsonb($2::text), true
                       )
                 WHERE id = $1
                """,
                row_id,
                category,
            )
            counters["classified"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("categorize: row %s — UPDATE failed: %s", row_id, exc)
            counters["errors"] += 1

    # Convert Counter → plain dict for cleaner JSON-style printing.
    counters["by_category"] = dict(counters["by_category"])
    return counters


# ── CLI ─────────────────────────────────────────────────────────────────────


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        counters = await categorize_press_signals(dry_run=args.dry_run, limit=args.limit)
    finally:
        await close_pool()

    print(
        "categorize_press_signals: "
        f"processed={counters['processed']} "
        f"classified={counters['classified']} "
        f"skipped_already_done={counters['skipped_already_done']} "
        f"errors={counters['errors']} "
        f"(dry_run={args.dry_run}, limit={args.limit})"
    )
    if counters["by_category"]:
        breakdown = ", ".join(
            f"{cat}={n}" for cat, n in sorted(counters["by_category"].items())
        )
        print(f"  by_category: {breakdown}")
    return 0 if counters["errors"] == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Classify press_release company_signals rows into one of six "
            "categories and patch the JSONB blob in-place. Idempotent — "
            "skips rows that already carry a `category` key."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify and report the per-category breakdown without writing.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of rows pulled in this run. Default: process all.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()


__all__ = ["categorize_press_signals"]
