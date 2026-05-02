"""Data-quality assertions: IC-track parallel-ladder rule (CLAUDE.md L211, Decision 2).

ICs (Distinguished Engineer, Principal Engineer, etc.) run a track parallel to
management. They must not appear as managers of non-IC personnel — those
edges violate the parallel-ladders invariant and indicate an IC-track
detection or hierarchy-assignment bug.
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.data_quality


async def test_no_non_ic_reports_to_ic_managers(fetch_all):
    """Edges where manager.is_ic_track=TRUE AND report.is_ic_track=FALSE must be 0."""
    rows = await fetch_all(
        """
        SELECT e.manager_id, e.report_id
          FROM org_reporting_edges e
          JOIN org_cluster_members ocm_mgr
            ON ocm_mgr.person_id = e.manager_id
          JOIN org_cluster_members ocm_rep
            ON ocm_rep.person_id = e.report_id
         WHERE e.is_current = TRUE
           AND ocm_mgr.is_ic_track = TRUE
           AND ocm_rep.is_ic_track = FALSE
         LIMIT 50
        """
    )
    assert rows == [], (
        f"Found {len(rows)} non-IC report(s) with IC manager. "
        f"First 5: {[(str(r['manager_id']), str(r['report_id'])) for r in rows[:5]]}"
    )


async def test_distinguished_engineers_have_no_management_reports(fetch_all):
    """Looser title-regex check: catches misclassifications that bypass is_ic_track.

    For each "Distinguished Engineer", confirm they don't manage anyone with
    a Manager / Director / VP title. This catches cases where the IC-track
    regex didn't fire on the manager but the title is unambiguously IC.
    """
    rows = await fetch_all(
        """
        SELECT e.manager_id,
               m.current_title  AS manager_title,
               e.report_id,
               r.current_title  AS report_title
          FROM org_reporting_edges e
          JOIN persons m ON m.id = e.manager_id
          JOIN persons r ON r.id = e.report_id
         WHERE e.is_current = TRUE
           AND m.current_title ILIKE '%distinguished engineer%'
           AND (
                 r.current_title ILIKE '%manager%'
              OR r.current_title ILIKE '%director%'
              OR r.current_title ~* '\\m(vp|svp|evp)\\M'
           )
         LIMIT 50
        """
    )
    assert rows == [], (
        f"Found {len(rows)} Distinguished Engineer(s) managing M/D/VP-titled reports. "
        f"First 5: {[(r['manager_title'], '→', r['report_title']) for r in rows[:5]]}"
    )
