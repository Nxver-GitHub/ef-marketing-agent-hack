"""Tenant isolation invariants for the org-chart materialized graph.

The pipeline writes account_id manually on every row (clusters, members,
edges, scopes). RLS policies are the perimeter defense, but the
pipeline must independently never write a cross-tenant row even when
RLS is bypassed (e.g., service-role connections used by the backend).

Each test runs a SQL JOIN that detects mismatches and asserts the
result is empty. On failure, the offending row IDs are surfaced in the
assertion message so an operator can investigate immediately rather
than re-running diagnostics.

Date stamp: 2026-04-29.
"""
from __future__ import annotations

import pytest


@pytest.mark.integration
async def test_no_cross_tenant_edges(fetch_all) -> None:
    """Every current edge.account_id matches both manager and report.

    Detects:
    - Edge writes that hard-coded the wrong account_id.
    - Cross-tenant person resolution leaking into hierarchy inference.
    - Migrations that drop the account_id consistency check.
    """
    rows = await fetch_all(
        """
        SELECT
          e.id            AS edge_id,
          e.account_id    AS edge_account,
          mp.account_id   AS manager_account,
          rp.account_id   AS report_account
        FROM org_reporting_edges e
        JOIN persons mp ON mp.id = e.manager_id
        JOIN persons rp ON rp.id = e.report_id
        WHERE e.is_current = TRUE
          AND (
              e.account_id <> mp.account_id
              OR e.account_id <> rp.account_id
          )
        LIMIT 25
        """,
    )
    assert not rows, (
        "Cross-tenant org_reporting_edges detected. "
        f"Sample edge_ids: {[str(r['edge_id']) for r in rows[:10]]}"
    )


@pytest.mark.integration
async def test_no_cross_tenant_cluster_members(fetch_all) -> None:
    """Every cluster_member.account_id matches its cluster's account_id.

    Detects: clustering writes that misattributed account_id when the
    employment_periods source row carried a different tenant.
    """
    rows = await fetch_all(
        """
        SELECT
          ocm.id          AS member_row_id,
          ocm.account_id  AS member_account,
          ofc.account_id  AS cluster_account
        FROM org_cluster_members ocm
        JOIN org_functional_clusters ofc ON ofc.id = ocm.cluster_id
        WHERE ocm.account_id <> ofc.account_id
        LIMIT 25
        """,
    )
    assert not rows, (
        "Cross-tenant org_cluster_members detected. "
        f"Sample row_ids: {[str(r['member_row_id']) for r in rows[:10]]}"
    )


@pytest.mark.integration
async def test_no_cross_tenant_scope_rows(fetch_all) -> None:
    """Every person_scope_estimates.account_id matches the person's account_id.

    Detects: scope estimation writing rows for a person under the wrong
    tenant (e.g., querying without the account_id filter and inheriting
    a stale variable).
    """
    rows = await fetch_all(
        """
        SELECT
          pse.id          AS scope_row_id,
          pse.account_id  AS scope_account,
          p.account_id    AS person_account
        FROM person_scope_estimates pse
        JOIN persons p ON p.id = pse.person_id
        WHERE pse.account_id <> p.account_id
        LIMIT 25
        """,
    )
    assert not rows, (
        "Cross-tenant person_scope_estimates detected. "
        f"Sample row_ids: {[str(r['scope_row_id']) for r in rows[:10]]}"
    )
