"""Org-chart Stage 1.1 — functional clustering (v3.1 Plan A).

Per CLAUDE.md Decision 2 ("functional clustering before hierarchy
assignment is not optional") and V3_PT2.md L25-65: cluster current persons
at each company by their functional_domain before any hierarchy work
touches the data. This is the foundation for the rest of the pipeline.

A naive seniority-sort produces a ladder; clustering by domain first
produces functional branches with peers. The IC track (Distinguished /
Principal / Staff Engineer / Fellow / Architect) parallels the management
track at the same seniority level — clustering tags those persons via
``is_ic_track`` so ``hierarchy.py`` never assigns them as managers of
non-IC personnel.

## Output

For every company with ≥3 enriched persons:

- One row in ``org_functional_clusters`` per (company, functional_domain)
  combination. When ``inferred_team`` is set on at least 2 employment_periods
  rows for that combination, an additional row is emitted per (company,
  functional_domain, sub_domain) sub-cluster.
- One row in ``org_cluster_members`` per person per cluster they belong to,
  carrying ``membership_confidence`` (0.95 / 0.90 / 0.70 per V3_PT2.md
  L41-44) and ``is_ic_track``.

## Membership confidence

Per V3_PT2.md L41-44:

- 0.95 — ``employment_periods.functional_domain`` is set (canonical)
- 0.90 — same canonical column AND ``inferred_team`` matches the
  sub-cluster's ``sub_domain``
- 0.70 — canonical column was NULL and the domain was inferred from the
  title via ``taxonomy.domain_from_title``

## Idempotency

The function is idempotent on repeat. It uses ``ON CONFLICT DO UPDATE`` on
both unique-index targets:

- ``org_functional_clusters (company_id, functional_domain) WHERE sub_domain IS NULL``
- ``org_functional_clusters (company_id, functional_domain, sub_domain) WHERE sub_domain IS NOT NULL``

The member upsert keys on ``(cluster_id, person_id)``. Re-running on the
same data produces no duplicate rows; updates ``member_count`` to the
latest count and bumps ``updated_at``.

## Tenancy

``account_id`` for each cluster + member row is read from
``employment_periods.account_id`` — same source-of-truth pattern
``score_runner.score_prospect`` uses (cross-tenant integrity is enforced at
the persistence layer, not at the route layer).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ..db import acquire, fetch
from ..taxonomy import (
    FUNCTIONAL_DOMAINS,
    classify_title,
    domain_from_title,
    is_ic_track,
)

log = logging.getLogger(__name__)


# Tweak via env if a deployment wants tighter/looser cluster gating.
#
# Lowered from 3 → 1 on 2026-05-01 to satisfy "every prospect lands in a
# chart" — companies with 1 or 2 current persons now produce a single-member
# cluster that the frontend renders against. Hierarchy inference still
# requires ≥2 cluster members to attempt edge writes (see
# `infer_cluster_hierarchy` early-return), so solo clusters are charted as
# "this person + unscraped peer placeholders" without fabricating edges.
MIN_CLUSTER_SIZE: int = 1
SUB_CLUSTER_MIN_MEMBERS: int = 2  # need ≥2 same-team people to be a sub-cluster


@dataclass(frozen=True, slots=True)
class ClusterRollup:
    """Per-company summary of a clustering pass — what was written."""

    company_id: UUID
    company_name: str
    cluster_count: int
    member_count: int
    ic_track_count: int


@dataclass(slots=True)
class _PersonRow:
    """In-memory slice of an employment_periods row joined with persons."""

    person_id: UUID
    account_id: UUID
    canonical_title: str | None
    canonical_domain: str | None
    canonical_team: str | None
    is_ic_track: bool


# ─── Public API ─────────────────────────────────────────────────────────────


async def cluster_company(
    company_id: UUID,
    *,
    min_size: int = MIN_CLUSTER_SIZE,
) -> ClusterRollup:
    """Cluster all current persons at one company. Returns the rollup.

    Skipped when fewer than ``min_size`` current persons exist — those
    companies aren't useful for hierarchy inference and get no cluster row.

    Raises ``LookupError`` if the company doesn't exist.
    """
    company_row = await _load_company(company_id)
    if company_row is None:
        raise LookupError(f"company {company_id} not found")

    persons = await _load_current_persons(company_id)
    if len(persons) < min_size:
        log.info(
            "clustering: skipping %s (%s) — only %d current persons (< %d)",
            company_row["name"], company_id, len(persons), min_size,
        )
        return ClusterRollup(
            company_id=company_id,
            company_name=company_row["name"],
            cluster_count=0,
            member_count=0,
            ic_track_count=0,
        )

    plan = _build_cluster_plan(persons)

    # Two-phase write:
    #   1. Per-row UPSERT of clusters — small N (typically <10/company); the
    #      partial unique index requires two distinct ON CONFLICT predicates
    #      (NULL vs NOT NULL sub_domain) so set-based upsert is awkward.
    #      `RETURNING id` lets us collect cluster ids for the bulk member
    #      step that follows.
    #   2. Bulk UPSERT of members via COPY into a temp table + INSERT…SELECT
    #      ON CONFLICT — large N (typically 100s-1000s/company). Same pattern
    #      as `scope._bulk_upsert_scopes`. The previous per-row loop ran one
    #      round trip per member; this collapses to one round trip per
    #      company.
    written_clusters = 0
    written_members = 0
    ic_count = 0
    async with acquire() as conn:
        async with conn.transaction():
            member_rows: list[tuple[UUID, UUID, UUID, float, bool]] = []
            for cluster_key, members in plan.items():
                domain, sub_domain = cluster_key
                cluster_id = await _upsert_cluster(
                    conn,
                    account_id=members[0][0].account_id,
                    company_id=company_id,
                    functional_domain=domain,
                    sub_domain=sub_domain,
                    member_count=len(members),
                )
                written_clusters += 1
                for person, confidence in members:
                    member_rows.append(
                        (
                            person.account_id,
                            cluster_id,
                            person.person_id,
                            confidence,
                            person.is_ic_track,
                        )
                    )
                    if person.is_ic_track:
                        ic_count += 1
            if member_rows:
                await _bulk_upsert_members(conn, member_rows)
                written_members = len(member_rows)

    return ClusterRollup(
        company_id=company_id,
        company_name=company_row["name"],
        cluster_count=written_clusters,
        member_count=written_members,
        ic_track_count=ic_count,
    )


async def cluster_all_companies(
    *,
    min_size: int = MIN_CLUSTER_SIZE,
) -> list[ClusterRollup]:
    """Cluster every company that has ≥``min_size`` current employment_periods.

    Convenience wrapper around ``cluster_company`` that scans the eligible
    set first and runs them serially. Serial-not-parallel by design — the
    pool is small (50 connections) and clustering is cheap; flooding it
    would starve the rest of the API.
    """
    eligible = await _eligible_company_ids(min_size=min_size)
    log.info("clustering: %d companies eligible (>= %d current persons)", len(eligible), min_size)
    rollups: list[ClusterRollup] = []
    for company_id in eligible:
        try:
            rollup = await cluster_company(company_id, min_size=min_size)
        except LookupError:
            # Orphan `employment_periods` row points to a deleted company.
            # Log + skip — the row will surface in a separate data-quality
            # audit; here we just don't want one bad row halting the whole
            # batch scan.
            log.warning(
                "clustering: company %s referenced by employment_periods "
                "but missing from companies table; skipping",
                company_id,
            )
            continue
        rollups.append(rollup)
    return rollups


# ─── Pure clustering logic (no DB) ──────────────────────────────────────────


def _build_cluster_plan(
    persons: list[_PersonRow],
) -> dict[tuple[str, str | None], list[tuple[_PersonRow, float]]]:
    """Group persons into (domain, sub_domain) buckets with confidences.

    Pure function — no DB. Tested via direct invocation. Output keys are
    ``(domain, None)`` for the main per-domain cluster and
    ``(domain, sub_domain)`` for sub-clusters with ≥``SUB_CLUSTER_MIN_MEMBERS``
    same-team members. Each person appears in exactly one main cluster (and
    optionally in one sub-cluster of the same domain).
    """
    by_domain: dict[str, list[_PersonRow]] = defaultdict(list)
    by_team: dict[tuple[str, str], list[_PersonRow]] = defaultdict(list)
    confidences: dict[UUID, float] = {}

    for p in persons:
        # Resolve domain: canonical column wins; NLP fallback otherwise.
        if p.canonical_domain and p.canonical_domain in FUNCTIONAL_DOMAINS:
            domain = p.canonical_domain
            base_conf = 0.95
        else:
            inferred = domain_from_title(p.canonical_title)
            if inferred is None:
                # Route into the `uncategorized` registry bucket so the
                # prospect still has a cluster row for the chart UI to
                # render against. Hierarchy inference short-circuits on
                # uncategorized clusters (no edges produced) — see
                # `taxonomy.HIERARCHY_ELIGIBLE_DOMAINS`. Confidence is low
                # so optimizers can later distinguish "we know" from
                # "we shrugged" when nudging weights.
                domain = "uncategorized"
                base_conf = 0.30
            else:
                domain = inferred
                base_conf = 0.70

        by_domain[domain].append(p)
        confidences[p.person_id] = base_conf

        # Sub-cluster by inferred_team — only when the team string is set
        # AND we trust the canonical domain. Inferring team without a
        # canonical domain compounds noise; skip.
        if p.canonical_domain and p.canonical_team:
            by_team[(domain, p.canonical_team)].append(p)

    plan: dict[tuple[str, str | None], list[tuple[_PersonRow, float]]] = {}
    for domain, members in by_domain.items():
        if not members:
            continue
        plan[(domain, None)] = [
            (p, confidences[p.person_id]) for p in members
        ]

    # Sub-clusters: only emit when ≥SUB_CLUSTER_MIN_MEMBERS share the same
    # inferred_team. Below that, the team label is too noisy to anchor.
    # When a sub-cluster fires, members get the +0.05 bump → 0.90 conf.
    for (domain, team), members in by_team.items():
        if len(members) < SUB_CLUSTER_MIN_MEMBERS:
            continue
        plan[(domain, team)] = [(p, 0.90) for p in members]

    return plan


# ─── DB I/O — small helpers, all stubbed in tests via monkeypatch ───────────


async def _load_company(company_id: UUID) -> dict[str, Any] | None:
    rows = await fetch(
        "SELECT id, canonical_name AS name FROM companies WHERE id = $1",
        company_id,
    )
    return dict(rows[0]) if rows else None


async def _load_current_persons(company_id: UUID) -> list[_PersonRow]:
    """Pull current-employees-of-this-company joined with their canonical title.

    Source of truth is ``persons.current_company_id`` — the per-person
    "where do they work right now" pointer maintained by the entity-resolution
    layer. We INTENTIONALLY don't gate on
    ``employment_periods.is_current = TRUE`` because the LinkedIn/Apify
    parser sets that flag whenever an end_date is missing, which inflates
    the "current" set with old jobs (Microsemi, Atmel, US Army, university
    PhD years, etc.) — confirmed at 1,984 "current" companies vs 585 actual
    current employers.

    Title resolution: ``persons.current_title`` wins, then we fall through
    to the matching ``employment_periods`` row at the SAME company (which
    might still be flagged is_current=TRUE for the right reason — the
    person genuinely works there now). If neither is set we land an
    untitled person who will route to the `uncategorized` cluster.
    """
    rows = await fetch(
        """
        SELECT
          p.id                                                        AS person_id,
          COALESCE(p.account_id, ep.account_id)                       AS account_id,
          COALESCE(p.current_title, ep.title)                         AS title,
          COALESCE(p.current_functional_domain, ep.functional_domain) AS domain,
          ep.inferred_team                                            AS team
        FROM persons p
        LEFT JOIN employment_periods ep
          ON  ep.person_id = p.id
          AND ep.company_id = p.current_company_id
        WHERE p.current_company_id = $1
        """,
        company_id,
    )
    return [
        _PersonRow(
            person_id=row["person_id"],
            account_id=row["account_id"],
            canonical_title=row["title"],
            canonical_domain=row["domain"],
            canonical_team=row["team"],
            is_ic_track=is_ic_track(row["title"]),
        )
        for row in rows
    ]


async def _eligible_company_ids(*, min_size: int) -> list[UUID]:
    """Companies that currently employ ≥``min_size`` known persons.

    Source of truth is ``persons.current_company_id`` (the entity-resolution
    layer's "where do they work right now" pointer), NOT
    ``employment_periods.is_current = TRUE`` — the LinkedIn/Apify parser
    over-flags `is_current` whenever an end_date is missing, which
    historically inflated the eligible set 3-4× with defunct/past
    employers (Microsemi, Atmel, university PhD years, etc.).
    """
    rows = await fetch(
        """
        SELECT current_company_id AS company_id, COUNT(*) AS n
        FROM persons
        WHERE current_company_id IS NOT NULL
        GROUP BY current_company_id
        HAVING COUNT(*) >= $1
        ORDER BY n DESC
        """,
        min_size,
    )
    return [row["company_id"] for row in rows]


async def _upsert_cluster(
    conn: Any,
    *,
    account_id: UUID,
    company_id: UUID,
    functional_domain: str,
    sub_domain: str | None,
    member_count: int,
) -> UUID:
    """Insert or update one cluster row, return its id."""
    row = await conn.fetchrow(
        """
        INSERT INTO org_functional_clusters
          (account_id, company_id, functional_domain, sub_domain, member_count)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (company_id, functional_domain)
          WHERE sub_domain IS NULL
          DO UPDATE SET
            member_count = EXCLUDED.member_count,
            updated_at = now()
        RETURNING id
        """ if sub_domain is None else """
        INSERT INTO org_functional_clusters
          (account_id, company_id, functional_domain, sub_domain, member_count)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (company_id, functional_domain, sub_domain)
          WHERE sub_domain IS NOT NULL
          DO UPDATE SET
            member_count = EXCLUDED.member_count,
            updated_at = now()
        RETURNING id
        """,
        account_id, company_id, functional_domain, sub_domain, member_count,
    )
    return UUID(str(row["id"]))


async def _upsert_member(
    conn: Any,
    *,
    account_id: UUID,
    cluster_id: UUID,
    person_id: UUID,
    membership_confidence: float,
    is_ic_track: bool,
) -> None:
    """Single-row upsert (kept for any non-batch caller).

    The bulk path goes through `_bulk_upsert_members` for the in-pipeline
    case. Existing tests + ad-hoc scripts may still use this single-row
    helper, so we leave it intact.
    """
    await conn.execute(
        """
        INSERT INTO org_cluster_members
          (account_id, cluster_id, person_id, membership_confidence, is_ic_track)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (cluster_id, person_id) DO UPDATE
          SET membership_confidence = EXCLUDED.membership_confidence,
              is_ic_track = EXCLUDED.is_ic_track
        """,
        account_id, cluster_id, person_id, membership_confidence, is_ic_track,
    )


async def _bulk_upsert_members(
    conn: Any,
    rows: list[tuple[UUID, UUID, UUID, float, bool]],
) -> None:
    """Set-based upsert for cluster members via COPY + temp table.

    Inputs: ``[(account_id, cluster_id, person_id, membership_confidence,
    is_ic_track), ...]``. Same pattern as `scope._bulk_upsert_scopes` —
    COPY into a transaction-scoped temp table, then `INSERT … SELECT … ON
    CONFLICT (cluster_id, person_id) DO UPDATE` to merge into the target.

    Why COPY instead of `unnest`: the bool column is straightforward, but
    keeping all four bulk-upsert call sites (scope + propagation + here +
    future ones) on the same pattern reduces cognitive load and makes the
    temp-table-truncate-rollover behavior obvious.

    ## Why we dedupe in-Python first

    Postgres' `ON CONFLICT DO UPDATE` cannot affect the same target row
    twice within a single command — if `rows` contains two tuples with the
    same (cluster_id, person_id), Postgres raises `CardinalityViolation`.
    The legacy per-row `_upsert_member` path masked this because each row
    was its own command; the bulk path is one command per chunk.

    Duplicates show up in the wild when a person has more than one
    current `employment_periods` row at the same company (concurrent roles
    or promotion-without-end-date). The previous-state value to keep is
    "last write wins" — same as the per-row path — which mirrors the way
    the legacy ON CONFLICT chain ended up. We dedupe by stable iteration
    order so re-running on the same input is identical.
    """
    if not rows:
        return

    deduped: dict[tuple[UUID, UUID], tuple[UUID, UUID, UUID, float, bool]] = {}
    for row in rows:
        # Key on (cluster_id, person_id) — the unique constraint Postgres
        # enforces. Last assignment wins, matching the per-row UPDATE order.
        deduped[(row[1], row[2])] = row
    rows = list(deduped.values())

    await conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _cluster_member_chunk (
          account_id              uuid NOT NULL,
          cluster_id              uuid NOT NULL,
          person_id               uuid NOT NULL,
          membership_confidence   numeric NOT NULL,
          is_ic_track             boolean NOT NULL
        ) ON COMMIT DROP
        """
    )
    await conn.execute("TRUNCATE _cluster_member_chunk")

    await conn.copy_records_to_table(
        "_cluster_member_chunk",
        records=rows,
        columns=[
            "account_id",
            "cluster_id",
            "person_id",
            "membership_confidence",
            "is_ic_track",
        ],
    )

    await conn.execute(
        """
        INSERT INTO org_cluster_members
          (account_id, cluster_id, person_id, membership_confidence, is_ic_track)
        SELECT
          account_id, cluster_id, person_id, membership_confidence, is_ic_track
        FROM _cluster_member_chunk
        ON CONFLICT (cluster_id, person_id) DO UPDATE
          SET membership_confidence = EXCLUDED.membership_confidence,
              is_ic_track = EXCLUDED.is_ic_track
        """
    )


__all__ = [
    "ClusterRollup",
    "MIN_CLUSTER_SIZE",
    "SUB_CLUSTER_MIN_MEMBERS",
    "_bulk_upsert_members",
    "cluster_all_companies",
    "cluster_company",
]
