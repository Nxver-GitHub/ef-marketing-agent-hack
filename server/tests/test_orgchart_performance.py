"""Tests for `credence.orgchart.performance` — v3.1 Plan A5.

Coverage:
1. _compute_counts pure logic — success/error math, half-weight team_wrong
2. MethodPerformance accuracy + below_tally_threshold properties
3. compute_method_performance reads counts correctly via shim
4. compute_all_account_performance orchestrates per-method calls
5. Upsert called only when N >= MIN_CORRECTIONS_FOR_TALLY
6. Upsert skipped when upsert=False kwarg
7. Empty distinct-methods list → empty rollup, no writes
"""
from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import pytest

from credence.orgchart import performance as perf_mod
from credence.orgchart.performance import (
    EDGE_WRONG_TYPES,
    MIN_CORRECTIONS_FOR_TALLY,
    TEAM_WRONG_TYPES,
    MethodPerformance,
    _compute_counts,
    compute_all_account_performance,
    compute_method_performance,
)


ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")


# ─── 1. _compute_counts ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_compute_counts_no_corrections_all_success() -> None:
    s, e = _compute_counts(edge_count=100, edge_wrong_corrections=0, team_wrong_corrections=0)
    assert s == 100
    assert e == 0


@pytest.mark.unit
def test_compute_counts_edge_wrong_subtracts_full_weight() -> None:
    """Direct edge-wrong correction counts as full error."""
    s, e = _compute_counts(edge_count=100, edge_wrong_corrections=10, team_wrong_corrections=0)
    assert e == 10
    assert s == 90


@pytest.mark.unit
def test_compute_counts_team_wrong_half_weight() -> None:
    """team_wrong corrections count half (V3_PT2.md L227)."""
    # 4 team_wrong → 2 errors (integer floor)
    s, e = _compute_counts(edge_count=100, edge_wrong_corrections=0, team_wrong_corrections=4)
    assert e == 2
    assert s == 98
    # Odd team_wrong floors to (n-1)/2
    s, e = _compute_counts(edge_count=100, edge_wrong_corrections=0, team_wrong_corrections=5)
    assert e == 2  # floor(5/2)
    assert s == 98


@pytest.mark.unit
def test_compute_counts_combined_edge_and_team_wrong() -> None:
    """Both error types add up: edge_wrong full + team_wrong half."""
    s, e = _compute_counts(edge_count=100, edge_wrong_corrections=5, team_wrong_corrections=6)
    # 5 + 3 = 8 errors; 100 - 8 = 92 success
    assert e == 8
    assert s == 92


@pytest.mark.unit
def test_compute_counts_clamps_success_at_zero() -> None:
    """If errors > edges (data inconsistency) success doesn't go negative."""
    s, e = _compute_counts(edge_count=5, edge_wrong_corrections=10, team_wrong_corrections=0)
    assert s == 0
    assert e == 10


# ─── 2. MethodPerformance properties ────────────────────────────────────────


@pytest.mark.unit
def test_method_performance_accuracy() -> None:
    p = MethodPerformance(
        account_id=ACCOUNT,
        inference_method="implicit_scoring",
        success_count=80,
        error_count=20,
    )
    assert p.accuracy == 0.8
    assert p.total == 100


@pytest.mark.unit
def test_method_performance_zero_total_returns_none_accuracy() -> None:
    p = MethodPerformance(ACCOUNT, "implicit_scoring", 0, 0)
    assert p.accuracy is None
    assert p.total == 0


@pytest.mark.unit
def test_below_tally_threshold_default_20() -> None:
    """Per V3_PT2.md L215, threshold is 20."""
    assert MIN_CORRECTIONS_FOR_TALLY == 20
    small = MethodPerformance(ACCOUNT, "method_a", 5, 5)
    big = MethodPerformance(ACCOUNT, "method_a", 50, 5)
    assert small.below_tally_threshold is True
    assert big.below_tally_threshold is False


# ─── 3-4. compute_method_performance / compute_all_account_performance ──────


