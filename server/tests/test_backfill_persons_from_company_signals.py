"""Tests for ``scripts.backfill_persons_from_company_signals``.

All tests are mock-only — no live DB. The asyncpg-backed ``credence.db``
helpers (``fetch``, ``fetchrow``, ``execute``) are monkeypatched on the
module under test so we can capture exactly what SQL would have run.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from scripts import backfill_persons_from_company_signals as mod
from scripts.backfill_persons_from_company_signals import (
    DEFAULT_ACCOUNT_ID,
    BackfillStats,
    PersonMention,
    _canonicalize,
    _looks_like_person_name,
    extract_mentions,
    process_signal_row,
    run_backfill,
)


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def db_mock(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Replace ``fetch`` / ``fetchrow`` / ``execute`` with AsyncMocks.

    The mocks are also wired directly onto the module so internal helper
    callsites pick them up. Default returns: empty list / None / ''.
    """
    fetch_mock = AsyncMock(return_value=[])
    fetchrow_mock = AsyncMock(return_value=None)
    execute_mock = AsyncMock(return_value="UPDATE 1")
    monkeypatch.setattr(mod, "fetch", fetch_mock)
    monkeypatch.setattr(mod, "fetchrow", fetchrow_mock)
    monkeypatch.setattr(mod, "execute", execute_mock)
    return {
        "fetch": fetch_mock,
        "fetchrow": fetchrow_mock,
        "execute": execute_mock,
    }


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ─── Pure extraction tests ─────────────────────────────────────────────────


@pytest.mark.unit
def test_extract_top_level_exec_appointment() -> None:
    """Test 1 — extract (name, title, company) from a known signal shape."""
    value = {
        "name": "Wei Chen",
        "title": "Chief Revenue Officer",
        "company": "Acme Robotics",
        "announcement_date": "2026-04-01",
    }
    mentions = extract_mentions(value)
    assert len(mentions) == 1
    assert mentions[0].name == "Wei Chen"
    assert mentions[0].title == "Chief Revenue Officer"
    assert mentions[0].company == "Acme Robotics"


@pytest.mark.unit
def test_extract_skips_signal_without_person_mention() -> None:
    """Test 2 — non-person blob extracts no mentions."""
    # No ``name``-shaped key at any level.
    value = {
        "headline": "Acme Robotics announces new factory",
        "category": "expansion",
        "summary": "The company will open a new facility in Q3.",
    }
    assert extract_mentions(value) == []


@pytest.mark.unit
def test_extract_handles_nested_people_list() -> None:
    """Test 8 — multi-person press release with parent-company inheritance."""
    value = {
        "company": "NVIDIA Corporation",
        "headline": "Three new VPs at NVIDIA",
        "people": [
            {"name": "Alice Anderson", "title": "VP of Engineering"},
            {"name": "Bob Burke", "title": "VP of Marketing"},
            {"name": "Carol Chen", "title": "VP of Sales"},
        ],
    }
    mentions = extract_mentions(value)
    assert len(mentions) == 3
    names = {m.name for m in mentions}
    assert names == {"Alice Anderson", "Bob Burke", "Carol Chen"}
    # Parent company inherited where children omit it.
    for m in mentions:
        assert m.company == "NVIDIA Corporation"


@pytest.mark.unit
def test_extract_handles_malformed_jsonb_string() -> None:
    """Test 7 — malformed string value returns [] without raising."""
    # Non-JSON string — extract_mentions should swallow JSONDecodeError.
    assert extract_mentions("not-json-at-all") == []
    # Bytes / weird types.
    assert extract_mentions(123) == []
    assert extract_mentions(None) == []


@pytest.mark.unit
def test_extract_rejects_non_person_name_values() -> None:
    """Filter heuristic blocks 'Press Release' / 'CEO' from name slot."""
    assert _looks_like_person_name("Press Release") is False
    assert _looks_like_person_name("CEO") is False
    assert _looks_like_person_name("Acme Inc") is False
    assert _looks_like_person_name("Wei Chen") is True
    assert _looks_like_person_name("Dr. James Clarke") is True


@pytest.mark.unit
def test_canonicalize_normalizes_case() -> None:
    """Test 9 — 'Wei Chen' and 'wei chen' both produce a (first, last) pair."""
    upper = _canonicalize(PersonMention(name="Wei Chen", title=None, company=None))
    lower = _canonicalize(PersonMention(name="wei chen", title=None, company=None))
    assert upper is not None and lower is not None
    # Match key is case-insensitive at lookup time, so canonical strings can
    # differ in case but the comparison is still equal.
    assert upper.name.lower() == lower.name.lower()


