"""Tests for `credence.score_runner` — Contract 6 dual-write persistence.

Mirrors the ``test_signals.py`` / ``test_enrich.py`` pattern: monkeypatches
the module-level ``fetch``, ``fetchrow``, and ``acquire`` so tests don't
need a live Postgres pool. A state dict captures both INSERTs (legacy
``scores`` and v3 ``score_records``) for assertion.

Coverage:
1. Dual-write — both INSERTs hit, both with the same account_id from prospects
2. weight_version_id propagated from active score_weights row
3. score_records.falsification_note non-empty (CHECK guard)
4. Legacy scores.falsification_notes stays TEXT[] (preserves v2 read shape)
5. Missing prospect → ScoreSetupError, no writes
6. Missing active weight version → ScoreSetupError, no writes
7. ScoreResult still returned to caller (worker / dry-run consumers)
8. Caller-supplied weights skip the signal_weights query
9. account_id source-of-truth — prospect's account_id wins over any caller hint
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from credence import score_runner
from credence.score_runner import ScoreSetupError

PROSPECT_ID = UUID("00000000-0000-0000-0000-aaaa00000001")
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")
WEIGHT_VERSION_ID = UUID("00000000-0000-0000-0000-bbbb00000001")


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def stub_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Programmable shim over score_runner's DB helpers.

    state["prospect_account"]      — the account_id returned for prospects lookup
    state["active_version"]        — the score_weights id returned
    state["signal_weights"]        — rows for load_weights()
    state["signals"]               — rows for the signals fetch
    state["writes_scores"]         — captured args from INSERT INTO scores
    state["writes_score_records"]  — captured args from INSERT INTO score_records
    state["missing_prospect"]      — flip True to simulate prospect not found
    state["missing_active_version"]— flip True to simulate seed gap
    """
    state: dict[str, Any] = {
        "prospect_account": ACCOUNT_ID,
        "active_version": WEIGHT_VERSION_ID,
        "signal_weights": [
            {
                "signal_type": "tenure_years",
                "authenticity_weight": 1.0,
                "authority_weight": 0.5,
                "warmth_weight": 0.0,
            },
        ],
        "signals": [
            {"signal_type": "tenure_years", "value": 5, "weight": 1.0, "confidence": 0.9},
        ],
        "writes_scores": [],
        "writes_score_records": [],
        "missing_prospect": False,
        "missing_active_version": False,
        "load_weights_calls": 0,
    }

    async def fake_fetchrow(sql: str, *args: Any) -> Any:
        sql_norm = " ".join(sql.split()).upper()
        if "FROM PROSPECTS WHERE ID" in sql_norm:
            if state["missing_prospect"]:
                return None
            return {"account_id": state["prospect_account"]}
        if "FROM SCORE_WEIGHTS" in sql_norm and "IS_ACTIVE = TRUE" in sql_norm:
            if state["missing_active_version"]:
                return None
            return {"id": state["active_version"]}
        return None

    async def fake_fetch(sql: str, *args: Any) -> list[dict]:
        sql_upper = sql.upper()
        if "FROM SIGNAL_WEIGHTS" in sql_upper:
            state["load_weights_calls"] += 1
            return [dict(r) for r in state["signal_weights"]]
        if "FROM SIGNALS" in sql_upper and "PROSPECT_ID" in sql_upper:
            return [dict(r) for r in state["signals"]]
        return []

    class _FakeTx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_a: Any) -> None:
            return None

    class _FakeConn:
        def transaction(self) -> _FakeTx:
            return _FakeTx()

        async def execute(self, sql: str, *args: Any) -> str:
            sql_upper = sql.upper()
            if "INSERT INTO SCORES" in sql_upper and "SCORE_RECORDS" not in sql_upper:
                state["writes_scores"].append(args)
            elif "INSERT INTO SCORE_RECORDS" in sql_upper:
                state["writes_score_records"].append(args)
            return "ok"

    class _FakeAcquireCtx:
        async def __aenter__(self) -> _FakeConn:
            return _FakeConn()

        async def __aexit__(self, *_a: Any) -> None:
            return None

    def fake_acquire() -> _FakeAcquireCtx:
        return _FakeAcquireCtx()

    monkeypatch.setattr(score_runner, "fetch", fake_fetch)
    monkeypatch.setattr(score_runner, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(score_runner, "acquire", fake_acquire)
    return state


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dual_write_propagates_account_id(stub_db) -> None:
    """Both INSERTs receive the prospect's account_id as their first arg."""
    await score_runner.score_prospect(PROSPECT_ID)

    assert len(stub_db["writes_scores"]) == 1
    assert len(stub_db["writes_score_records"]) == 1

    scores_args = stub_db["writes_scores"][0]
    assert scores_args[0] == ACCOUNT_ID
    assert scores_args[1] == PROSPECT_ID

    records_args = stub_db["writes_score_records"][0]
    assert records_args[0] == ACCOUNT_ID
    assert records_args[1] == PROSPECT_ID


@pytest.mark.unit
@pytest.mark.asyncio
async def test_score_records_uses_active_weight_version(stub_db) -> None:
    """The active weight_version_id from score_weights is propagated."""
    custom_version = UUID("00000000-0000-0000-0000-bbbb00000002")
    stub_db["active_version"] = custom_version

    await score_runner.score_prospect(PROSPECT_ID)

    records_args = stub_db["writes_score_records"][0]
    # signature: (account_id, prospect_id, weight_version_id, ...)
    assert records_args[2] == custom_version


@pytest.mark.unit
@pytest.mark.asyncio
async def test_falsification_note_non_empty_string(stub_db) -> None:
    """score_records.falsification_note must satisfy length(trim(...)) > 0."""
    await score_runner.score_prospect(PROSPECT_ID)

    records_args = stub_db["writes_score_records"][0]
    # signature: (..., authenticity, authority, warmth, overall, falsification_note)
    falsification_note = records_args[7]
    assert isinstance(falsification_note, str)
    assert falsification_note.strip()
    # Joined from the four canonical notes — newlines between them.
    assert "\n" in falsification_note
    assert falsification_note.count("\n") == 3  # 4 notes → 3 separators


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_scores_keeps_falsification_notes_list(stub_db) -> None:
    """Legacy scores.falsification_notes stays TEXT[] for v2-read parity."""
    await score_runner.score_prospect(PROSPECT_ID)

    scores_args = stub_db["writes_scores"][0]
    # signature: (account_id, prospect_id, auth, authority, warmth, overall, notes)
    notes = scores_args[6]
    assert isinstance(notes, list)
    assert len(notes) == 4
    assert all(isinstance(n, str) and n.strip() for n in notes)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_prospect_raises_setup_error(stub_db) -> None:
    """ScoreSetupError when the prospect row is missing — no writes happen."""
    stub_db["missing_prospect"] = True

    with pytest.raises(ScoreSetupError, match=r"prospect.*not found"):
        await score_runner.score_prospect(PROSPECT_ID)

    assert stub_db["writes_scores"] == []
    assert stub_db["writes_score_records"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_active_version_raises_setup_error(stub_db) -> None:
    """ScoreSetupError when the tenant has no active score_weights — no writes."""
    stub_db["missing_active_version"] = True

    with pytest.raises(ScoreSetupError, match="no active score_weights"):
        await score_runner.score_prospect(PROSPECT_ID)

    assert stub_db["writes_scores"] == []
    assert stub_db["writes_score_records"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_score_result_for_caller(stub_db) -> None:
    """Function still returns the computed ScoreResult (worker reads return)."""
    result = await score_runner.score_prospect(PROSPECT_ID)

    assert hasattr(result, "authenticity_score")
    assert hasattr(result, "authority_score")
    assert hasattr(result, "warmth_score")
    assert hasattr(result, "overall_score")
    assert isinstance(result.falsification_notes, list)
    assert len(result.falsification_notes) == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_caller_provided_weights_skips_load(stub_db) -> None:
    """When weights are passed in, signal_weights table isn't queried."""
    custom_weights = [
        {
            "signal_type": "patent_count",
            "authenticity_weight": 0.5,
            "authority_weight": 1.0,
            "warmth_weight": 0.0,
        },
    ]
    await score_runner.score_prospect(PROSPECT_ID, weights=custom_weights)

    assert stub_db["load_weights_calls"] == 0
    assert len(stub_db["writes_scores"]) == 1
    assert len(stub_db["writes_score_records"]) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_account_id_sourced_from_prospect_not_caller(stub_db) -> None:
    """The persisted account_id is the prospect's, not anything caller-controlled.

    Both INSERTs must use prospects.account_id even if a hypothetical caller
    were trying to write a score against a prospect from another tenant —
    the route layer enforces caller authorization separately.
    """
    other_account = UUID("00000000-0000-0000-0000-000000000fff")
    stub_db["prospect_account"] = other_account

    await score_runner.score_prospect(PROSPECT_ID)

    assert stub_db["writes_scores"][0][0] == other_account
    assert stub_db["writes_score_records"][0][0] == other_account
