"""Tree-shape invariants for the org-chart hierarchy planner.

Tests the pure-logic _build_cluster_hierarchy and _build_path_confidences
functions for cycle freedom, single-manager-per-report, span-cap respect,
IC-track segregation, determinism, and propagation monotonicity.
"""
from __future__ import annotations

import random
from uuid import UUID, uuid4

import pytest

from credence.orgchart.hierarchy import (
    SPAN_LIMITS,
    ClusterMember,
    HierarchyEdge,
    _build_cluster_hierarchy,
)
from credence.orgchart.propagation import EdgeConfidence, _build_path_confidences
from credence.taxonomy import seniority_tier


_ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")


def _make_member(
    title: str,
    seniority: int,
    *,
    is_ic: bool = False,
    person_id: UUID | None = None,
) -> ClusterMember:
    return ClusterMember(
        person_id=person_id or uuid4(),
        account_id=_ACCOUNT,
        title=title,
        seniority=seniority,
        is_ic_track=is_ic,
        sub_domain=None,
        inferred_team=None,
    )


def _has_cycle(edges: list[HierarchyEdge]) -> bool:
    """DFS-based cycle check on the directed manager->report graph."""
    children: dict[UUID, list[UUID]] = {}
    for e in edges:
        children.setdefault(e.manager_id, []).append(e.report_id)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[UUID, int] = {}
    nodes = set(children.keys()) | {r for kids in children.values() for r in kids}
    for n in nodes:
        color[n] = WHITE

    def dfs(u: UUID) -> bool:
        color[u] = GRAY
        for v in children.get(u, []):
            if color.get(v, WHITE) == GRAY:
                return True
            if color.get(v, WHITE) == WHITE and dfs(v):
                return True
        color[u] = BLACK
        return False

    return any(color[n] == WHITE and dfs(n) for n in nodes)


@pytest.mark.unit
def test_no_cycles_under_any_input():
    """Guards: the planner's union-find cycle check never permits a cycle,
    regardless of input shape."""
    rng = random.Random(20260429)
    titles_by_band = {
        "vp": ("VP Engineering", 70),
        "director": ("Director", 60),
        "senior_eng": ("Senior Engineer", 50),
        "engineer": ("Engineer", 35),
    }
    for trial in range(15):
        size = rng.randint(3, 12)
        members: list[ClusterMember] = []
        for _ in range(size):
            band = rng.choice(list(titles_by_band.values()))
            members.append(_make_member(band[0], band[1] + rng.randint(-2, 2)))
        edges, _, _ = _build_cluster_hierarchy(members, min_confidence=0.45)
        assert not _has_cycle(edges), (
            f"Trial {trial}: cycle detected in edges {edges}"
        )


@pytest.mark.unit
def test_single_manager_per_report_invariant():
    """Guards: every report appears at most once on the report side."""
    members = [
        _make_member("VP Engineering", 70),
        _make_member("Director", 60),
        _make_member("Senior Engineer", 50),
        _make_member("Senior Engineer", 50),
        _make_member("Senior Engineer", 50),
    ]
    edges, _, _ = _build_cluster_hierarchy(members, min_confidence=0.45)
    seen_reports: set[UUID] = set()
    for e in edges:
        assert e.report_id not in seen_reports, (
            f"report_id {e.report_id} has multiple managers"
        )
        seen_reports.add(e.report_id)


@pytest.mark.unit
def test_span_caps_respected():
    """Guards: SPAN_LIMITS bounds the count of direct reports per manager.
    Director (seniority 60) cap = 10."""
    director = _make_member("Director", 60)
    members = [director]
    # 15 ICs at seniority 50 — gap 10 -> +0.30 gap, well above min 0.45 total.
    for _ in range(15):
        members.append(_make_member("Senior Engineer", 50))
    edges, _, span_violations = _build_cluster_hierarchy(
        members, min_confidence=0.45
    )
    director_edges = [e for e in edges if e.manager_id == director.person_id]
    director_cap = SPAN_LIMITS[seniority_tier(director.seniority)]
    assert len(director_edges) <= director_cap, (
        f"Director got {len(director_edges)} reports; cap = {director_cap}"
    )
    # The cap is 10; we fed 15 reports, so we expect exactly 10.
    assert len(director_edges) == director_cap, (
        f"Expected director to be saturated at {director_cap}; got {len(director_edges)}"
    )


