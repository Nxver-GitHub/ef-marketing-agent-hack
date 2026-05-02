"""Prototype: employment-overlap-based disambiguation for paper authors.

Replaces the rolled-back name-only resolver. Investigates whether
shared employment_periods can disambiguate same-name candidates.

For each paper:
  1. Look up the source-prospect (already in paper_authors as the
     canonical resolved author).
  2. Re-fetch the full author list from Semantic Scholar.
  3. For each OTHER author name, find all persons in DB matching by
     normalized name.
  4. For each candidate, check whether they share an employment_period
     (same company_id) with the source-prospect — at any point in
     either career.
  5. Only emit a resolution if at least one overlap exists.

Compared to name-only matching, this should drop most ambiguous Wei-
Chen-style hits where two persons share a name but never crossed paths.

This is a 100-paper sampler — it does NOT write to paper_authors. It
prints stats so we can decide whether the strategy is worth shipping
across all 4,197 papers.

Usage:
    cd server && uv run --env-file ../.env.local python \\
      -m scripts.prototype_author_overlap_resolver
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import asyncpg
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("proto")

S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
SAMPLE_SIZE = 100


def _norm(name: str) -> str:
    return " ".join(name.lower().split())


async def main() -> None:
    dsn = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg:", "postgresql:",
    )
    conn = await asyncpg.connect(dsn)
    try:
        # 1. Build (normalized name → [person_id, ...]) index.
        log.info("loading persons name index ...")
        rows = await conn.fetch(
            "SELECT id, canonical_name FROM public.persons "
            "WHERE canonical_name IS NOT NULL"
        )
        name_index: dict[str, list[str]] = {}
        for r in rows:
            key = _norm(r["canonical_name"])
            if not key or len(key) < 4:
                continue
            name_index.setdefault(key, []).append(str(r["id"]))
        log.info("indexed %d unique names from %d persons",
                 len(name_index), len(rows))

        # 2. Pull a SAMPLE of papers + their canonical author.
        sample = await conn.fetch(
            """
            SELECT p.id AS paper_id, p.semantic_scholar_id,
                   pa.person_id AS canonical_person_id,
                   pers.canonical_name AS canonical_name
            FROM public.papers p
            JOIN public.paper_authors pa ON pa.paper_id = p.id
            JOIN public.persons pers ON pers.id = pa.person_id
            WHERE p.semantic_scholar_id IS NOT NULL
            ORDER BY random()
            LIMIT $1
            """,
            SAMPLE_SIZE,
        )
        log.info("sampled %d (paper, canonical_author) pairs", len(sample))
        if not sample:
            return

        # 3. Pre-load the canonical authors' employment_periods —
        #    we'll join against these for each candidate.
        canonical_ids = list({str(r["canonical_person_id"]) for r in sample})
        emp_rows = await conn.fetch(
            "SELECT person_id, company_id FROM public.employment_periods "
            "WHERE person_id = ANY($1::uuid[])",
            canonical_ids,
        )
        canonical_companies: dict[str, set[str]] = {}
        for er in emp_rows:
            canonical_companies.setdefault(
                str(er["person_id"]), set()
            ).add(str(er["company_id"]))

        # 4. Batch-fetch authors from Semantic Scholar.
        ssids = [r["semantic_scholar_id"] for r in sample]
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                S2_BATCH_URL,
                params={"fields": "authors.name"},
                json={"ids": ssids},
            )
        if r.status_code != 200:
            log.error("S2 batch failed: HTTP %d", r.status_code)
            return
        s2_papers = r.json()
        ssid_to_authors: dict[str, list[str]] = {}
        for p in s2_papers:
            if not p:
                continue
            authors = [
                a.get("name", "") for a in (p.get("authors") or [])
                if isinstance(a, dict) and isinstance(a.get("name"), str)
            ]
            ssid_to_authors[p["paperId"]] = authors

        # 5. Per-paper: for each non-canonical author name, find candidates
        #    and apply the overlap gate.
        stats = {
            "papers": 0,
            "papers_with_other_authors": 0,
            "name_only_candidates": 0,
            "ambiguous_names": 0,  # name with >1 candidate
            "overlap_resolved": 0,  # disambiguation succeeded
            "overlap_rejected": 0,  # candidate had no employment overlap
            "name_with_no_overlap_match": 0,
        }
        # Sample 5 example resolutions for the report.
        examples: list[dict[str, Any]] = []

        for r in sample:
            stats["papers"] += 1
            ssid = r["semantic_scholar_id"]
            canon_id = str(r["canonical_person_id"])
            canon_companies = canonical_companies.get(canon_id, set())
            canon_name_norm = _norm(r["canonical_name"])

            authors = ssid_to_authors.get(ssid, [])
            if not authors:
                continue
            other_names = [
                _norm(n) for n in authors
                if _norm(n) and _norm(n) != canon_name_norm
            ]
            if not other_names:
                continue
            stats["papers_with_other_authors"] += 1

            for name_key in other_names:
                candidates = name_index.get(name_key, [])
                if not candidates:
                    continue
                stats["name_only_candidates"] += len(candidates)
                if len(candidates) > 1:
                    stats["ambiguous_names"] += 1

                # Apply overlap gate
                resolved = []
                rejected = []
                for cand_id in candidates:
                    if cand_id == canon_id:
                        continue  # self-loop, skip
                    cand_companies_row = await conn.fetchval(
                        "SELECT array_agg(DISTINCT company_id::text) "
                        "FROM public.employment_periods WHERE person_id = $1::uuid",
                        cand_id,
                    )
                    cand_companies = set(cand_companies_row or [])
                    overlap = canon_companies & cand_companies
                    if overlap:
                        resolved.append((cand_id, len(overlap)))
                    else:
                        rejected.append(cand_id)

                if resolved:
                    stats["overlap_resolved"] += len(resolved)
                    if len(examples) < 5:
                        examples.append({
                            "canonical": r["canonical_name"],
                            "other_author_name": name_key,
                            "n_candidates": len(candidates),
                            "n_resolved": len(resolved),
                            "shared_companies": resolved[0][1] if resolved else 0,
                        })
                else:
                    stats["overlap_rejected"] += len(rejected)
                    stats["name_with_no_overlap_match"] += 1

        # 6. Report
        print()
        print("=== PROTOTYPE RESULTS — author-overlap disambiguation ===")
        print(f"papers sampled:                     {stats['papers']}")
        print(f"papers with other authors:          {stats['papers_with_other_authors']}")
        print(f"name-only candidate hits:           {stats['name_only_candidates']}")
        print(f"  - ambiguous (n>1 candidates):     {stats['ambiguous_names']}")
        print(f"overlap-resolved (kept):            {stats['overlap_resolved']}")
        print(f"overlap-rejected (filtered out):    {stats['overlap_rejected']}")
        print(f"author names with no overlap match: {stats['name_with_no_overlap_match']}")
        print()
        if stats["name_only_candidates"]:
            keep_rate = 100 * stats["overlap_resolved"] / stats["name_only_candidates"]
            print(f"keep rate vs name-only:             {keep_rate:.1f}%")
            print(f"(name-only baseline would emit all {stats['name_only_candidates']} candidate edges;")
            print(f" overlap-gated emits {stats['overlap_resolved']} with company-history backing)")
        print()
        if examples:
            print("=== sample resolutions ===")
            for ex in examples:
                print(
                    f"  canonical={ex['canonical']!r}  "
                    f"co_author={ex['other_author_name']!r}  "
                    f"candidates={ex['n_candidates']}  "
                    f"resolved={ex['n_resolved']}  "
                    f"shared_companies={ex['shared_companies']}"
                )

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
