"""Phase 1 — bulk Apify enrichment for the 27 untouched target companies.

Identifies "untouched" as companies in TARGET_COMPANIES with <50 persons
who currently have a linkedin_url AND a current employment_period at that
company (canonical_name match, case-insensitive).

Hard constraint per user (2026-05-01): 500 persons/company max, no
overrun. MODE_FULL_EMAIL ($12/1k) for full enrichment. Apollo gap-fill
remains on for any missing emails post-Apify.

Reuses existing infrastructure:
- credence.enrichment._target_companies.TARGET_COMPANIES (59 entries)
- scripts.run_bulk_enrichment.validate_all_slugs (slug probe)
- scripts.run_bulk_enrichment.run_bulk (the runner — defaults to
  MODE_FULL_EMAIL via pipeline.enrich_company defaults)

Usage:
    cd server && uv run --env-file ../.env.local python \\
      -m scripts.run_untouched_enrichment
"""
from __future__ import annotations

import asyncio
import os
import sys
from uuid import UUID

import asyncpg

from credence.enrichment._target_companies import (
    TARGET_COMPANIES,
    TargetCompany,
)
from scripts.run_bulk_enrichment import (
    DEFAULT_ACCOUNT_ID,
    RESULTS_FILE,
    run_bulk,
    validate_all_slugs,
)

# Hard cap from user — DO NOT increase without explicit user authorization.
HARD_CAP_PER_COMPANY = 500
MIN_ENRICHED_THRESHOLD = 50  # below this = "untouched"


async def find_untouched(
    candidates: tuple[TargetCompany, ...],
) -> list[TargetCompany]:
    """Return only candidates with <MIN_ENRICHED_THRESHOLD enriched persons."""
    dsn = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg:", "postgresql:",
    )
    conn = await asyncpg.connect(dsn)
    try:
        untouched: list[TargetCompany] = []
        for c in candidates:
            cnt = await conn.fetchval(
                """
                SELECT count(DISTINCT ep.person_id)
                FROM public.companies co
                JOIN public.employment_periods ep ON ep.company_id = co.id
                JOIN public.persons p ON p.id = ep.person_id
                WHERE p.linkedin_url IS NOT NULL
                  AND ep.is_current = TRUE
                  AND lower(co.canonical_name) = lower($1)
                """,
                c.canonical_name,
            )
            if (cnt or 0) < MIN_ENRICHED_THRESHOLD:
                untouched.append(c)
        return untouched
    finally:
        await conn.close()


async def main() -> None:
    if not os.environ.get("APIFY_TOKEN"):
        sys.exit("ERROR: APIFY_TOKEN not in env (load .env.local first)")

    print(f"=== Phase 0: identify untouched companies ===")
    untouched = await find_untouched(TARGET_COMPANIES)
    print(f"  candidates:  {len(TARGET_COMPANIES)}")
    print(f"  untouched:   {len(untouched)}  (<{MIN_ENRICHED_THRESHOLD} enriched persons)")
    print()
    for c in untouched:
        print(f"  - {c.canonical_name:40s} /company/{c.linkedin_slug}/")
    print()

    if not untouched:
        sys.exit("nothing to do — all targets are enriched")

    print(f"=== Phase 1: validating {len(untouched)} LinkedIn slugs (~$1.20 spend) ===")
    good, bad = await validate_all_slugs(list(untouched))
    print(f"  good slugs:  {len(good)}/{len(untouched)}")
    if bad:
        print("  BAD SLUGS — fix _target_companies.py and re-run:")
        for c in bad:
            print(f"    ✗ {c.canonical_name:40s} → /company/{c.linkedin_slug}/")
    print()

    if not good:
        sys.exit("ERROR: no good slugs; aborting")

    # Cost projection at MODE_FULL_EMAIL ($12/1k) — pipeline.enrich_company default
    est_max = len(good) * HARD_CAP_PER_COMPANY * 12 / 1000
    print(f"=== Phase 2: bulk enrichment of {len(good)} companies ===")
    print(f"  max_persons/company={HARD_CAP_PER_COMPANY} (HARD CAP)")
    print(f"  max profiles total: {len(good) * HARD_CAP_PER_COMPANY:,}")
    print(f"  cost ceiling: ~${est_max:.0f} at MODE_FULL_EMAIL ($12/1k)")
    print()

    await run_bulk(
        good,
        account_id=DEFAULT_ACCOUNT_ID,
        max_persons=HARD_CAP_PER_COMPANY,
        parallelism=8,
        do_apollo=True,
    )
    print()
    print(f"=== JSONL audit log: {RESULTS_FILE} ===")


if __name__ == "__main__":
    asyncio.run(main())
