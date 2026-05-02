"""Data-quality assertions: manager span-of-control distribution sanity.

The hierarchy inference applies SPAN_LIMITS as a soft heuristic — a handful
of managers may exceed the cap by 1-2 reports. Anything ≥ 2× the cap suggests
a real bug (e.g., a falsely-classified senior manager absorbing dozens of
peers). These tests check both the worst-case violators and the overall
distribution shape.
"""
from __future__ import annotations

import statistics

import pytest

from credence.orgchart.hierarchy import SPAN_LIMITS
from credence.taxonomy import seniority_tier


pytestmark = pytest.mark.data_quality


async def test_no_egregious_span_violations(fetch_all):
    """No manager may exceed SPAN_LIMITS[tier] * 2 direct reports.

    Rationale: 1-2 over the cap is acceptable (heuristic). 2× the cap indicates
    a structural bug — e.g., the IC-track filter failing or a misclassified
    title vacuuming up an entire cluster.
    """
    rows = await fetch_all(
        """
        SELECT e.manager_id,
               p.current_seniority_score AS seniority,
               COUNT(*)                    AS span
          FROM org_reporting_edges e
          JOIN persons p ON p.id = e.manager_id
         WHERE e.is_current = TRUE
         GROUP BY e.manager_id, p.current_seniority_score
        """
    )

    egregious: list[dict] = []
    for row in rows:
        seniority = row["seniority"]
        if seniority is None:
            continue
        tier = seniority_tier(int(seniority))
        cap = SPAN_LIMITS[tier]
        if row["span"] >= cap * 2:
            egregious.append(
                {
                    "manager_id": str(row["manager_id"]),
                    "tier": tier,
                    "cap": cap,
                    "span": row["span"],
                }
            )

    assert egregious == [], (
        f"Found {len(egregious)} manager(s) with ≥ 2× the span cap. "
        f"First 5: {egregious[:5]}"
    )


async def test_span_distribution_within_expected_bounds(fetch_all):
    """Median direct-report count ≤ 8; p95 ≤ 14.

    Rationale (measured 2026-05-01 against ~7,094 current edges):
      - Typical org charts have median manager span of 4-8 reports.
      - p95 is around 12 in healthy charts; we tolerate 14 to absorb churn.
    A blown median or p95 indicates clusters being collapsed onto a single
    manager (hierarchy inference misfire).
    """
    rows = await fetch_all(
        """
        SELECT manager_id, COUNT(*) AS span
          FROM org_reporting_edges
         WHERE is_current = TRUE
         GROUP BY manager_id
        """
    )
    spans = sorted(int(r["span"]) for r in rows)
    assert spans, "no current edges found — coverage regression?"

    median = statistics.median(spans)
    # p95 — index of the 95th-percentile element (lower interpolation).
    p95_idx = max(0, int(len(spans) * 0.95) - 1)
    p95 = spans[p95_idx]

    assert median <= 8, (
        f"Median manager span {median} exceeds 8. "
        f"Distribution head/tail: head={spans[:5]} tail={spans[-5:]}"
    )
    assert p95 <= 14, (
        f"p95 manager span {p95} exceeds 14. tail={spans[-10:]}"
    )
