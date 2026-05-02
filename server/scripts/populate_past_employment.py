"""Targeted past-employment backfill — Path 1 of msg 179.

Bypasses ``backfill_v3.py``'s slow per-prospect loop (~5h on 20k prospects)
by working directly on the 2000 prospects that already have ``persons`` rows.
For each ``career_history`` signal:

1. Walk role[] entries.
2. For each role with a non-empty ``company``, look up the matching v3
   ``companies.id`` by canonical name; ``INSERT`` if missing.
3. ``INSERT`` an ``employment_periods`` row with ``is_current=FALSE``,
   ``start_year`` / ``end_year`` from ``_parse_role_years``, fold-NULL-title
   on conflict so we never duplicate.

Idempotent. Safe to re-run. Reuses the parsing helpers from
``backfill_v3.py`` so canonical name + year semantics stay in lock-step.

Run::

    cd server && uv run python scripts/populate_past_employment.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
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

DEFAULT_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")


@dataclass(frozen=True, slots=True)
class PastRolePlan:
    person_id: UUID
    company_canonical: str
    company_raw: str
    title: str | None
    start_year: int | None
    end_year: int | None
    current_company_id: UUID | None  # so we skip current-employer roles


@dataclass(slots=True)
class Counters:
    plans_total: int = 0
    plans_skipped_current: int = 0
    companies_inserted: int = 0
    companies_matched: int = 0
    employments_inserted: int = 0
    employments_updated: int = 0
    employments_skipped: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


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


async def _ensure_company(
    conn: asyncpg.Connection,
    canonical: str,
    raw: str,
    cache: dict[str, UUID],
    counters: Counters,
    *,
    dry_run: bool,
) -> UUID | None:
    """Find-or-insert v3 ``companies`` row, returning its id."""
    if canonical in cache:
        return cache[canonical]
    existing = await conn.fetchval(
        "SELECT id FROM companies WHERE canonical_name = $1",
        canonical,
    )
    if existing is not None:
        counters.companies_matched += 1
        cache[canonical] = existing
        return existing
    if dry_run:
        return None
    new_id = await conn.fetchval(
        """
        INSERT INTO companies (canonical_name, name_variants, account_id)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        canonical, [raw] if raw and raw != canonical else [],
        DEFAULT_ACCOUNT_ID,
    )
    counters.companies_inserted += 1
    cache[canonical] = new_id
    return new_id


async def _upsert_past_employment(
    conn: asyncpg.Connection,
    plan: PastRolePlan,
    company_id: UUID,
    counters: Counters,
    *,
    dry_run: bool,
) -> None:
    """Insert past employment_period or fold years/title onto existing NULL row."""
    # Match either an exact title-equal row OR a legacy NULL-title row, prefer
    # exact match. Same shape as the patched ``backfill_v3.upsert_employment``.
    existing = await conn.fetchrow(
        """
        SELECT id, title, start_year, end_year FROM employment_periods
        WHERE person_id = $1 AND company_id = $2 AND is_current = FALSE
          AND (title IS NULL OR COALESCE(title, '') = COALESCE($3, ''))
        ORDER BY (title IS NOT NULL) DESC, id
        LIMIT 1
        """,
        plan.person_id, company_id, plan.title,
    )
    if existing is not None:
        if dry_run:
            counters.employments_updated += 1
            return
        # Idempotent retro-fill — never overwrite a non-NULL value.
        await conn.execute(
            """
            UPDATE employment_periods
            SET start_year        = COALESCE(start_year, $2),
                end_year          = COALESCE(end_year, $3),
                title             = COALESCE(title, $4),
                seniority_score   = COALESCE(seniority_score, $5),
                functional_domain = COALESCE(functional_domain, $6)
            WHERE id = $1
            """,
            existing["id"], plan.start_year, plan.end_year, plan.title,
            infer_seniority(plan.title) if plan.title else None,
            infer_functional_domain(plan.title) if plan.title else None,
        )
        counters.employments_updated += 1
        return

    if dry_run:
        counters.employments_inserted += 1
        return
    await conn.execute(
        """
        INSERT INTO employment_periods (
          person_id, company_id, title, functional_domain, seniority_score,
          is_current, account_id, start_year, end_year
        )
        VALUES ($1, $2, $3, $4, $5, FALSE, $6, $7, $8)
        """,
        plan.person_id, company_id, plan.title,
        infer_functional_domain(plan.title) if plan.title else None,
        infer_seniority(plan.title) if plan.title else None,
        DEFAULT_ACCOUNT_ID,
        plan.start_year, plan.end_year,
    )
    counters.employments_inserted += 1


