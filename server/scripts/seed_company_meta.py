"""Seed company enrichment columns from the static frontend build artifact.

`src/lib/company-meta.generated.ts` is a build artifact produced by the
existing `scripts/enrich-companies.mjs` job — it carries the manually
curated description / HQ / industry / partnerships for ~170 priority
companies that the demo cares about. The plan (COMPANY_ENRICHMENT_PLAN.md
Step 2) calls for one-time pivoting that data into the live `companies`
table so the backend has the same context the frontend has been displaying.

## What this writes

For each `(canonical_name, GeneratedCompanyMeta)` pair in the TS file we
UPDATE the matching `companies` row with:

  * description (string, may be NULL)
  * hq_city, hq_state, hq_country (TEXT)
  * industry_tags (single-element TEXT[])
  * employee_count_estimate (parsed from "100k+" / "10k-100k" → INT bucket)
  * partnerships (TEXT[])
  * enrichment_status = 'done'  (static seed counts as enriched)

## Why use the existing companies row

Per CLAUDE.md Decision rules: never create duplicate records when one
already exists. We match on `canonical_name` (the existing canonical
column) and UPDATE in place. New companies that exist in the TS file
but NOT in `companies` are logged + skipped — they need to land via the
normal entity-resolution path first.

## Idempotency

Pure UPDATE — no INSERTs, no DELETEs. Re-running on the same data
overwrites the same fields and re-stamps `enrichment_last_run`.
Safe to run as many times as the operator wants.

## Usage

    cd server
    DATABASE_URL=...  uv run python -m scripts.seed_company_meta --dry-run
    DATABASE_URL=...  uv run python -m scripts.seed_company_meta
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

# Allow running as a script (`python server/scripts/seed_company_meta.py`)
# or as a module (`python -m scripts.seed_company_meta`). The latter is the
# documented invocation.
if __package__ in (None, ""):
    _SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _SERVER_DIR not in sys.path:
        sys.path.insert(0, _SERVER_DIR)

from credence.db import close_pool, fetch  # noqa: E402

log = logging.getLogger(__name__)


# ── Path resolution ─────────────────────────────────────────────────────────


def _repo_root() -> Path:
    """Walk upward from this file to find the repo root.

    Repo root is the directory that contains both `src/` and `server/`.
    Falls back to two-levels-up if the marker isn't found (so a test that
    monkeypatches the file location doesn't crash).
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "src").is_dir() and (parent / "server").is_dir():
            return parent
    return here.parents[2]


def _ts_path() -> Path:
    return _repo_root() / "src" / "lib" / "company-meta.generated.ts"


# ── Pure parsers ────────────────────────────────────────────────────────────


# The TS file is machine-generated and has a stable shape:
#   export const GENERATED_COMPANY_META: Record<string, GeneratedCompanyMeta> = { ... };
# Each value is a JSON-compatible object literal (keys quoted by the
# generator). We extract the {...} block and json.loads it.
_EXPORT_RE = re.compile(
    r"export\s+const\s+(?:COMPANY_META|GENERATED_COMPANY_META)\s*"
    r"(?::\s*[^=]+)?"
    r"\s*=\s*(\{[\s\S]+?\})\s*;?\s*\Z",
    re.MULTILINE,
)


def parse_company_meta(ts_text: str) -> dict[str, dict[str, Any]]:
    """Extract the COMPANY_META object literal from the TS file.

    Handles both legacy (`COMPANY_META`) and current
    (`GENERATED_COMPANY_META`) export names. The regex grabs everything
    from the opening brace to the trailing semicolon — the file always
    ends with the export so we anchor on `\\Z`.

    The TS generator emits two non-JSON shapes we need to rewrite:
      1. Trailing commas inside objects/arrays.
      2. Bare-identifier inner keys (`country: "US"` not `"country": "US"`).
    Outer keys are already quoted in the file because company names have
    spaces; only the inner property names are bare.
    """
    match = _EXPORT_RE.search(ts_text)
    if not match:
        raise ValueError(
            "Could not find COMPANY_META / GENERATED_COMPANY_META export — "
            "is src/lib/company-meta.generated.ts present and well-formed?"
        )
    blob = match.group(1)
    # Strip trailing commas inside objects/arrays.
    blob = re.sub(r",(\s*[}\]])", r"\1", blob)
    # Quote bare-identifier object keys: `{ country: ` → `{ "country": `.
    # Match `{` or `,` followed by whitespace + identifier + `:` and
    # rewrite. Anchor on `[{,]` to skip inside string literals (those
    # are preceded by a different character like `:` or `[`).
    blob = re.sub(
        r'(?P<lead>[{,]\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:',
        r'\g<lead>"\g<key>":',
        blob,
    )
    return json.loads(blob)


