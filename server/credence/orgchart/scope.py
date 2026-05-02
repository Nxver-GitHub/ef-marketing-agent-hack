"""Org-chart Stage 1.3 — scope estimation (v3.1 Plan A, Task A3).

Per CLAUDE.md L186-187 and V3_PT2.md L155-180: for every person who appears
as a manager in ``org_reporting_edges``, summarize what they own — the
``person_scope_estimates`` row that feeds the Authority sub-score of the
scoring model and powers the "VP-of-Engineering owns ML compiler" hover-card
on the org chart UI.

## What gets computed

| Field | Source |
|---|---|
| ``owns_functions`` | Distinct ``functional_domain`` of clusters they're a member of (managers anchor a cluster's domain) |
| ``owns_technologies`` | Distinct ``sub_domain`` of sub-clusters they belong to |
| ``team_size_min`` | Direct-report count from ``org_reporting_edges`` |
| ``team_size_max`` | Transitive subtree size (BFS down the manager → report graph) |
| ``budget_authority_level`` | Mapped from ``persons.current_seniority_score`` per V3_PT2.md L172-178 |
| ``owns_products`` / ``owns_regions`` | Reserved — not yet wired (no upstream signal source). Defaults to empty arrays. |

## Why it's a flat per-person table

Scope is read once per render of a person's profile card. Pre-computing it
into ``person_scope_estimates`` (keyed on ``person_id`` UNIQUE) is cheaper
than running the BFS and array-aggregations on every hit. The table refreshes
when hierarchy or clustering changes — re-run ``estimate_all_scopes()`` after
a hierarchy pass to keep it current.

## Idempotency

``ON CONFLICT (person_id) DO UPDATE`` on the unique constraint. Re-running
on the same data overwrites the row and bumps ``computed_at``.

## Tenancy

``account_id`` for each scope row is read from the underlying
``org_reporting_edges.account_id`` (manager-side). Same source-of-truth
pattern as ``hierarchy.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ..db import acquire, fetch
from ..taxonomy import seniority_tier

log = logging.getLogger(__name__)


# ─── Tunables ────────────────────────────────────────────────────────────────


# Seniority-score → budget_authority_level mapping. V3_PT2.md L172-178 lists
# the buckets verbatim. The keys must match the CHECK constraint in the
# `person_scope_estimates` table:
#   ('individual', 'team', 'department', 'division', 'company')
#
# We reuse the seniority_tier function from taxonomy.py since the buckets
# parallel the span-of-control tiers — c_suite owns "company" budget, vp
# owns "department", etc. Keeping a separate mapping here so the levels stay
# decoupled from span limits if v3.2 needs to differentiate (e.g., a Director
# at a 5000-person company might own department-level budget while a Director
# at a 50-person startup owns company-level).
_TIER_TO_BUDGET: dict[str, str] = {
    "c_suite": "company",
    "svp": "division",
    "vp": "department",
    "director": "team",
    "manager": "individual",
}


# ─── Public types ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ScopeEstimate:
    """In-memory shape of a `person_scope_estimates` row.

    Constructed by the pure planner; the orchestrator translates it into
    the DB INSERT column list. Empty arrays mean "no signal" — distinct
    from "owns nothing" but the UI renders them the same way.
    """

    person_id: UUID
    account_id: UUID
    owns_functions: list[str] = field(default_factory=list)
    owns_technologies: list[str] = field(default_factory=list)
    owns_products: list[str] = field(default_factory=list)
    owns_regions: list[str] = field(default_factory=list)
    team_size_min: int | None = None
    team_size_max: int | None = None
    budget_authority_level: str | None = None


@dataclass(slots=True)
class PersonRollup:
    """Slice of (persons JOIN org_cluster_members JOIN org_functional_clusters)
    used by the pure planner. The orchestrator builds these via a single SQL
    fetch per account; the planner doesn't touch the DB.
    """

    person_id: UUID
    account_id: UUID
    seniority: int | None
    cluster_domains: list[str] = field(default_factory=list)
    cluster_sub_domains: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ManagerNode:
    """A node in the manager → report graph. Used by the BFS subtree counter."""

    person_id: UUID
    direct_reports: list[UUID] = field(default_factory=list)


# ─── Pure scope logic (no DB) ────────────────────────────────────────────────


def _budget_level_from_seniority(seniority: int | None) -> str | None:
    """V3_PT2.md L172-178: seniority bucket → CHECK-keyspace budget level."""
    if seniority is None:
        return None
    return _TIER_TO_BUDGET.get(seniority_tier(seniority))


def _subtree_size(
    root_id: UUID,
    nodes_by_id: dict[UUID, ManagerNode],
    *,
    max_depth: int = 8,
) -> int:
    """BFS over the manager → report graph to count the transitive subtree.

    `max_depth` caps recursion so a pathological cycle (which validation.py
    catches separately) can't hang the orchestrator. The cap of 8 covers
    every realistic org (CEO → SVP → VP → Director → Sr Mgr → Mgr → Sr Eng
    → Eng is 7 levels). A deeper tree means we have a cycle or a very flat
    org with anomalies — log + truncate is the right behavior.
    """
    visited: set[UUID] = {root_id}
    frontier: list[tuple[UUID, int]] = [(root_id, 0)]
    count = 0

    while frontier:
        node_id, depth = frontier.pop()
        if depth >= max_depth:
            continue
        node = nodes_by_id.get(node_id)
        if node is None:
            continue
        for child in node.direct_reports:
            if child in visited:
                continue
            visited.add(child)
            count += 1
            frontier.append((child, depth + 1))

    return count


def _estimate_one_scope(
    person: PersonRollup,
    *,
    direct_report_count: int,
    subtree_count: int,
) -> ScopeEstimate:
    """Pure scope construction for one person.

    `direct_report_count` and `subtree_count` come from the orchestrator's
    pre-built manager → report graph; passing them in keeps the function
    independent of the BFS implementation and trivially testable.
    """
    # Dedupe + sort the cluster-derived arrays for stable output
    # (idempotency + deterministic test assertions).
    owns_functions = sorted(set(person.cluster_domains))
    owns_technologies = sorted(
        s for s in set(person.cluster_sub_domains) if s
    )

    # Team-size: direct count is the floor, subtree count is the ceiling.
    # Both are None when the person isn't a manager (no edges → 0/0, but
    # we report None to distinguish "not a manager" from "manager with 0
    # current reports" — 0 reports is unusual but a real state during
    # transitions).
    if direct_report_count == 0 and subtree_count == 0:
        team_min: int | None = None
        team_max: int | None = None
    else:
        team_min = direct_report_count
        # Subtree includes direct reports — guard against a stale graph
        # producing subtree < direct (would violate the team_size_min ≤
        # team_size_max CHECK constraint).
        team_max = max(subtree_count, direct_report_count)

    return ScopeEstimate(
        person_id=person.person_id,
        account_id=person.account_id,
        owns_functions=owns_functions,
        owns_technologies=owns_technologies,
        team_size_min=team_min,
        team_size_max=team_max,
        budget_authority_level=_budget_level_from_seniority(person.seniority),
    )


def _build_scope_plan(
    persons: list[PersonRollup],
    edges: list[tuple[UUID, UUID]],
) -> dict[UUID, ScopeEstimate]:
    """Pure planner: given persons + manager → report edges, build the
    full per-person scope dict.

    Returns ``{person_id: ScopeEstimate}`` for every person in `persons`.
    """
    # Build the manager → reports adjacency.
    nodes_by_id: dict[UUID, ManagerNode] = {p.person_id: ManagerNode(p.person_id) for p in persons}
    for manager_id, report_id in edges:
        node = nodes_by_id.get(manager_id)
        if node is None:
            # Manager not in our person set (rare — happens when the manager
            # belongs to a different cluster's account or has been deleted).
            # Skip without crashing.
            continue
        node.direct_reports.append(report_id)

    out: dict[UUID, ScopeEstimate] = {}
    for person in persons:
        node = nodes_by_id[person.person_id]
        direct = len(node.direct_reports)
        subtree = _subtree_size(person.person_id, nodes_by_id) if direct else 0
        out[person.person_id] = _estimate_one_scope(
            person,
            direct_report_count=direct,
            subtree_count=subtree,
        )
    return out


# ─── Public DB orchestrator API ──────────────────────────────────────────────


# Chunk size for bulk scope upserts. Picked alongside the propagation chunk
# to keep each round trip well under the Supabase pooler statement timeout.
# At 12k+ persons per tenant the per-row INSERT loop took 15+ minutes;
# unnest-driven bulk upsert in 500-row chunks lands the same workload in
# under a minute.
_SCOPE_UPSERT_CHUNK_SIZE: int = 500


async def estimate_account_scopes(account_id: UUID) -> int:
    """Recompute every person_scope_estimates row for one tenant.

    Returns the number of rows written (≈ count of persons in the tenant
    who appear in any cluster). Idempotent — re-running upserts.

    ## Why this batches

    The original implementation issued one INSERT per person inside one
    transaction. On the Supabase pooler this took ~15 min for a 20k-person
    tenant and tripped the idempotency tests' patience. The new path:

      1. Builds the per-person `ScopeEstimate` plan in memory (pure planner).
      2. Materializes the upsert payload as 9 parallel arrays.
      3. Sends one `INSERT … FROM unnest(...) ON CONFLICT DO UPDATE` per
         chunk, each in its own short transaction so the connection budget
         stays at one and statement_timeout resets per chunk.

    Behavior is identical to the per-row path: same conflict target, same
    EXCLUDED column set, same `computed_at` bump.
    """
    persons = await _load_persons_for_account(account_id)
    if not persons:
        return 0
    edges = await _load_edges_for_account(account_id)

    plan = _build_scope_plan(persons, edges)
    estimates = list(plan.values())

    written = 0
    async with acquire() as conn:
        for start in range(0, len(estimates), _SCOPE_UPSERT_CHUNK_SIZE):
            chunk = estimates[start : start + _SCOPE_UPSERT_CHUNK_SIZE]
            async with conn.transaction():
                await _bulk_upsert_scopes(conn, chunk)
            written += len(chunk)
    return written


async def estimate_all_scopes() -> int:
    """Recompute scopes across every tenant. Convenience batch scan."""
    account_ids = await _all_account_ids_with_clusters()
    log.info("scope: %d tenants eligible", len(account_ids))
    total = 0
    for aid in account_ids:
        total += await estimate_account_scopes(aid)
    return total


# ─── DB I/O — small helpers, all mockable via monkeypatch ───────────────────


async def _load_persons_for_account(account_id: UUID) -> list[PersonRollup]:
    """One row per (person × cluster) — same person can appear in multiple
    clusters across companies, so we group by `person_id` after the fetch.
    """
    rows = await fetch(
        """
        SELECT
          p.id                        AS person_id,
          ocm.account_id              AS account_id,
          p.current_seniority_score   AS seniority,
          ofc.functional_domain       AS domain,
          ofc.sub_domain              AS sub_domain
        FROM org_cluster_members ocm
        JOIN persons p                 ON p.id = ocm.person_id
        JOIN org_functional_clusters ofc ON ofc.id = ocm.cluster_id
        WHERE ocm.account_id = $1
        """,
        account_id,
    )
    by_person: dict[UUID, PersonRollup] = {}
    for row in rows:
        pid = row["person_id"]
        rollup = by_person.get(pid)
        if rollup is None:
            rollup = PersonRollup(
                person_id=pid,
                account_id=row["account_id"],
                seniority=row["seniority"],
            )
            by_person[pid] = rollup
        rollup.cluster_domains.append(row["domain"])
        if row["sub_domain"] is not None:
            rollup.cluster_sub_domains.append(row["sub_domain"])
    return list(by_person.values())


async def _load_edges_for_account(account_id: UUID) -> list[tuple[UUID, UUID]]:
    """Pull (manager_id, report_id) pairs for the current reporting tree."""
    rows = await fetch(
        """
        SELECT manager_id, report_id
        FROM org_reporting_edges
        WHERE account_id = $1 AND is_current = TRUE
        """,
        account_id,
    )
    return [(row["manager_id"], row["report_id"]) for row in rows]


async def _all_account_ids_with_clusters() -> list[UUID]:
    rows = await fetch(
        "SELECT DISTINCT account_id FROM org_functional_clusters",
    )
    return [row["account_id"] for row in rows]


async def _upsert_scope(conn: Any, estimate: ScopeEstimate) -> None:
    """ON CONFLICT (person_id) DO UPDATE — the table's UNIQUE(person_id).

    Kept for any single-row caller; the bulk path uses
    `_bulk_upsert_scopes` for chunked writes.
    """
    await conn.execute(
        """
        INSERT INTO person_scope_estimates (
          account_id, person_id, owns_products, owns_technologies,
          owns_functions, owns_regions, team_size_min, team_size_max,
          budget_authority_level
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (person_id) DO UPDATE SET
          account_id              = EXCLUDED.account_id,
          owns_products           = EXCLUDED.owns_products,
          owns_technologies       = EXCLUDED.owns_technologies,
          owns_functions          = EXCLUDED.owns_functions,
          owns_regions            = EXCLUDED.owns_regions,
          team_size_min           = EXCLUDED.team_size_min,
          team_size_max           = EXCLUDED.team_size_max,
          budget_authority_level  = EXCLUDED.budget_authority_level,
          computed_at             = now()
        """,
        estimate.account_id,
        estimate.person_id,
        estimate.owns_products,
        estimate.owns_technologies,
        estimate.owns_functions,
        estimate.owns_regions,
        estimate.team_size_min,
        estimate.team_size_max,
        estimate.budget_authority_level,
    )


async def _bulk_upsert_scopes(
    conn: Any,
    estimates: list[ScopeEstimate],
) -> None:
    """Set-based upsert for a chunk of ScopeEstimates via COPY + temp table.

    ## Why COPY + temp, not unnest

    Each scope row carries four `text[]` array columns (owns_products,
    owns_technologies, owns_functions, owns_regions). Postgres `unnest()`
    flattens multidimensional arrays into a single 1D output set, so passing
    `text[][]` and expecting a column-of-arrays per row does NOT work — the
    array contents would collapse into the rows themselves.

    The canonical fast bulk-upsert pattern in Postgres is:

      1. Create a session-scoped TEMP table mirroring the target's columns.
      2. `COPY` the rows in via asyncpg's binary protocol (one round trip).
      3. `INSERT … SELECT … FROM tmp ON CONFLICT DO UPDATE` to merge into
         the target with the same conflict semantics as the per-row path.
      4. `TRUNCATE tmp` so the next chunk starts clean.

    `ON COMMIT DROP` removes the temp table at chunk-transaction end. Each
    chunk gets its own short transaction, so the table lifecycle is bounded
    even under long-running propagation passes.

    Behavior is identical to `_upsert_scope`: same conflict target
    (`person_id`), same `EXCLUDED.*` projection, same `computed_at` bump.
    """
    if not estimates:
        return

    # Temp table mirrors the columns we touch — not the entire target table,
    # so we don't need to worry about default-valued columns or generated
    # columns that exist in person_scope_estimates.
    await conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _scope_chunk (
          account_id              uuid NOT NULL,
          person_id               uuid NOT NULL,
          owns_products           text[] NOT NULL DEFAULT '{}',
          owns_technologies       text[] NOT NULL DEFAULT '{}',
          owns_functions          text[] NOT NULL DEFAULT '{}',
          owns_regions            text[] NOT NULL DEFAULT '{}',
          team_size_min           int,
          team_size_max           int,
          budget_authority_level  text
        ) ON COMMIT DROP
        """
    )
    # CREATE TEMP TABLE IF NOT EXISTS lets us reuse the table across calls
    # within the same transaction; otherwise the COMMIT DROP would force a
    # recreate on every chunk. Truncating first guarantees we never see
    # leftovers from a previous chunk.
    await conn.execute("TRUNCATE _scope_chunk")

    records = [
        (
            e.account_id,
            e.person_id,
            e.owns_products,
            e.owns_technologies,
            e.owns_functions,
            e.owns_regions,
            e.team_size_min,
            e.team_size_max,
            e.budget_authority_level,
        )
        for e in estimates
    ]
    await conn.copy_records_to_table(
        "_scope_chunk",
        records=records,
        columns=[
            "account_id",
            "person_id",
            "owns_products",
            "owns_technologies",
            "owns_functions",
            "owns_regions",
            "team_size_min",
            "team_size_max",
            "budget_authority_level",
        ],
    )

    await conn.execute(
        """
        INSERT INTO person_scope_estimates (
          account_id, person_id, owns_products, owns_technologies,
          owns_functions, owns_regions, team_size_min, team_size_max,
          budget_authority_level
        )
        SELECT
          account_id, person_id, owns_products, owns_technologies,
          owns_functions, owns_regions, team_size_min, team_size_max,
          budget_authority_level
        FROM _scope_chunk
        ON CONFLICT (person_id) DO UPDATE SET
          account_id              = EXCLUDED.account_id,
          owns_products           = EXCLUDED.owns_products,
          owns_technologies       = EXCLUDED.owns_technologies,
          owns_functions          = EXCLUDED.owns_functions,
          owns_regions            = EXCLUDED.owns_regions,
          team_size_min           = EXCLUDED.team_size_min,
          team_size_max           = EXCLUDED.team_size_max,
          budget_authority_level  = EXCLUDED.budget_authority_level,
          computed_at             = now()
        """
    )


__all__ = [
    "ManagerNode",
    "PersonRollup",
    "ScopeEstimate",
    "_build_scope_plan",
    "_budget_level_from_seniority",
    "_bulk_upsert_scopes",
    "_estimate_one_scope",
    "_subtree_size",
    "estimate_account_scopes",
    "estimate_all_scopes",
]
