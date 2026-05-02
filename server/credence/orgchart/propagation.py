"""Org-chart Stage 1.5 — confidence propagation (v3.1 Plan A, Task A8).

Per CLAUDE.md L186: every edge has ``confidence`` (local edge score) AND
``path_confidence`` (the propagated-from-root score). The hierarchy.py
writer fills in ``confidence`` at write time; this module fills in
``path_confidence`` as a separate post-pass that walks the materialized
edges as a directed forest.

## Why a post-pass

Path confidence depends on the entire chain from root to node — when
``hierarchy.py`` writes an edge, the rest of the chain may not be
materialized yet (clustering can produce edges out of seniority order).
A separate pass after all edges land is the only way to compute it
correctly without holding a dependency graph in the writer's head.

## The math

For a chain ``CEO → SVP → VP → Director → Manager`` with edge confidences
``[0.95, 0.78, 0.85, 0.74]``:

```
path_confidence(CEO)       = 1.0
path_confidence(SVP)       = 1.0  * 0.95 = 0.95
path_confidence(VP)        = 0.95 * 0.78 = 0.741
path_confidence(Director)  = 0.741 * 0.85 = 0.630
path_confidence(Manager)   = 0.630 * 0.74 = 0.466
```

A leaf person's ``path_confidence`` reflects the cumulative likelihood that
the entire reporting chain above them is correct. A 0.466 path means
roughly 47% confidence that the chain is structurally right.

## Decision 4 + Decision 7

CLAUDE.md Decision 7 (pre-materialized warm paths) extends to the org chart:
the UI reads ``org_reporting_edges`` directly and never recomputes
``path_confidence`` on the fly. Decision 4 (unknown nodes rendered) means
edges to/from unresolved-target nodes still get propagated; the UI styles
them differently but the math is identical.

## Cycle handling

If validation.py reports a cycle, propagation skips the cycle's nodes (their
``path_confidence`` stays NULL) rather than infinite-looping. The pass logs
a warning for each skipped node so operators can find them.

## Idempotency

Pure UPDATE — re-running on the same data produces the same
``path_confidence`` values. No INSERTs, no schema impact.

## Tenancy

Reads + writes scoped to one ``account_id`` per orchestrator call. The
batch scan (`propagate_all_accounts`) iterates account-by-account.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ..db import acquire, fetch

log = logging.getLogger(__name__)


# ─── Public types ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EdgeConfidence:
    """A (manager → report) edge with its local confidence."""

    manager_id: UUID
    report_id: UUID
    confidence: float


@dataclass(slots=True)
class PropagationRollup:
    """Per-account summary of a propagation pass."""

    account_id: UUID
    edges_total: int
    edges_propagated: int
    cycle_skipped: int = 0
    orphan_skipped: int = 0


# ─── Pure propagation logic (no DB) ──────────────────────────────────────────


def _build_path_confidences(
    edges: list[EdgeConfidence],
) -> tuple[dict[UUID, float], set[UUID]]:
    """Pure planner: walk the forest and compute path_confidence per node.

    Returns ``(path_conf_by_person, skipped_in_cycles)``. Roots (persons who
    appear as a manager in some edge but never as a report) get
    path_confidence = 1.0; their reports get the edge confidence directly;
    further descendants multiply down the chain.

    Persons reachable only through a cycle are returned in the second
    element so the caller can log + skip them. The first element only
    contains nodes whose path is well-defined.
    """
    # Build (report_id → manager_id) and (manager_id → list of (report_id, conf)).
    parent_of: dict[UUID, UUID] = {}
    edge_conf: dict[tuple[UUID, UUID], float] = {}
    children_of: dict[UUID, list[UUID]] = defaultdict(list)
    for e in edges:
        parent_of[e.report_id] = e.manager_id
        edge_conf[(e.manager_id, e.report_id)] = e.confidence
        children_of[e.manager_id].append(e.report_id)

    # Identify roots: every manager_id that doesn't appear as a report.
    all_managers = set(children_of.keys())
    all_reports = set(parent_of.keys())
    roots = all_managers - all_reports

    # Detect cycle members via a separate DFS so propagation doesn't
    # infinite-loop. A node is in a cycle if walking up parents never
    # reaches a root.
    cycle_members: set[UUID] = set()
    for node in list(parent_of.keys()):
        seen: set[UUID] = set()
        cur: UUID | None = node
        while cur is not None and cur not in seen:
            seen.add(cur)
            cur = parent_of.get(cur)
        if cur is not None:
            # We re-encountered a node — entire walk is in a cycle.
            cycle_members.update(seen)

    # BFS from each root, multiplying confidences down.
    path_conf: dict[UUID, float] = {}
    for root in roots:
        if root in cycle_members:
            continue
        path_conf[root] = 1.0
        frontier: list[UUID] = [root]
        while frontier:
            node = frontier.pop()
            base = path_conf[node]
            for child in children_of.get(node, []):
                if child in cycle_members:
                    continue
                conf = edge_conf[(node, child)]
                path_conf[child] = base * conf
                frontier.append(child)

    return path_conf, cycle_members


# ─── Public DB orchestrator API ──────────────────────────────────────────────


# Chunk size for bulk path_confidence writes. Picked to keep each UPDATE round
# trip below Supabase's pooler statement-timeout (default ~30s on the session
# pooler) while still amortizing per-call overhead. A 7k-edge tenant takes
# ~7-15 chunks at this size.
_PROPAGATION_CHUNK_SIZE: int = 500


async def propagate_account(account_id: UUID) -> PropagationRollup:
    """Compute + write path_confidence for every current edge in one tenant.

    Returns a summary so callers can surface "X edges propagated, Y skipped
    due to cycles" in admin tools. Pure UPDATE on `org_reporting_edges`;
    no INSERTs, no schema impact.

    ## Why this batches

    Earlier versions issued one UPDATE per edge in a single transaction.
    On the Supabase session pooler, ~5k+ edges in one transaction tripped
    the statement_timeout. The new path:

      1. Computes the (manager_id, report_id, path_confidence) tuples in
         memory via the pure planner (`_build_path_confidences`).
      2. Batches them into ``_PROPAGATION_CHUNK_SIZE`` chunks.
      3. For each chunk, sends a single UPDATE … FROM unnest(...) — one
         round trip, set-based update, fast under the pooler.
      4. Each chunk is its own transaction so a long-running run doesn't
         hold one transaction open against the pooler for the whole pass.

    Behavioral compatibility is preserved: cycle members are still skipped,
    orphans are still counted, the rollup shape is identical.
    """
    edges = await _load_edges(account_id)
    if not edges:
        return PropagationRollup(
            account_id=account_id,
            edges_total=0,
            edges_propagated=0,
        )

    path_conf, cycle_members = _build_path_confidences(edges)

    # Single pass to classify every edge into one of three buckets.
    to_write: list[tuple[UUID, UUID, float]] = []
    cycle_skipped = 0
    orphan_skipped = 0
    for e in edges:
        if e.report_id in cycle_members:
            cycle_skipped += 1
            continue
        if e.report_id not in path_conf:
            orphan_skipped += 1
            continue
        to_write.append((e.manager_id, e.report_id, path_conf[e.report_id]))

    propagated = 0
    if to_write:
        # One acquire() for the whole pass — opening a fresh pooled connection
        # per chunk hits Supabase's session-pool MaxClients ceiling on tenants
        # with thousands of edges. We hold a single connection and run each
        # chunk in its own short transaction so the statement_timeout window
        # resets per chunk while the connection budget stays at one.
        async with acquire() as conn:
            for start in range(0, len(to_write), _PROPAGATION_CHUNK_SIZE):
                chunk = to_write[start : start + _PROPAGATION_CHUNK_SIZE]
                async with conn.transaction():
                    await _bulk_update_path_confidence(conn, chunk)
                propagated += len(chunk)

    if cycle_skipped:
        log.warning(
            "propagation: %d edges in cycles skipped for account %s",
            cycle_skipped, account_id,
        )

    return PropagationRollup(
        account_id=account_id,
        edges_total=len(edges),
        edges_propagated=propagated,
        cycle_skipped=cycle_skipped,
        orphan_skipped=orphan_skipped,
    )


async def propagate_all_accounts() -> list[PropagationRollup]:
    """Convenience batch scan — propagate every tenant's tree."""
    account_ids = await _all_account_ids()
    log.info("propagation: %d tenants eligible", len(account_ids))
    rollups: list[PropagationRollup] = []
    for aid in account_ids:
        rollups.append(await propagate_account(aid))
    return rollups


