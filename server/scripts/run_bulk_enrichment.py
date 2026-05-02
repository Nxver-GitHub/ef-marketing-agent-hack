"""Bulk Tier-1 enrichment runner — 60 companies via Apify Full+email.

Two-phase execution:

1. **Slug validation pass** — small sync probe (5 profiles each, ~$3.60
   total) to surface bad LinkedIn slugs BEFORE committing the bulk run.
   harvestapi returns ``[]`` silently for unknown slugs (verified live
   2026-04-30 with "marvell-semiconductor" → 404, "marvell" → 8839).
2. **Bulk pass** — only-known-good slugs, max_persons=500 per task doc,
   8-way parallelism (Apify Starter caps at 32 concurrent runs; we
   leave headroom for other system traffic).

Streams progress every batch + writes a per-company result JSON line to
``/tmp/credence-bulk-enrichment.jsonl`` so any crash mid-run preserves
the audit trail.

Usage:
    cd server && uv run --env-file ../.env.local python -m scripts.run_bulk_enrichment

Cost ceiling: ~$640 for full 60 × 500 profile run with Apollo gap-fill
enabled. Apify Starter ($29/mo) prepays first $29 — overage auto-billed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import UUID

from credence.enrichment._target_companies import (
    TARGET_COMPANIES,
    TargetCompany,
)
from credence.enrichment.apify import (
    MODE_SHORT,
    find_company_employees_sync,
)
from credence.enrichment.pipeline import (
    CompanyEnrichmentResult,
    enrich_company,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("bulk_enrichment")

# Default tenant
DEFAULT_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")

# Tunables
MAX_PERSONS_PER_COMPANY = 500
PARALLELISM = 8                # Apify Starter cap is 32; leave headroom
RESULTS_FILE = "/tmp/credence-bulk-enrichment.jsonl"


def _to_jsonable(obj: Any) -> Any:
    """asdict() but tolerant of nested dataclass values."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


def _result_summary(c: TargetCompany, r: CompanyEnrichmentResult) -> str:
    wr = r.write_result
    write_summary = (
        f"persons={wr.persons_inserted}+{wr.persons_updated} "
        f"emp={wr.employment_periods_inserted}+{wr.employment_periods_updated} "
        f"edu={wr.education_periods_inserted}+{wr.education_periods_updated}"
        if wr else "no-write"
    )
    return (
        f"  ✓ {c.canonical_name:32s} | "
        f"profiles={r.apify_profiles_pulled:3d} | "
        f"cost={r.total_cost_cents:4d}¢ | "
        f"{write_summary}"
        + (f" | err={len(r.errors)}" if r.errors else "")
    )


# ─── Phase 1: slug validation ──────────────────────────────────────────────


