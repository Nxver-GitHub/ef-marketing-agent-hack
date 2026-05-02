"""Scoring math tests for org-chart implicit-edge confidence.

Pins exact numeric outputs for the 7 components and their combination,
so any drift in V3_PT2.md L102-115 verbatim weights surfaces immediately.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from credence.orgchart.hierarchy import (
    COMPONENT_KEYS,
    IMPLICIT_SCORE_CAP,
    ClusterMember,
    _patent_cluster_score,
    _score_pair,
    _seniority_gap_score,
)


def _member(
    title: str,
    seniority: int,
    *,
    is_ic: bool = False,
    sub_domain: str | None = None,
    person_id: UUID | None = None,
) -> ClusterMember:
    """Build a ClusterMember with sensible defaults for scoring tests."""
    return ClusterMember(
        person_id=person_id or uuid4(),
        account_id=UUID("00000000-0000-0000-0000-000000000001"),
        title=title,
        seniority=seniority,
        is_ic_track=is_ic,
        sub_domain=sub_domain,
        inferred_team=None,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "manager_seniority,report_seniority,expected",
    [
        (70, 60, 0.30),  # gap 10, in [8,15]
        (60, 50, 0.30),  # gap 10
        (60, 53, 0.18),  # gap 7, in [5,8)
        (90, 70, 0.12),  # gap 20, in (15,25]
        (70, 67, 0.0),   # gap 3, below 5
        (95, 65, 0.0),   # gap 30, above 25
        (60, 60, 0.0),   # peers
        (50, 70, 0.0),   # reverse gap
    ],
)
def test_seniority_gap_buckets_lock_v3pt2(manager_seniority, report_seniority, expected):
    """Guards V3_PT2.md L102-105 seniority-gap bucket table."""
    assert _seniority_gap_score(manager_seniority, report_seniority) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "shared,expected",
    [
        (0, 0.0),
        (1, 0.05),
        (2, 0.10),
        (3, 0.15),
        (5, 0.15),  # cap
        (-1, 0.0),  # nonsense input
    ],
)
def test_patent_cluster_score_caps_at_15_after_3_shared(shared, expected):
    """Guards V3_PT2.md L114 — patent cluster scales linearly to 3 shared
    then caps at +0.15."""
    assert _patent_cluster_score(shared) == pytest.approx(expected, rel=1e-6)


@pytest.mark.unit
def test_score_pair_full_components_sum_to_canonical_078():
    """Pins the canonical 'well-behaved manager-edge' total at 0.78.
    If this drifts, the UI confidence-band thresholds need re-evaluation."""
    manager = _member("VP Engineering", 70)
    report = _member("Senior Engineer", 60)
    score = _score_pair(
        manager,
        report,
        same_sub_domain=False,
        shared_patents=0,
        geographic_compatible=True,
        manager_has_capacity=True,
    )
    # 0.30 (gap) + 0.25 (domain) + 0.10 (manager_title) + 0.05 (span) + 0.08 (geo) = 0.78
    assert score.total == pytest.approx(0.78, abs=1e-9), (
        f"Canonical edge score drifted to {score.total}; expected 0.78. "
        f"Components: {score.components}"
    )


@pytest.mark.unit
def test_score_pair_clamps_at_implicit_score_cap():
    """Guards: raw component sum >0.95 must clamp to IMPLICIT_SCORE_CAP."""
    manager = _member("Director", 60)
    report = _member("Senior Engineer", 50)
    score = _score_pair(
        manager,
        report,
        same_sub_domain=True,
        shared_patents=3,
        geographic_compatible=True,
        manager_has_capacity=True,
    )
    # Raw: 0.30+0.25+0.15+0.10+0.05+0.15+0.08 = 1.08 -> clamp to 0.95
    assert score.total == IMPLICIT_SCORE_CAP, (
        f"Expected clamp to {IMPLICIT_SCORE_CAP}; got {score.total}"
    )


@pytest.mark.unit
def test_score_pair_ic_mismatch_zeros():
    """Guards Decision 2 / _ic_track_compatible: an IC manager over a
    non-IC report must score 0 regardless of other signals."""
    manager = _member("Distinguished Engineer", 70, is_ic=True)
    report = _member("Senior Manager", 60, is_ic=False)
    score = _score_pair(
        manager,
        report,
        same_sub_domain=True,
        shared_patents=3,
        geographic_compatible=True,
        manager_has_capacity=True,
    )
    assert score.total == 0.0, f"IC mismatch must zero out; got {score.total}"


@pytest.mark.unit
def test_score_pair_self_pair_zeros():
    """Guards: a person cannot manage themselves."""
    pid = uuid4()
    person = _member("VP Engineering", 70, person_id=pid)
    score = _score_pair(person, person, same_sub_domain=False)
    assert score.total == 0.0, f"Self-pair must zero out; got {score.total}"


@pytest.mark.unit
def test_edge_score_components_sum_within_001_of_total():
    """Guards: per-component decomposition stays consistent with the
    clamped total within float-rounding + clamp tolerance (0.01)."""
    cases = [
        # (manager_title, manager_sen, report_title, report_sen, same_sub, shared)
        ("VP Engineering", 70, "Senior Engineer", 60, False, 0),
        ("Director", 60, "Senior Engineer", 50, False, 1),
        ("SVP", 80, "Director", 65, True, 0),
        ("VP Engineering", 70, "Senior Engineer", 60, True, 2),
        ("CEO", 100, "VP Engineering", 80, False, 0),
    ]
    for mt, ms, rt, rs, same_sub, shared in cases:
        manager = _member(mt, ms)
        report = _member(rt, rs)
        score = _score_pair(
            manager,
            report,
            same_sub_domain=same_sub,
            shared_patents=shared,
            geographic_compatible=True,
            manager_has_capacity=True,
        )
        component_sum = sum(score.components.values())
        # The total may be clamped down from component_sum; the test allows
        # both equality (no clamp) and clamped-down (component_sum > total).
        # We assert |min(component_sum, IMPLICIT_SCORE_CAP) - total| < 0.01.
        clamped_sum = min(component_sum, IMPLICIT_SCORE_CAP)
        assert abs(clamped_sum - score.total) < 0.01, (
            f"For {mt}->{rt}: clamped_sum={clamped_sum}, total={score.total}, "
            f"components={score.components}"
        )


@pytest.mark.unit
def test_dominant_component_in_keyspace():
    """Guards: dominant_component must always be a valid COMPONENT_KEYS
    member, even for zero-score canonical fallbacks."""
    cases = [
        # well-behaved
        (_member("VP Engineering", 70), _member("Senior Engineer", 60), False, 0),
        # IC mismatch -> zero edge
        (_member("Distinguished Engineer", 70, is_ic=True),
         _member("Manager", 55, is_ic=False), False, 0),
        # implausible gap -> zero edge
        (_member("Engineer", 35), _member("Senior Engineer", 40), False, 0),
        # full stack
        (_member("Director", 60), _member("Senior Engineer", 50), True, 3),
    ]
    for manager, report, same_sub, shared in cases:
        score = _score_pair(
            manager,
            report,
            same_sub_domain=same_sub,
            shared_patents=shared,
            geographic_compatible=True,
            manager_has_capacity=True,
        )
        assert score.dominant_component in COMPONENT_KEYS, (
            f"dominant_component {score.dominant_component!r} not in "
            f"COMPONENT_KEYS for pair {manager.title}->{report.title}"
        )