# ─── Idempotency / writer tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_existing_person_not_re_inserted(db_mock: dict[str, AsyncMock]) -> None:
    """Test 3 — when persons already has the row, no INSERT fires."""
    person_id = uuid4()
    # _find_person is the first DB call (fetchrow). Return an existing row
    # whose current_title is already populated → goes to ``already_known``.
    db_mock["fetchrow"].return_value = {
        "id": person_id,
        "canonical_name": "Wei Chen",
        "current_title": "Chief Revenue Officer",
        "current_company_id": uuid4(),
    }
    stats = BackfillStats()
    row = {
        "id": uuid4(),
        "signal_type": "exec_appointment",
        "value": {
            "name": "Wei Chen",
            "title": "Chief Revenue Officer",
            "company": "Acme Robotics",
        },
    }
    await process_signal_row(
        row, stats, dry_run=False, account_id=DEFAULT_ACCOUNT_ID,
    )
    assert stats.already_known_skipped == 1
    assert stats.new_persons_inserted == 0
    assert stats.new_prospects_inserted == 0
    # No INSERTs / UPDATEs were issued.
    assert db_mock["execute"].await_count == 0


@pytest.mark.asyncio
async def test_title_backfill_when_null(db_mock: dict[str, AsyncMock]) -> None:
    """Test 4 — current_title NULL → UPDATE fires."""
    person_id = uuid4()
    db_mock["fetchrow"].return_value = {
        "id": person_id,
        "canonical_name": "Wei Chen",
        "current_title": None,            # ← NULL; backfill should fire
        "current_company_id": uuid4(),
    }
    stats = BackfillStats()
    row = {
        "id": uuid4(),
        "signal_type": "exec_appointment",
        "value": {
            "name": "Wei Chen",
            "title": "Chief Revenue Officer",
            "company": "Acme Robotics",
        },
    }
    await process_signal_row(
        row, stats, dry_run=False, account_id=DEFAULT_ACCOUNT_ID,
    )
    assert stats.title_backfills_updated == 1
    # Single UPDATE call; the SQL must guard with ``current_title IS NULL``.
    assert db_mock["execute"].await_count == 1
    sql_arg = db_mock["execute"].await_args.args[0]
    assert "current_title IS NULL" in sql_arg


@pytest.mark.asyncio
async def test_title_not_overwritten_when_set(db_mock: dict[str, AsyncMock]) -> None:
    """Test 5 — current_title already populated → never overwritten."""
    db_mock["fetchrow"].return_value = {
        "id": uuid4(),
        "canonical_name": "Wei Chen",
        "current_title": "VP of Engineering",  # ← already set
        "current_company_id": uuid4(),
    }
    stats = BackfillStats()
    row = {
        "id": uuid4(),
        "signal_type": "exec_appointment",
        "value": {
            "name": "Wei Chen",
            "title": "Chief Revenue Officer",  # ← would overwrite
            "company": "Acme Robotics",
        },
    }
    await process_signal_row(
        row, stats, dry_run=False, account_id=DEFAULT_ACCOUNT_ID,
    )
    assert stats.title_backfills_updated == 0
    assert stats.already_known_skipped == 1
    assert db_mock["execute"].await_count == 0


@pytest.mark.asyncio
async def test_dry_run_emits_no_insert(db_mock: dict[str, AsyncMock]) -> None:
    """Test 6 — dry-run mode never issues INSERTs or UPDATEs."""
    # No existing person → would normally create prospect + person.
    db_mock["fetchrow"].return_value = None
    stats = BackfillStats()
    row = {
        "id": uuid4(),
        "signal_type": "exec_appointment",
        "value": {
            "name": "Wei Chen",
            "title": "Chief Revenue Officer",
            "company": "Acme Robotics",
        },
    }
    await process_signal_row(
        row, stats, dry_run=True, account_id=DEFAULT_ACCOUNT_ID,
    )
    # Stats reflect the would-have-been work.
    assert stats.new_prospects_inserted == 1
    assert stats.new_persons_inserted == 1
    # But no writes were actually issued.
    assert db_mock["execute"].await_count == 0
    # _find_person reads via fetchrow — so a single SELECT is allowed,
    # but no INSERT-style fetchrow should fire. Easy proxy: only one
    # fetchrow call in the dry-run path.
    assert db_mock["fetchrow"].await_count == 1


