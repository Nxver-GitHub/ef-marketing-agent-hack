"""Tests for apify_orphan_watcher — pure helpers + HTTP layer + state IO."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from scripts import apify_orphan_watcher as watcher
from credence.enrichment.apify import PROFILE_ACTOR_ID


# ── Fixtures ────────────────────────────────────────────────────────────────


ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")


def _run(
    run_id: str = "abc123",
    *,
    act_id: str = PROFILE_ACTOR_ID,
    status: str = "SUCCEEDED",
    finished_at_ms: int | None = 1714579200000,
    full_profile: int = 1500,
) -> dict[str, Any]:
    """Build a synthetic /v2/actor-runs item."""
    item: dict[str, Any] = {
        "id": run_id,
        "actId": act_id,
        "status": status,
    }
    if finished_at_ms is not None:
        item["finishedAt"] = finished_at_ms
    item["chargedEventCounts"] = {"full-profile": full_profile}
    return item


# ── State file IO ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_load_state_missing_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    assert watcher._load_state(path) == set()


@pytest.mark.unit
def test_load_state_malformed_json_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert watcher._load_state(path) == set()


@pytest.mark.unit
def test_load_state_non_dict_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps([1, 2, 3]))
    assert watcher._load_state(path) == set()


@pytest.mark.unit
def test_load_state_recovered_list_parsed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"recovered": ["run1", "run2", 99]}))
    # Non-string entries are filtered.
    assert watcher._load_state(path) == {"run1", "run2"}


@pytest.mark.unit
def test_save_state_atomic_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    watcher._save_state(path, {"runA", "runB"})
    out = json.loads(path.read_text())
    assert out == {"recovered": ["runA", "runB"]}  # sorted


@pytest.mark.unit
def test_save_state_overwrites_existing(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    watcher._save_state(path, {"old1"})
    watcher._save_state(path, {"new1", "new2"})
    out = json.loads(path.read_text())
    assert out["recovered"] == ["new1", "new2"]


# ── _parse_run_payload ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_run_payload_full_shape() -> None:
    parsed = watcher._parse_run_payload(_run("xyz", full_profile=1234))
    assert parsed is not None
    assert parsed.run_id == "xyz"
    assert parsed.act_id == PROFILE_ACTOR_ID
    assert parsed.status == "SUCCEEDED"
    assert parsed.charged_full_profile == 1234
    assert parsed.finished_at_unix == 1714579200.0


@pytest.mark.unit
def test_parse_run_payload_iso_finishedAt() -> None:
    item = _run()
    item["finishedAt"] = "2026-05-01T22:00:00.000Z"
    parsed = watcher._parse_run_payload(item)
    assert parsed is not None
    assert parsed.finished_at_unix is not None


@pytest.mark.unit
def test_parse_run_payload_missing_fields_returns_none() -> None:
    assert watcher._parse_run_payload({}) is None
    assert watcher._parse_run_payload({"id": "x"}) is None
    assert watcher._parse_run_payload({"id": "x", "status": "SUCCEEDED"}) is None


@pytest.mark.unit
def test_parse_run_payload_charged_actor_agnostic() -> None:
    """The live profile-scraper actor uses ``profile`` (msg 232), not the
    ``full-profile`` key the watcher originally hardcoded. The fix is to
    sum any positive numeric value in chargedEventCounts."""
    item = _run()
    item["chargedEventCounts"] = {"profile": 1451, "profile_with_email": 0}
    parsed = watcher._parse_run_payload(item)
    assert parsed is not None
    assert parsed.charged_full_profile == 1451


@pytest.mark.unit
def test_parse_run_payload_charged_sums_multiple_positive_keys() -> None:
    """Robust to actors that bill on multiple line items."""
    item = _run()
    item["chargedEventCounts"] = {"profile": 100, "extra": 50, "skipped": 0}
    parsed = watcher._parse_run_payload(item)
    assert parsed is not None
    assert parsed.charged_full_profile == 150


@pytest.mark.unit
def test_parse_run_payload_charged_ignores_booleans_and_negatives() -> None:
    item = _run()
    item["chargedEventCounts"] = {"profile": 7, "ok": True, "broken": -1}
    parsed = watcher._parse_run_payload(item)
    assert parsed is not None
    # bool excluded (despite being int subclass), negative excluded.
    assert parsed.charged_full_profile == 7


@pytest.mark.unit
def test_parse_run_payload_charged_camelCase_legacy() -> None:
    """Legacy ``fullProfile`` key still produces a positive count under the
    new actor-agnostic logic (since it's a positive numeric value)."""
    item = _run()
    item["chargedEventCounts"] = {"fullProfile": 99}
    parsed = watcher._parse_run_payload(item)
    assert parsed is not None
    assert parsed.charged_full_profile == 99


# ── _should_recover ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_should_recover_happy_path() -> None:
    run = watcher._parse_run_payload(_run("happy"))
    assert run is not None
    decision, reason = watcher._should_recover(
        run,
        already_recovered=set(),
        now_unix=1714579200.0 + 600,  # 10 min after finishedAt
        min_age_seconds=300,
        profile_actor_id=PROFILE_ACTOR_ID,
    )
    assert decision is True


@pytest.mark.unit
def test_should_recover_skips_failed_status() -> None:
    run = watcher._parse_run_payload(_run("dead", status="FAILED"))
    assert run is not None
    decision, reason = watcher._should_recover(
        run,
        already_recovered=set(),
        now_unix=time.time(),
        min_age_seconds=300,
        profile_actor_id=PROFILE_ACTOR_ID,
    )
    assert decision is False
    assert "FAILED" in reason


@pytest.mark.unit
def test_should_recover_skips_wrong_actor() -> None:
    run = watcher._parse_run_payload(_run("wrong", act_id="someother~actor"))
    assert run is not None
    decision, reason = watcher._should_recover(
        run,
        already_recovered=set(),
        now_unix=time.time(),
        min_age_seconds=300,
        profile_actor_id=PROFILE_ACTOR_ID,
    )
    assert decision is False
    assert "act_id" in reason


@pytest.mark.unit
def test_should_recover_skips_already_recovered() -> None:
    run = watcher._parse_run_payload(_run("done"))
    assert run is not None
    decision, reason = watcher._should_recover(
        run,
        already_recovered={"done"},
        now_unix=1714579200.0 + 600,
        min_age_seconds=300,
        profile_actor_id=PROFILE_ACTOR_ID,
    )
    assert decision is False
    assert reason == "already_recovered"


@pytest.mark.unit
def test_should_recover_skips_empty_dataset() -> None:
    run = watcher._parse_run_payload(_run("zilch", full_profile=0))
    assert run is not None
    decision, reason = watcher._should_recover(
        run,
        already_recovered=set(),
        now_unix=1714579200.0 + 600,
        min_age_seconds=300,
        profile_actor_id=PROFILE_ACTOR_ID,
    )
    assert decision is False
    assert "empty_dataset" in reason


@pytest.mark.unit
def test_should_recover_skips_too_recent() -> None:
    run = watcher._parse_run_payload(_run("fresh"))
    assert run is not None
    decision, reason = watcher._should_recover(
        run,
        already_recovered=set(),
        now_unix=1714579200.0 + 60,  # 1 min after finishedAt
        min_age_seconds=300,
        profile_actor_id=PROFILE_ACTOR_ID,
    )
    assert decision is False
    assert "too_recent" in reason


@pytest.mark.unit
def test_should_recover_no_finished_at_still_recovers() -> None:
    """SUCCEEDED + no finishedAt → recover (false-negative not worth)."""
    run = watcher._parse_run_payload(_run("ts_missing", finished_at_ms=None))
    assert run is not None
    decision, _ = watcher._should_recover(
        run,
        already_recovered=set(),
        now_unix=time.time(),
        min_age_seconds=300,
        profile_actor_id=PROFILE_ACTOR_ID,
    )
    assert decision is True


# ── _should_stop ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_should_stop_max_hours() -> None:
    state = watcher.WatcherState(started_at_unix=1000.0)
    stop, reason = watcher._should_stop(
        state, max_hours=1.0, max_runs=999, max_empty_cycles=60,
        now_unix=1000.0 + 3600 + 1,
    )
    assert stop is True
    assert "max_hours" in reason


@pytest.mark.unit
def test_should_stop_max_runs() -> None:
    state = watcher.WatcherState(runs_recovered_this_session=30)
    stop, reason = watcher._should_stop(
        state, max_hours=999, max_runs=30, max_empty_cycles=60,
        now_unix=time.time(),
    )
    assert stop is True
    assert "max_runs" in reason


@pytest.mark.unit
def test_should_stop_default_empty_cycles_threshold() -> None:
    """Default threshold is now 60 — a 3-cycle gap shouldn't abort."""
    state = watcher.WatcherState(consecutive_empty_cycles=3)
    stop, _ = watcher._should_stop(
        state, max_hours=999, max_runs=999, max_empty_cycles=60,
        now_unix=time.time(),
    )
    assert stop is False
    # And the legacy aggressive threshold still works when explicitly chosen.
    stop, reason = watcher._should_stop(
        state, max_hours=999, max_runs=999, max_empty_cycles=3,
        now_unix=time.time(),
    )
    assert stop is True
    assert "empty_cycles" in reason


@pytest.mark.unit
def test_should_stop_max_empty_cycles_999_keeps_alive() -> None:
    """`--max-empty-cycles 999` keeps the watcher alive past short empty bursts."""
    state = watcher.WatcherState(consecutive_empty_cycles=100)
    stop, _ = watcher._should_stop(
        state, max_hours=999, max_runs=999, max_empty_cycles=999,
        now_unix=time.time(),
    )
    assert stop is False


@pytest.mark.unit
def test_should_stop_continues_when_under_thresholds() -> None:
    state = watcher.WatcherState(
        started_at_unix=time.time(),
        runs_recovered_this_session=5,
        consecutive_empty_cycles=1,
    )
    stop, _ = watcher._should_stop(
        state, max_hours=24, max_runs=30, max_empty_cycles=60,
        now_unix=time.time(),
    )
    assert stop is False


# ── _fetch_recent_runs HTTP layer ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_recent_runs_parses_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/actor-runs"
        params = dict(request.url.params)
        assert params["desc"] == "true"
        assert params["limit"] == "50"
        return httpx.Response(
            200,
            json={
                "data": {
                    "items": [
                        _run("alpha"),
                        _run("beta", status="FAILED"),
                        _run("gamma", full_profile=0),
                    ]
                }
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.apify.com") as client:
        runs = await watcher._fetch_recent_runs(client, token="t1", limit=50)
    assert len(runs) == 3
    assert {r.run_id for r in runs} == {"alpha", "beta", "gamma"}


@pytest.mark.asyncio
async def test_fetch_recent_runs_http_error_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        runs = await watcher._fetch_recent_runs(client, token="t1")
    assert runs == []


@pytest.mark.asyncio
async def test_fetch_recent_runs_non_200_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        runs = await watcher._fetch_recent_runs(client, token="t1")
    assert runs == []


@pytest.mark.asyncio
async def test_fetch_recent_runs_malformed_body_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "the right shape"])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        runs = await watcher._fetch_recent_runs(client, token="t1")
    assert runs == []


# ── _poll_once integration ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_once_recovers_eligible_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end one cycle: fetch → filter → invoke recover_run → mark state."""
    state = watcher.WatcherState()
    state_path = tmp_path / "state.json"

    eligible_run = _run("eligible1")
    too_recent = _run("fresh1")
    too_recent["finishedAt"] = int(time.time() * 1000)  # now → too recent

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"items": [eligible_run, too_recent]}},
        )

    transport = httpx.MockTransport(handler)
    recover_calls: list[str] = []

    async def fake_recover(run_id: str, account_id: UUID, mapping: dict) -> dict:
        recover_calls.append(run_id)
        return {"items_fetched": 100, "persisted": 95}

    monkeypatch.setattr(watcher, "recover_run", fake_recover)

    async with httpx.AsyncClient(transport=transport) as client:
        recovered = await watcher._poll_once(
            state,
            client=client,
            token="t1",
            account_id=ACCOUNT_ID,
            url_to_prospect={},
            state_path=state_path,
            profile_actor_id=PROFILE_ACTOR_ID,
            min_age_seconds=300,
            list_limit=50,
            max_runs=10,
        )
    assert recovered == 1
    assert recover_calls == ["eligible1"]
    assert state.recovered_run_ids == {"eligible1"}
    # State persisted to disk before the recover_run call.
    persisted = json.loads(state_path.read_text())
    assert persisted == {"recovered": ["eligible1"]}