# Map a free-form employee_count_estimate string to a representative int.
# The TS file uses bucketed strings ("10k-100k", "100k+"). The DB column
# is INTEGER, so we land a midpoint or floor. None when unparseable.
_EMPLOYEE_BUCKETS: dict[str, int] = {
    # Legacy / fine-grained labels (kept for back-compat with older
    # generator runs and unit tests).
    "1-10": 5,
    "10-50": 30,
    "50-200": 125,
    "200-500": 350,
    "500-1k": 750,
    "1k-5k": 3000,
    "5k-10k": 7500,
    # Live `enrich-companies.mjs` output as of 2026-05 — these are the
    # five buckets the generator actually emits today (verified against
    # the checked-in `src/lib/company-meta.generated.ts`). Keep both
    # sets so a generator regression doesn't silently zero-out the
    # `employee_count_estimate` column for ~97/168 companies.
    "<100": 50,
    "100-1k": 500,
    "1k-10k": 5000,
    "10k-100k": 50000,
    "100k+": 150000,
}


def parse_employee_count(value: Any) -> int | None:
    """Best-effort parse of the bucket string into an int. None for unknown."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    return _EMPLOYEE_BUCKETS.get(value.strip())


def build_update_row(meta: dict[str, Any]) -> dict[str, Any]:
    """Pure: shape one TS metadata blob into the columns we'll UPDATE.

    Filters out fields we don't care about (prospect_count is a TS-side
    counter, not a column we own).
    """
    industry = meta.get("industry")
    return {
        "description":             meta.get("description"),
        "hq_city":                 meta.get("hq_city"),
        "hq_state":                meta.get("state"),
        "hq_country":              meta.get("country"),
        "industry_tags":           [industry] if industry else [],
        "employee_count_estimate": parse_employee_count(meta.get("employee_count_estimate")),
        "partnerships":            meta.get("partnerships", []),
        "enrichment_status":       "done",
    }


# ── DB writer ───────────────────────────────────────────────────────────────


async def seed_companies(*, dry_run: bool = False) -> dict[str, int]:
    """Pivot the TS metadata file into `companies` UPDATEs.

    Returns a counter rollup `{matched, updated, missing, errors}`.

    `matched` = TS rows whose canonical_name resolved to a `companies.id`.
    `updated` = `matched` minus dry-run skips.
    `missing` = TS rows with no matching companies row (logged for the
                operator — these companies need entity resolution first).
    `errors`  = per-row UPDATE failures (logged + counted, don't abort).
    """
    ts_path = _ts_path()
    if not ts_path.exists():
        raise FileNotFoundError(f"missing seed source: {ts_path}")
    ts_text = ts_path.read_text()
    meta_by_name = parse_company_meta(ts_text)
    log.info("seed: parsed %d company entries from %s", len(meta_by_name), ts_path)

    counters = {"matched": 0, "updated": 0, "missing": 0, "errors": 0}

    # Bulk-resolve canonical_name → id in one query so we don't make
    # 170 individual SELECTs. The IN list is bounded by the TS file size
    # (~170-300 entries), well within asyncpg's bind-parameter limit.
    names = list(meta_by_name.keys())
    rows = await fetch(
        """
        SELECT id, canonical_name
        FROM companies
        WHERE canonical_name = ANY($1::text[])
        """,
        names,
    )
    id_by_name = {row["canonical_name"]: row["id"] for row in rows}

    for canonical_name, meta in meta_by_name.items():
        company_id = id_by_name.get(canonical_name)
        if company_id is None:
            counters["missing"] += 1
            log.info("seed: skip %r — not in companies table", canonical_name)
            continue
        counters["matched"] += 1

        update_row = build_update_row(meta)
        if dry_run:
            log.info("seed: [DRY] would update %s (%s)", canonical_name, company_id)
            continue

        # Per-row UPDATE — small N, no need to bulk; the DB round trip is the
        # cost driver and 170 round trips is ~5s on the pooler. Using
        # named-parameter UPDATE keeps the SQL legible.
        try:
            await fetch(
                """
                UPDATE companies SET
                  description             = COALESCE($2, description),
                  hq_city                 = COALESCE($3, hq_city),
                  hq_state                = COALESCE($4, hq_state),
                  hq_country              = COALESCE($5, hq_country),
                  industry_tags           = $6,
                  employee_count_estimate = COALESCE($7, employee_count_estimate),
                  partnerships            = $8,
                  enrichment_status       = $9,
                  enrichment_last_run     = now(),
                  updated_at              = now()
                WHERE id = $1
                """,
                company_id,
                update_row["description"],
                update_row["hq_city"],
                update_row["hq_state"],
                update_row["hq_country"],
                update_row["industry_tags"],
                update_row["employee_count_estimate"],
                update_row["partnerships"],
                update_row["enrichment_status"],
            )
            counters["updated"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("seed: UPDATE failed for %s — %s", canonical_name, exc)
            counters["errors"] += 1

    return counters


# ── CLI ─────────────────────────────────────────────────────────────────────


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        counters = await seed_companies(dry_run=args.dry_run)
    finally:
        await close_pool()
    print(
        f"seed_company_meta: matched={counters['matched']} "
        f"updated={counters['updated']} missing={counters['missing']} "
        f"errors={counters['errors']} (dry_run={args.dry_run})"
    )
    return 0 if counters["errors"] == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time pivot of company-meta.generated.ts into companies table."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report intent without writing")
    args = parser.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()


__all__ = [
    "build_update_row",
    "parse_company_meta",
    "parse_employee_count",
    "seed_companies",
]