@pytest.fixture
def stub_perf_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Programmable shim for fetch() + the upsert connection."""
    state: dict[str, Any] = {
        # one entry per inference_method
        # (edge_count, edge_wrong, team_wrong)
        "method_counts": {
            "implicit_scoring": (100, 5, 0),  # 95% accuracy
            "explicit_sec_filing": (10, 0, 0),  # 100% accuracy, but below threshold
        },
        "writes": [],
    }

    async def fake_fetch(sql: str, *args: Any) -> list[dict]:
        sql_norm = " ".join(sql.split()).upper()
        if "DISTINCT INFERENCE_METHOD" in sql_norm:
            return [
                {"inference_method": method}
                for method in state["method_counts"].keys()
            ]
        if "ORG_REPORTING_EDGES" in sql_norm and "WHERE ACCOUNT_ID" in sql_norm:
            # Multi-subquery counts query — args are (account_id, method, EDGE_WRONG, TEAM_WRONG)
            method = args[1]
            counts = state["method_counts"].get(method, (0, 0, 0))
            return [{
                "edge_count": counts[0],
                "edge_wrong": counts[1],
                "team_wrong": counts[2],
            }]
        return []

    class _FakeConn:
        def transaction(self):
            class _Tx:
                async def __aenter__(self_): return None
                async def __aexit__(self_, *_a): return None
            return _Tx()

        async def execute(self, sql: str, *args: Any) -> str:
            state["writes"].append({
                "account_id": args[0],
                "inference_method": args[1],
                "success_count": args[2],
                "error_count": args[3],
                "accuracy": args[4],
            })
            return "ok"

    class _AcquireCtx:
        async def __aenter__(self): return _FakeConn()
        async def __aexit__(self, *_a): return None

    monkeypatch.setattr(perf_mod, "fetch", fake_fetch)
    monkeypatch.setattr(perf_mod, "acquire", lambda: _AcquireCtx())
    return state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compute_single_method_returns_correct_tally(stub_perf_db) -> None:
    perf = await compute_method_performance(ACCOUNT, "implicit_scoring")
    assert perf.success_count == 95
    assert perf.error_count == 5
    assert perf.accuracy == 0.95


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compute_all_returns_perf_per_method(stub_perf_db) -> None:
    rollup = await compute_all_account_performance(ACCOUNT, upsert=False)
    methods = {p.inference_method for p in rollup}
    assert methods == {"implicit_scoring", "explicit_sec_filing"}
    impl = next(p for p in rollup if p.inference_method == "implicit_scoring")
    assert impl.accuracy == 0.95


# ─── 5-7. Upsert behavior ────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upsert_skips_below_threshold(stub_perf_db) -> None:
    """`explicit_sec_filing` has only 10 total < 20 threshold → no upsert."""
    await compute_all_account_performance(ACCOUNT, upsert=True)
    written_methods = {w["inference_method"] for w in stub_perf_db["writes"]}
    assert "implicit_scoring" in written_methods
    # below-threshold method should NOT be written
    assert "explicit_sec_filing" not in written_methods


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upsert_false_skips_all_writes(stub_perf_db) -> None:
    """upsert=False short-circuits the DB write entirely."""
    rollup = await compute_all_account_performance(ACCOUNT, upsert=False)
    assert len(rollup) == 2  # both methods compute
    assert stub_perf_db["writes"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upsert_writes_carry_correct_fields(stub_perf_db) -> None:
    await compute_all_account_performance(ACCOUNT, upsert=True)
    write = next(
        w for w in stub_perf_db["writes"]
        if w["inference_method"] == "implicit_scoring"
    )
    assert write["account_id"] == ACCOUNT
    assert write["success_count"] == 95
    assert write["error_count"] == 5
    assert write["accuracy"] == 0.95


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_methods_returns_empty_rollup(stub_perf_db) -> None:
    stub_perf_db["method_counts"] = {}
    rollup = await compute_all_account_performance(ACCOUNT)
    assert rollup == []
    assert stub_perf_db["writes"] == []


@pytest.mark.unit
def test_keyspaces_exhaust_correction_types() -> None:
    """EDGE_WRONG_TYPES + TEAM_WRONG_TYPES = the full 4-value keyspace."""
    from credence.orgchart.corrections import VALID_CORRECTION_TYPES
    assert EDGE_WRONG_TYPES | TEAM_WRONG_TYPES == VALID_CORRECTION_TYPES