async def validate_slug(company: TargetCompany) -> tuple[TargetCompany, int]:
    """Sync probe of 5 profiles in Short mode. Returns ``(company, count)``.

    Short mode is $4/1k = 0.4¢/profile × 5 = 2¢ per probe → $1.20 for all
    60. Tells us in ~10s whether the slug is good without hitting the
    bulk cost budget. count=0 means slug is invalid (or company genuinely
    has no LinkedIn presence — same outcome for our purposes).
    """
    try:
        result = await find_company_employees_sync(
            company.linkedin_url,
            max_items=5,
            mode=MODE_SHORT,
            timeout_seconds=120.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("validate_slug %s raised: %s", company.linkedin_slug, exc)
        return company, 0
    return company, (len(result.profiles) if result else 0)


async def validate_all_slugs(
    companies: list[TargetCompany],
    *,
    parallelism: int = 8,
) -> tuple[list[TargetCompany], list[TargetCompany]]:
    """Validate every company's slug. Returns ``(good, bad)`` lists."""
    sem = asyncio.Semaphore(parallelism)
    results: list[tuple[TargetCompany, int]] = []

    async def _bounded(c: TargetCompany) -> None:
        async with sem:
            results.append(await validate_slug(c))

    await asyncio.gather(*(_bounded(c) for c in companies))

    good = [c for c, n in results if n > 0]
    bad = [c for c, n in results if n == 0]
    return good, bad


# ─── Phase 2: bulk enrichment ──────────────────────────────────────────────


async def run_one(
    company: TargetCompany,
    *,
    account_id: UUID,
    max_persons: int,
    do_apollo: bool,
) -> CompanyEnrichmentResult:
    """Run enrich_company for one target. Returns the result regardless
    of success/error so we can audit failures in the JSONL log."""
    try:
        return await enrich_company(
            company.linkedin_url,
            account_id=account_id,
            company_name=company.canonical_name,
            max_persons=max_persons,
            do_apollo_email_gapfill=do_apollo,
            do_github=False,        # GitHub usernames not discoverable in bulk
        )
    except Exception as exc:  # noqa: BLE001 — partial-results contract
        logger.exception("enrich_company crashed for %s", company.canonical_name)
        # Return a degraded result so the JSONL log still gets a row
        return CompanyEnrichmentResult(
            company_url=company.linkedin_url,
            company_name=company.canonical_name,
            errors=[f"enrich_company crashed: {exc}"],
        )


async def run_bulk(
    companies: list[TargetCompany],
    *,
    account_id: UUID = DEFAULT_ACCOUNT_ID,
    max_persons: int = MAX_PERSONS_PER_COMPANY,
    parallelism: int = PARALLELISM,
    do_apollo: bool = True,
) -> list[tuple[TargetCompany, CompanyEnrichmentResult]]:
    """Fire enrich_company for every company with bounded parallelism."""
    sem = asyncio.Semaphore(parallelism)
    log_fh = open(RESULTS_FILE, "a")
    results: list[tuple[TargetCompany, CompanyEnrichmentResult]] = []
    completed = 0
    total_cost = 0
    start = time.time()

    async def _bounded(c: TargetCompany) -> None:
        nonlocal completed, total_cost
        async with sem:
            t0 = time.time()
            r = await run_one(
                c,
                account_id=account_id,
                max_persons=max_persons,
                do_apollo=do_apollo,
            )
            elapsed = time.time() - t0
            results.append((c, r))
            completed += 1
            total_cost += r.total_cost_cents

            # Stream progress
            print(
                _result_summary(c, r) +
                f" | {elapsed:5.1f}s | "
                f"[{completed}/{len(companies)} | total ${total_cost/100:.2f}]"
            )
            sys.stdout.flush()

            # Append to JSONL audit log
            log_fh.write(json.dumps({
                "company": c.canonical_name,
                "linkedin_slug": c.linkedin_slug,
                "tier": c.tier,
                "priority": c.priority,
                "result": _to_jsonable(r),
                "elapsed_s": round(elapsed, 1),
            }) + "\n")
            log_fh.flush()

    await asyncio.gather(*(_bounded(c) for c in companies))
    log_fh.close()

    total_elapsed = time.time() - start
    print()
    print(f"=== bulk run complete: {completed}/{len(companies)} in {total_elapsed:.0f}s ===")
    print(f"=== total spend: ${total_cost/100:.2f} ===")
    return results


# ─── Main ──────────────────────────────────────────────────────────────────


async def main() -> None:
    if not os.environ.get("APIFY_TOKEN"):
        sys.exit("ERROR: APIFY_TOKEN not in env (load .env.local first)")

    companies = list(TARGET_COMPANIES)
    print(f"=== Phase 1: validating {len(companies)} LinkedIn slugs (~$1.20 spend) ===")
    good, bad = await validate_all_slugs(companies)
    print(f"  good slugs:  {len(good)}/{len(companies)}")
    if bad:
        print("  BAD SLUGS — fix _target_companies.py and re-run:")
        for c in bad:
            print(f"    ✗ {c.canonical_name:32s} → /company/{c.linkedin_slug}/")
    print()

    if not good:
        sys.exit("ERROR: no good slugs; aborting")

    print(f"=== Phase 2: bulk enrichment of {len(good)} companies ===")
    print(f"  max_persons/co={MAX_PERSONS_PER_COMPANY}, parallelism={PARALLELISM}")
    print(f"  estimated cost: ~${len(good) * 10.60:.0f} (Apify Full+email + Apollo gap-fill)")
    print()

    await run_bulk(good)
    print()
    print(f"=== JSONL audit log: {RESULTS_FILE} ===")


if __name__ == "__main__":
    asyncio.run(main())
