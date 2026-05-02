"""Recover SUCCEEDED Apify datasets that the bulk runner abandoned at timeout.

DEFAULT_RUN_MAX_WAIT_SECONDS=3600 timed out chunks before they actually
finished on Apify side. Apify still completed them after timeout with
full profile data. This script fetches those datasets, parses them, and
runs them through the standard write path so prospects get marked.

Usage:
    APIFY_TOKEN=... DATABASE_URL=... uv run python -m \
        scripts.apify_recover_datasets <run_id1> <run_id2> ...
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

# Ensure server/ is on path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from credence.db import acquire, close_pool
from credence.enrichment import apify as apify_mod
from credence.enrichment.normalizer import from_apify
from credence.enrichment.writer import write_canonical_persons
from credence.jobs.bulk_apify_profile_lookup import (
    INSERT_MARKER_SIGNAL_SQL,
    MARKER_SIGNAL_SOURCE,
    MARKER_SIGNAL_TYPE,
    UnenrichedProspect,
    _build_marker_value,
    _normalize_linkedin_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger(__name__)


async def fetch_dataset_items(run_id: str, token: str, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch all dataset items from a run."""
    # Get run to find defaultDatasetId
    r = await client.get(f"https://api.apify.com/v2/actor-runs/{run_id}?token={token}", timeout=30)
    r.raise_for_status()
    ds_id = r.json()["data"]["defaultDatasetId"]
    # Stream items
    items: list[dict[str, Any]] = []
    offset = 0
    PAGE = 1000
    while True:
        r = await client.get(
            f"https://api.apify.com/v2/datasets/{ds_id}/items"
            f"?token={token}&offset={offset}&limit={PAGE}&clean=true",
            timeout=60,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        items.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return items


async def fetch_unenriched_prospects_with_linkedin(
    account_id: UUID,
) -> dict[str, UnenrichedProspect]:
    """Build linkedin_url → UnenrichedProspect map for matching."""
    out: dict[str, UnenrichedProspect] = {}
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, linkedin_url, account_id FROM prospects "
            "WHERE account_id = $1 AND linkedin_url IS NOT NULL AND linkedin_url <> ''",
            account_id,
        )
    for r in rows:
        url_norm = _normalize_linkedin_url(r["linkedin_url"])
        if url_norm:
            out[url_norm] = UnenrichedProspect(
                id=r["id"], name=r["name"],
                linkedin_url=r["linkedin_url"], account_id=r["account_id"],
            )
    return out


async def insert_marker(prospect_id: UUID, account_id: UUID) -> None:
    import json as _json
    async with acquire() as conn:
        await conn.execute(
            INSERT_MARKER_SIGNAL_SQL,
            prospect_id, account_id,
            MARKER_SIGNAL_SOURCE, MARKER_SIGNAL_TYPE,
            _json.dumps(_build_marker_value()),
        )


async def stamp_source_prospect(prospect_id: UUID, linkedin_url: str) -> None:
    async with acquire() as conn:
        await conn.execute(
            "UPDATE persons SET source_prospect_id = $1 "
            "WHERE linkedin_url = $2 AND source_prospect_id IS NULL",
            prospect_id, linkedin_url,
        )


async def _persist_one(profile: Any, prospect: UnenrichedProspect, counts: dict[str, int]) -> None:
    """Persist a single matched profile (writer + stamp + marker)."""
    canonical = from_apify(profile)
    if canonical is None:
        counts["parse_failed"] += 1
        return
    try:
        await write_canonical_persons([canonical], account_id=prospect.account_id)
        counts["persisted"] += 1
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  persist failed for {prospect.id}: {exc!r}")
        counts["errors"] += 1
        return
    if profile.linkedin_url:
        try:
            await stamp_source_prospect(prospect.id, profile.linkedin_url)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"  stamp failed: {exc!r}")
    try:
        await insert_marker(prospect.id, prospect.account_id)
        counts["marker_written"] += 1
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  marker failed: {exc!r}")


async def recover_run(run_id: str, account_id: UUID, url_to_prospect: dict[str, UnenrichedProspect]) -> dict[str, int]:
    token = os.environ["APIFY_TOKEN"]
    counts = {"items_fetched": 0, "matched": 0, "persisted": 0, "marker_written": 0, "no_match": 0, "parse_failed": 0, "errors": 0}
    async with httpx.AsyncClient() as client:
        log.info(f"recovering {run_id} ...")
        items = await fetch_dataset_items(run_id, token, client)
        counts["items_fetched"] = len(items)
        log.info(f"  fetched {len(items)} items")

    # First pass: parse + match (single-threaded, in-memory only).
    matched: list[tuple[Any, UnenrichedProspect]] = []
    for raw in items:
        profile = apify_mod.parse_profile(raw)
        if profile is None:
            counts["parse_failed"] += 1
            continue
        url_norm = _normalize_linkedin_url(profile.linkedin_url)
        prospect = url_to_prospect.get(url_norm)
        if prospect is None:
            counts["no_match"] += 1
            continue
        counts["matched"] += 1
        matched.append((profile, prospect))
    log.info(f"  parsed+matched {len(matched)}/{counts['items_fetched']} items")

    # Second pass: parallelized persists. Bounded by Supabase pool size
    # (15ish). Concurrency=10 keeps headroom for the new caller's DB writes.
    BATCH = 50
    SEM = asyncio.Semaphore(10)
    async def _bounded(profile: Any, prospect: UnenrichedProspect) -> None:
        async with SEM:
            await _persist_one(profile, prospect, counts)
    for i in range(0, len(matched), BATCH):
        batch = matched[i:i+BATCH]
        await asyncio.gather(*(_bounded(p, pr) for p, pr in batch), return_exceptions=False)
        if (i // BATCH) % 4 == 0:
            log.info(f"  persisted batch {i//BATCH+1}/{(len(matched)+BATCH-1)//BATCH} (cumulative={counts['persisted']})")
    return counts


async def main(run_ids: list[str], account_id: UUID) -> None:
    log.info(f"loading prospect lookup for account {account_id}")
    url_to_prospect = await fetch_unenriched_prospects_with_linkedin(account_id)
    log.info(f"  {len(url_to_prospect)} unenriched prospects with linkedin_url")
    grand: dict[str, int] = {}
    for run_id in run_ids:
        c = await recover_run(run_id, account_id, url_to_prospect)
        log.info(f"  {run_id}: {c}")
        for k, v in c.items():
            grand[k] = grand.get(k, 0) + v
    log.info(f"GRAND TOTAL: {grand}")
    await close_pool()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: apify_recover_datasets.py <run_id1> [run_id2 ...]")
        sys.exit(1)
    runs = sys.argv[1:]
    acct = UUID(os.environ.get("ACCOUNT_ID", "00000000-0000-0000-0000-000000000001"))
    asyncio.run(main(runs, acct))
