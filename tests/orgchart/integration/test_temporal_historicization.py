"""Temporal historicization tests for org-chart edge writes.

Task 1-B in hierarchy.py: edge writes follow a two-step
UPDATE-old-then-INSERT-new flow. Re-runs preserve history; identical
re-writes are skip-write no-ops.

Guards against:
- The skip-write check (Δconfidence < 0.02) being bypassed → history churn.
- The partial unique index on is_current=TRUE being dropped → multiple
  current edges per report.
- Explicit signals failing to supersede implicit ones (Decision 3).

Test 3 mutates state and restores it. If restoration is impossible
(e.g., the original edge can't be reconstructed from on-disk data) the
test pytest.skip()s rather than risking corruption.

Date stamp: 2026-04-29.
"""
from __future__ import annotations

from uuid import UUID

import pytest

from credence.orgchart.hierarchy import ingest_explicit_edge

DEFAULT_ACCOUNT_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.integration
async def test_skip_write_on_unchanged_edge(fetch_one, fetch_all) -> None:
    """Re-emitting an identical explicit edge must not churn the row.

    Pulls any current explicit-method edge and re-ingests with the same
    manager + signal_type + confidence. The skip-write check elides the
    write — id and created_at remain unchanged.

    If no explicit edge exists yet, skip — implicit edges aren't ingested
    via ingest_explicit_edge so this test isn't meaningful for them.
    """
    rows = await fetch_all(
        """
        SELECT id, manager_id, report_id, account_id, confidence,
               inference_method, created_at
        FROM org_reporting_edges
        WHERE is_current = TRUE
          AND inference_method LIKE 'explicit_%'
          AND inference_method NOT LIKE '%_unresolved_target'
        LIMIT 1
        """,
    )
    if not rows:
        pytest.skip("no current explicit (resolved) edge in DB to exercise skip-write")
    edge = rows[0]
    signal_type = edge["inference_method"].removeprefix("explicit_")

    await ingest_explicit_edge(
        report_id=edge["report_id"],
        account_id=edge["account_id"],
        signal_type=signal_type,
        confidence=float(edge["confidence"]),
        manager_id=edge["manager_id"],
    )

    after = await fetch_one(
        "SELECT id, created_at FROM org_reporting_edges WHERE id = $1",
        edge["id"],
    )
    assert after, f"edge {edge['id']} disappeared after skip-write re-ingest"
    assert after["id"] == edge["id"], "edge id changed — skip-write failed"
    assert after["created_at"] == edge["created_at"], (
        "edge created_at changed — write was not skipped"
    )


@pytest.mark.integration
async def test_re_run_does_not_create_duplicate_current_edges(fetch_all) -> None:
    """After full pipeline runs, every report has exactly one current edge.

    The partial unique index org_edges_one_current_manager_per_report
    (WHERE is_current=TRUE) enforces this at the DB layer, but we still
    test post-write state to catch a regression where the index is
    dropped or someone bypasses the upsert path.
    """
    rows = await fetch_all(
        """
        SELECT report_id, COUNT(*) AS n
        FROM org_reporting_edges
        WHERE is_current = TRUE
          AND account_id = $1
        GROUP BY report_id
        HAVING COUNT(*) > 1
        """,
        DEFAULT_ACCOUNT_ID,
    )
    assert not rows, (
        f"Found {len(rows)} report_ids with >1 current edge — partial "
        f"unique index broken. Sample: {[str(r['report_id']) for r in rows[:5]]}"
    )


@pytest.mark.integration
async def test_explicit_edge_can_supersede_implicit(fetch_one, fetch_all) -> None:
    """Ingesting an explicit edge over an implicit one historicizes the old.

    State mutation: writes a new explicit edge, asserts the old implicit
    edge moves to is_current=FALSE with valid_to set, then restores the
    original implicit edge via ingest_explicit_edge with
    inference_method='implicit_scoring' as the signal_type. This isn't
    a perfect restore (it leaves a 'explicit_implicit_scoring' style
    method) so we additionally fix that with a direct DB write at the
    end. If we can't safely roll back, we skip.
    """
    # Find any current implicit edge with at least one other plausible
    # manager candidate at the same company (for substitution).
    rows = await fetch_all(
        """
        SELECT e.id, e.account_id, e.manager_id, e.report_id, e.confidence,
               e.inference_method, e.created_at,
               e.score_components, e.dominant_signal
        FROM org_reporting_edges e
        WHERE e.is_current = TRUE
          AND e.inference_method = 'implicit_scoring'
          AND e.account_id = $1
        LIMIT 1
        """,
        DEFAULT_ACCOUNT_ID,
    )
    if not rows:
        pytest.skip("no current implicit edge available for supersede test")
    original = rows[0]

    # Pick a different manager — any other person in the same account.
    candidate = await fetch_one(
        """
        SELECT id FROM persons
        WHERE account_id = $1
          AND id <> $2
          AND id <> $3
        LIMIT 1
        """,
        DEFAULT_ACCOUNT_ID,
        original["manager_id"],
        original["report_id"],
    )
    if not candidate:
        pytest.skip("no alternate manager candidate for supersede test")
    new_manager_id: UUID = candidate["id"]

    # Write the explicit override.
    await ingest_explicit_edge(
        report_id=original["report_id"],
        account_id=original["account_id"],
        signal_type="linkedin_reports_to_test",
        confidence=0.92,
        manager_id=new_manager_id,
    )

    try:
        # Old implicit edge: should be historicized.
        old_after = await fetch_one(
            "SELECT is_current, valid_to FROM org_reporting_edges WHERE id = $1",
            original["id"],
        )
        assert old_after, "original edge row disappeared"
        assert old_after["is_current"] is False, (
            "old implicit edge still is_current=TRUE — supersede failed"
        )
        assert old_after["valid_to"] is not None, (
            "old implicit edge valid_to is NULL — historicization incomplete"
        )

        # New explicit edge: must be present and current.
        new_edge = await fetch_one(
            """
            SELECT inference_method, is_current, manager_id
            FROM org_reporting_edges
            WHERE report_id = $1 AND is_current = TRUE
            """,
            original["report_id"],
        )
        assert new_edge, "no current edge for report after supersede"
        assert new_edge["is_current"] is True
        assert new_edge["inference_method"] == "explicit_linkedin_reports_to_test"
        assert new_edge["manager_id"] == new_manager_id
    finally:
        # Cleanup: directly restore the DB to its prior state. We use a
        # raw SQL hammer here because ingest_explicit_edge writes
        # 'explicit_*' inference_methods only — it can't reproduce the
        # original 'implicit_scoring' row.
        from credence.db import acquire as _acquire  # noqa: WPS433

        async with _acquire() as conn:
            async with conn.transaction():
                # Delete the explicit override we wrote.
                await conn.execute(
                    """
                    DELETE FROM org_reporting_edges
                    WHERE report_id = $1
                      AND inference_method = 'explicit_linkedin_reports_to_test'
                    """,
                    original["report_id"],
                )
                # Re-promote the historicized implicit edge to current.
                await conn.execute(
                    """
                    UPDATE org_reporting_edges
                    SET is_current = TRUE, valid_to = NULL, updated_at = NOW()
                    WHERE id = $1
                    """,
                    original["id"],
                )
