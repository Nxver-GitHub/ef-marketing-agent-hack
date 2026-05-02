"""Phase 1 — bulk LinkedIn enrichment via apimaestro/* actors.

Replaces ``run_untouched_enrichment.py`` which used harvestapi/* (gated
at 10 free runs per actor — verified live 2026-05-01 via run logs).
Apimaestro's actors don't have that gate.

Two-stage flow per company:
  A. ``list_company_employees`` (max 500/co)  → $0.01 × N items
  B. ``fetch_profile_detail`` per employee     → $0.005 × N items

Total per fully-enriched person: $0.015. For 27 companies × 500: $202.50.

Hard cap: 500/company (user constraint, 2026-05-01).

Usage:
    cd server && uv run --env-file ../.env.local python \\
      -m scripts.run_apimaestro_enrichment
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from credence.enrichment._target_companies import (
    TARGET_COMPANIES,
    TargetCompany,
)
from credence.enrichment.apify_apimaestro import (
    fetch_profile_detail,
    list_company_employees,
    to_canonical_person,
)
from credence.enrichment.writer import write_canonical_persons

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("apimaestro_bulk")

DEFAULT_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")

# User constraint — DO NOT increase without explicit re-authorization.
# Per user 2026-05-01 Option B: 27 untouched cos (empty + sparse) at
# ~350/co fits the $149.63 monthly Apify budget (27 × 350 × $0.015 =
# $141.75 + ~$7 safety). Remaining 26 cos (partial 50-499) need a
# second billing cycle to reach the 500-per-co target.
HARD_CAP_PER_COMPANY = 350
MIN_ENRICHED_THRESHOLD = 50  # below this → "untouched"

# Concurrency
COMPANIES_IN_FLIGHT = 4    # Stage A is per-company sequential, this gates how
                           # many companies overlap end-to-end.
PROFILES_PER_COMPANY = 8   # Stage B parallelism within one company

RESULTS_FILE = "/tmp/credence-apimaestro-enrichment.jsonl"


async def find_untouched(candidates: tuple[TargetCompany, ...]) -> list[TargetCompany]:
    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg:", "postgresql:")
    conn = await asyncpg.connect(dsn)
    try:
        out: list[TargetCompany] = []
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
                out.append(c)
        return out
    finally:
        await conn.close()


async def enrich_one_company(
    c: TargetCompany,
    *,
    client: httpx.AsyncClient,
    profile_sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Run Stage A → Stage B → write for one company. Returns audit dict."""
    t0 = time.time()
    out: dict[str, Any] = {
        "company": c.canonical_name,
        "linkedin_slug": c.linkedin_slug,
        "tier": c.tier,
        "stage_a_items": 0,
        "stage_a_cost_cents": 0,
        "stage_b_succeeded": 0,
        "stage_b_cost_cents": 0,
        "persons_inserted": 0,
        "persons_updated": 0,
        "employment_inserted": 0,
        "education_inserted": 0,
        "errors": [],
        "elapsed_s": 0.0,
    }

    # Stage A — list employees
    try:
        employees, cost_a = await list_company_employees(
            c.linkedin_url, max_items=HARD_CAP_PER_COMPANY, client=client,
        )
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"stage_a: {exc!r}")
        out["elapsed_s"] = round(time.time() - t0, 1)
        return out

    out["stage_a_items"] = len(employees)
    out["stage_a_cost_cents"] = cost_a

    if not employees:
        out["elapsed_s"] = round(time.time() - t0, 1)
        return out

    # Stage B — fetch full detail per employee, semaphore-bounded
    async def fetch_one(emp) -> tuple[Any, int]:
        async with profile_sem:
            try:
                return await fetch_profile_detail(
                    emp.public_identifier, client=client,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Stage B %s raised: %s", emp.public_identifier, exc)
                return None, 0

    results = await asyncio.gather(*(fetch_one(e) for e in employees))

    canonical_persons = []
    cost_b = 0
    for prof, cost in results:
        cost_b += cost
        if prof is None:
            continue
        canon = to_canonical_person(prof)
        if canon is not None:
            canonical_persons.append(canon)

    out["stage_b_succeeded"] = len(canonical_persons)
    out["stage_b_cost_cents"] = cost_b

    # Write — single batch via existing canonical writer
    try:
        wr = await write_canonical_persons(
            canonical_persons,
            account_id=DEFAULT_ACCOUNT_ID,
            primary_company_name=c.canonical_name,
        )
        out["persons_inserted"] = wr.persons_inserted
        out["persons_updated"] = wr.persons_updated
        out["employment_inserted"] = wr.employment_periods_inserted
        out["education_inserted"] = wr.education_periods_inserted
        out["errors"].extend(wr.errors[:5] if wr.errors else [])
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"writer: {exc!r}")

    out["elapsed_s"] = round(time.time() - t0, 1)
    return out


