"""Recover Apify dataset → Supabase for runs that the bulk script abandoned.

Context: ``run_bulk_enrichment.py`` first run had a bug in
``apify.wait_for_run`` — it returned ``UNKNOWN`` on the first transient
HTTP error (502, connection drop) instead of retrying. The actor
continued running on Apify's side and accumulated profiles in datasets
we never fetched. We paid for those profiles ($27 charged across 15
runs); this script extracts that data and writes it to Supabase.

Identifies each dataset's company by inspecting the first profile's
``currentPosition[0].companyUniversalName`` — far more reliable than
trying to recover the input options.

Usage:
    cd server && uv run --env-file ../.env.local python -m scripts.recover_apify_runs
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any
from uuid import UUID

import httpx

from credence.enrichment.apify import APIFY_API_BASE, parse_profile
from credence.enrichment.normalizer import merge_records
from credence.enrichment.writer import write_canonical_persons

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger("recover")

DEFAULT_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")

# Run IDs that have data we paid for (collected by hand from msg history)
ABANDONED_RUNS: tuple[tuple[str, str], ...] = (
    # (run_id, default_dataset_id)
    ("rYxGgTiUABG1BPvST", "hilNIYQyyGeS0mieO"),  # Intel
    ("VtRemd4InQ6okZnMF", "5DuJofA8jObl9bHRF"),
    ("BMTiaTNtF2LhM56Ii", "bUqiPbNhmIMqem3pR"),
    ("86yQvfYBgfRhxRWil", "GF47rsvbzq7iAivER"),
    ("xfdIFCSong9669061", "MEMUBm5x75wTWgE4h"),
    ("okZ7TWg4kNT13gwMR", "ufX4rRsrPOrI5ecDl"),
    ("rf3qp7l1NS84fMJ7I", "sOb0e9xNcRmf0hn7O"),
    ("0JdspFVEHKfg7pfLF", "bdFC4OEOcvbdbhJfO"),
    ("vzHcoZbH3rmi2A8Jn", "76q3GhGhBwY4tBlpL"),
    ("MCdCu0lGMmaoJkRw3", "9p0Ss2KUGLKiy7pIp"),
    ("uLGZjc6iBcI3xs5gV", "dl9y21x04TlRkVe56"),
    ("ht3DFsQJcFnfSUe4Z", "z6xwEfnWRCdjsU80A"),
    ("oB5EHdzJJ4JA5KDds", "NG2XxVWaXrHCsLjDY"),
    ("4j3vWtVRSpUAYSFQk", "1MuqIM18qkUEIp4Xe"),
    ("WSUBFYbZdNMbnf3U6", "YzmhMj0ZFGNKCr8gN"),
)


async def fetch_dataset(
    dataset_id: str, *, token: str, client: httpx.AsyncClient
) -> list[dict[str, Any]]:
    """Pull all items from one Apify dataset."""
    r = await client.get(
        f"{APIFY_API_BASE}/datasets/{dataset_id}/items?token={token}&clean=1"
    )
    if r.status_code != 200:
        logger.warning("dataset %s fetch HTTP %d", dataset_id, r.status_code)
        return []
    try:
        items = r.json()
    except ValueError:
        return []
    return items if isinstance(items, list) else []


def identify_company(items: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Return ``(canonical_name, linkedin_slug)`` from the first profile's
    current employer."""
    for p in items:
        if not isinstance(p, dict):
            continue
        positions = p.get("currentPosition") or []
        if isinstance(positions, list) and positions:
            cur = positions[0]
            if isinstance(cur, dict):
                name = (cur.get("companyName") or "").strip()
                slug = (cur.get("companyUniversalName") or "").strip()
                if name and slug:
                    return name, slug
    return None


async def process_one_dataset(
    run_id: str,
    dataset_id: str,
    *,
    token: str,
    client: httpx.AsyncClient,
) -> tuple[str, int, int, int]:
    """Process one (run_id, dataset_id) pair end-to-end. Returns
    ``(company_name, n_profiles, persons_inserted, persons_updated)``.
    """
    items = await fetch_dataset(dataset_id, token=token, client=client)
    if not items:
        return run_id, 0, 0, 0

    ident = identify_company(items)
    if ident is None:
        return run_id, len(items), 0, 0
    canonical_name, _slug = ident

    profiles = []
    for raw in items:
        p = parse_profile(raw)
        if p is not None:
            profiles.append(p)
    canonical_persons = merge_records({"apify": profiles})

    try:
        wr = await write_canonical_persons(
            canonical_persons,
            account_id=DEFAULT_ACCOUNT_ID,
            primary_company_name=canonical_name,
        )
        n_inserted = wr.persons_inserted
        n_updated = wr.persons_updated
        print(
            f"  ✓ {canonical_name:32s} | profiles={len(profiles):3d} | "
            f"persons={n_inserted}+{n_updated} | "
            f"emp={wr.employment_periods_inserted}+{wr.employment_periods_updated} | "
            f"edu={wr.education_periods_inserted}+{wr.education_periods_updated} | "
            f"sigs={wr.person_signals_written} | err={len(wr.errors)}"
        )
        sys.stdout.flush()
        return canonical_name, len(profiles), n_inserted, n_updated
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {canonical_name}: {exc}")
        sys.stdout.flush()
        return canonical_name, len(profiles), 0, 0


async def main() -> None:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit("ERROR: APIFY_TOKEN not in env")

    # Bounded parallelism — 5 concurrent datasets. Higher than this risks
    # Supabase pool exhaustion (default ~15 connections at the pool layer
    # × 5 dataset workers × N parallel writes-within-a-dataset).
    sem = asyncio.Semaphore(5)
    results: list[tuple[str, int, int, int]] = []

    async def _bounded(run_id: str, ds_id: str, client: httpx.AsyncClient) -> None:
        async with sem:
            r = await process_one_dataset(run_id, ds_id, token=token, client=client)
            results.append(r)

    print(f"=== parallel recovery: {len(ABANDONED_RUNS)} datasets, sem=5 ===")
    async with httpx.AsyncClient(timeout=120.0) as client:
        await asyncio.gather(
            *(_bounded(rid, did, client) for rid, did in ABANDONED_RUNS)
        )

    total_profiles = sum(r[1] for r in results)
    total_inserts = sum(r[2] for r in results)
    total_updates = sum(r[3] for r in results)
    print()
    print(f"=== Recovery complete ===")
    print(f"Total profiles parsed: {total_profiles}")
    print(f"Total persons inserted: {total_inserts}")
    print(f"Total persons updated: {total_updates}")


if __name__ == "__main__":
    asyncio.run(main())
