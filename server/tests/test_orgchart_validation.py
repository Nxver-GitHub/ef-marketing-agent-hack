"""Tests for `credence.orgchart.validation` — Plan A Stage 1.4 (Task A7)."""
from __future__ import annotations

from uuid import UUID

import pytest

from credence.orgchart.validation import (
    _PersonInfo,
    _build_validation_report,
    _check_cycles,
    _check_ic_misclassifications,
    _check_span_violations,
)


def _pid(n: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-cccc{n:08d}")


def _person(*, pid: int, seniority: int | None = 60, is_ic: bool = False) -> _PersonInfo:
    return _PersonInfo(person_id=_pid(pid), seniority=seniority, is_ic_track=is_ic)


# ── Span violations ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_span_violation_director_with_11_reports() -> None:
    """Director cap is 10. Build 11 edges to one director → one violation."""
    director = _person(pid=1, seniority=60)  # director tier
    persons = {director.person_id: director}
    edges = [(director.person_id, _pid(10 + i)) for i in range(11)]

    violations = _check_span_violations(edges, persons)
    assert len(violations) == 1
    v = violations[0]
    assert v.manager_id == director.person_id
    assert v.seniority_tier == "director"
    assert v.direct_report_count == 11
    assert v.span_cap == 10


@pytest.mark.unit
def test_span_violation_under_cap_passes() -> None:
    vp = _person(pid=1, seniority=70)  # vp cap 8
    persons = {vp.person_id: vp}
    edges = [(vp.person_id, _pid(10 + i)) for i in range(7)]
    assert _check_span_violations(edges, persons) == []


@pytest.mark.unit
def test_span_violation_unknown_seniority_skipped() -> None:
    """Manager without seniority can't be tier-checked; skip rather than guess."""
    mystery = _person(pid=1, seniority=None)
    persons = {mystery.person_id: mystery}
    edges = [(mystery.person_id, _pid(10 + i)) for i in range(50)]
    assert _check_span_violations(edges, persons) == []


# ── Cycle detection ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cycle_detection_simple_loop() -> None:
    """A → B → A is one cycle."""
    edges = [(_pid(1), _pid(2)), (_pid(2), _pid(1))]
    violations = _check_cycles(edges)
    assert len(violations) == 1
    cycle = violations[0].cycle
    # Canonical rotation puts the smallest UUID first.
    assert cycle[0] == _pid(1)
    assert cycle[-1] == cycle[0]  # closes on itself


@pytest.mark.unit
def test_cycle_detection_triangle() -> None:
    """A → B → C → A is one triangle cycle, not three."""
    edges = [(_pid(1), _pid(2)), (_pid(2), _pid(3)), (_pid(3), _pid(1))]
    violations = _check_cycles(edges)
    assert len(violations) == 1


@pytest.mark.unit
def test_cycle_detection_no_cycle() -> None:
    """Linear chain has no cycle."""
    edges = [(_pid(1), _pid(2)), (_pid(2), _pid(3)), (_pid(3), _pid(4))]
    assert _check_cycles(edges) == []


@pytest.mark.unit
def test_cycle_detection_multiple_distinct_cycles() -> None:
    """Two separate loops → 2 violations."""
    edges = [
        # Loop 1: 1 ↔ 2
        (_pid(1), _pid(2)),
        (_pid(2), _pid(1)),
        # Loop 2: 10 → 11 → 10
        (_pid(10), _pid(11)),
        (_pid(11), _pid(10)),
    ]
    violations = _check_cycles(edges)
    assert len(violations) == 2


# ── IC misclassification ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_ic_misclassification_non_ic_under_ic_manager() -> None:
    """A non-IC report whose manager is on the IC track → violation."""
    ic_manager = _person(pid=1, seniority=70, is_ic=True)
    non_ic_report = _person(pid=2, seniority=60, is_ic=False)
    persons = {p.person_id: p for p in [ic_manager, non_ic_report]}
    edges = [(ic_manager.person_id, non_ic_report.person_id)]

    violations = _check_ic_misclassifications(edges, persons)
    assert len(violations) == 1
    assert violations[0].manager_id == ic_manager.person_id
    assert violations[0].report_id == non_ic_report.person_id


@pytest.mark.unit
def test_ic_misclassification_ic_under_ic_is_ok() -> None:
    """IC report under IC manager is fine (parallel track stays parallel)."""
    ic_mgr = _person(pid=1, seniority=70, is_ic=True)
    ic_rep = _person(pid=2, seniority=60, is_ic=True)
    persons = {p.person_id: p for p in [ic_mgr, ic_rep]}
    edges = [(ic_mgr.person_id, ic_rep.person_id)]
    assert _check_ic_misclassifications(edges, persons) == []


@pytest.mark.unit
def test_ic_misclassification_non_ic_under_non_ic_is_ok() -> None:
    """Standard management chain — no violation."""
    mgr = _person(pid=1, seniority=70, is_ic=False)
    rep = _person(pid=2, seniority=60, is_ic=False)
    persons = {p.person_id: p for p in [mgr, rep]}
    edges = [(mgr.person_id, rep.person_id)]
    assert _check_ic_misclassifications(edges, persons) == []


# ── Aggregate report ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_validation_report_aggregates_all_three_checks() -> None:
    director = _person(pid=1, seniority=60)
    ic_mgr = _person(pid=2, seniority=70, is_ic=True)
    non_ic_rep = _person(pid=3, seniority=60, is_ic=False)
    cycle_a = _person(pid=10, seniority=60)
    cycle_b = _person(pid=11, seniority=60)

    persons = {p.person_id: p for p in [director, ic_mgr, non_ic_rep, cycle_a, cycle_b]}
    edges = [
        # Span violation: 11 reports under director
        *[(director.person_id, _pid(100 + i)) for i in range(11)],
        # IC violation: ic_mgr → non_ic_rep
        (ic_mgr.person_id, non_ic_rep.person_id),
        # Cycle: cycle_a ↔ cycle_b
        (cycle_a.person_id, cycle_b.person_id),
        (cycle_b.person_id, cycle_a.person_id),
    ]

    report = _build_validation_report(edges, persons)
    assert not report.is_clean
    assert len(report.span_violations) == 1
    assert len(report.cycle_violations) == 1
    assert len(report.ic_violations) == 1
    assert report.total_violations == 3


@pytest.mark.unit
def test_clean_org_returns_clean_report() -> None:
    vp = _person(pid=1, seniority=70)
    eng = _person(pid=2, seniority=55)
    persons = {p.person_id: p for p in [vp, eng]}
    edges = [(vp.person_id, eng.person_id)]

    report = _build_validation_report(edges, persons)
    assert report.is_clean
    assert report.total_violations == 0
