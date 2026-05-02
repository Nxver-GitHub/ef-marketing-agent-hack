"""Tier-2 prospect selection — picks the top 10% for deep enrichment.

Per user direction (2026-04-30): LinkedIn posts/comments and news mentions
are expensive at $0.25-1.00/prospect. Running them on every prospect ($60k+
total) is wasteful when ~80% of prospects are below the buying-authority
threshold to matter for warm-path outreach. This module gates the spend.

## Selection logic

Default ranking: ``current_seniority_score`` desc, ties broken by
``persons.id`` for stable ordering. Caller can pass an alternate scorer.

The choice of seniority over score is deliberate: Tier-2 enrichment
(LinkedIn posts → engagement signals; news → Authority/Authenticity)
adds the most value for senior people whose Authority sub-score
already dominates the ranking. A high-scored individual contributor
matters less than a Director-tier prospect with merely-average score.

## How tier is recorded

Sets ``persons.enrichment_tier = 3`` for promoted prospects. Tier
semantics:

  0 — default; pre-enrichment
  1 — basic enrich (Apollo / no LinkedIn URL fallback path)
  2 — bulk Apify deep enrichment complete
  3 — promoted for Tier-2 (posts + news + GitHub deep)

Existing tier values:
- ``writer.py:_upsert_person`` sets tier=2 (Apify path) or tier=1 (no
  LinkedIn URL path) during the bulk run.
- This module promotes the top decile to tier=3 ahead of the Tier-2
  enrichment routes.

## Idempotency

Re-running ``promote_top_decile()`` against an already-promoted DB:
- The same set of prospects is selected (deterministic by seniority desc)
- ``enrichment_tier = 3`` SET stays at 3 — no toggling
- Demoting requires explicit ``demote_tier_3()`` (separate fn)

## Related modules

- ``apify_posts.py`` — reads ``WHERE enrichment_tier >= 3``
- ``news.py`` — same filter
- ``writer.py`` — sets initial tier during bulk
- ``orgchart/clustering.py`` — uses ``enrichment_tier`` to gate
  hierarchy inference (only tier ≥ 2 persons feed the cluster)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from ..db import execute, fetch

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PriorityProspect:
    """One prospect selected for Tier-2 enrichment."""

    person_id: UUID
    canonical_name: str
    linkedin_url: str | None
    current_company_name: str | None  # joined from companies
    current_title: str | None
    seniority_score: int | None


# ─── Selection ──────────────────────────────────────────────────────────────


async def select_top_decile(
    account_id: UUID,
    *,
    percentile: int = 10,
    min_seniority_score: int = 50,
) -> list[PriorityProspect]:
    """Return the top ``percentile``% of persons (default 10%) by seniority.

    Filters:
      - ``account_id`` matches (tenant-scoped)
      - ``current_seniority_score`` is non-null AND ≥ ``min_seniority_score``
        (default 50 = Manager tier; below this isn't worth Tier-2 spend)
      - Has a LinkedIn URL (Tier-2 modules need it for input)

    Order: seniority desc, then stable by id.

    Returns an empty list when the tenant has no eligible persons.
    """
    if percentile <= 0 or percentile > 100:
        raise ValueError(f"percentile must be in (0, 100]; got {percentile}")

    # First count eligible persons to compute "top N"
    count_row = await fetch(
        """
        SELECT count(*) AS n
        FROM public.persons p
        WHERE p.account_id = $1
          AND p.current_seniority_score IS NOT NULL
          AND p.current_seniority_score >= $2
          AND p.linkedin_url IS NOT NULL
        """,
        account_id,
        min_seniority_score,
    )
    total = int(count_row[0]["n"]) if count_row else 0
    if total == 0:
        return []

    # ``ceil`` so that small populations still get at least 1
    target_n = max(1, (total * percentile + 99) // 100)

    rows = await fetch(
        """
        SELECT p.id, p.canonical_name, p.linkedin_url,
               p.current_title, p.current_seniority_score,
               c.canonical_name AS company_name
        FROM public.persons p
        LEFT JOIN public.companies c ON c.id = p.current_company_id
        WHERE p.account_id = $1
          AND p.current_seniority_score IS NOT NULL
          AND p.current_seniority_score >= $2
          AND p.linkedin_url IS NOT NULL
        ORDER BY p.current_seniority_score DESC, p.id
        LIMIT $3
        """,
        account_id,
        min_seniority_score,
        target_n,
    )
    return [
        PriorityProspect(
            person_id=UUID(str(r["id"])),
            canonical_name=str(r["canonical_name"]),
            linkedin_url=str(r["linkedin_url"]) if r.get("linkedin_url") else None,
            current_company_name=str(r["company_name"]) if r.get("company_name") else None,
            current_title=str(r["current_title"]) if r.get("current_title") else None,
            seniority_score=int(r["current_seniority_score"])
                if r.get("current_seniority_score") is not None else None,
        )
        for r in rows
    ]


async def promote_to_tier_3(person_ids: list[UUID]) -> int:
    """Set ``enrichment_tier = 3`` for the given person ids.

    Idempotent — already-tier-3 rows stay at 3. Returns the number of
    rows actually updated (which may equal len(person_ids) if all were
    promoted, or less if some were already at tier 3).
    """
    if not person_ids:
        return 0
    # `xmax` discriminates updated-this-call vs already-at-tier-3.
    # asyncpg's `execute` returns the row count via the command tag.
    result = await execute(
        """
        UPDATE public.persons
        SET enrichment_tier = 3,
            updated_at = NOW()
        WHERE id = ANY($1::uuid[])
          AND enrichment_tier < 3
        """,
        list(person_ids),
    )
    # Parse "UPDATE N" from the command tag
    if isinstance(result, str) and result.startswith("UPDATE "):
        try:
            return int(result.split()[1])
        except (IndexError, ValueError):
            return 0
    return 0


async def promote_top_decile(
    account_id: UUID,
    *,
    percentile: int = 10,
    min_seniority_score: int = 50,
) -> tuple[list[PriorityProspect], int]:
    """One-shot: select top decile + promote to tier 3.

    Returns ``(prospects, n_promoted)`` where ``n_promoted`` is the
    count of rows whose tier actually moved (already-tier-3 rows are
    excluded). Idempotent re-runs return ``n_promoted = 0``.
    """
    prospects = await select_top_decile(
        account_id,
        percentile=percentile,
        min_seniority_score=min_seniority_score,
    )
    if not prospects:
        return [], 0
    n_promoted = await promote_to_tier_3([p.person_id for p in prospects])
    return prospects, n_promoted


async def list_tier_3(account_id: UUID, *, limit: int = 5000) -> list[PriorityProspect]:
    """Read-only: every tier-3 prospect in the tenant. Drives the
    Tier-2 enrichment routes (``apify_posts``, ``news``).
    """
    rows = await fetch(
        """
        SELECT p.id, p.canonical_name, p.linkedin_url,
               p.current_title, p.current_seniority_score,
               c.canonical_name AS company_name
        FROM public.persons p
        LEFT JOIN public.companies c ON c.id = p.current_company_id
        WHERE p.account_id = $1
          AND p.enrichment_tier >= 3
        ORDER BY p.current_seniority_score DESC NULLS LAST, p.id
        LIMIT $2
        """,
        account_id,
        limit,
    )
    return [
        PriorityProspect(
            person_id=UUID(str(r["id"])),
            canonical_name=str(r["canonical_name"]),
            linkedin_url=str(r["linkedin_url"]) if r.get("linkedin_url") else None,
            current_company_name=str(r["company_name"]) if r.get("company_name") else None,
            current_title=str(r["current_title"]) if r.get("current_title") else None,
            seniority_score=int(r["current_seniority_score"])
                if r.get("current_seniority_score") is not None else None,
        )
        for r in rows
    ]


__all__ = [
    "PriorityProspect",
    "select_top_decile",
    "promote_to_tier_3",
    "promote_top_decile",
    "list_tier_3",
]
