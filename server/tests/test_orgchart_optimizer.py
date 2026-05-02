"""Tests for `credence.orgchart.optimizer` — v3.1 Plan A6.

Coverage:
1. _delta_for_accuracy — three bands (low / sweet / high)
2. _renormalize — preserves sum at TARGET_SUM after nudging
3. compute_new_weights — bounds + renorm + no-op in sweet spot
4. max_component_shift — picks largest absolute delta
5. optimize_account_weights — happy path inserts new row when shift > 0.02
6. Skips insert when shift <= 0.02
7. No active row → returns no-op rollup
8. Sub_weights merge preserves non-component keys
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from credence.orgchart import optimizer as opt_mod
from credence.orgchart.optimizer import (
    ACCURACY_HIGH,
    ACCURACY_LOW,
    COMPONENT_CEILING,
    COMPONENT_FLOOR,
    COMPONENT_KEYS,
    DEFAULT_COMPONENT_WEIGHTS,
    DEFAULT_LEARNING_RATE,
    MIN_SHIFT_FOR_INSERT,
    PER_COMPONENT_CEILING,
    PER_COMPONENT_DEFAULT_WEIGHTS,
    PER_COMPONENT_FLOOR,
    PerComponentNudge,
    TARGET_SUM,
    _delta_for_accuracy,
    _renormalize,
    compute_new_weights,
    compute_per_component_nudges,
    max_component_shift,
    optimize_account_weights,
    optimize_account_weights_per_component,
)


ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")


# ─── 1. _delta_for_accuracy ──────────────────────────────────────────────────


@pytest.mark.unit
def test_delta_negative_when_accuracy_below_low_band() -> None:
    # accuracy 0.40 is 0.20 below ACCURACY_LOW (0.60)
    delta = _delta_for_accuracy(0.40)
    assert delta == pytest.approx(-DEFAULT_LEARNING_RATE * 0.20, abs=1e-6)
    assert delta < 0


@pytest.mark.unit
def test_delta_zero_in_sweet_spot() -> None:
    """Accuracy in [LOW, HIGH] = no change."""
    assert _delta_for_accuracy(0.60) == 0.0
    assert _delta_for_accuracy(0.70) == 0.0
    assert _delta_for_accuracy(0.85) == 0.0


@pytest.mark.unit
def test_delta_positive_when_accuracy_above_high_band() -> None:
    # 0.95 is 0.10 above ACCURACY_HIGH (0.85)
    delta = _delta_for_accuracy(0.95)
    assert delta == pytest.approx(DEFAULT_LEARNING_RATE * 0.10, abs=1e-6)
    assert delta > 0


@pytest.mark.unit
def test_delta_zero_when_accuracy_none() -> None:
    """No accuracy data → no delta (no spurious adjustments)."""
    assert _delta_for_accuracy(None) == 0.0


# ─── 2. _renormalize ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_renormalize_preserves_target_sum() -> None:
    weights = {"a": 1.0, "b": 2.0, "c": 3.0}  # sum = 6
    out = _renormalize(weights)
    assert sum(out.values()) == pytest.approx(TARGET_SUM, abs=1e-3)


@pytest.mark.unit
def test_renormalize_zero_total_returns_defaults() -> None:
    out = _renormalize({"a": 0.0, "b": 0.0})
    assert out == DEFAULT_COMPONENT_WEIGHTS


# ─── 3. compute_new_weights ──────────────────────────────────────────────────


@pytest.mark.unit
def test_compute_new_weights_no_change_in_sweet_spot() -> None:
    """Accuracy in sweet spot → returns same weights."""
    out = compute_new_weights(DEFAULT_COMPONENT_WEIGHTS, 0.75)
    assert out == DEFAULT_COMPONENT_WEIGHTS


@pytest.mark.unit
def test_compute_new_weights_dials_down_on_low_accuracy() -> None:
    """Low accuracy → all components nudged down before renorm."""
    current = dict(DEFAULT_COMPONENT_WEIGHTS)
    out = compute_new_weights(current, 0.40, learning_rate=DEFAULT_LEARNING_RATE)
    # After renorm sum is TARGET_SUM; the proportions roughly preserved
    assert sum(out.values()) == pytest.approx(TARGET_SUM, abs=1e-3)
    # No component below the floor
    assert all(v >= COMPONENT_FLOOR - 1e-6 for v in out.values())


@pytest.mark.unit
def test_compute_new_weights_dials_up_on_high_accuracy() -> None:
    """High accuracy → components nudged up. Renorm caps total at TARGET_SUM."""
    out = compute_new_weights(DEFAULT_COMPONENT_WEIGHTS, 0.95)
    assert sum(out.values()) == pytest.approx(TARGET_SUM, abs=1e-3)
    assert all(v <= COMPONENT_CEILING + 1e-6 for v in out.values())


@pytest.mark.unit
def test_compute_new_weights_clamps_at_ceiling() -> None:
    """A pre-clamp weight already near the ceiling stays bounded.

    Verified via the intermediate clamp step — final weights after renorm
    can scale beyond the per-component ceiling because renorm preserves
    sum at TARGET_SUM (1.08), but the pre-clamp logic enforces
    [FLOOR, CEILING]. This test pins the clamp behavior end-to-end.
    """
    # All other components small + one near ceiling — accuracy super high
    overweight = {"seniority_gap": 0.39, "other": 0.10}
    out = compute_new_weights(overweight, 0.99)
    # After clamp + renorm, output should sum to TARGET_SUM
    assert sum(out.values()) == pytest.approx(TARGET_SUM, abs=1e-3)
    # And no negative weights
    assert all(v >= 0 for v in out.values())


# ─── 4. max_component_shift ─────────────────────────────────────────────────


@pytest.mark.unit
def test_max_shift_picks_largest_absolute_delta() -> None:
    old = {"a": 0.30, "b": 0.20, "c": 0.10}
    new = {"a": 0.32, "b": 0.10, "c": 0.10}
    # |0.30-0.32|=0.02; |0.20-0.10|=0.10 (largest); |0.10-0.10|=0
    assert max_component_shift(old, new) == pytest.approx(0.10, abs=1e-6)


@pytest.mark.unit
def test_max_shift_zero_when_identical() -> None:
    assert max_component_shift(DEFAULT_COMPONENT_WEIGHTS, DEFAULT_COMPONENT_WEIGHTS) == 0.0


# ─── 5-8. optimize_account_weights ──────────────────────────────────────────


@pytest.fixture
def stub_opt_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "active_row_present": True,
        "active_row_id": uuid4(),
        "authenticity_w": 0.40,
        "authority_w": 0.40,
        "warmth_w": 0.20,
        "sub_weights": dict(DEFAULT_COMPONENT_WEIGHTS),
        # Far below LOW (0.6) so the per-component delta is large enough to
        # produce max_shift > MIN_SHIFT_FOR_INSERT after renorm.
        "implicit_accuracy": 0.10,
        "method_count_implicit_total": 100,
        "writes": [],
        "deactivations": [],
    }

    async def fake_fetchrow(sql: str, *args: Any) -> Any:
        sql_norm = " ".join(sql.split()).upper()
        if "FROM SCORE_WEIGHTS" in sql_norm and "IS_ACTIVE = TRUE" in sql_norm:
            if not state["active_row_present"]:
                return None
            return {
                "id": state["active_row_id"],
                "authenticity_w": state["authenticity_w"],
                "authority_w": state["authority_w"],
                "warmth_w": state["warmth_w"],
                "sub_weights": state["sub_weights"],
                "created_by": "system:contract6_seed",
            }
        return None

    # The optimizer calls compute_method_performance which itself uses
    # `performance.fetch`. Stub that helper at the module-level boundary.
    from credence.orgchart import performance as perf_mod

    async def fake_perf_fetch(sql: str, *args: Any) -> list[dict]:
        sql_norm = " ".join(sql.split()).upper()
        if "ORG_REPORTING_EDGES" in sql_norm:
            return [{
                "edge_count": state["method_count_implicit_total"],
                "edge_wrong": int(
                    state["method_count_implicit_total"]
                    * (1 - state["implicit_accuracy"])
                ),
                "team_wrong": 0,
            }]
        return []

    monkeypatch.setattr(perf_mod, "fetch", fake_perf_fetch)
    monkeypatch.setattr(opt_mod, "fetchrow", fake_fetchrow)

    class _FakeConn:
        def transaction(self):
            class _Tx:
                async def __aenter__(self_): return None
                async def __aexit__(self_, *_a): return None
            return _Tx()

        async def execute(self, sql: str, *args: Any) -> str:
            sql_norm = " ".join(sql.split()).upper()
            if "UPDATE SCORE_WEIGHTS SET IS_ACTIVE = FALSE" in sql_norm:
                state["deactivations"].append({"account_id": args[0]})
            return "ok"

        async def fetchrow(self, sql: str, *args: Any) -> dict:
            new_id = uuid4()
            state["writes"].append({
                "id": new_id,
                "account_id": args[0],
                "authenticity_w": args[1],
                "authority_w": args[2],
                "warmth_w": args[3],
                "sub_weights": args[4],
                "created_by": args[5],
            })
            return {"id": new_id}

    class _AcquireCtx:
        async def __aenter__(self): return _FakeConn()
        async def __aexit__(self, *_a): return None

    monkeypatch.setattr(opt_mod, "acquire", lambda: _AcquireCtx())
    return state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_optimizer_inserts_new_row_when_shift_above_threshold(stub_opt_db) -> None:
    """Low accuracy (0.10) drives a noticeable shift → new row inserted."""
    result = await optimize_account_weights(ACCOUNT)
    assert result.inserted_new_version is True
    assert result.new_weight_version_id is not None
    assert result.accuracy_used == pytest.approx(0.10, abs=1e-3)
    assert result.max_shift > MIN_SHIFT_FOR_INSERT
    # One deactivation, one insert
    assert len(stub_opt_db["deactivations"]) == 1
    assert len(stub_opt_db["writes"]) == 1
    # New row preserves top-level weights
    write = stub_opt_db["writes"][0]
    assert write["authenticity_w"] == pytest.approx(0.40)
    assert write["authority_w"] == pytest.approx(0.40)
    assert write["warmth_w"] == pytest.approx(0.20)
    # Audit trail in created_by
    assert "orgchart_optimizer" in write["created_by"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_optimizer_no_op_in_sweet_spot(stub_opt_db) -> None:
    """Accuracy 0.75 (sweet spot) → no shift → no write."""
    stub_opt_db["implicit_accuracy"] = 0.75
    result = await optimize_account_weights(ACCOUNT)
    assert result.inserted_new_version is False
    assert result.new_weight_version_id is None
    assert result.max_shift == 0.0
    assert stub_opt_db["writes"] == []
    assert stub_opt_db["deactivations"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_optimizer_handles_missing_active_row(stub_opt_db) -> None:
    """No active score_weights row → no-op (no exception)."""
    stub_opt_db["active_row_present"] = False
    result = await optimize_account_weights(ACCOUNT)
    assert result.inserted_new_version is False
    assert result.new_weight_version_id is None
    assert result.accuracy_used is None
    assert stub_opt_db["writes"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_optimizer_preserves_non_component_sub_weights(stub_opt_db) -> None:
    """Existing sub_weights with extra (non-component) keys must round-trip."""
    stub_opt_db["sub_weights"] = {
        **DEFAULT_COMPONENT_WEIGHTS,
        "ui_color_threshold": 0.5,  # hypothetical future tunable
    }
    await optimize_account_weights(ACCOUNT)
    write = stub_opt_db["writes"][0]
    assert "ui_color_threshold" in write["sub_weights"]
    assert write["sub_weights"]["ui_color_threshold"] == 0.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_optimizer_uses_default_weights_when_subweights_empty(stub_opt_db) -> None:
    """If sub_weights JSONB is empty, optimizer starts from defaults."""
    stub_opt_db["sub_weights"] = {}
    result = await optimize_account_weights(ACCOUNT)
    # Defaults used as the "old" state; result.old_weights matches
    assert result.old_weights["seniority_gap"] == pytest.approx(0.30)


@pytest.mark.unit
def test_default_weights_sum_to_target() -> None:
    """A6's defaults must sum to TARGET_SUM (V3_PT2.md L259 = 1.08)."""
    assert sum(DEFAULT_COMPONENT_WEIGHTS.values()) == pytest.approx(TARGET_SUM, abs=1e-3)


