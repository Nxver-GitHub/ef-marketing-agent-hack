"""Tests for bulk_conference_talk_extraction — the per-account
conference co-presenter runner.

Pure-function unit coverage + a fake-conn end-to-end exercising the
full algorithm. No live DB.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from credence.jobs import bulk_conference_talk_extraction as job


# ── Fixtures ────────────────────────────────────────────────────────────────


ACCOUNT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
P1 = UUID("00000000-0000-0000-0000-000000000001")
P2 = UUID("00000000-0000-0000-0000-000000000002")
P3 = UUID("00000000-0000-0000-0000-000000000003")
P4 = UUID("00000000-0000-0000-0000-000000000004")


def _signal_value(
    event: str,
    year: Any,
    title: str = "A talk",
    url: str = "https://example.com/x",
) -> dict[str, Any]:
    return {"event": event, "year": year, "title": title, "url": url}


# ── _normalize_event_name ───────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("RSA Conference 2022", "rsa conference"),
        ("Black Hat USA 2023", "black hat usa"),
        ("NeurIPS 2024", "neurips"),
        ("  DEF CON 30  ", "def con 30"),
        ("ACM CCS 2021", "acm ccs"),
        ("RSA Conference 2022 ", "rsa conference"),
        ("rsa conference 2022", "rsa conference"),
        ("RSA Conference", "rsa conference"),
        ("", ""),
        ("   ", ""),
        ("2022", ""),
    ],
)
def test_normalize_event_name(raw: str, expected: str) -> None:
    assert job._normalize_event_name(raw) == expected


# ── _parse_year ─────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2022", 2022),
        ("2024", 2024),
        (2023, 2023),
        ("RSA 2022", 2022),
        (1989, None),       # below MIN
        (2031, None),       # above MAX
        ("garbage", None),
        ("", None),
        (None, None),
        (2024.0, 2024),
        (True, None),
    ],
)
def test_parse_year(raw: Any, expected: int | None) -> None:
    assert job._parse_year(raw) == expected


# ── _entry_from_row ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_entry_from_row_happy_path() -> None:
    row = job.ConferenceTalkRow(
        id=UUID(int=10),
        prospect_id=P1,
        event_raw="RSA Conference 2022",
        year_raw="2022",
        title="Supply Chain Risks",
        url="https://example.com",
    )
    entry = job._entry_from_row(row)
    assert entry is not None
    assert entry.prospect_id == P1
    assert entry.event_canonical == "rsa conference"
    assert entry.year == 2022
    assert entry.title == "Supply Chain Risks"


@pytest.mark.unit
def test_entry_from_row_empty_event_returns_none() -> None:
    row = job.ConferenceTalkRow(
        id=UUID(int=10), prospect_id=P1, event_raw="",
        year_raw="2022", title="x", url=None,
    )
    assert job._entry_from_row(row) is None


@pytest.mark.unit
def test_entry_from_row_bad_year_returns_none() -> None:
    row = job.ConferenceTalkRow(
        id=UUID(int=10), prospect_id=P1, event_raw="RSA",
        year_raw="banana", title="x", url=None,
    )
    assert job._entry_from_row(row) is None


# ── _build_index ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_build_index_dedupes_same_prospect_same_group() -> None:
    rows = [
        job.ConferenceTalkRow(
            id=UUID(int=10), prospect_id=P1,
            event_raw="RSA Conference 2022", year_raw="2022",
            title="Talk A", url=None,
        ),
        job.ConferenceTalkRow(
            id=UUID(int=11), prospect_id=P1,
            event_raw="RSA Conference 2022", year_raw="2022",
            title="Talk B", url=None,
        ),
    ]
    index, _ = job._build_index(rows)
    assert len(index[("rsa conference", 2022)]) == 1


@pytest.mark.unit
def test_build_index_counts_unparseable() -> None:
    rows = [
        job.ConferenceTalkRow(
            id=UUID(int=10), prospect_id=P1,
            event_raw="", year_raw="2022", title="x", url=None,
        ),
        job.ConferenceTalkRow(
            id=UUID(int=11), prospect_id=P2,
            event_raw="RSA", year_raw="garbage", title="x", url=None,
        ),
    ]
    _, unparseable = job._build_index(rows)
    assert unparseable == 2


# ── _pairs_from_index ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_pairs_from_index_skips_singletons() -> None:
    e1 = job.TalkEntry(P1, "rsa conference", 2022, "RSA 2022", "x")
    pairs = list(job._pairs_from_index({("rsa conference", 2022): [e1]}))
    assert pairs == []


@pytest.mark.unit
def test_pairs_from_index_four_prospect_group_yields_six_pairs() -> None:
    entries = [
        job.TalkEntry(P1, "rsa conference", 2022, "RSA 2022", "a"),
        job.TalkEntry(P2, "rsa conference", 2022, "RSA 2022", "b"),
        job.TalkEntry(P3, "rsa conference", 2022, "RSA 2022", "c"),
        job.TalkEntry(P4, "rsa conference", 2022, "RSA 2022", "d"),
    ]
    pairs = list(
        job._pairs_from_index({("rsa conference", 2022): entries})
    )
    # 4 choose 2 = 6
    assert len(pairs) == 6
    for a, b in pairs:
        assert a.prospect_id < b.prospect_id
    pair_ids = {(a.prospect_id, b.prospect_id) for a, b in pairs}
    assert pair_ids == {
        (P1, P2), (P1, P3), (P1, P4),
        (P2, P3), (P2, P4),
        (P3, P4),
    }


@pytest.mark.unit
def test_pairs_from_index_separates_groups_by_year() -> None:
    e1 = job.TalkEntry(P1, "rsa conference", 2022, "RSA 2022", "a")
    e2 = job.TalkEntry(P2, "rsa conference", 2023, "RSA 2023", "b")
    pairs = list(job._pairs_from_index({
        ("rsa conference", 2022): [e1],
        ("rsa conference", 2023): [e2],
    }))
    assert pairs == []


# ── _signal_exists ──────────────────────────────────────────────────────────


class _RecordingConn:
    def __init__(self, fetchval_return: Any = None) -> None:
        self._fetchval_return = fetchval_return
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.fetchval_calls.append((sql, args))
        if callable(self._fetchval_return):
            return self._fetchval_return(sql, args)
        return self._fetchval_return

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.fetch_calls.append((sql, args))
        return []


@pytest.mark.unit
async def test_signal_exists_query_well_formed() -> None:
    conn = _RecordingConn(fetchval_return=None)
    result = await job._signal_exists(
        conn,  # type: ignore[arg-type]
        P1,
        "conference_co_presenter",
        "rsa conference",
        2022,
        str(P2),
    )
    assert result is False
    assert len(conn.fetchval_calls) == 1
    sql, args = conn.fetchval_calls[0]
    assert "FROM signals" in sql
    assert "value->>'event_normalized'" in sql
    assert "value->>'year'" in sql
    assert "value->>'connected_to'" in sql
    assert "LIMIT 1" in sql
    assert args == (P1, "conference_co_presenter", "rsa conference", "2022", str(P2))


@pytest.mark.unit
async def test_signal_exists_returns_true_when_present() -> None:
    conn = _RecordingConn(fetchval_return=1)
    assert await job._signal_exists(
        conn,  # type: ignore[arg-type]
        P1, "conference_co_presenter", "rsa conference", 2022, str(P2),
    ) is True


# ── End-to-end with patched acquire ─────────────────────────────────────────


class _FakeAcquire:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _RecordingConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


@pytest.fixture
def patched_acquire(monkeypatch: pytest.MonkeyPatch):
    state: dict[str, Any] = {"conns": [], "exists_keys": set(), "rows": []}

    def make_conn() -> _RecordingConn:
        def fetchval_router(sql: str, args: tuple[Any, ...]) -> Any:
            if "FROM signals" in sql:
                key = tuple(args)
                if key in state["exists_keys"]:
                    return 1
                state["exists_keys"].add(key)
                return None
            return None

        conn = _RecordingConn(fetchval_return=fetchval_router)

        async def fake_fetch(sql: str, *args: Any) -> list[Any]:
            conn.fetch_calls.append((sql, args))
            if "FROM signals" in sql:
                return state["rows"]
            return []

        conn.fetch = fake_fetch  # type: ignore[method-assign]
        state["conns"].append(conn)
        return conn

    def fake_acquire() -> _FakeAcquire:
        return _FakeAcquire(make_conn())

    monkeypatch.setattr(job, "acquire", fake_acquire)
    return state


def _row(prospect_id: UUID, event: str, year: Any, title: str = "A talk") -> dict[str, Any]:
    """asyncpg-row shaped dict — value comes out as a JSON string."""
    return {
        "id": UUID(int=int(prospect_id) + 1000),
        "prospect_id": prospect_id,
        "value": json.dumps(_signal_value(event, year, title)),
    }


@pytest.mark.unit
async def test_end_to_end_emits_co_presenter_pair(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["rows"] = [
        _row(P1, "RSA Conference 2022", "2022", "Supply Chain Risks"),
        _row(P2, "RSA Conference 2022", "2022", "Zero Trust"),
    ]
    rollup = await job.bulk_conference_talk_extraction_account(ACCOUNT_ID)
    assert rollup.talks_read == 2
    assert rollup.event_groups == 1
    assert rollup.pairs_emitted == 1
    assert rollup.signals_inserted == 1
    assert rollup.signals_skipped_dedup == 0

    write_conn = patched_acquire["conns"][1]
    inserts = [c for c in write_conn.execute_calls if "INSERT INTO signals" in c[0]]
    assert len(inserts) == 1
    _, args = inserts[0]
    # args order: prospect_id, account_id, source, signal_type, json, confidence
    assert args[0] == P1
    assert args[1] == ACCOUNT_ID
    assert args[2] == job.SIGNAL_SOURCE
    assert args[3] == "conference_co_presenter"
    # args[4] is the dict passed directly to asyncpg's jsonb codec — NOT
    # json.dumps. Catches the regression that double-encodes structured_value.
    assert isinstance(args[4], dict)
    assert args[4]["event_normalized"] == "rsa conference"
    assert args[4]["year"] == 2022
    assert args[4]["connected_to"] == str(P2)
    assert args[4]["title_a"] == "Supply Chain Risks"
    assert args[4]["title_b"] == "Zero Trust"
    assert isinstance(args[5], float)
    assert 0.0 < args[5] < 1.0


@pytest.mark.unit
async def test_end_to_end_four_prospect_group_emits_six_pairs(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["rows"] = [
        _row(P1, "Black Hat USA 2023", 2023),
        _row(P2, "Black Hat USA 2023", 2023),
        _row(P3, "Black Hat USA 2023", 2023),
        _row(P4, "Black Hat USA 2023", 2023),
    ]
    rollup = await job.bulk_conference_talk_extraction_account(ACCOUNT_ID)
    # 4 choose 2 = 6
    assert rollup.event_groups == 1
    assert rollup.pairs_emitted == 6
    assert rollup.signals_inserted == 6


@pytest.mark.unit
async def test_end_to_end_dry_run_writes_nothing(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["rows"] = [
        _row(P1, "NeurIPS 2024", 2024),
        _row(P2, "NeurIPS 2024", 2024),
    ]
    rollup = await job.bulk_conference_talk_extraction_account(
        ACCOUNT_ID, dry_run=True,
    )
    assert rollup.dry_run is True
    assert rollup.pairs_emitted == 1
    assert rollup.signals_inserted == 0
    # Only one acquire (read), no second acquire for writes.
    assert len(patched_acquire["conns"]) == 1


@pytest.mark.unit
async def test_end_to_end_rerun_dedupes(patched_acquire: dict[str, Any]) -> None:
    patched_acquire["rows"] = [
        _row(P1, "RSA Conference 2022", 2022),
        _row(P2, "RSA Conference 2022", 2022),
    ]
    first = await job.bulk_conference_talk_extraction_account(ACCOUNT_ID)
    second = await job.bulk_conference_talk_extraction_account(ACCOUNT_ID)
    assert first.signals_inserted == 1
    assert second.signals_inserted == 0
    assert second.signals_skipped_dedup == 1


@pytest.mark.unit
async def test_end_to_end_different_years_no_pairs(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["rows"] = [
        _row(P1, "RSA Conference 2022", 2022),
        _row(P2, "RSA Conference 2023", 2023),
    ]
    rollup = await job.bulk_conference_talk_extraction_account(ACCOUNT_ID)
    assert rollup.pairs_emitted == 0
    assert rollup.signals_inserted == 0


@pytest.mark.unit
async def test_end_to_end_normalizes_event_with_trailing_year(
    patched_acquire: dict[str, Any],
) -> None:
    """'RSA Conference 2022' and 'RSA Conference' both → 'rsa conference'."""
    patched_acquire["rows"] = [
        _row(P1, "RSA Conference 2022", 2022),
        _row(P2, "RSA Conference", 2022),
    ]
    rollup = await job.bulk_conference_talk_extraction_account(ACCOUNT_ID)
    assert rollup.pairs_emitted == 1


# ── CLI ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cli_requires_scope() -> None:
    parser = job._build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


@pytest.mark.unit
def test_cli_parses_account_id_path() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(
        ["--account-id", str(ACCOUNT_ID), "--limit", "10", "--dry-run"]
    )
    assert args.account_id == ACCOUNT_ID
    assert args.limit == 10
    assert args.dry_run is True
    assert args.all_accounts is False


@pytest.mark.unit
def test_cli_parses_all_accounts_path() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(["--all-accounts"])
    assert args.all_accounts is True
    assert args.account_id is None
