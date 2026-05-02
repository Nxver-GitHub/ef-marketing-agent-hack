"""Data-quality assertions: coverage floors that catch silent regression.

These bounds were measured 2026-05-01 against the live default tenant:
  * 157 companies clustered (assert ≥ 100)
  * ~7,094 persons with a manager (assert ≥ 5,000)
  * ~20,038 persons total (assert ≥ 15,000)

The floors are bounded generously to absorb churn — if any of these trips,
something has wiped or stopped writing data.
"""
from __future__ import annotations

import statistics

import pytest


pytestmark = pytest.mark.data_quality


async def test_minimum_companies_clustered(fetch_one):
    """≥ 100 distinct companies have at least one functional cluster."""
    row = await fetch_one(
        "SELECT COUNT(DISTINCT company_id) AS n FROM org_functional_clusters"
    )
    n = int(row["n"])
    assert n >= 100, (
        f"Only {n} companies clustered; baseline (2026-05-01) was 157, floor 100. "
        f"A drop suggests pipeline halt or data wipe."
    )


async def test_minimum_persons_with_manager_assigned(fetch_one):
    """≥ 5,000 distinct persons have a current manager edge."""
    row = await fetch_one(
        """
        SELECT COUNT(DISTINCT report_id) AS n
          FROM org_reporting_edges
         WHERE is_current = TRUE
        """
    )
    n = int(row["n"])
    assert n >= 5000, (
        f"Only {n} persons with manager assigned; baseline ~7,094, floor 5,000."
    )


async def test_persons_table_minimum_coverage(fetch_one):
    """≥ 15,000 persons exist. Catches accidental table wipes."""
    row = await fetch_one("SELECT COUNT(*) AS n FROM persons")
    n = int(row["n"])
    assert n >= 15000, (
        f"persons table has only {n} rows; baseline ~20,038, floor 15,000."
    )


async def test_minimum_clusters_per_eligible_company(fetch_all):
    """Median company has ≥ 2 functional clusters (engineering + something else)."""
    rows = await fetch_all(
        """
        SELECT company_id, COUNT(*) AS n
          FROM org_functional_clusters
         GROUP BY company_id
        """
    )
    if not rows:
        pytest.skip("no clusters present — coverage regression caught upstream")
    counts = sorted(int(r["n"]) for r in rows)
    median = statistics.median(counts)
    assert median >= 2, (
        f"Median clusters/company is {median} (< 2). "
        f"Healthy companies should have engineering + at least one other domain. "
        f"Distribution head: {counts[:10]}"
    )


async def test_no_orphan_score_rows(fetch_one):
    """Every person_scope_estimates row must reference an existing persons row."""
    row = await fetch_one(
        """
        SELECT COUNT(*) AS n
          FROM person_scope_estimates pse
          LEFT JOIN persons p ON p.id = pse.person_id
         WHERE p.id IS NULL
        """
    )
    n = int(row.get("n", 0))
    assert n == 0, (
        f"Found {n} orphan rows in person_scope_estimates "
        f"(person_id with no matching persons row)."
    )