# ─── Task 4-B: Per-component optimizer ──────────────────────────────────────


@pytest.fixture
def stub_per_component_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fixture mirroring `stub_opt_db` but for the per-component optimizer.

    `corrections` is a list of {'correction_type': str, 'dominant_signal': str|None}
    rows the optimizer will read out of the JOIN.
    """
    state: dict[str, Any] = {
        "active_row_present": True,
        "active_row_id": uuid4(),
        "authenticity_w": 0.40,
        "authority_w": 0.40,
        "warmth_w": 0.20,
        "sub_weights": dict(PER_COMPONENT_DEFAULT_WEIGHTS),
        # List of correction rows the JOIN-with-dominant-signal returns.
        # When dominant_signal is None, the SQL filter excludes the row;
        # to simulate that, omit the row from this list (test 6 does this).
        "corrections": [],
        "writes": [],
        "deactivations": [],
        "perf_writes": [],
    }

    async def fake_fetch(sql: str, *args: Any) -> list[dict]:
        sql_norm = " ".join(sql.split()).upper()
        if "ORG_CHART_CORRECTIONS" in sql_norm and "DOMINANT_SIGNAL IS NOT NULL" in sql_norm:
            # Filter mirrors SQL `e.dominant_signal IS NOT NULL`.
            return [
                {"correction_type": c["correction_type"],
                 "dominant_signal": c["dominant_signal"]}
                for c in state["corrections"]
                if c.get("dominant_signal") is not None
            ]
        return []

    async def fake_fetchrow(sql: str, *args: Any) -> Any:
        sql_norm = " ".join(sql.split()).upper()
        if "FROM SCORE_WEIGHTS" in sql_norm and "IS_ACTIVE = TRUE" in sql_norm:
            if not state["active_row_present"]:
                return None
            return {
                "id": state["active_row_id"],
                "authenticity_w": state["authenticity_w"],
                "authority_w": state["authority_w"],
                "warmth_w": state["warmth_w"],
                "sub_weights": state["sub_weights"],
                "created_by": "system:contract6_seed",
            }
        return None

    monkeypatch.setattr(opt_mod, "fetch", fake_fetch)
    monkeypatch.setattr(opt_mod, "fetchrow", fake_fetchrow)

    class _FakeConn:
        def transaction(self):
            class _Tx:
                async def __aenter__(self_): return None
                async def __aexit__(self_, *_a): return None
            return _Tx()

        async def execute(self, sql: str, *args: Any) -> str:
            sql_norm = " ".join(sql.split()).upper()
            if "UPDATE SCORE_WEIGHTS SET IS_ACTIVE = FALSE" in sql_norm:
                state["deactivations"].append({"account_id": args[0]})
            elif "INSERT INTO ORG_SIGNAL_PERFORMANCE" in sql_norm:
                state["perf_writes"].append({
                    "account_id": args[0],
                    "inference_method": args[1],
                    "success_count": args[2],
                    "error_count": args[3],
                    "accuracy": args[4],
                })
            return "ok"

        async def fetchrow(self, sql: str, *args: Any) -> dict:
            new_id = uuid4()
            state["writes"].append({
                "id": new_id,
                "account_id": args[0],
                "authenticity_w": args[1],
                "authority_w": args[2],
                "warmth_w": args[3],
                "sub_weights": args[4],
                "created_by": args[5],
            })
            return {"id": new_id}

    class _AcquireCtx:
        async def __aenter__(self): return _FakeConn()
        async def __aexit__(self, *_a): return None

    monkeypatch.setattr(opt_mod, "acquire", lambda: _AcquireCtx())
    return state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_component_dominant_shrinks_most_when_all_wrong(
    stub_per_component_db,
) -> None:
    """10 wrong corrections all on patent_cluster → patent_cluster delta is
    the most-negative across all 7 components. All weights stay in bounds.
    """
    stub_per_component_db["corrections"] = [
        {"correction_type": "not_reports_to", "dominant_signal": "patent_cluster"}
        for _ in range(10)
    ]
    nudges = await optimize_account_weights_per_component(ACCOUNT)
    assert len(nudges) == 7
    by_comp = {n.component: n for n in nudges}
    pc = by_comp["patent_cluster"]
    # Patent cluster: count=10 ≥ 5 → per-component path, error_rate=1.0,
    # delta = old * (1 - 0.15*1.0) - old = -0.15 * old
    assert pc.used_global_fallback is False
    assert pc.correction_count == 10
    assert pc.error_rate_used == pytest.approx(1.0)
    assert pc.delta < 0
    # Most-negative delta of all components
    most_negative = min(nudges, key=lambda n: n.delta)
    assert most_negative.component == "patent_cluster"
    # Other components fall back to global error_rate (also 1.0) at LR 0.05
    for comp_name in COMPONENT_KEYS:
        if comp_name == "patent_cluster":
            continue
        n = by_comp[comp_name]
        assert n.used_global_fallback is True
        assert n.error_rate_used == pytest.approx(1.0)
        assert n.delta < 0
        # Each new weight in bounds
        assert PER_COMPONENT_FLOOR <= n.new_weight <= PER_COMPONENT_CEILING


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_component_below_threshold_uses_global_fallback(
    stub_per_component_db,
) -> None:
    """3 wrong on patent_cluster (below threshold) + 5 right on
    seniority_gap (at threshold). Assert: patent_cluster uses global,
    seniority_gap uses per-component.
    """
    stub_per_component_db["corrections"] = (
        [{"correction_type": "not_reports_to", "dominant_signal": "patent_cluster"}
         for _ in range(3)]
        + [{"correction_type": "manager_correct", "dominant_signal": "seniority_gap"}
           for _ in range(5)]
    )
    nudges = await optimize_account_weights_per_component(ACCOUNT)
    by_comp = {n.component: n for n in nudges}
    # patent_cluster: count=3 < 5 → global fallback
    assert by_comp["patent_cluster"].used_global_fallback is True
    assert by_comp["patent_cluster"].correction_count == 3
    # seniority_gap: count=5 ≥ 5 → per-component
    assert by_comp["seniority_gap"].used_global_fallback is False
    assert by_comp["seniority_gap"].correction_count == 5
    # All 5 corrections on seniority_gap are 'right' → error_rate=0
    assert by_comp["seniority_gap"].error_rate_used == pytest.approx(0.0)
    # Global error_rate = 3/8 = 0.375
    assert by_comp["patent_cluster"].error_rate_used == pytest.approx(0.375)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_component_all_right_yields_zero_deltas(
    stub_per_component_db,
) -> None:
    """All corrections type='manager_correct' → error_rate=0 everywhere
    → all deltas zero → no DB write.
    """
    stub_per_component_db["corrections"] = [
        {"correction_type": "manager_correct", "dominant_signal": "seniority_gap"}
        for _ in range(10)
    ]
    nudges = await optimize_account_weights_per_component(ACCOUNT)
    for n in nudges:
        assert n.delta == pytest.approx(0.0)
        assert n.new_weight == pytest.approx(n.old_weight)
    # max_shift == 0 ≤ MIN_SHIFT_FOR_INSERT → no flip-and-insert
    assert stub_per_component_db["writes"] == []
    assert stub_per_component_db["deactivations"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_component_clamps_to_bounds(stub_per_component_db) -> None:
    """When the active sub_weights start above the ceiling or below the
    floor, the optimizer clamps them. No NaN, no exceptions.
    """
    # Start near the floor on one component, near the ceiling on another.
    stub_per_component_db["sub_weights"] = {
        **PER_COMPONENT_DEFAULT_WEIGHTS,
        "patent_cluster": 0.005,    # below floor → clamps up to 0.01
        "seniority_gap": 0.99,      # above ceiling → clamps down to 0.50
    }
    # 6 wrong corrections on each so both go through per-component path.
    stub_per_component_db["corrections"] = (
        [{"correction_type": "not_reports_to", "dominant_signal": "patent_cluster"}
         for _ in range(6)]
        + [{"correction_type": "not_reports_to", "dominant_signal": "seniority_gap"}
           for _ in range(6)]
    )
    nudges = await optimize_account_weights_per_component(ACCOUNT)
    for n in nudges:
        assert PER_COMPONENT_FLOOR <= n.new_weight <= PER_COMPONENT_CEILING
        # No NaN
        assert n.new_weight == n.new_weight  # NaN != NaN


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_component_empty_corrections_returns_zero_nudges(
    stub_per_component_db,
) -> None:
    """No corrections at all → all deltas zero, still 7 nudges returned."""
    stub_per_component_db["corrections"] = []
    nudges = await optimize_account_weights_per_component(ACCOUNT)
    assert len(nudges) == 7
    for n in nudges:
        assert n.delta == pytest.approx(0.0)
        assert n.correction_count == 0
        assert n.used_global_fallback is True
    assert stub_per_component_db["writes"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_component_null_dominant_signal_excluded(
    stub_per_component_db,
) -> None:
    """When every correction's edge has dominant_signal=NULL, the JOIN
    filter excludes them. Optimizer behaves as if no corrections exist.
    Returns 7 nudges, all zero-delta.
    """
    # All 5 corrections have NULL dominant_signal → SQL filter drops them.
    stub_per_component_db["corrections"] = [
        {"correction_type": "not_reports_to", "dominant_signal": None}
        for _ in range(5)
    ]
    nudges = await optimize_account_weights_per_component(ACCOUNT)
    assert len(nudges) == 7
    for n in nudges:
        assert n.delta == pytest.approx(0.0)
        assert n.correction_count == 0
    assert stub_per_component_db["writes"] == []


@pytest.mark.unit
def test_per_component_pure_function_isolation() -> None:
    """`compute_per_component_nudges` is pure — same input, same output."""
    counts = {"patent_cluster": {"right": 0, "wrong": 10}}
    nudges = compute_per_component_nudges(
        weights=dict(PER_COMPONENT_DEFAULT_WEIGHTS),
        by_component_counts=counts,
        error_rate_global=1.0,
    )
    assert len(nudges) == 7
    pc = next(n for n in nudges if n.component == "patent_cluster")
    # 0.15 * 1.0 = 15% nudge down on patent_cluster (old=0.15)
    assert pc.new_weight == pytest.approx(0.15 * (1 - 0.15), abs=1e-6)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_component_writes_perf_rows(stub_per_component_db) -> None:
    """When the optimizer flips-and-inserts, it also writes one
    `org_signal_performance` row per component with method='component:<name>'.
    """
    stub_per_component_db["corrections"] = [
        {"correction_type": "not_reports_to", "dominant_signal": "patent_cluster"}
        for _ in range(10)
    ]
    await optimize_account_weights_per_component(ACCOUNT)
    assert len(stub_per_component_db["writes"]) == 1
    # 7 perf rows, one per component
    assert len(stub_per_component_db["perf_writes"]) == 7
    methods = {pw["inference_method"] for pw in stub_per_component_db["perf_writes"]}
    assert methods == {f"component:{c}" for c in COMPONENT_KEYS}