def _build_plans(
    rows: list[asyncpg.Record],
) -> list[PastRolePlan]:
    """Walk career_history JSON → PastRolePlan rows."""
    plans: list[PastRolePlan] = []
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
        for role in roles:
            if not isinstance(role, dict):
                continue
            company_raw = role.get("company")
            if not isinstance(company_raw, str) or not company_raw.strip():
                continue
            canonical = normalize_company(company_raw)
            if not canonical:
                continue
            title_raw = role.get("role")
            title = (
                title_raw.strip()
                if isinstance(title_raw, str) and title_raw.strip()
                else None
            )
            start, end = _parse_role_years(role.get("years"))
            plans.append(PastRolePlan(
                person_id=row["person_id"],
                company_canonical=canonical,
                company_raw=company_raw.strip(),
                title=title,
                start_year=start,
                end_year=end,
                current_company_id=row["current_company_id"],
            ))
    return plans


async def main(dry_run: bool, limit: int | None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    dsn = _normalize_dsn(_load_dsn())
    counters = Counters()
    company_cache: dict[str, UUID] = {}

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        # Step 1: pull (person_id, current_company_id, value) for every
        # career_history signal whose prospect has a backfilled person.
        rows = await conn.fetch(
            """
            SELECT p.id AS person_id, p.current_company_id, s.value
            FROM signals s
            JOIN prospects pr ON pr.id = s.prospect_id::uuid
            JOIN persons p
              ON (p.linkedin_url IS NOT NULL AND p.linkedin_url = pr.linkedin_url)
              OR (p.linkedin_url IS NULL AND p.canonical_name = pr.name)
            WHERE s.signal_type = 'career_history'
            """,
        )
        log.info("Loaded %d career_history signals for backfilled persons", len(rows))

        plans = _build_plans(rows)
        if limit is not None:
            plans = plans[:limit]
        counters.plans_total = len(plans)
        log.info("Built %d past-role plans", counters.plans_total)
        if dry_run:
            for p in plans[:5]:
                log.info("  sample: %s", p)

        # Step 2: process plans — ensure_company + upsert_past_employment.
        # Skip roles that resolve to the person's current employer (those are
        # already covered by the is_current=TRUE row).
        for plan in plans:
            try:
                # current-employer guard
                if plan.current_company_id is not None and plan.company_canonical in company_cache:
                    if company_cache[plan.company_canonical] == plan.current_company_id:
                        counters.plans_skipped_current += 1
                        continue

                async with conn.transaction():
                    company_id = await _ensure_company(
                        conn, plan.company_canonical, plan.company_raw,
                        company_cache, counters, dry_run=dry_run,
                    )
                    if company_id is None:
                        # dry-run path with no company — record as skipped
                        counters.employments_skipped += 1
                        continue
                    if company_id == plan.current_company_id:
                        counters.plans_skipped_current += 1
                        continue
                    await _upsert_past_employment(
                        conn, plan, company_id, counters, dry_run=dry_run,
                    )
            except Exception as exc:
                counters.failures.append((str(plan.person_id), repr(exc)))
                log.exception(
                    "past_employment upsert failed for person %s @ %s",
                    plan.person_id, plan.company_canonical,
                )

        log.info(
            "Counters — plans=%d skipped_current=%d "
            "companies[new=%d, matched=%d] employments[new=%d, updated=%d, skipped=%d] "
            "failures=%d",
            counters.plans_total,
            counters.plans_skipped_current,
            counters.companies_inserted,
            counters.companies_matched,
            counters.employments_inserted,
            counters.employments_updated,
            counters.employments_skipped,
            len(counters.failures),
        )

        # Final state
        with_year = await conn.fetchval(
            "SELECT count(*) FROM employment_periods WHERE start_year IS NOT NULL"
        )
        past = await conn.fetchval(
            "SELECT count(*) FROM employment_periods WHERE is_current = FALSE"
        )
        total = await conn.fetchval(
            "SELECT count(*) FROM employment_periods"
        )
        log.info(
            "Post-state: total=%d past=%d with_year=%d",
            total, past, with_year,
        )
    finally:
        await conn.close()

    return 1 if counters.failures else 0


def _argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Targeted past-employment backfill from career_history signals.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Build plans + count what would happen; write nothing.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Cap number of role plans processed (for staged runs).",
    )
    return p


if __name__ == "__main__":
    args = _argparser().parse_args()
    sys.exit(asyncio.run(main(dry_run=args.dry_run, limit=args.limit)))
