"""Fast year-fill for employment_periods, bypassing the per-prospect backfill loop.

The full ``backfill_v3`` re-runs ensure_company + ensure_person + role lookup
for every prospect in the v2 ``prospects`` table (~20k rows). On Supabase
pgbouncer, that's ~5h of round-trips and we don't need most of it: we
already have 2000 ``persons`` rows backfilled, the gap is just NULL years
on past ``employment_periods`` rows.

This script does ONE pass:

1. Pull all (person_id, company_canonical, parsed_start, parsed_end) tuples
   from v2 ``signals.career_history`` rows whose ``prospect_id`` matches an
   existing ``persons.id``.
2. UPDATE the matching ``employment_periods`` rows where ``start_year IS NULL``,
   matching by (person_id, company_id, is_current=FALSE) and folding in
   NULL-title rows (so legacy ``past_companies`` placeholders pick up years
   too).

Idempotent. Safe to re-run. Reads from same career_history → year extraction
helpers as ``backfill_v3.py`` so the parsing stays in lock-step.

Run::

    cd server && uv run python scripts/fill_employment_years.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from uuid import UUID

import asyncpg

# Make ``credence`` importable when run from server/scripts
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from credence.backfill_v3 import (
    _parse_role_years,
    infer_functional_domain,
    infer_seniority,
    normalize_company,
)

log = logging.getLogger(__name__)


def _normalize_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _load_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return dsn
    env_path = Path(__file__).resolve().parents[2] / ".env.local"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("DATABASE_URL not set")


async def main(dry_run: bool) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    dsn = _normalize_dsn(_load_dsn())

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        # Step 1: pull every (prospect_id, role) where the prospect has a
        # backfilled person record. persons.id is a fresh UUID; the linkage is
        # through ``linkedin_url`` (or canonical name + current_company_id as
        # a fallback when LinkedIn is missing).
        rows = await conn.fetch(
            """
            SELECT p.id AS person_id, s.value
            FROM signals s
            JOIN prospects pr ON pr.id = s.prospect_id::uuid
            JOIN persons p
              ON (p.linkedin_url IS NOT NULL AND p.linkedin_url = pr.linkedin_url)
              OR (p.linkedin_url IS NULL AND p.canonical_name = pr.name)
            WHERE s.signal_type = 'career_history'
            """,
        )
        log.info("Loaded %d career_history signals for backfilled persons", len(rows))

        # Step 2: walk roles, build (person_id, canonical_company, start, end, title) plan.
        # Direct asyncpg connection (no credence pool init) returns JSONB as str —
        # parse it before walking.
        plan: list[tuple[UUID, str, int | None, int | None, str | None]] = []
        for row in rows:
            value = row["value"]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    continue
            if not isinstance(value, dict):
                continue
            roles = value.get("roles")
            if not isinstance(roles, list):
                continue
            for r in roles:
                if not isinstance(r, dict):
                    continue
                co = r.get("company")
                if not isinstance(co, str) or not co.strip():
                    continue
                start, end = _parse_role_years(r.get("years"))
                if start is None and end is None:
                    continue  # nothing to fill
                title_raw = r.get("role")
                title = (
                    title_raw.strip()
                    if isinstance(title_raw, str) and title_raw.strip()
                    else None
                )
                plan.append((
                    row["person_id"],
                    normalize_company(co),
                    start, end, title,
                ))

        log.info("Year-bearing role plan size: %d", len(plan))
        if dry_run:
            sample = plan[:5]
            for s in sample:
                log.info("  sample plan row: %s", s)
            await conn.close()
            return 0

        # Step 3: UPDATE matching employment_periods. We resolve canonical_company
        # → companies.id inside the same query; only fill NULL years.
        updated = 0
        for person_id, canonical_co, start, end, title in plan:
            result = await conn.execute(
                """
                UPDATE employment_periods ep
                SET start_year        = COALESCE(ep.start_year, $3),
                    end_year          = COALESCE(ep.end_year, $4),
                    title             = COALESCE(ep.title, $5),
                    seniority_score   = COALESCE(ep.seniority_score, $6),
                    functional_domain = COALESCE(ep.functional_domain, $7)
                FROM companies c
                WHERE c.canonical_name = $2
                  AND ep.company_id    = c.id
                  AND ep.person_id     = $1
                  AND ep.is_current    = FALSE
                  AND (ep.title IS NULL OR COALESCE(ep.title, '') = COALESCE($5, ''))
                  AND (ep.start_year IS NULL OR ep.end_year IS NULL OR ep.title IS NULL)
                """,
                person_id, canonical_co, start, end, title,
                infer_seniority(title) if title else None,
                infer_functional_domain(title) if title else None,
            )
            # asyncpg execute returns "UPDATE n"
            try:
                updated += int(result.split()[-1])
            except (ValueError, IndexError):
                pass

        log.info("Updated %d employment_periods rows", updated)

        with_year = await conn.fetchval(
            "SELECT count(*) FROM employment_periods WHERE start_year IS NOT NULL"
        )
        total = await conn.fetchval(
            "SELECT count(*) FROM employment_periods"
        )
        log.info("Post-state: %d / %d employment_periods have start_year", with_year, total)
    finally:
        await conn.close()

    return 0


def _argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fast SQL-direct year-fill for employment_periods.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print plan size + sample rows; write nothing.",
    )
    return p


if __name__ == "__main__":
    args = _argparser().parse_args()
    sys.exit(asyncio.run(main(dry_run=args.dry_run)))