@pytest.mark.unit
def test_ic_track_never_manages_non_ic():
    """Guards Decision 2 parallel-ladder: IC-track person never has a
    non-IC direct report."""
    dist_eng = _make_member("Distinguished Engineer", 70, is_ic=True)
    director = _make_member("Director", 60, is_ic=False)
    eng1 = _make_member("Senior Engineer", 50, is_ic=False)
    eng2 = _make_member("Senior Engineer", 50, is_ic=False)
    eng3 = _make_member("Senior Engineer", 50, is_ic=False)
    members = [dist_eng, director, eng1, eng2, eng3]
    edges, _, _ = _build_cluster_hierarchy(members, min_confidence=0.45)
    non_ic_ids = {director.person_id, eng1.person_id, eng2.person_id, eng3.person_id}
    bad = [
        e for e in edges
        if e.manager_id == dist_eng.person_id and e.report_id in non_ic_ids
    ]
    assert not bad, (
        f"Distinguished Engineer assigned non-IC reports: {bad}"
    )


@pytest.mark.unit
def test_orphan_root_is_not_self_managed():
    """Guards: a single-person cluster produces no edges; the lone CEO does
    not appear as their own manager."""
    ceo = _make_member("CEO", 100)
    edges, skipped, span_violations = _build_cluster_hierarchy(
        [ceo], min_confidence=0.45
    )
    assert edges == [], f"Single-person cluster should produce no edges, got {edges}"
    for e in edges:
        assert e.manager_id != e.report_id, "Self-managed edge produced"


@pytest.mark.unit
def test_deterministic_output_under_repeated_invocation():
    """Guards: the planner's tie-break (manager_id, report_id ascending) is
    stable so the same input always produces the same output set."""
    members = [
        _make_member("VP Engineering", 70),
        _make_member("Director", 60),
        _make_member("Senior Engineer", 50),
        _make_member("Senior Engineer", 50),
        _make_member("Engineer", 35),
    ]
    runs = []
    for _ in range(3):
        edges, _, _ = _build_cluster_hierarchy(members, min_confidence=0.45)
        runs.append(sorted(
            (e.manager_id, e.report_id, round(e.confidence, 6)) for e in edges
        ))
    assert runs[0] == runs[1] == runs[2], (
        f"Non-deterministic output across runs:\n{runs[0]}\n{runs[1]}\n{runs[2]}"
    )


@pytest.mark.unit
def test_propagation_path_confidence_monotonic_down_chain():
    """Guards: path_confidence multiplies down the chain, never increases.
    Locks the cumulative-product invariant."""
    # 5-deep chain with strictly-decreasing seniority.
    ceo = uuid4()
    svp = uuid4()
    vp = uuid4()
    director = uuid4()
    manager = uuid4()
    edges = [
        EdgeConfidence(manager_id=ceo, report_id=svp, confidence=0.95),
        EdgeConfidence(manager_id=svp, report_id=vp, confidence=0.78),
        EdgeConfidence(manager_id=vp, report_id=director, confidence=0.85),
        EdgeConfidence(manager_id=director, report_id=manager, confidence=0.74),
    ]
    path_conf, cycles = _build_path_confidences(edges)
    assert not cycles, f"Unexpected cycle members: {cycles}"
    chain = [ceo, svp, vp, director, manager]
    confidences = [path_conf[n] for n in chain]
    # Root must be 1.0; each subsequent path_conf must be <= the parent's.
    assert confidences[0] == pytest.approx(1.0), f"Root path_conf = {confidences[0]}"
    for i in range(1, len(chain)):
        assert confidences[i] <= confidences[i - 1] + 1e-9, (
            f"Non-monotonic: chain[{i - 1}]={confidences[i - 1]} "
            f"-> chain[{i}]={confidences[i]}"
        )
    # Spot-check the multiplicative product at the leaf.
    expected_leaf = 1.0 * 0.95 * 0.78 * 0.85 * 0.74
    assert confidences[-1] == pytest.approx(expected_leaf, rel=1e-9), (
        f"Leaf path_conf = {confidences[-1]}; expected {expected_leaf}"
    )
