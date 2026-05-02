"""Decision 3 enforcement: explicit signals always beat implicit scoring.

CLAUDE.md Decision 3: "any explicit signal returns immediately with its
own confidence score. The implicit scoring model only runs when no
explicit signal exists. Never combine explicit and implicit — explicit
wins."

Two regression vectors are guarded:

1. End-to-end: a hierarchy re-run must not overwrite a present explicit
   edge — the implicit pipeline filters them out before writing.

2. Internal: hierarchy._filter_against_explicit_edges drops candidate
   implicit edges whose report_id already has an explicit current edge.
   This is the unit-level guard the e2e relies on.

Test 1 mutates state (writes a test explicit edge and deletes it).
If no eligible "no current edge" prospect exists in the DB, we skip
to avoid corrupting an existing chart.

Date stamp: 2026-04-29.
"""
from __future__ import annotations

from uuid import UUID

import pytest

from credence.orgchart.hierarchy import (
    HierarchyEdge,
    _filter_against_explicit_edges,
    infer_company_hierarchy,
    ingest_explicit_edge,
)

DEFAULT_ACCOUNT_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.integration
async def test_explicit_edge_persists_after_implicit_re_run(
    fetch_one, fetch_all
) -> None:
    """Explicit edges survive implicit pipeline re-runs.

    Picks a person with no current edge, writes an explicit edge for them,
    runs implicit hierarchy inference for that person's company, and
    asserts the explicit edge is still the current one.
    """
    # Find a person with: a current company assignment, in default tenant,
    # who currently has NO current reporting edge at all.
    candidate = await fetch_one(
        """
        SELECT p.id AS person_id, p.current_company_id
        FROM persons p
        WHERE p.account_id = $1
          AND p.current_company_id IS NOT NULL
          AND p.is_unresolved_target = FALSE
          AND NOT EXISTS (
              SELECT 1 FROM org_reporting_edges e
              WHERE e.report_id = p.id AND e.is_current = TRUE
          )
        LIMIT 1
        """,
        DEFAULT_ACCOUNT_ID,
    )
    if not candidate:
        pytest.skip("no edge-less prospect available; cannot test override")

    report_id = candidate["person_id"]
    company_id = candidate["current_company_id"]

    # Find another person at the same company to be the manager.
    manager_row = await fetch_one(
        """
        SELECT id FROM persons
        WHERE account_id = $1
          AND current_company_id = $2
          AND id <> $3
          AND is_unresolved_target = FALSE
        LIMIT 1
        """,
        DEFAULT_ACCOUNT_ID,
        company_id,
        report_id,
    )
    if not manager_row:
        pytest.skip("no manager candidate at same company for override test")

    manager_id = manager_row["id"]

    await ingest_explicit_edge(
        report_id=report_id,
        account_id=DEFAULT_ACCOUNT_ID,
        signal_type="test_override",
        confidence=0.91,
        manager_id=manager_id,
    )

    try:
        # Re-run hierarchy inference for the company. This should NOT
        # overwrite our explicit edge.
        await infer_company_hierarchy(company_id)

        current_edges = await fetch_all(
            """
            SELECT inference_method, manager_id
            FROM org_reporting_edges
            WHERE report_id = $1 AND is_current = TRUE
            """,
            report_id,
        )
        assert len(current_edges) == 1, (
            f"expected exactly one current edge for {report_id}, "
            f"got {len(current_edges)}"
        )
        edge = current_edges[0]
        assert edge["inference_method"] == "explicit_test_override", (
            f"explicit edge was overwritten — current method is "
            f"{edge['inference_method']}"
        )
        assert edge["manager_id"] == manager_id, (
            "explicit edge manager_id was changed by implicit re-run"
        )
    finally:
        # Cleanup: drop the test edges (current + any historicized rows
        # we may have produced if the test failed mid-flight).
        from credence.db import acquire as _acquire  # noqa: WPS433

        async with _acquire() as conn:
            await conn.execute(
                """
                DELETE FROM org_reporting_edges
                WHERE report_id = $1
                  AND inference_method = 'explicit_test_override'
                """,
                report_id,
            )


@pytest.mark.integration
async def test_filter_against_explicit_edges_blocks_implicit_overwrite(
    fetch_one,
) -> None:
    """Direct unit-level test of _filter_against_explicit_edges against live data.

    Constructs a fake implicit HierarchyEdge for a report that already
    has an explicit current edge in the DB. The filter must drop it.

    Skips if no current explicit edge exists in the DB (filter has
    nothing real to compare against).
    """
    explicit = await fetch_one(
        """
        SELECT report_id, manager_id
        FROM org_reporting_edges
        WHERE is_current = TRUE
          AND inference_method LIKE 'explicit_%'
          AND account_id = $1
        LIMIT 1
        """,
        DEFAULT_ACCOUNT_ID,
    )
    if not explicit:
        pytest.skip("no explicit current edge in DB — filter has nothing to test")

    fake_implicit = HierarchyEdge(
        manager_id=explicit["manager_id"],
        report_id=explicit["report_id"],
        confidence=0.80,
        inference_method="implicit_scoring",
        score_components={
            "seniority_gap": 0.30,
            "domain_match": 0.25,
            "subdomain_match": 0.0,
            "manager_title": 0.10,
            "span_capacity": 0.05,
            "patent_cluster": 0.0,
            "geographic_scope": 0.08,
        },
        dominant_signal="seniority_gap",
    )

    filtered = await _filter_against_explicit_edges([fake_implicit])
    assert filtered == [], (
        "implicit edge for a report with an explicit current edge "
        "was not filtered out"
    )
