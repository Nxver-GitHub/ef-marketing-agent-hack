"""Performance budgets for the org-chart pipeline stages.

These tests fail when a stage runs slower than its documented budget. Use
``pytest -s`` to see the actual elapsed times printed to stdout. Budgets
assume the live DB; they're loose enough to absorb network jitter but tight
enough to catch a query-plan regression (e.g., a clustering JOIN that loses
its index).
"""
from __future__ import annotations

import time
from uuid import UUID

import pytest

# Mirrors conftest constants; duplicated to keep this module importable
# without depending on a `tests.orgchart` package path.
DEFAULT_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")
INTEL_COMPANY_ID = UUID("e6c126a6-5a70-4968-b37a-3648292e60ab")


pytestmark = pytest.mark.performance


async def test_intel_pipeline_completes_under_60_seconds():
    """``cluster_company(INTEL)`` ≤ 60s.

    Intel has ~150 cluster members. Clustering is the slowest stage,
    dominated by the persons-JOIN-employment_periods query. > 60s = either
    the DB slowed down or the query plan regressed (e.g., dropped index).
    """
    from credence.db import close_pool
    from credence.orgchart import clustering

    start = time.monotonic()
    try:
        await clustering.cluster_company(INTEL_COMPANY_ID)
    finally:
        await close_pool()
    elapsed = time.monotonic() - start

    print(f"\ncluster_company(Intel) elapsed: {elapsed:.2f}s")
    assert elapsed <= 60.0, (
        f"cluster_company(Intel) took {elapsed:.2f}s, budget 60s"
    )


async def test_validation_account_completes_under_30_seconds():
    """``validate_account`` ≤ 30s — reads only edges + cluster_members."""
    from credence.db import close_pool
    from credence.orgchart import validation

    start = time.monotonic()
    try:
        await validation.validate_account(DEFAULT_ACCOUNT_ID)
    finally:
        await close_pool()
    elapsed = time.monotonic() - start

    print(f"\nvalidate_account elapsed: {elapsed:.2f}s")
    assert elapsed <= 30.0, (
        f"validate_account took {elapsed:.2f}s, budget 30s"
    )


async def test_propagate_account_completes_under_30_seconds():
    """``propagate_account`` ≤ 30s — confidence propagation pass."""
    from credence.db import close_pool
    from credence.orgchart import propagation

    start = time.monotonic()
    try:
        await propagation.propagate_account(DEFAULT_ACCOUNT_ID)
    finally:
        await close_pool()
    elapsed = time.monotonic() - start

    print(f"\npropagate_account elapsed: {elapsed:.2f}s")
    assert elapsed <= 30.0, (
        f"propagate_account took {elapsed:.2f}s, budget 30s"
    )
