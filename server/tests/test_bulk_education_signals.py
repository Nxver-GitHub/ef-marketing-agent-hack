"""Tests for bulk_education_signals — the per-account education-cohort runner.

Pure-function unit coverage + a fake-conn end-to-end exercising the full
algorithm. No live DB.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from credence.jobs import bulk_education_signals as job


# ── Fixtures ────────────────────────────────────────────────────────────────


ACCOUNT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
P1 = UUID("00000000-0000-0000-0000-000000000001")
P2 = UUID("00000000-0000-0000-0000-000000000002")
P3 = UUID("00000000-0000-0000-0000-000000000003")
P4 = UUID("00000000-0000-0000-0000-000000000004")


def _signal_value(degrees: list[dict[str, Any]]) -> dict[str, Any]:
    return {"degrees": degrees}


# ── _classify_degree ────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MBA", "mba"),
        ("Master of Business Administration", "mba"),
        ("PhD", "phd"),
        ("Ph.D. in Computer Science", "phd"),
        ("Doctor of Philosophy", "phd"),
        ("Doctorate", "phd"),
        ("Executive Management Program", "exec_ed"),
        ("Executive Education", "exec_ed"),
        ("Bachelor of Science", "undergrad"),
        ("B.S.", "undergrad"),
        ("BA", "undergrad"),
        ("Undergraduate", "undergrad"),
        ("Master of Science", "masters"),
        ("M.S.", "masters"),
        ("MS", "masters"),
        ("", None),
        ("   ", None),
        ("Erasmus Program", None),
        ("Some Random Cert", None),
    ],
)
def test_classify_degree(raw: str, expected: str | None) -> None:
    assert job._classify_degree(raw) == expected


# ── _normalize_school ───────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MIT", "massachusetts institute of technology"),
        ("UCLA", "university of california, los angeles"),
        ("USC", "university of southern california"),
        ("Harvard Business School", "harvard university"),
        ("The University of Texas", "university of texas"),
        ("  Cornell University  ", "cornell university"),
        ("Stanford University.", "stanford university"),
        ("UC Berkeley", "university of california, berkeley"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_school(raw: str, expected: str) -> None:
    assert job._normalize_school(raw) == expected


# ── _cohort_signal_type ─────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "kind,expected",
    [
        ("mba", "same_mba_cohort"),
        ("phd", "same_phd_program"),
        ("exec_ed", "executive_education"),
        ("undergrad", "same_undergrad_cohort"),
        ("masters", None),
        ("garbage", None),
    ],
)
def test_cohort_signal_type(kind: str, expected: str | None) -> None:
    assert job._cohort_signal_type(kind) == expected


# ── _entry_from_degree ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_entry_from_degree_happy_path() -> None:
    entry = job._entry_from_degree(
        P1,
        {"school": "MIT", "degree": "Ph.D.", "field": "EE"},
    )
    assert entry is not None
    assert entry.prospect_id == P1
    assert entry.school_canonical == "massachusetts institute of technology"
    assert entry.degree_kind == "phd"
    assert entry.field == "EE"
    assert entry.degree_raw == "Ph.D."
    assert entry.school_raw == "MIT"


@pytest.mark.unit
def test_entry_from_degree_unclassifiable_returns_none() -> None:
    assert job._entry_from_degree(
        P1, {"school": "MIT", "degree": "Erasmus Program", "field": ""}
    ) is None


@pytest.mark.unit
def test_entry_from_degree_empty_school_returns_none() -> None:
    assert job._entry_from_degree(
        P1, {"school": "", "degree": "MBA", "field": ""}
    ) is None


# ── _build_index ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_build_index_dedupes_same_prospect_same_group() -> None:
    rows = [
        job.EducationSignalRow(
            id=UUID(int=10),
            prospect_id=P1,
            degrees_json=[
                {"school": "MIT", "degree": "B.S.", "field": "EE"},
                {"school": "MIT", "degree": "M.S.", "field": "EE"},
                {"school": "MIT", "degree": "Ph.D.", "field": "EE"},
            ],
        ),
    ]
    index, _ = job._build_index(rows)
    # P1 appears once per (school, kind) — bs/ms/phd are different kinds.
    assert len(index[("massachusetts institute of technology", "undergrad")]) == 1
    assert len(index[("massachusetts institute of technology", "masters")]) == 1
    assert len(index[("massachusetts institute of technology", "phd")]) == 1


@pytest.mark.unit
def test_build_index_counts_unclassifiable() -> None:
    rows = [
        job.EducationSignalRow(
            id=UUID(int=10),
            prospect_id=P1,
            degrees_json=[
                {"school": "MIT", "degree": "MBA"},
                {"school": "", "degree": "Erasmus"},  # bad school
                {"school": "Cornell", "degree": "Erasmus Program"},  # bad degree
            ],
        ),
    ]
    _, unclassifiable = job._build_index(rows)
    assert unclassifiable == 2


# ── _pairs_from_index ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_pairs_from_index_skips_singletons() -> None:
    e1 = job.ProspectEducationEntry(
        prospect_id=P1, school_canonical="harvard university",
        degree_kind="mba", field=None, degree_raw="MBA", school_raw="HBS",
    )
    pairs = list(job._pairs_from_index({("harvard university", "mba"): [e1]}))
    assert pairs == []


@pytest.mark.unit
def test_pairs_from_index_skips_masters_groups() -> None:
    e1 = job.ProspectEducationEntry(
        P1, "mit", "masters", None, "M.S.", "MIT"
    )
    e2 = job.ProspectEducationEntry(
        P2, "mit", "masters", None, "M.S.", "MIT"
    )
    pairs = list(job._pairs_from_index({("mit", "masters"): [e1, e2]}))
    assert pairs == []


@pytest.mark.unit
def test_pairs_from_index_three_prospect_group_yields_three_pairs() -> None:
    e1 = job.ProspectEducationEntry(P1, "harvard university", "mba", None, "MBA", "HBS")
    e2 = job.ProspectEducationEntry(P2, "harvard university", "mba", None, "MBA", "HBS")
    e3 = job.ProspectEducationEntry(P3, "harvard university", "mba", None, "MBA", "HBS")
    pairs = list(
        job._pairs_from_index({("harvard university", "mba"): [e1, e2, e3]})
    )
    assert len(pairs) == 3
    for a, b, st in pairs:
        assert a.prospect_id < b.prospect_id
        assert st == "same_mba_cohort"
    pair_ids = {(a.prospect_id, b.prospect_id) for a, b, _ in pairs}
    assert pair_ids == {(P1, P2), (P1, P3), (P2, P3)}


@pytest.mark.unit
def test_pairs_from_index_signal_type_per_kind() -> None:
    e1u = job.ProspectEducationEntry(P1, "ucla", "undergrad", None, "BA", "UCLA")
    e2u = job.ProspectEducationEntry(P2, "ucla", "undergrad", None, "BA", "UCLA")
    e1p = job.ProspectEducationEntry(P3, "mit", "phd", None, "PhD", "MIT")
    e2p = job.ProspectEducationEntry(P4, "mit", "phd", None, "PhD", "MIT")
    pairs = list(job._pairs_from_index({
        ("ucla", "undergrad"): [e1u, e2u],
        ("mit", "phd"): [e1p, e2p],
    }))
    sts = sorted(st for _, _, st in pairs)
    assert sts == ["same_phd_program", "same_undergrad_cohort"]


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
        "same_mba_cohort",
        "harvard university",
        str(P2),
    )
    assert result is False
    assert len(conn.fetchval_calls) == 1
    sql, args = conn.fetchval_calls[0]
    assert "FROM signals" in sql
    assert "value->>'institution_normalized'" in sql
    assert "value->>'connected_to'" in sql
    assert "LIMIT 1" in sql
    assert args == (P1, "same_mba_cohort", "harvard university", str(P2))


@pytest.mark.unit
async def test_signal_exists_returns_true_when_present() -> None:
    conn = _RecordingConn(fetchval_return=1)
    assert await job._signal_exists(
        conn,  # type: ignore[arg-type]
        P1, "same_mba_cohort", "harvard university", str(P2),
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
                key = (args[0], args[1], args[2], args[3])
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


def _row(prospect_id: UUID, degrees: list[dict[str, Any]]) -> dict[str, Any]:
    """asyncpg-row shaped dict — value comes out as a JSON string."""
    return {
        "id": UUID(int=int(prospect_id) + 1000),
        "prospect_id": prospect_id,
        "value": json.dumps(_signal_value(degrees)),
    }


@pytest.mark.unit
async def test_end_to_end_emits_mba_cohort_pair(patched_acquire: dict[str, Any]) -> None:
    patched_acquire["rows"] = [
        _row(P1, [{"school": "Harvard Business School", "degree": "MBA", "field": ""}]),
        _row(P2, [{"school": "Harvard Business School", "degree": "MBA", "field": ""}]),
    ]
    rollup = await job.bulk_education_signals_account(ACCOUNT_ID)
    assert rollup.education_signals_read == 2
    assert rollup.cohort_groups == 1
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
    assert args[3] == "same_mba_cohort"
    # args[4] is the dict passed directly to asyncpg's jsonb codec.
    assert isinstance(args[4], dict)
    assert args[4]["institution_normalized"] == "harvard university"
    assert args[4]["connected_to"] == str(P2)
    assert isinstance(args[5], float)
    assert 0.0 < args[5] < 1.0


@pytest.mark.unit
async def test_end_to_end_three_prospect_group_emits_three_pairs(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["rows"] = [
        _row(P1, [{"school": "Stanford University", "degree": "PhD", "field": "CS"}]),
        _row(P2, [{"school": "Stanford University", "degree": "PhD", "field": "CS"}]),
        _row(P3, [{"school": "Stanford University", "degree": "PhD", "field": "CS"}]),
    ]
    rollup = await job.bulk_education_signals_account(ACCOUNT_ID)
    assert rollup.pairs_emitted == 3
    assert rollup.signals_inserted == 3


@pytest.mark.unit
async def test_end_to_end_dry_run_writes_nothing(patched_acquire: dict[str, Any]) -> None:
    patched_acquire["rows"] = [
        _row(P1, [{"school": "MIT", "degree": "PhD", "field": "EE"}]),
        _row(P2, [{"school": "MIT", "degree": "PhD", "field": "EE"}]),
    ]
    rollup = await job.bulk_education_signals_account(ACCOUNT_ID, dry_run=True)
    assert rollup.dry_run is True
    assert rollup.pairs_emitted == 1
    assert rollup.signals_inserted == 0
    # Only one acquire (read), no second acquire for writes.
    assert len(patched_acquire["conns"]) == 1


@pytest.mark.unit
async def test_end_to_end_rerun_dedupes(patched_acquire: dict[str, Any]) -> None:
    patched_acquire["rows"] = [
        _row(P1, [{"school": "MIT", "degree": "PhD"}]),
        _row(P2, [{"school": "MIT", "degree": "PhD"}]),
    ]
    first = await job.bulk_education_signals_account(ACCOUNT_ID)
    second = await job.bulk_education_signals_account(ACCOUNT_ID)
    assert first.signals_inserted == 1
    assert second.signals_inserted == 0
    assert second.signals_skipped_dedup == 1


@pytest.mark.unit
async def test_end_to_end_masters_only_group_emits_nothing(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["rows"] = [
        _row(P1, [{"school": "MIT", "degree": "M.S."}]),
        _row(P2, [{"school": "MIT", "degree": "M.S."}]),
    ]
    rollup = await job.bulk_education_signals_account(ACCOUNT_ID)
    assert rollup.pairs_emitted == 0
    assert rollup.signals_inserted == 0


@pytest.mark.unit
async def test_end_to_end_alias_maps_disparate_school_strings(
    patched_acquire: dict[str, Any],
) -> None:
    """'Harvard Business School' and 'Harvard College' both → 'harvard university' for MBAs.

    But Harvard College → undergrad, HBS → MBA, so we use two MBAs.
    """
    patched_acquire["rows"] = [
        _row(P1, [{"school": "Harvard Business School", "degree": "MBA"}]),
        _row(P2, [{"school": "Harvard University", "degree": "MBA"}]),
    ]
    rollup = await job.bulk_education_signals_account(ACCOUNT_ID)
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
