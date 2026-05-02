"""Tests for `GET /score/{prospect_id}` — Contract 6 lazy-recompute route.

The route consumes SwiftElk's score_runner persistence (writes to
`score_records`) but is independently testable: we monkeypatch `fetchrow`
and `score_prospect` so the test never touches Postgres.

Coverage:
1. Cache hit: `score_records` already has a row for (prospect_id, active_version)
   → return as-is, recomputed=False, score_prospect NOT called.
2. Cache miss: empty score_records → call score_prospect, re-fetch, return,
   recomputed=True.
3. Prospect not found → 404.
4. No active weight version for tenant → 503.
5. Writer post-condition violation: score_prospect runs but score_records
   still has no row → 500.
6. Multi-row cutover safety: ORDER BY computed_at DESC picks newest.
7. Response field shape — every Contract 6 field present and typed.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from credence import api as api_module
from credence.api import app

PROSPECT_ID = UUID("00000000-0000-0000-0000-bbbb00000001")
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")
WEIGHT_VERSION_ID = UUID("00000000-0000-0000-0000-cccc00000001")


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Credence-Demo": "true"},
    ) as c:
        yield c


@pytest.fixture
def stub_score_route(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub fetchrow + score_prospect with a programmable state machine.

    state["prospect"] — None means 404; dict means present.
    state["weight"]   — None means 503; dict means active version exists.
    state["score_records"] — list[dict]; the most-recent matching row wins.
    state["score_prospect_calls"] — counter tracking compute invocations.
    state["score_prospect_writes_record"] — when True, a successful
        score_prospect appends to state["score_records"].
    """
    state: dict[str, Any] = {
        "prospect": {"account_id": ACCOUNT_ID},
        "weight": {"id": WEIGHT_VERSION_ID},
        "score_records": [],
        "score_prospect_calls": 0,
        "score_prospect_writes_record": True,
        "fresh_score_payload": {
            "authenticity_score": 80.0,
            "authority_score": 70.0,
            "warmth_score": 50.0,
            "overall_score": 70.0,
            "falsification_note": "Lin Wei may have left TSMC since last enrichment.",
        },
    }

    async def fake_fetchrow(sql: str, *args: Any) -> Any:
        sql_norm = " ".join(sql.split()).upper()
        if "FROM PROSPECTS WHERE ID" in sql_norm:
            return state["prospect"]
        if "FROM SCORE_WEIGHTS" in sql_norm:
            return state["weight"]
        if "FROM SCORE_RECORDS" in sql_norm:
            # The route passes (prospect_id, weight_version_id); pick newest match.
            pid, wvid = args[0], args[1]
            matching = [
                r for r in state["score_records"]
                if r["prospect_id"] == pid and r["weight_version_id"] == wvid
            ]
            if not matching:
                return None
            return max(matching, key=lambda r: r["computed_at"])
        return None

    async def fake_score_prospect(prospect_id: UUID) -> Any:
        state["score_prospect_calls"] += 1
        if state["score_prospect_writes_record"]:
            state["score_records"].append({
                "prospect_id": prospect_id,
                "weight_version_id": WEIGHT_VERSION_ID,
                "computed_at": datetime.now(tz=UTC),
                **state["fresh_score_payload"],
            })

        # Match the legacy ScoreResult shape so the existing POST /score
        # route (which we don't touch) keeps working in this test module.
        class _Result:
            authenticity_score = state["fresh_score_payload"]["authenticity_score"]
            authority_score = state["fresh_score_payload"]["authority_score"]
            warmth_score = state["fresh_score_payload"]["warmth_score"]
            overall_score = state["fresh_score_payload"]["overall_score"]
            falsification_notes = [state["fresh_score_payload"]["falsification_note"]]  # noqa: RUF012 — test stub re-instantiated per fixture
        return _Result()

    monkeypatch.setattr(api_module, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(api_module, "score_prospect", fake_score_prospect)
    return state


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_cache_hit_returns_existing_record(client, stub_score_route) -> None:
    cached_at = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)
    stub_score_route["score_records"].append({
        "prospect_id": PROSPECT_ID,
        "weight_version_id": WEIGHT_VERSION_ID,
        "computed_at": cached_at,
        "authenticity_score": 75.0,
        "authority_score": 65.0,
        "warmth_score": 45.0,
        "overall_score": 65.0,
        "falsification_note": "cached note",
    })

    resp = await client.get(f"/score/{PROSPECT_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["prospect_id"] == str(PROSPECT_ID)
    assert body["weight_version_id"] == str(WEIGHT_VERSION_ID)
    assert body["authenticity_score"] == 75.0
    assert body["overall_score"] == 65.0
    assert body["falsification_note"] == "cached note"
    assert body["recomputed"] is False
    assert stub_score_route["score_prospect_calls"] == 0


@pytest.mark.unit
async def test_cache_miss_triggers_recompute(client, stub_score_route) -> None:
    assert stub_score_route["score_records"] == []

    resp = await client.get(f"/score/{PROSPECT_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["recomputed"] is True
    assert body["weight_version_id"] == str(WEIGHT_VERSION_ID)
    # Score values come from the fresh_score_payload fixture.
    assert body["authenticity_score"] == 80.0
    assert body["authority_score"] == 70.0
    assert body["overall_score"] == 70.0
    assert stub_score_route["score_prospect_calls"] == 1


@pytest.mark.unit
async def test_prospect_not_found_returns_404(client, stub_score_route) -> None:
    stub_score_route["prospect"] = None
    resp = await client.get(f"/score/{PROSPECT_ID}")
    assert resp.status_code == 404
    assert "prospect not found" in resp.json()["detail"]
    assert stub_score_route["score_prospect_calls"] == 0


@pytest.mark.unit
async def test_no_active_weight_returns_503(client, stub_score_route) -> None:
    stub_score_route["weight"] = None
    resp = await client.get(f"/score/{PROSPECT_ID}")
    assert resp.status_code == 503
    assert "no active score_weights" in resp.json()["detail"]
    assert stub_score_route["score_prospect_calls"] == 0


@pytest.mark.unit
async def test_writer_no_op_returns_500(client, stub_score_route) -> None:
    """If score_prospect runs but score_records still has no matching row,
    this is a contract violation between the route and score_runner — surface
    as 500 rather than returning a half-formed response."""
    stub_score_route["score_prospect_writes_record"] = False

    resp = await client.get(f"/score/{PROSPECT_ID}")
    assert resp.status_code == 500
    assert "did not write" in resp.json()["detail"]
    assert stub_score_route["score_prospect_calls"] == 1


@pytest.mark.unit
async def test_multiple_rows_pick_newest(client, stub_score_route) -> None:
    """During the dual-write cutover window, score_runner may briefly produce
    multiple rows for the same (prospect, version). Route must pick newest."""
    older = datetime(2026, 4, 30, 10, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 4, 30, 14, 0, 0, tzinfo=UTC)

    stub_score_route["score_records"].extend([
        {
            "prospect_id": PROSPECT_ID,
            "weight_version_id": WEIGHT_VERSION_ID,
            "computed_at": older,
            "authenticity_score": 50.0, "authority_score": 50.0,
            "warmth_score": 50.0, "overall_score": 50.0,
            "falsification_note": "old",
        },
        {
            "prospect_id": PROSPECT_ID,
            "weight_version_id": WEIGHT_VERSION_ID,
            "computed_at": newer,
            "authenticity_score": 90.0, "authority_score": 90.0,
            "warmth_score": 90.0, "overall_score": 90.0,
            "falsification_note": "new",
        },
    ])

    resp = await client.get(f"/score/{PROSPECT_ID}")
    body = resp.json()
    assert body["falsification_note"] == "new"
    assert body["overall_score"] == 90.0
    assert body["recomputed"] is False


@pytest.mark.unit
async def test_response_shape_complete(client, stub_score_route) -> None:
    """Lock the response field set so frontend consumers don't get surprised."""
    resp = await client.get(f"/score/{PROSPECT_ID}")
    body = resp.json()
    expected_fields = {
        "prospect_id", "weight_version_id",
        "authenticity_score", "authority_score", "warmth_score", "overall_score",
        "falsification_note", "computed_at", "recomputed",
    }
    assert set(body.keys()) == expected_fields