# ─── DB I/O ──────────────────────────────────────────────────────────────────


async def _load_edges(account_id: UUID) -> list[EdgeConfidence]:
    rows = await fetch(
        """
        SELECT manager_id, report_id, confidence
        FROM org_reporting_edges
        WHERE account_id = $1 AND is_current = TRUE
        """,
        account_id,
    )
    return [
        EdgeConfidence(
            manager_id=row["manager_id"],
            report_id=row["report_id"],
            confidence=float(row["confidence"]),
        )
        for row in rows
    ]


async def _all_account_ids() -> list[UUID]:
    rows = await fetch(
        "SELECT DISTINCT account_id FROM org_reporting_edges",
    )
    return [row["account_id"] for row in rows]


async def _update_path_confidence(
    conn: Any,
    *,
    manager_id: UUID,
    report_id: UUID,
    path_confidence: float,
) -> None:
    """Update one current edge's path_confidence.

    Kept for backwards compatibility / single-edge call sites; the bulk
    path now goes through `_bulk_update_path_confidence`.
    """
    await conn.execute(
        """
        UPDATE org_reporting_edges
        SET path_confidence = $1, updated_at = now()
        WHERE manager_id = $2 AND report_id = $3 AND is_current = TRUE
        """,
        path_confidence,
        manager_id,
        report_id,
    )


async def _bulk_update_path_confidence(
    conn: Any,
    rows: list[tuple[UUID, UUID, float]],
) -> None:
    """Set-based UPDATE for a chunk of (manager_id, report_id, path_conf) tuples.

    Uses `unnest($1::uuid[], $2::uuid[], $3::numeric[])` so the entire chunk
    is one round trip. Postgres planner can hash-join the chunk against the
    org_reporting_edges index and update all matching current edges in a
    single set-based pass — orders of magnitude faster than the per-edge
    UPDATE loop, and bounded in time to avoid the pooler statement timeout.
    """
    if not rows:
        return
    managers = [r[0] for r in rows]
    reports = [r[1] for r in rows]
    confs = [r[2] for r in rows]
    await conn.execute(
        """
        UPDATE org_reporting_edges AS e
        SET path_confidence = u.pc,
            updated_at      = now()
        FROM unnest($1::uuid[], $2::uuid[], $3::numeric[]) AS u(mgr, rpt, pc)
        WHERE e.manager_id = u.mgr
          AND e.report_id  = u.rpt
          AND e.is_current = TRUE
        """,
        managers,
        reports,
        confs,
    )


__all__ = [
    "EdgeConfidence",
    "PropagationRollup",
    "_build_path_confidences",
    "propagate_account",
    "propagate_all_accounts",
]
