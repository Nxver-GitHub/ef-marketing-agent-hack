"""End-to-end org-chart pipeline integration test.

Exercises the full A1 (clustering) → A2 (hierarchy inference) → A8
(path-confidence propagation) → A3 (scope estimation) → A7 (validation)
pipeline against the live Supabase DB, using Intel as a stable canary.

Why Intel? It's the most well-populated company in dev seed data
(~hundreds of current employment_periods spanning hardware engineering,
software, manufacturing ops, sales). It exercises every stage:
multiple sub-clusters, IC track, span-cap edges, multi-level hierarchy.

Each test guards against a different regression class — see per-test
docstrings. Tolerances are deliberately loose to absorb realistic
data drift; the floors here are the "obviously broken" thresholds, not
the "exact match" targets.

Date stamp: 2026-04-29.
"""
from __future__ import annotations

import pytest

from credence.orgchart.clustering import ClusterRollup, cluster_company
from credence.orgchart.hierarchy import (
    HierarchyRollup,
    infer_company_hierarchy,
)
from credence.orgchart.propagation import PropagationRollup, propagate_account
from credence.orgchart.scope import estimate_account_scopes
from credence.orgchart.validation import ValidationReport, validate_account

from uuid import UUID

# Constants mirrored from conftest.py — relative import doesn't resolve
# because tests/orgchart isn't a package. Conftest is still auto-loaded
# by pytest for fixtures.
DEFAULT_ACCOUNT_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")
INTEL_COMPANY_ID: UUID = UUID("e6c126a6-5a70-4968-b37a-3648292e60ab")


@pytest.mark.integration
async def test_intel_full_pipeline_round_trip(fetch_one) -> None:
    """End-to-end: clustering → hierarchy → propagation → scope → validation.

    Guards against any single stage silently producing zero output. If a
    schema migration drops a join column, a query regresses to returning
    nothing, or a transaction raises mid-pipeline, this test catches it
    before customers see an empty chart.
    """
    # ── A1: cluster Intel ───────────────────────────────────────────────────
    cluster_rollup = await cluster_company(INTEL_COMPANY_ID)
    assert isinstance(cluster_rollup, ClusterRollup)
    # Intel has hundreds of enriched persons; 5 clusters / 100 members is
    # the absolute floor — well below current state (~10 clusters, ~300
    # members on 2026-04-29). Anything lower means the clustering query
    # silently dropped most rows.
    assert cluster_rollup.cluster_count >= 5, (
        f"Intel cluster_count={cluster_rollup.cluster_count}; expected ≥5. "
        "Clustering may have lost domains."
    )
    assert cluster_rollup.member_count >= 100, (
        f"Intel member_count={cluster_rollup.member_count}; expected ≥100."
    )
    # Intel ships chips — it has Distinguished/Principal/Staff Engineers.
    # Floor of 5 IC-track persons absorbs taxonomy churn; current is ~30.
    assert cluster_rollup.ic_track_count >= 5, (
        f"Intel ic_track_count={cluster_rollup.ic_track_count}; expected ≥5. "
        "IC-track classifier may be regressing."
    )

    # ── A2: hierarchy inference ─────────────────────────────────────────────
    hierarchy_rollups = await infer_company_hierarchy(INTEL_COMPANY_ID)
    assert isinstance(hierarchy_rollups, list)
    assert all(isinstance(r, HierarchyRollup) for r in hierarchy_rollups)
    edges_written = sum(r.edges_written for r in hierarchy_rollups)
    # Each cluster typically writes 5-30 implicit edges; 50 across Intel
    # is the floor. Current state writes a few hundred.
    assert edges_written >= 50, (
        f"Intel edges_written={edges_written}; expected ≥50. "
        "Hierarchy inference may have stopped writing edges."
    )

    # ── A8: propagation across the whole tenant ─────────────────────────────
    prop_rollup = await propagate_account(DEFAULT_ACCOUNT_ID)
    assert isinstance(prop_rollup, PropagationRollup)
    # Propagation runs across the entire tenant (all companies, not just
    # Intel) — so its edge count is necessarily ≥ Intel's edges_written.
    assert prop_rollup.edges_propagated >= edges_written, (
        f"propagate_account edges_propagated={prop_rollup.edges_propagated} "
        f"< Intel edges_written={edges_written}. Propagation lost edges."
    )

    # ── A3: scope estimation for the tenant ─────────────────────────────────
    scope_count = await estimate_account_scopes(DEFAULT_ACCOUNT_ID)
    assert isinstance(scope_count, int)
    # Intel alone has >100 cluster members; tenant scope total is much
    # larger. 100 floor catches a complete-failure regression.
    assert scope_count >= 100, (
        f"estimate_account_scopes={scope_count}; expected ≥100."
    )

    # ── A7: validation ──────────────────────────────────────────────────────
    report = await validate_account(DEFAULT_ACCOUNT_ID)
    assert isinstance(report, ValidationReport)
    # Current state on 2026-04-29: 2 violations. Bound generously at <50
    # to absorb future drift while still catching catastrophic regressions
    # (e.g., span-cap enforcement broken).
    assert report.total_violations < 50, (
        f"ValidationReport total_violations={report.total_violations}; "
        f"expected <50. Span={len(report.span_violations)} "
        f"Cycle={len(report.cycle_violations)} "
        f"IC={len(report.ic_violations)}"
    )


@pytest.mark.integration
async def test_intel_chart_has_known_clusters(fetch_all) -> None:
    """After clustering, Intel must have all four expected functional domains.

    Intel is a semiconductor + manufacturing + sales company. If any of
    these four domains is missing, the clustering taxonomy has regressed
    or the seed data lost rows. The product is built on these primary
    domain branches.
    """
    # Run A1 to ensure clusters are materialized for this test session.
    await cluster_company(INTEL_COMPANY_ID)

    rows = await fetch_all(
        """
        SELECT functional_domain
        FROM org_functional_clusters
        WHERE company_id = $1
          AND sub_domain IS NULL
        """,
        INTEL_COMPANY_ID,
    )
    domains = {row["functional_domain"] for row in rows}
    expected = {
        "hardware_engineering",
        "software_engineering",
        "sales_marketing",
        "manufacturing_ops",
    }
    missing = expected - domains
    assert not missing, (
        f"Intel chart missing expected functional domains: {missing}. "
        f"Present domains: {sorted(domains)}"
    )


@pytest.mark.integration
async def test_intel_chart_has_implicit_scoring_edges(fetch_one) -> None:
    """The chart must be majority implicit-scored.

    Until explicit-edge ingesters (job-posting parser, SEC proxy, etc.)
    ship, all of Intel's reporting edges should come from the implicit
    scorer. Floor of 50 catches a regression where the scorer stops
    emitting edges entirely (e.g., min-confidence threshold change).
    """
    # Make sure A1 + A2 have run for Intel.
    await cluster_company(INTEL_COMPANY_ID)
    await infer_company_hierarchy(INTEL_COMPANY_ID)

    row = await fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM org_reporting_edges e
        WHERE e.is_current = TRUE
          AND e.inference_method = 'implicit_scoring'
          AND e.manager_id IN (
              SELECT person_id
              FROM employment_periods
              WHERE company_id = $1
                AND is_current = TRUE
          )
        """,
        INTEL_COMPANY_ID,
    )
    count = int(row.get("n", 0))
    assert count >= 50, (
        f"Intel implicit_scoring edges={count}; expected ≥50. "
        "The chart should be majority implicit-scored on 2026-04-29."
    )
