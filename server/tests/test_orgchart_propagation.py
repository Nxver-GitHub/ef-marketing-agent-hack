"""Tests for `credence.orgchart.propagation` — Plan A Stage 1.5 (Task A8)."""
from __future__ import annotations

from uuid import UUID

import pytest

from credence.orgchart.propagation import (
    EdgeConfidence,
    _build_path_confidences,
)


def _pid(n: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-cccc{n:08d}")


def _edge(mgr: int, rep: int, conf: float) -> EdgeConfidence:
    return EdgeConfidence(manager_id=_pid(mgr), report_id=_pid(rep), confidence=conf)


# ── Pure path-confidence math ────────────────────────────────────────────────


@pytest.mark.unit
def test_single_root_chain_propagates_product() -> None:
    """CEO → SVP (0.95) → VP (0.78) → Director (0.85) → Manager (0.74).

    Expected:
      CEO       = 1.0
      SVP       = 0.95
      VP        = 0.741   (0.95 * 0.78)
      Director  = 0.630   (0.741 * 0.85)
      Manager   = 0.466   (0.630 * 0.74)
    """
    edges = [
        _edge(1, 2, 0.95),  # CEO → SVP
        _edge(2, 3, 0.78),  # SVP → VP
        _edge(3, 4, 0.85),  # VP → Director
        _edge(4, 5, 0.74),  # Director → Manager
    ]
    path_conf, cycles = _build_path_confidences(edges)

    assert cycles == set()
    assert path_conf[_pid(1)] == 1.0
    assert path_conf[_pid(2)] == pytest.approx(0.95)
    assert path_conf[_pid(3)] == pytest.approx(0.95 * 0.78)
    assert path_conf[_pid(4)] == pytest.approx(0.95 * 0.78 * 0.85)
    assert path_conf[_pid(5)] == pytest.approx(0.95 * 0.78 * 0.85 * 0.74)


@pytest.mark.unit
def test_single_root_branched_tree() -> None:
    """One root, two branches — each branch's leaves multiply only its own chain."""
    edges = [
        _edge(1, 2, 0.9),   # root → A
        _edge(1, 3, 0.8),   # root → B
        _edge(2, 4, 0.95),  # A → leaf1
        _edge(3, 5, 0.7),   # B → leaf2
    ]
    path_conf, _ = _build_path_confidences(edges)

    assert path_conf[_pid(1)] == 1.0
    assert path_conf[_pid(2)] == pytest.approx(0.9)
    assert path_conf[_pid(3)] == pytest.approx(0.8)
    assert path_conf[_pid(4)] == pytest.approx(0.9 * 0.95)
    assert path_conf[_pid(5)] == pytest.approx(0.8 * 0.7)


@pytest.mark.unit
def test_multiple_roots_independent_trees() -> None:
    """Two disjoint trees — each gets its own root with path_conf=1.0."""
    edges = [
        _edge(1, 2, 0.9),
        _edge(10, 11, 0.7),
    ]
    path_conf, _ = _build_path_confidences(edges)

    assert path_conf[_pid(1)] == 1.0
    assert path_conf[_pid(10)] == 1.0
    assert path_conf[_pid(2)] == pytest.approx(0.9)
    assert path_conf[_pid(11)] == pytest.approx(0.7)


# ── Cycle handling ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cycle_members_excluded_from_path_conf() -> None:
    """A → B → A is a cycle; both nodes should be skipped, no path_conf."""
    edges = [_edge(1, 2, 0.9), _edge(2, 1, 0.85)]
    path_conf, cycles = _build_path_confidences(edges)

    # Both 1 and 2 are in the cycle
    assert _pid(1) in cycles
    assert _pid(2) in cycles
    assert _pid(1) not in path_conf
    assert _pid(2) not in path_conf


@pytest.mark.unit
def test_cycle_does_not_block_independent_subtree() -> None:
    """A cycle in one component shouldn't prevent another component's path
    from being computed."""
    edges = [
        # Cycle: 1 ↔ 2
        _edge(1, 2, 0.9),
        _edge(2, 1, 0.85),
        # Healthy chain: 10 → 11
        _edge(10, 11, 0.75),
    ]
    path_conf, cycles = _build_path_confidences(edges)

    assert _pid(1) in cycles
    assert _pid(2) in cycles
    assert path_conf[_pid(10)] == 1.0
    assert path_conf[_pid(11)] == pytest.approx(0.75)


# ── Edge cases ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_empty_input_returns_empty_dict() -> None:
    path_conf, cycles = _build_path_confidences([])
    assert path_conf == {}
    assert cycles == set()


@pytest.mark.unit
def test_single_edge_two_nodes() -> None:
    """Smallest non-trivial input: one root, one leaf."""
    edges = [_edge(1, 2, 0.88)]
    path_conf, cycles = _build_path_confidences(edges)
    assert cycles == set()
    assert path_conf[_pid(1)] == 1.0
    assert path_conf[_pid(2)] == pytest.approx(0.88)


@pytest.mark.unit
def test_path_conf_capped_below_one() -> None:
    """Each propagation step multiplies by a value ≤ 1, so path_conf
    monotonically decreases. Locking the invariant."""
    edges = [
        _edge(1, 2, 0.9),
        _edge(2, 3, 0.85),
        _edge(3, 4, 0.95),
    ]
    path_conf, _ = _build_path_confidences(edges)
    # Walk down chain: each must be ≤ previous
    chain = [path_conf[_pid(i)] for i in (1, 2, 3, 4)]
    for prev, curr in zip(chain, chain[1:]):
        assert curr <= prev