async def main() -> None:
    if not os.environ.get("APIFY_TOKEN"):
        sys.exit("ERROR: APIFY_TOKEN not in env (load .env.local first)")

    print("=== Phase 0: identify untouched target companies ===", flush=True)
    untouched = await find_untouched(TARGET_COMPANIES)
    print(f"  candidates:  {len(TARGET_COMPANIES)}", flush=True)
    print(f"  untouched:   {len(untouched)}  (<{MIN_ENRICHED_THRESHOLD} enriched)", flush=True)
    print(flush=True)
    for c in untouched:
        print(f"  - {c.canonical_name:40s} /company/{c.linkedin_slug}/", flush=True)
    print(flush=True)

    if not untouched:
        sys.exit("nothing to do — all targets enriched")

    est_max_cents = len(untouched) * HARD_CAP_PER_COMPANY * (1 + 0.5)  # $0.015/person × 100
    print(f"=== Phase 1: bulk enrichment of {len(untouched)} companies ===", flush=True)
    print(f"  HARD CAP per company: {HARD_CAP_PER_COMPANY}", flush=True)
    print(f"  Stage A actor:  apimaestro/linkedin-company-employees-scraper-no-cookies ($0.01/item)", flush=True)
    print(f"  Stage B actor:  apimaestro/linkedin-profile-detail ($0.005/item)", flush=True)
    print(f"  Concurrency:    {COMPANIES_IN_FLIGHT} co × {PROFILES_PER_COMPANY} profiles/co", flush=True)
    print(f"  Cost ceiling:   ~${est_max_cents/100:.2f}", flush=True)
    print(flush=True)

    co_sem = asyncio.Semaphore(COMPANIES_IN_FLIGHT)
    profile_sem = asyncio.Semaphore(PROFILES_PER_COMPANY)
    log_fh = open(RESULTS_FILE, "a")
    completed = 0
    total_cents = 0
    start = time.time()

    async def _bounded(c: TargetCompany, client: httpx.AsyncClient) -> None:
        nonlocal completed, total_cents
        async with co_sem:
            r = await enrich_one_company(c, client=client, profile_sem=profile_sem)
            completed += 1
            total_cents += r["stage_a_cost_cents"] + r["stage_b_cost_cents"]
            print(
                f"  ✓ {c.canonical_name:40s} | "
                f"stageA={r['stage_a_items']:3d} | "
                f"stageB={r['stage_b_succeeded']:3d} | "
                f"persons={r['persons_inserted']}+{r['persons_updated']} | "
                f"emp={r['employment_inserted']} edu={r['education_inserted']} | "
                f"cost=${(r['stage_a_cost_cents']+r['stage_b_cost_cents'])/100:.2f} | "
                f"{r['elapsed_s']:5.1f}s | "
                f"[{completed}/{len(untouched)} | total ${total_cents/100:.2f}]",
                flush=True,
            )
            log_fh.write(json.dumps(r) + "\n")
            log_fh.flush()

    async with httpx.AsyncClient(timeout=600.0) as client:
        await asyncio.gather(*(_bounded(c, client) for c in untouched))

    log_fh.close()
    total_elapsed = time.time() - start
    print(flush=True)
    print(f"=== bulk run complete: {completed}/{len(untouched)} in {total_elapsed:.0f}s ===", flush=True)
    print(f"=== total Apify spend: ${total_cents/100:.2f} ===", flush=True)
    print(f"=== JSONL audit: {RESULTS_FILE} ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
