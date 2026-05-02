"""Data-quality assertions: confidence / path_confidence stay in their contracts.

Pins (a) the [0,1] range CHECK constraint, (b) the IMPLICIT_SCORE_CAP for
implicit edges, (c) the Decision 3 ordering (explicit > implicit on average),
and (d) the dominant_signal keyspace. These are catches for schema-migration
drift — the DB constraints enforce most of these; the tests confirm the
constraints survive future migrations.
"""
from __future__ import annotations

import pytest

from credence.orgchart.hierarchy import IMPLICIT_SCORE_CAP


pytestmark = pytest.mark.data_quality


_DOMINANT_SIGNAL_KEYSPACE: frozenset[str] = frozenset(
    {
        "seniority_gap",
        "domain_match",
        "subdomain_match",
        "manager_title",
        "span_capacity",
        "patent_cluster",
        "geographic_scope",
        "unknown",
    }
)


async def test_path_confidence_in_range_zero_to_one(fetch_one):
    """path_confidence ∈ [0, 1] for every current edge that has it set."""
    row = await fetch_one(
        """
        SELECT MIN(path_confidence) AS lo,
               MAX(path_confidence) AS hi,
               COUNT(*)             AS n
          FROM org_reporting_edges
         WHERE is_current = TRUE
           AND path_confidence IS NOT NULL
        """
    )
    if not row or row.get("n", 0) == 0:
        pytest.skip("no edges with path_confidence set yet")
    lo, hi = float(row["lo"]), float(row["hi"])
    assert lo >= 0.0, f"path_confidence min {lo} below 0"
    assert hi <= 1.0, f"path_confidence max {hi} above 1"


async def test_local_confidence_below_implicit_cap(fetch_one):
    """Implicit-scoring edges must have confidence ≤ IMPLICIT_SCORE_CAP (0.95).

    Pins the contract that implicit scoring never exceeds the cap. If this
    drifts, an explicit signal would lose its priority over a high-scoring
    implicit edge, violating Decision 3.
    """
    row = await fetch_one(
        """
        SELECT MAX(confidence) AS hi, COUNT(*) AS n
          FROM org_reporting_edges
         WHERE is_current = TRUE
           AND inference_method = 'implicit_scoring'
        """
    )
    if not row or row.get("n", 0) == 0:
        pytest.skip("no implicit_scoring edges present")
    hi = float(row["hi"])
    assert hi <= IMPLICIT_SCORE_CAP + 1e-9, (
        f"implicit max confidence {hi:.4f} exceeds cap {IMPLICIT_SCORE_CAP}"
    )


async def test_explicit_edges_have_higher_confidence_than_implicit_median(fetch_one):
    """Decision 3: explicit signals are more confident than implicit on average."""
    row = await fetch_one(
        """
        SELECT
          AVG(CASE WHEN inference_method LIKE 'explicit_%'
                   THEN confidence END) AS explicit_avg,
          COUNT(*)  FILTER (WHERE inference_method LIKE 'explicit_%') AS explicit_n,
          AVG(CASE WHEN inference_method = 'implicit_scoring'
                   THEN confidence END) AS implicit_avg,
          COUNT(*)  FILTER (WHERE inference_method = 'implicit_scoring') AS implicit_n
        FROM org_reporting_edges
        WHERE is_current = TRUE
        """
    )
    if not row or (row.get("explicit_n") or 0) < 5:
        pytest.skip("fewer than 5 explicit edges; ordering not yet meaningful")
    if (row.get("implicit_n") or 0) == 0:
        pytest.skip("no implicit edges to compare against")

    explicit_avg = float(row["explicit_avg"])
    implicit_avg = float(row["implicit_avg"])
    assert explicit_avg > implicit_avg, (
        f"Decision 3 violated: explicit avg confidence {explicit_avg:.3f} "
        f"≤ implicit avg {implicit_avg:.3f}"
    )


async def test_dominant_signal_keyspace_compliance(fetch_all):
    """Every distinct dominant_signal value must be in the 8-element keyspace."""
    rows = await fetch_all(
        """
        SELECT DISTINCT dominant_signal
          FROM org_reporting_edges
         WHERE dominant_signal IS NOT NULL
        """
    )
    seen = {r["dominant_signal"] for r in rows}
    out_of_band = seen - _DOMINANT_SIGNAL_KEYSPACE
    assert not out_of_band, (
        f"Found dominant_signal values outside keyspace: {sorted(out_of_band)}. "
        f"Allowed: {sorted(_DOMINANT_SIGNAL_KEYSPACE)}"
    )
