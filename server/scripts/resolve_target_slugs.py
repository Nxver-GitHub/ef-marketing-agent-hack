"""Slug-resolution pass for TARGET_COMPANIES.

For each untouched target company (<50 enriched persons), generate
candidate LinkedIn slugs and probe each via harvestapi
(``find_company_employees_sync`` with maxItems=5, MODE_SHORT). The
first candidate that returns >0 profiles wins.

Cost: ~5 candidates × ~5 profiles × $4/1k = $0.10 per company × 27
companies = ~$2.70 max.

Outputs:
- Stdout report: per-company old slug → resolved slug + employee count
- ``/tmp/credence-resolved-slugs.json`` — JSON map ``{name: slug}``
- Print a Python snippet ready to paste into _target_companies.py

Usage:
    cd server && uv run --env-file ../.env.local python \\
      -m scripts.resolve_target_slugs
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Iterable

import asyncpg

from credence.enrichment._target_companies import (
    TARGET_COMPANIES,
    TargetCompany,
)
from credence.enrichment.apify import (
    MODE_SHORT,
    find_company_employees_sync,
)

OUT_FILE = "/tmp/credence-resolved-slugs.json"

# Suffix tokens to strip when generating candidate slugs.
STRIP_SUFFIX = (
    "technologies", "technology", "corporation", "corp", "inc",
    "industries", "systems", "holdings", "limited", "ltd",
    "company", "co",
)
# Words to drop entirely (mid-name) when generating candidate slugs.
DROP_WORD = ("the", "&", "and")


def _slugify(words: Iterable[str]) -> str:
    return re.sub(r"[^a-z0-9-]", "", "-".join(w.lower() for w in words))


def candidate_slugs(name: str, current: str) -> list[str]:
    """Generate candidate LinkedIn slugs for a company.

    Order of trials matters — most-specific first so we don't accept
    a too-generic slug that happens to resolve to a different company.
    """
    parts = re.split(r"\s+", name.strip())
    parts_lc = [p.lower() for p in parts]

    cands: list[str] = []
    # 1. The current slug — in case it's right and last failure was transient.
    if current:
        cands.append(current)
    # 2. Hyphenated full name.
    cands.append(_slugify(parts_lc))
    # 3. Strip common corporate suffixes.
    if parts_lc[-1] in STRIP_SUFFIX:
        cands.append(_slugify(parts_lc[:-1]))
    # 4. First two words (handles "Cadence Design Systems" → "cadence-design").
    if len(parts_lc) >= 2:
        cands.append(_slugify(parts_lc[:2]))
    # 5. Drop "the" / "&" / "and" tokens.
    cleaned = [p for p in parts_lc if p not in DROP_WORD]
    if cleaned and cleaned != parts_lc:
        cands.append(_slugify(cleaned))
    # 6. First word only.
    cands.append(_slugify(parts_lc[:1]))
    # 7. Concatenated lowercase no separator (handles "andurilindustries").
    cands.append(_slugify(["".join(parts_lc)]))
    # 8. First two words concatenated no separator (samsungsemiconductor pattern).
    if len(parts_lc) >= 2:
        cands.append(_slugify(["".join(parts_lc[:2])]))
    # 9. "name-corporation" / "name-inc" canonical patterns.
    cands.append(_slugify([*parts_lc, "corporation"]))
    cands.append(_slugify([*parts_lc, "inc"]))

    # De-dupe preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for s in cands:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


async def find_untouched() -> list[TargetCompany]:
    dsn = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg:", "postgresql:",
    )
    conn = await asyncpg.connect(dsn)
    try:
        rows = []
        for c in TARGET_COMPANIES:
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
            if (cnt or 0) < 50:
                rows.append(c)
        return rows
    finally:
        await conn.close()


async def probe_one(slug: str) -> int:
    """Return employee count for one slug. Empty / 404 → 0."""
    url = f"https://www.linkedin.com/company/{slug}/"
    try:
        r = await find_company_employees_sync(
            url, max_items=5, mode=MODE_SHORT, timeout_seconds=120.0,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  probe {slug}: error {exc}", file=sys.stderr)
        return 0
    return len(r.profiles) if r else 0


async def resolve_company(c: TargetCompany) -> tuple[TargetCompany, str | None, int]:
    """Try candidates one at a time. Returns (company, winning_slug, count)."""
    cands = candidate_slugs(c.canonical_name, c.linkedin_slug)
    print(f"  {c.canonical_name:42s} candidates: {cands[:5]}")
    for slug in cands:
        n = await probe_one(slug)
        if n > 0:
            print(f"    ✓ {slug:40s} → {n} profiles")
            return c, slug, n
    print(f"    ✗ no slug worked for {c.canonical_name}")
    return c, None, 0


async def main() -> None:
    if not os.environ.get("APIFY_TOKEN"):
        sys.exit("ERROR: APIFY_TOKEN not in env (load .env.local first)")

    print("=== identifying untouched target companies ===")
    untouched = await find_untouched()
    print(f"  candidates:  {len(TARGET_COMPANIES)}")
    print(f"  untouched:   {len(untouched)}")
    print()

    if not untouched:
        sys.exit("nothing to resolve — all targets enriched")

    print("=== probing candidate slugs (parallel sem=8, ~$0.10/company) ===", flush=True)
    sem = asyncio.Semaphore(8)
    resolved: dict[str, dict[str, str | int | None]] = {}

    async def _bounded(c: TargetCompany) -> None:
        async with sem:
            company, slug, n = await resolve_company(c)
            resolved[company.canonical_name] = {
                "old_slug": company.linkedin_slug,
                "new_slug": slug,
                "employee_probe_count": n,
            }

    await asyncio.gather(*(_bounded(c) for c in untouched))
    won = sum(1 for info in resolved.values() if info["new_slug"])

    print()
    print(f"=== resolved {won}/{len(untouched)} slugs ===")
    print()

    # Print Python snippet to paste into _target_companies.py
    print("=== suggested edits (slug changes only) ===")
    for name, info in resolved.items():
        if info["new_slug"] and info["new_slug"] != info["old_slug"]:
            print(f"  {name:42s}: {info['old_slug']!r} → {info['new_slug']!r}")
    print()
    print(f"=== writing JSON map: {OUT_FILE} ===")
    with open(OUT_FILE, "w") as fh:
        json.dump(resolved, fh, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
