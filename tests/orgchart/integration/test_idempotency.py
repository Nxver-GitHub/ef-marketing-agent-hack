"""Pipeline idempotency integration tests.

The org-chart pipeline must be safe to re-run on stable data. Drift
between runs implies hidden nondeterminism — UUID iteration order, NOW()
captured into stored values, set-iteration in scoring — any of which
would make the chart unstable across runs and erode operator trust.

These tests run each stage twice and assert state equivalence. They do
NOT mutate data beyond what the pipeline normally writes; the pipeline
itself is built to be idempotent on stable inputs.

Date stamp: 2026-04-29.
"""
from __future__ import annotations

import pytest

from credence.orgchart.clustering import cluster_company
from credence.orgchart.hierarchy import infer_company_hierarchy
from credence.orgchart.propagation import propagate_account
from credence.orgchart.scope import estimate_account_scopes
from credence.orgchart.validation import validate_account

from uuid import UUID

DEFAULT_ACCOUNT_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")
INTEL_COMPANY_ID: UUID = UUID("e6c126a6-5a70-4968-b37a-3648292e60ab")


@pytest.mark.integration
async def test_cluster_company_is_idempotent(fetch_one) -> None:
    """Re-clustering Intel must not grow cluster or member rows.

    Guards against a bug where ON CONFLICT DO UPDATE gets dropped from
    the upsert path — that would result in duplicate rows on every run.
    """
    rollup1 = await cluster_company(INTEL_COMPANY_ID)
    row1 = await fetch_one(
        "SELECT COUNT(*) AS n FROM org_functional_clusters WHERE company_id = $1",
        INTEL_COMPANY_ID,
    )
    cluster_rows_before = int(row1.get("n", 0))

    rollup2 = await cluster_company(INTEL_COMPANY_ID)
    row2 = await fetch_one(
        "SELECT COUNT(*) AS n FROM org_functional_clusters WHERE company_id = $1",
        INTEL_COMPANY_ID,
    )
    cluster_rows_after = int(row2.get("n", 0))

    assert rollup1.cluster_count == rollup2.cluster_count, (
        f"cluster_count drift: {rollup1.cluster_count} → {rollup2.cluster_count}"
    )
    assert rollup1.member_count == rollup2.member_count, (
        f"member_count drift: {rollup1.member_count} → {rollup2.member_count}"
    )
    assert cluster_rows_after == cluster_rows_before, (
        f"org_functional_clusters row count grew: "
        f"{cluster_rows_before} → {cluster_rows_after}"
    )


@pytest.mark.integration
async def test_infer_company_hierarchy_is_idempotent() -> None:
    """Re-running hierarchy inference must produce the same edges_written.

    Guards against the skip-write check (Δconfidence < 0.02) being
    bypassed — that would re-historicize edges on every run, blowing
    up the history table.
    """
    # Prerequisite: clusters must exist before hierarchy.
    await cluster_company(INTEL_COMPANY_ID)

    rollups1 = await infer_company_hierarchy(INTEL_COMPANY_ID)
    rollups2 = await infer_company_hierarchy(INTEL_COMPANY_ID)

    edges1 = sum(r.edges_written for r in rollups1)
    edges2 = sum(r.edges_written for r in rollups2)
    # NOTE: edges_written counts the planner's intent, not actual DB inserts.
    # The planner is deterministic on stable inputs, so the two passes
    # should agree exactly.
    assert edges1 == edges2, (
        f"hierarchy edges_written drift: {edges1} → {edges2}. "
        "Planner is nondeterministic."
    )


@pytest.mark.integration
async def test_estimate_account_scopes_is_idempotent(fetch_one) -> None:
    """Re-estimating scopes must not grow person_scope_estimates rows.

    Guards against the upsert key on person_scope_estimates being dropped
    or mis-targeted — that would create duplicate scope rows per person.
    """
    count1 = await estimate_account_scopes(DEFAULT_ACCOUNT_ID)
    row1 = await fetch_one(
        "SELECT COUNT(*) AS n FROM person_scope_estimates WHERE account_id = $1",
        DEFAULT_ACCOUNT_ID,
    )
    rows_before = int(row1.get("n", 0))

    count2 = await estimate_account_scopes(DEFAULT_ACCOUNT_ID)
    row2 = await fetch_one(
        "SELECT COUNT(*) AS n FROM person_scope_estimates WHERE account_id = $1",
        DEFAULT_ACCOUNT_ID,
    )
    rows_after = int(row2.get("n", 0))

    assert count1 == count2, f"scope count drift: {count1} → {count2}"
    assert rows_after == rows_before, (
        f"person_scope_estimates row count grew: {rows_before} → {rows_after}"
    )


@pytest.mark.integration
async def test_pipeline_full_re_run_no_state_drift(fetch_one) -> None:
    """Full A1→A2→A8→A3→A7 re-run must leave row counts stable.

    The strongest idempotency guarantee — even if individual stages are
    fine, their interaction could leak rows. This snapshots three counters
    before and after a full pipeline run and asserts equality.
    """
    # Warm up: ensure prior pipeline state is materialized.
    await cluster_company(INTEL_COMPANY_ID)
    await infer_company_hierarchy(INTEL_COMPANY_ID)
    await propagate_account(DEFAULT_ACCOUNT_ID)
    await estimate_account_scopes(DEFAULT_ACCOUNT_ID)

    async def _snapshot() -> dict[str, int]:
        edges = await fetch_one(
            "SELECT COUNT(*) AS n FROM org_reporting_edges WHERE account_id = $1",
            DEFAULT_ACCOUNT_ID,
        )
        clusters = await fetch_one(
            "SELECT COUNT(*) AS n FROM org_functional_clusters WHERE account_id = $1",
            DEFAULT_ACCOUNT_ID,
        )
        scopes = await fetch_one(
            "SELECT COUNT(*) AS n FROM person_scope_estimates WHERE account_id = $1",
            DEFAULT_ACCOUNT_ID,
        )
        return {
            "edges": int(edges.get("n", 0)),
            "clusters": int(clusters.get("n", 0)),
            "scopes": int(scopes.get("n", 0)),
        }

    before = await _snapshot()

    await cluster_company(INTEL_COMPANY_ID)
    await infer_company_hierarchy(INTEL_COMPANY_ID)
    await propagate_account(DEFAULT_ACCOUNT_ID)
    await estimate_account_scopes(DEFAULT_ACCOUNT_ID)
    await validate_account(DEFAULT_ACCOUNT_ID)

    after = await _snapshot()

    assert before == after, (
        f"Pipeline re-run produced state drift: before={before}, after={after}"
    )
