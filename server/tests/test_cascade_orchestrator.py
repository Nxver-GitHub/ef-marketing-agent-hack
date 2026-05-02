"""Tests for cascade_orchestrator (Wave 11.2).

Pure-Python helpers + state IO + cascade execution + watch_loop driven
through monkeypatched callables. No live DB or subprocess invocations.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from scripts import cascade_orchestrator as orch


# ── Fixtures ────────────────────────────────────────────────────────────────


ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ── _build_cascade_steps ────────────────────────────────────────────────────


@pytest.mark.unit
def test_build_cascade_steps_in_order() -> None:
    steps = orch._build_cascade_steps(ACCOUNT_ID, db_concurrency=4)
    names = [s.name for s in steps]
    assert names == [
        "bulk_career_overlap_signals",
        "bulk_education_signals",
        "career_overlap_clustering",
    ]


@pytest.mark.unit
def test_build_cascade_steps_account_id_threaded() -> None:
    steps = orch._build_cascade_steps(ACCOUNT_ID, db_concurrency=4)
    for step in steps[:2]:
        assert "--account-id" in step.argv
        assert str(ACCOUNT_ID) in step.argv


@pytest.mark.unit
def test_build_cascade_steps_clustering_has_db_concurrency() -> None:
    steps = orch._build_cascade_steps(ACCOUNT_ID, db_concurrency=8)
    cluster_step = steps[2]
    assert "--db-concurrency" in cluster_step.argv
    assert "8" in cluster_step.argv
    assert "--all" in cluster_step.argv


@pytest.mark.unit
def test_build_cascade_steps_allow_missing_years_toggle() -> None:
    on = orch._build_cascade_steps(ACCOUNT_ID, db_concurrency=4, allow_missing_years=True)
    assert "--allow-missing-years" in on[2].argv
    off = orch._build_cascade_steps(ACCOUNT_ID, db_concurrency=4, allow_missing_years=False)
    assert "--allow-missing-years" not in off[2].argv


@pytest.mark.unit
def test_build_cascade_steps_uses_python_executable() -> None:
    steps = orch._build_cascade_steps(ACCOUNT_ID, db_concurrency=4)
    # First arg of each subprocess invocation is the active Python.
    for step in steps:
        assert step.argv[0] == sys.executable
        assert step.argv[1] == "-m"


# ── State file IO ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_load_state_missing_file_returns_epoch_marker(tmp_path: Path) -> None:
    state = orch._load_state(tmp_path / "state.json")
    assert state.last_marker_ts == EPOCH
    assert state.cascades_run_total == 0


@pytest.mark.unit
def test_load_state_malformed_json_returns_epoch(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")
    state = orch._load_state(path)
    assert state.last_marker_ts == EPOCH


@pytest.mark.unit
def test_load_state_invalid_iso_returns_epoch(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"last_marker_ts": "not-a-date"}))
    state = orch._load_state(path)
    assert state.last_marker_ts == EPOCH


@pytest.mark.unit
def test_load_state_iso_with_z_suffix(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "last_marker_ts": "2026-05-01T22:00:00.000Z",
        "cascades_run": 7,
    }))
    state = orch._load_state(path)
    assert state.last_marker_ts.tzinfo is not None
    assert state.last_marker_ts.year == 2026
    assert state.cascades_run_total == 7


@pytest.mark.unit
def test_save_state_atomic_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = orch.OrchestratorState(
        last_marker_ts=datetime(2026, 5, 1, 22, 0, tzinfo=timezone.utc),
        cascades_run_total=3,
    )
    orch._save_state(path, state, last_run_persons_count=421)
    out = json.loads(path.read_text())
    assert out["last_marker_ts"].startswith("2026-05-01T22:00:00")
    assert out["cascades_run"] == 3
    assert out["last_run_persons_count"] == 421


# ── _should_stop ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_should_stop_max_hours() -> None:
    state = orch.OrchestratorState(
        last_marker_ts=EPOCH, started_at_unix=1000.0
    )
    stop, reason = orch._should_stop(
        state, max_hours=1.0, max_runs=99, max_empty_polls=99,
        now_unix=1000.0 + 3601,
    )
    assert stop is True
    assert "max_hours" in reason


@pytest.mark.unit
def test_should_stop_max_runs() -> None:
    state = orch.OrchestratorState(
        last_marker_ts=EPOCH, cascades_run_session=30
    )
    stop, reason = orch._should_stop(
        state, max_hours=99, max_runs=30, max_empty_polls=99,
        now_unix=time.time(),
    )
    assert stop is True
    assert "max_runs" in reason


@pytest.mark.unit
def test_should_stop_consecutive_empty_polls() -> None:
    state = orch.OrchestratorState(
        last_marker_ts=EPOCH, consecutive_empty_polls=60
    )
    stop, reason = orch._should_stop(
        state, max_hours=99, max_runs=99, max_empty_polls=60,
        now_unix=time.time(),
    )
    assert stop is True
    assert "empty_polls" in reason


@pytest.mark.unit
def test_should_stop_continues_under_thresholds() -> None:
    state = orch.OrchestratorState(
        last_marker_ts=EPOCH,
        started_at_unix=time.time(),
        cascades_run_session=2,
        consecutive_empty_polls=5,
    )
    stop, _ = orch._should_stop(
        state, max_hours=24, max_runs=30, max_empty_polls=60,
        now_unix=time.time(),
    )
    assert stop is False


# ── _run_one_cascade ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_one_cascade_all_succeed() -> None:
    invoked: list[str] = []

    async def fake_run(step: orch.CascadeStep) -> int:
        invoked.append(step.name)
        return 0

    steps = orch._build_cascade_steps(ACCOUNT_ID, db_concurrency=4)
    result = await orch._run_one_cascade(steps, runner=fake_run)
    assert result.aborted_at is None
    assert [name for name, rc in result.steps] == [s.name for s in steps]
    assert all(rc == 0 for _, rc in result.steps)
    assert invoked == [
        "bulk_career_overlap_signals",
        "bulk_education_signals",
        "career_overlap_clustering",
    ]


@pytest.mark.asyncio
async def test_run_one_cascade_aborts_on_failure() -> None:
    """If step 2 fails, step 3 must NOT run."""
    invoked: list[str] = []

    async def fake_run(step: orch.CascadeStep) -> int:
        invoked.append(step.name)
        if step.name == "bulk_education_signals":
            return 7
        return 0

    steps = orch._build_cascade_steps(ACCOUNT_ID, db_concurrency=4)
    result = await orch._run_one_cascade(steps, runner=fake_run)
    assert result.aborted_at == "bulk_education_signals"
    assert invoked == ["bulk_career_overlap_signals", "bulk_education_signals"]
    assert result.steps[-1] == ("bulk_education_signals", 7)


# ── watch_loop integration ──────────────────────────────────────────────────


def _state_with_marker(marker: datetime) -> Path:
    """Helper — caller replaces the on-disk state in test-local tmp_path."""
    return Path("never used directly")


@pytest.mark.asyncio
async def test_watch_loop_below_threshold_no_cascade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If new_persons < threshold the cascade must not fire."""
    state_path = tmp_path / "state.json"
    persons_returned = [50]  # below threshold

    async def fake_count(account_id: UUID, marker: datetime) -> int:
        return persons_returned[0]

    runner_calls: list[str] = []

    async def fake_run(step: orch.CascadeStep) -> int:
        runner_calls.append(step.name)
        return 0

    # Use very small intervals + caps so the loop exits after one iter.
    final = await orch.watch_loop(
        ACCOUNT_ID,
        interval_seconds=0,
        threshold=100,
        max_hours=999,
        max_runs=999,
        max_empty_polls=1,  # exit after 1 empty poll
        state_path=state_path,
        count_persons=fake_count,
        runner=fake_run,
    )
    assert final.cascades_run_session == 0
    assert runner_calls == []
    assert final.consecutive_empty_polls == 1
    # State file written even though no cascade ran.
    persisted = json.loads(state_path.read_text())
    assert persisted["last_run_persons_count"] == 50


