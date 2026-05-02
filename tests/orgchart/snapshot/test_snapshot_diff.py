"""Snapshot diff for the Intel chart shape against ``intel_baseline.json``.

Tolerances are deliberately loose to absorb churn — small drift in cluster
sizes or member counts is expected as new persons / employment periods are
backfilled. A baseline-busting failure means either:
  (a) the inference algorithm changed (rebaseline intentionally), or
  (b) the chart silently shifted (investigate before rebaselining).
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

# Mirrors conftest.INTEL_COMPANY_ID; duplicated to keep this module importable
# without depending on a `tests.orgchart` package path.
INTEL_COMPANY_ID = UUID("e6c126a6-5a70-4968-b37a-3648292e60ab")


pytestmark = pytest.mark.snapshot

_BASELINE_PATH = Path(__file__).parent / "intel_baseline.json"
_BASELINE = json.loads(_BASELINE_PATH.read_text())


async def test_intel_cluster_count_within_tolerance(fetch_one):
    """Intel cluster count must be within ±3 of baseline.

    Tolerance ±3 absorbs sub-clusters appearing/disappearing as data shifts.
    """
    row = await fetch_one(
        "SELECT COUNT(*) AS n FROM org_functional_clusters WHERE company_id = $1",
        INTEL_COMPANY_ID,
    )
    current = int(row["n"])
    baseline = int(_BASELINE["cluster_count"])
    assert abs(current - baseline) <= 3, (
        f"Intel cluster_count drift: baseline={baseline}, current={current} "
        f"(tolerance ±3)"
    )


async def test_intel_cluster_member_count_within_tolerance(fetch_one):
    """Intel total cluster member count must be within ±50 of baseline.

    Tolerance ±50 absorbs new persons backfills.
    """
    row = await fetch_one(
        """
        SELECT COUNT(*) AS n
          FROM org_cluster_members ocm
          JOIN org_functional_clusters ofc ON ofc.id = ocm.cluster_id
         WHERE ofc.company_id = $1
        """,
        INTEL_COMPANY_ID,
    )
    current = int(row["n"])
    baseline = int(_BASELINE["cluster_member_count"])
    assert abs(current - baseline) <= 50, (
        f"Intel cluster_member_count drift: baseline={baseline}, "
        f"current={current} (tolerance ±50)"
    )


async def test_intel_edges_count_within_tolerance(fetch_one):
    """Intel edges count must be within ±30% of baseline.

    Tolerance loose because edge counts shift more than cluster shapes —
    inference may add or remove edges based on signal availability.
    """
    row = await fetch_one(
        """
        SELECT COUNT(*) AS n
          FROM org_reporting_edges e
          JOIN org_cluster_members ocm ON ocm.person_id = e.report_id
          JOIN org_functional_clusters ofc ON ofc.id = ocm.cluster_id
         WHERE ofc.company_id = $1
           AND e.is_current = TRUE
        """,
        INTEL_COMPANY_ID,
    )
    current = int(row["n"])
    baseline = int(_BASELINE["edges_count"])
    lo, hi = baseline * 0.70, baseline * 1.30
    assert lo <= current <= hi, (
        f"Intel edges_count drift: baseline={baseline}, current={current} "
        f"(tolerance ±30%, expected [{lo:.0f}, {hi:.0f}])"
    )


async def test_intel_top_clusters_match(fetch_all):
    """Top 3 clusters by member_count (baseline) must still be in current top 3.

    Set membership only — order may shift. Catches a major shape change in
    Intel's chart (e.g., the engineering cluster collapsing).
    """
    rows = await fetch_all(
        """
        SELECT ofc.functional_domain,
               COUNT(*) AS member_count
          FROM org_cluster_members ocm
          JOIN org_functional_clusters ofc ON ofc.id = ocm.cluster_id
         WHERE ofc.company_id = $1
         GROUP BY ofc.functional_domain
         ORDER BY member_count DESC
         LIMIT 3
        """,
        INTEL_COMPANY_ID,
    )
    current_top3 = {r["functional_domain"] for r in rows}
    baseline_clusters = sorted(
        _BASELINE["clusters"], key=lambda c: c["member_count"], reverse=True
    )
    baseline_top3 = {c["functional_domain"] for c in baseline_clusters[:3]}

    assert current_top3 == baseline_top3, (
        f"Intel top-3 functional clusters changed.\n"
        f"  baseline: {sorted(baseline_top3)}\n"
        f"  current : {sorted(current_top3)}"
    )


async def test_intel_mean_confidence_within_tolerance(fetch_one):
    """Intel mean edge confidence must be within ±0.10 of baseline.

    Loose tolerance — a shift of >0.10 in average confidence suggests the
    scoring model changed (intentional or otherwise).
    """
    row = await fetch_one(
        """
        SELECT AVG(e.confidence) AS avg_conf
          FROM org_reporting_edges e
          JOIN org_cluster_members ocm ON ocm.person_id = e.report_id
          JOIN org_functional_clusters ofc ON ofc.id = ocm.cluster_id
         WHERE ofc.company_id = $1
           AND e.is_current = TRUE
        """,
        INTEL_COMPANY_ID,
    )
    if row is None or row.get("avg_conf") is None:
        pytest.skip("no current Intel edges to compute mean confidence")
    current = float(row["avg_conf"])
    baseline = float(_BASELINE["confidence_stats"]["mean_confidence"])
    assert abs(current - baseline) <= 0.10, (
        f"Intel mean_confidence drift: baseline={baseline:.3f}, "
        f"current={current:.3f} (tolerance ±0.10)"
    )
