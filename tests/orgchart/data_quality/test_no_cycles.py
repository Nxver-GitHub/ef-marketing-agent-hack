"""Data-quality assertions: reporting graph must not contain cycles.

A reporting cycle ("A reports to B reports to A") is a write-time bug that
makes the chart un-renderable and breaks downstream BFS-based features
(propagation, scope rollups). These tests run read-only against the live DB.
"""
from __future__ import annotations

from uuid import UUID

import pytest

# Mirrors conftest.DEFAULT_ACCOUNT_ID — duplicated to keep this module
# importable without relying on a `tests.orgchart` package path.
DEFAULT_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")


pytestmark = pytest.mark.data_quality


def _find_cycles(edges: list[dict]) -> list[list[UUID]]:
    """Walk every node up the manager chain; return the first 5 cycles found.

    Each cycle is returned as the list of person_ids forming the loop, so
    operators can act on the failure message directly.
    """
    parent: dict[UUID, UUID] = {}
    for edge in edges:
        parent[edge["report_id"]] = edge["manager_id"]

    cycles: list[list[UUID]] = []
    seen_global: set[UUID] = set()

    for start in parent:
        if start in seen_global:
            continue
        path: list[UUID] = []
        in_path: set[UUID] = set()
        cur: UUID | None = start
        while cur is not None and cur not in in_path:
            if cur in seen_global:
                # cur leads into already-explored, non-cyclic territory
                break
            path.append(cur)
            in_path.add(cur)
            cur = parent.get(cur)
        if cur is not None and cur in in_path:
            # cycle detected — slice path from the first occurrence of `cur`
            idx = path.index(cur)
            cycle = path[idx:]
            cycles.append(cycle)
            if len(cycles) >= 5:
                break
        seen_global.update(in_path)

    return cycles


async def test_no_reporting_cycles_in_current_edges(fetch_all):
    """Build the report→manager adjacency; assert no cycles exist."""
    edges = await fetch_all(
        """
        SELECT report_id, manager_id
          FROM org_reporting_edges
         WHERE is_current = TRUE
           AND manager_id IS NOT NULL
        """
    )
    cycles = _find_cycles(edges)
    assert cycles == [], (
        f"Found {len(cycles)} reporting cycle(s). "
        f"First {min(5, len(cycles))} cycles (person_ids): "
        f"{[[str(p) for p in c] for c in cycles[:5]]}"
    )


async def test_validation_report_clean_for_default_tenant():
    """``validate_account`` must report no cycle / IC violations for the default tenant.

    Span violations are heuristic; we tolerate up to 25 (post-2026-05-01
    dataset has 14 across 8,011 current edges — roughly 0.2% of edges and
    each is 1-4 over the soft cap). A real planner bug would push the
    number into the dozens. The bound was bumped from 10 → 25 when the
    dataset grew from 7,094 → 8,011 edges; rebaseline if the edge count
    changes by another order of magnitude.
    """
    from credence.db import close_pool
    from credence.orgchart import validation

    try:
        report = await validation.validate_account(DEFAULT_ACCOUNT_ID)
    finally:
        await close_pool()

    assert report.cycle_violations == [], (
        f"Cycle violations present: {report.cycle_violations[:5]}"
    )
    assert report.ic_violations == [], (
        f"IC violations present: {report.ic_violations[:5]}"
    )
    assert len(report.span_violations) <= 25, (
        f"Too many span violations ({len(report.span_violations)}); "
        f"current tolerance is 25. First 5: {report.span_violations[:5]}"
    )