@pytest.mark.asyncio
async def test_handles_malformed_jsonb_in_signal(
    db_mock: dict[str, AsyncMock],
) -> None:
    """Test 7 — malformed JSONB → counted as extraction failure, not raise."""
    stats = BackfillStats()
    # ``value`` is a string that isn't valid JSON. extract_mentions returns
    # [] in this case (not a raise), so it lands as 0 mentions, not an
    # extraction_failure. To exercise the failure path we monkeypatch
    # extract_mentions to raise.
    def raises(_value: Any) -> Any:
        raise json.JSONDecodeError("boom", "doc", 0)
    import scripts.backfill_persons_from_company_signals as m
    original = m.extract_mentions
    m.extract_mentions = raises  # type: ignore[assignment]
    try:
        await process_signal_row(
            {"id": uuid4(), "signal_type": "press_release", "value": "junk"},
            stats, dry_run=False, account_id=DEFAULT_ACCOUNT_ID,
        )
    finally:
        m.extract_mentions = original  # type: ignore[assignment]
    assert stats.extraction_failures == 1
    assert stats.mentions_extracted == 0


@pytest.mark.asyncio
async def test_multiple_persons_per_signal_value(
    db_mock: dict[str, AsyncMock],
) -> None:
    """Test 8 — a press release naming 3 people promotes all 3."""
    # Per mention we expect 4 fetchrow calls in this order:
    #   1) _find_person SELECT  → None (nobody known)
    #   2) _insert_prospect INSERT RETURNING → {"id": <uuid>}
    #   3) _ensure_company_id SELECT → {"id": <uuid>} (existing company hit)
    #   4) _insert_person INSERT RETURNING → {"id": <uuid>}
    db_mock["fetchrow"].side_effect = [
        None, {"id": uuid4()}, {"id": uuid4()}, {"id": uuid4()},  # Alice
        None, {"id": uuid4()}, {"id": uuid4()}, {"id": uuid4()},  # Bob
        None, {"id": uuid4()}, {"id": uuid4()}, {"id": uuid4()},  # Carol
    ]
    stats = BackfillStats()
    row = {
        "id": uuid4(),
        "signal_type": "press_release",
        "value": {
            "company": "NVIDIA",
            "people": [
                {"name": "Alice Anderson", "title": "VP Eng"},
                {"name": "Bob Burke", "title": "VP Marketing"},
                {"name": "Carol Chen", "title": "VP Sales"},
            ],
        },
    }
    await process_signal_row(
        row, stats, dry_run=False, account_id=DEFAULT_ACCOUNT_ID,
    )
    assert stats.mentions_extracted == 3
    # Each mention should produce one prospect + one person insert.
    assert stats.new_persons_inserted == 3
    assert stats.new_prospects_inserted == 3


@pytest.mark.asyncio
async def test_account_id_populated_on_writes(
    db_mock: dict[str, AsyncMock],
) -> None:
    """Test 10 — every INSERT carries the default-tenant account_id."""
    prospect_id = uuid4()
    company_id = uuid4()
    person_id = uuid4()
    db_mock["fetchrow"].side_effect = [
        None,                                      # _find_person → no match
        {"id": prospect_id},                       # _insert_prospect RETURNING
        {"id": company_id},                        # _ensure_company_id SELECT
        {"id": person_id},                         # _insert_person RETURNING
    ]
    stats = BackfillStats()
    row = {
        "id": uuid4(),
        "signal_type": "exec_appointment",
        "value": {
            "name": "Wei Chen",
            "title": "Chief Revenue Officer",
            "company": "Acme Robotics",
        },
    }
    await process_signal_row(
        row, stats, dry_run=False, account_id=DEFAULT_ACCOUNT_ID,
    )
    # Filter to INSERT calls only — those are the writes the task spec
    # requires to carry account_id. Read-only SELECTs (like the company
    # lookup before insert) are not required to.
    insert_calls = [
        c for c in db_mock["fetchrow"].await_args_list
        if "INSERT INTO" in c.args[0]
    ]
    assert len(insert_calls) >= 2, "expected ≥2 INSERTs (prospect + person)"
    for call in insert_calls:
        assert any(
            arg == DEFAULT_ACCOUNT_ID for arg in call.args
        ), f"account_id missing from call: {call.args}"


# ─── End-to-end glue test ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_backfill_collects_stats(
    db_mock: dict[str, AsyncMock],
) -> None:
    """``run_backfill`` correctly threads SELECT → process loop → stats."""
    # Single signal in the candidate pool.
    db_mock["fetch"].return_value = [
        {
            "id": uuid4(),
            "signal_type": "exec_appointment",
            "value": {
                "name": "Wei Chen",
                "title": "CRO",
                "company": "Acme Robotics",
            },
            "prospect_id": None,
        },
    ]
    # No existing person → dry-run treats as "would insert".
    db_mock["fetchrow"].return_value = None
    stats = await run_backfill(dry_run=True, limit=10)
    assert stats.signals_scanned == 1
    assert stats.new_persons_inserted == 1
    assert stats.new_prospects_inserted == 1
    assert "exec_appointment" in stats.by_signal_type