@pytest.mark.asyncio
async def test_watch_loop_above_threshold_fires_cascade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state.json"

    async def fake_count(account_id: UUID, marker: datetime) -> int:
        return 250  # above threshold

    runner_calls: list[str] = []

    async def fake_run(step: orch.CascadeStep) -> int:
        runner_calls.append(step.name)
        return 0

    final = await orch.watch_loop(
        ACCOUNT_ID,
        interval_seconds=0,
        threshold=100,
        max_hours=999,
        max_runs=1,  # exit after one cascade
        max_empty_polls=999,
        state_path=state_path,
        count_persons=fake_count,
        runner=fake_run,
    )
    assert final.cascades_run_session == 1
    assert final.cascades_run_total == 1
    # Marker advanced past EPOCH (set to NOW() at cascade start).
    assert final.last_marker_ts > EPOCH
    # All 3 cascade steps ran in order.
    assert runner_calls == [
        "bulk_career_overlap_signals",
        "bulk_education_signals",
        "career_overlap_clustering",
    ]


@pytest.mark.asyncio
async def test_watch_loop_aborted_cascade_does_not_advance_marker(
    tmp_path: Path,
) -> None:
    """Failed cascade leaves the marker so the next poll re-fires."""
    state_path = tmp_path / "state.json"
    # Pre-seed with a known marker.
    seed_marker = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    state_path.write_text(json.dumps({
        "last_marker_ts": seed_marker.isoformat(),
        "cascades_run": 0,
    }))

    async def fake_count(account_id: UUID, marker: datetime) -> int:
        return 200

    async def fake_run(step: orch.CascadeStep) -> int:
        return 1 if step.name == "bulk_career_overlap_signals" else 0

    final = await orch.watch_loop(
        ACCOUNT_ID,
        interval_seconds=0,
        threshold=100,
        max_hours=999,
        max_runs=1,
        max_empty_polls=999,
        state_path=state_path,
        count_persons=fake_count,
        runner=fake_run,
    )
    # Marker preserved (cascade aborted at step 1).
    assert final.last_marker_ts == seed_marker


# ── CLI parser ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cli_defaults() -> None:
    parser = orch._build_arg_parser()
    args = parser.parse_args([])
    assert args.account_id == UUID("00000000-0000-0000-0000-000000000001")
    assert args.interval == orch.DEFAULT_INTERVAL_MINUTES
    assert args.threshold == orch.DEFAULT_THRESHOLD
    assert args.max_hours == orch.DEFAULT_MAX_HOURS
    assert args.db_concurrency == orch.DEFAULT_DB_CONCURRENCY


@pytest.mark.unit
def test_cli_custom_overrides() -> None:
    parser = orch._build_arg_parser()
    args = parser.parse_args([
        "--interval", "10",
        "--threshold", "500",
        "--max-hours", "12",
        "--max-runs", "5",
        "--db-concurrency", "8",
    ])
    assert args.interval == 10
    assert args.threshold == 500
    assert args.max_hours == 12.0
    assert args.max_runs == 5
    assert args.db_concurrency == 8
