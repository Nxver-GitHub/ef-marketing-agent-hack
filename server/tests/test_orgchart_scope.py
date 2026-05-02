"""Tests for `credence.orgchart.scope` — Plan A Stage 1.3 (Task A3)."""
from __future__ import annotations

from uuid import UUID

import pytest

from credence.orgchart.scope import (
    PersonRollup,
    _build_scope_plan,
    _budget_level_from_seniority,
    _estimate_one_scope,
    _subtree_size,
    ManagerNode,
)


ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")


def _pid(n: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-cccc{n:08d}")


# ── _budget_level_from_seniority ─────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("seniority", "expected"),
    [
        (None, None),
        (35, "individual"),  # engineer
        (50, "individual"),  # manager tier (< 55)
        (55, "team"),        # director tier
        (62, "team"),        # senior director
        (70, "department"),  # VP
        (80, "division"),    # SVP
        (90, "company"),     # CTO/CFO
        (100, "company"),    # CEO
    ],
)
def test_budget_level_from_seniority(seniority: int | None, expected: str | None) -> None:
    assert _budget_level_from_seniority(seniority) == expected


# ── _subtree_size ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_subtree_size_simple_chain() -> None:
    """CEO → SVP → VP → Director → Manager. CEO sees 4 subtree members."""
    nodes = {
        _pid(i): ManagerNode(_pid(i), direct_reports=([_pid(i + 1)] if i < 5 else []))
        for i in range(1, 6)
    }
    assert _subtree_size(_pid(1), nodes) == 4
    assert _subtree_size(_pid(3), nodes) == 2  # VP sees Director + Manager
    assert _subtree_size(_pid(5), nodes) == 0  # leaf


@pytest.mark.unit
def test_subtree_size_breadth() -> None:
    """One CEO with 3 direct reports, each with 2 reports. CEO subtree = 9."""
    leaves = [_pid(100 + i) for i in range(6)]
    middles = [_pid(10 + i) for i in range(3)]
    ceo = _pid(1)

    nodes = {ceo: ManagerNode(ceo, direct_reports=middles)}
    for i, m in enumerate(middles):
        nodes[m] = ManagerNode(m, direct_reports=leaves[2 * i : 2 * i + 2])
    for leaf in leaves:
        nodes[leaf] = ManagerNode(leaf, direct_reports=[])

    assert _subtree_size(ceo, nodes) == 9
    for m in middles:
        assert _subtree_size(m, nodes) == 2


@pytest.mark.unit
def test_subtree_size_caps_at_max_depth() -> None:
    """A pathological deep chain stops at max_depth (no infinite recursion)."""
    chain = [_pid(i) for i in range(20)]
    nodes = {
        chain[i]: ManagerNode(chain[i], direct_reports=([chain[i + 1]] if i < 19 else []))
        for i in range(20)
    }
    # max_depth=5 means we count at most 5 levels of descendants from root.
    # Each level adds 1 person (linear chain), so subtree from root = 5.
    assert _subtree_size(chain[0], nodes, max_depth=5) == 5


# ── _estimate_one_scope ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_estimate_one_scope_full_path() -> None:
    person = PersonRollup(
        person_id=_pid(1),
        account_id=ACCOUNT,
        seniority=70,  # VP tier
        cluster_domains=["hardware_engineering", "hardware_engineering"],  # dedup
        cluster_sub_domains=["verification", "verification", "rtl"],
    )
    est = _estimate_one_scope(person, direct_report_count=5, subtree_count=18)

    assert est.person_id == _pid(1)
    assert est.account_id == ACCOUNT
    assert est.owns_functions == ["hardware_engineering"]
    assert est.owns_technologies == ["rtl", "verification"]  # sorted
    assert est.team_size_min == 5
    assert est.team_size_max == 18
    assert est.budget_authority_level == "department"


@pytest.mark.unit
def test_estimate_one_scope_non_manager_has_null_team_size() -> None:
    """A person with no reports gets None team-size, not 0."""
    person = PersonRollup(
        person_id=_pid(1),
        account_id=ACCOUNT,
        seniority=35,
        cluster_domains=["software_engineering"],
    )
    est = _estimate_one_scope(person, direct_report_count=0, subtree_count=0)
    assert est.team_size_min is None
    assert est.team_size_max is None


@pytest.mark.unit
def test_estimate_one_scope_subtree_floor_protects_check_constraint() -> None:
    """Stale graph: subtree count < direct count. Result must satisfy
    team_size_min ≤ team_size_max CHECK on the DB side."""
    person = PersonRollup(
        person_id=_pid(1),
        account_id=ACCOUNT,
        seniority=60,
        cluster_domains=["software_engineering"],
    )
    est = _estimate_one_scope(person, direct_report_count=5, subtree_count=2)
    assert est.team_size_min is not None and est.team_size_max is not None
    assert est.team_size_min <= est.team_size_max


# ── _build_scope_plan ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_build_scope_plan_full_org() -> None:
    """1 VP managing 2 Senior Engineers; the VP's scope = 2 direct, 2 subtree."""
    vp = PersonRollup(
        person_id=_pid(1),
        account_id=ACCOUNT,
        seniority=70,
        cluster_domains=["hardware_engineering"],
    )
    se1 = PersonRollup(
        person_id=_pid(2),
        account_id=ACCOUNT,
        seniority=60,
        cluster_domains=["hardware_engineering"],
    )
    se2 = PersonRollup(
        person_id=_pid(3),
        account_id=ACCOUNT,
        seniority=60,
        cluster_domains=["hardware_engineering"],
    )
    edges = [(_pid(1), _pid(2)), (_pid(1), _pid(3))]

    plan = _build_scope_plan([vp, se1, se2], edges)

    assert len(plan) == 3
    vp_scope = plan[_pid(1)]
    assert vp_scope.team_size_min == 2
    assert vp_scope.team_size_max == 2
    assert vp_scope.budget_authority_level == "department"

    # Senior engineers are leaves
    assert plan[_pid(2)].team_size_min is None
    assert plan[_pid(3)].team_size_min is None


@pytest.mark.unit
def test_build_scope_plan_multi_cluster_owner() -> None:
    """A person in 2 clusters (e.g., a VP overseeing both hardware + software)
    gets both domains in owns_functions."""
    vp = PersonRollup(
        person_id=_pid(1),
        account_id=ACCOUNT,
        seniority=80,  # SVP — owns "division"
        cluster_domains=["hardware_engineering", "software_engineering"],
        cluster_sub_domains=["compiler"],
    )
    plan = _build_scope_plan([vp], edges=[])
    assert plan[_pid(1)].owns_functions == ["hardware_engineering", "software_engineering"]
    assert plan[_pid(1)].owns_technologies == ["compiler"]
    assert plan[_pid(1)].budget_authority_level == "division"


@pytest.mark.unit
def test_build_scope_plan_skips_unknown_manager() -> None:
    """An edge whose manager_id is not in the persons set is silently skipped."""
    se = PersonRollup(
        person_id=_pid(2),
        account_id=ACCOUNT,
        seniority=60,
        cluster_domains=["hardware_engineering"],
    )
    # Manager _pid(99) isn't in persons — should not crash
    plan = _build_scope_plan([se], edges=[(_pid(99), _pid(2))])
    assert _pid(2) in plan
    # SE is leaf; subtree is 0.
    assert plan[_pid(2)].team_size_min is None
