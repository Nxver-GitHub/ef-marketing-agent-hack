"""Backfill paper_authors with co-author resolution against existing persons.

Why this exists
---------------
``bulk_scholar_ingest`` only inserts the source prospect as a known
author per paper. The Semantic Scholar API returned the full author list
in the same response, but ``_format_paper_record`` discarded all but
the count. Result: 4,197 papers with avg 1 known author each → only 1
``academic_co_author_multi`` edge in v3 person_connections.

This script re-fetches each paper's author list via Semantic Scholar's
batch endpoint (``POST /paper/batch?fields=authors.name``, up to 500
ids/req), resolves each author's name against existing
``persons.canonical_name`` (case-insensitive exact match), and INSERTs
new rows into ``paper_authors``. Idempotent via the unique
``(paper_id, person_id)`` index.

After this lands, re-run ``paper_clustering --all --write-v3`` to
materialize the new authorship pairs as ``academic_co_author_*`` edges.

Cost: $0 (Semantic Scholar is free).
Wall time: ~10 papers/sec batched → ~7 min for 4,197 papers, plus
name resolution against ~37k persons (one query per author).

Usage:
    cd server && uv run --env-file ../.env.local python \\
      -m scripts.expand_paper_authors
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any

import asyncpg
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("expand_paper_authors")

S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
BATCH_SIZE = 500
HTTP_TIMEOUT = 60.0


def _normalize(name: str) -> str:
    """Lower + collapse whitespace. Used as the join key."""
    return " ".join(name.lower().split())


async def _fetch_papers_batch(
    client: httpx.AsyncClient,
    ssids: list[str],
    *,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch authors for up to BATCH_SIZE papers in one round trip."""
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key
    try:
        r = await client.post(
            S2_BATCH_URL,
            params={"fields": "authors.name,authors.affiliations"},
            json={"ids": ssids},
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        log.warning("batch HTTP error: %s", exc)
        return []
    if r.status_code == 429:
        log.warning("rate-limited; sleeping 15s")
        await asyncio.sleep(15)
        return await _fetch_papers_batch(client, ssids, api_key=api_key)
    if r.status_code != 200:
        log.warning("batch HTTP %d: %s", r.status_code, r.text[:300])
        return []
    body = r.json()
    if not isinstance(body, list):
        return []
    return body


async def _build_name_index(conn: asyncpg.Connection) -> dict[str, list[str]]:
    """Return ``{normalized_name: [person_id, ...]}`` for the whole persons table.

    Multiple persons can share a name; emit ALL matches and let paper_clustering
    dedupe. This is intentional — ambiguity at the resolution step is fine because
    the unique ``(paper_id, person_id)`` index keeps the writes idempotent.
    """
    log.info("loading persons name index ...")
    rows = await conn.fetch(
        "SELECT id, canonical_name FROM public.persons WHERE canonical_name IS NOT NULL"
    )
    idx: dict[str, list[str]] = {}
    for r in rows:
        key = _normalize(r["canonical_name"])
        if not key or len(key) < 4:  # skip near-empty
            continue
        idx.setdefault(key, []).append(str(r["id"]))
    log.info("indexed %d unique names from %d persons", len(idx), len(rows))
    return idx


async def _resolve_authors(
    paper: dict[str, Any],
    name_index: dict[str, list[str]],
) -> list[str]:
    """Return list of person UUIDs whose canonical_name matches an author."""
    authors = paper.get("authors") or []
    if not isinstance(authors, list):
        return []
    out: list[str] = []
    for a in authors:
        if not isinstance(a, dict):
            continue
        name = a.get("name")
        if not isinstance(name, str):
            continue
        key = _normalize(name)
        if not key:
            continue
        out.extend(name_index.get(key, []))
    return out


async def main() -> None:
    dsn = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg:", "postgresql:",
    )
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip() or None
    if api_key:
        log.info("using Semantic Scholar API key")
    else:
        log.info("no Semantic Scholar API key — using free unauthenticated tier")

    conn = await asyncpg.connect(dsn)
    try:
        # 1. Load persons name index for in-memory resolution
        name_index = await _build_name_index(conn)

        # 2. Pull all papers + map (semantic_scholar_id → paper_id, account_id)
        rows = await conn.fetch(
            "SELECT id, semantic_scholar_id, account_id FROM public.papers "
            "WHERE semantic_scholar_id IS NOT NULL"
        )
        papers_meta = {r["semantic_scholar_id"]: (str(r["id"]), str(r["account_id"])) for r in rows}
        log.info("processing %d papers", len(papers_meta))

        # 3. Batch-fetch authors via Semantic Scholar
        ssids = list(papers_meta.keys())
        new_pa_inserts = 0
        skipped = 0
        unresolved = 0
        t0 = time.time()

        async with httpx.AsyncClient() as client:
            for i in range(0, len(ssids), BATCH_SIZE):
                batch = ssids[i : i + BATCH_SIZE]
                papers = await _fetch_papers_batch(client, batch, api_key=api_key)
                for j, paper in enumerate(papers):
                    if paper is None:
                        unresolved += 1
                        continue
                    ssid = batch[j]
                    paper_id, account_id = papers_meta[ssid]
                    person_ids = await _resolve_authors(paper, name_index)
                    if not person_ids:
                        unresolved += 1
                        continue
                    # Dedupe — multiple authors might resolve to the same person
                    for pid in set(person_ids):
                        try:
                            r = await conn.execute(
                                """
                                INSERT INTO public.paper_authors
                                  (paper_id, person_id, account_id)
                                VALUES ($1, $2, $3)
                                ON CONFLICT (paper_id, person_id) DO NOTHING
                                """,
                                paper_id, pid, account_id,
                            )
                            if r.endswith("0"):
                                skipped += 1  # already existed
                            else:
                                new_pa_inserts += 1
                        except asyncpg.PostgresError as exc:
                            log.warning("insert failed for %s/%s: %s", paper_id, pid, exc)

                elapsed = time.time() - t0
                log.info(
                    "batch %d/%d done | new=%d skipped=%d unresolved=%d | %.1fs",
                    i // BATCH_SIZE + 1,
                    (len(ssids) + BATCH_SIZE - 1) // BATCH_SIZE,
                    new_pa_inserts, skipped, unresolved, elapsed,
                )

        log.info(
            "ROLLUP | papers=%d | new_paper_authors=%d | skipped_existing=%d | unresolved=%d | wall=%.1fs",
            len(ssids), new_pa_inserts, skipped, unresolved, time.time() - t0,
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