@pytest.mark.asyncio
async def test_poll_once_empty_returns_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = watcher.WatcherState()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"items": []}})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        watcher, "recover_run",
        lambda *a, **kw: pytest.fail("should not be called on empty cycle"),
    )

    async with httpx.AsyncClient(transport=transport) as client:
        recovered = await watcher._poll_once(
            state,
            client=client,
            token="t1",
            account_id=ACCOUNT_ID,
            url_to_prospect={},
            state_path=tmp_path / "state.json",
            profile_actor_id=PROFILE_ACTOR_ID,
            min_age_seconds=300,
            list_limit=50,
            max_runs=10,
        )
    assert recovered == 0


@pytest.mark.asyncio
async def test_poll_once_marks_recovered_before_calling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If recover_run crashes, the run_id is still marked → no re-fetch."""
    state = watcher.WatcherState()
    state_path = tmp_path / "state.json"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"items": [_run("crashy")]}},
        )

    transport = httpx.MockTransport(handler)

    async def crash(run_id: str, *_: Any, **__: Any) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(watcher, "recover_run", crash)

    async with httpx.AsyncClient(transport=transport) as client:
        await watcher._poll_once(
            state,
            client=client,
            token="t1",
            account_id=ACCOUNT_ID,
            url_to_prospect={},
            state_path=state_path,
            profile_actor_id=PROFILE_ACTOR_ID,
            min_age_seconds=0,  # bypass age filter for the test fixture's old timestamp
            list_limit=50,
            max_runs=10,
        )
    # Even though recover_run crashed, the run_id should be in the state file.
    assert "crashy" in state.recovered_run_ids
    persisted = json.loads(state_path.read_text())
    assert "crashy" in persisted["recovered"]


# ── CLI parser ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cli_default_account_id() -> None:
    parser = watcher._build_arg_parser()
    args = parser.parse_args([])
    assert args.account_id == UUID("00000000-0000-0000-0000-000000000001")
    assert args.interval == watcher.DEFAULT_INTERVAL_SECONDS
    assert args.max_hours == watcher.DEFAULT_MAX_HOURS
    # New default: 60 empty cycles before exit (was 3 — too aggressive).
    assert args.max_empty_cycles == watcher.EMPTY_CYCLE_EXIT_THRESHOLD == 60


@pytest.mark.unit
def test_cli_custom_overrides() -> None:
    parser = watcher._build_arg_parser()
    args = parser.parse_args([
        "--interval", "30",
        "--max-hours", "6",
        "--max-runs", "5",
        "--min-age-seconds", "120",
        "--max-empty-cycles", "999",
    ])
    assert args.interval == 30
    assert args.max_hours == 6.0
    assert args.max_runs == 5
    assert args.min_age_seconds == 120
    assert args.max_empty_cycles == 999
