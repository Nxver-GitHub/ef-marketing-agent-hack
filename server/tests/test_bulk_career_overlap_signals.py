"""Tests for bulk_career_overlap_signals — per-account career-overlap runner.

Pure-function unit coverage + a fake-conn end-to-end exercising the full
algorithm. No live DB.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from credence.jobs import bulk_career_overlap_signals as job


# ── Fixtures ────────────────────────────────────────────────────────────────


ACCOUNT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
P1 = UUID("00000000-0000-0000-0000-000000000001")
P2 = UUID("00000000-0000-0000-0000-000000000002")
P3 = UUID("00000000-0000-0000-0000-000000000003")
P4 = UUID("00000000-0000-0000-0000-000000000004")
COMPANY_A = UUID("11111111-1111-1111-1111-111111111111")
COMPANY_B = UUID("22222222-2222-2222-2222-222222222222")


def _row(
    person_id: UUID,
    company_id: UUID = COMPANY_A,
    *,
    start_year: int = 2018,
    end_year: int | None = 2022,
    title: str | None = "Engineer",
    functional_domain: str | None = "hardware_engineering",
    seniority_score: int | None = 50,
    is_current: bool = False,
    inferred_team: str | None = None,
    source_prospect_id: UUID | None = None,
) -> job.EmploymentRow:
    # Default the test source_prospect_id to person_id — most tests don't
    # care which prospect-id space is in use, they just need a valid UUID.
    return job.EmploymentRow(
        person_id=person_id,
        company_id=company_id,
        title=title,
        functional_domain=functional_domain,
        seniority_score=seniority_score,
        start_year=start_year,
        end_year=end_year,
        is_current=is_current,
        inferred_team=inferred_team,
        source_prospect_id=source_prospect_id or person_id,
    )


# ── _classify_overlap_kind ──────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "team_a,team_b,domain_a,domain_b,score_a,score_b,expected",
    [
        # Same team — wins.
        ("memory", "memory", "hardware_engineering", "hardware_engineering", 50, 60, "career_overlap_same_team"),
        # Same team — even with different domains/big senior gap.
        ("memory", "memory", "software_engineering", "research", 30, 80, "career_overlap_same_team"),
        # Same domain + senior gap < 10.
        (None, None, "hardware_engineering", "hardware_engineering", 50, 55, "career_overlap_same_domain"),
        # Same domain but senior gap >= 10 → general.
        (None, None, "hardware_engineering", "hardware_engineering", 40, 60, "career_overlap_general"),
        # Different domains → general.
        (None, None, "hardware_engineering", "software_engineering", 50, 52, "career_overlap_general"),
        # No team, no domain match, no scores → general.
        (None, None, None, None, None, None, "career_overlap_general"),
        # team_a None → falls through to domain check.
        (None, "memory", "hardware_engineering", "hardware_engineering", 50, 51, "career_overlap_same_domain"),
    ],
)
def test_classify_overlap_kind(
    team_a: str | None,
    team_b: str | None,
    domain_a: str | None,
    domain_b: str | None,
    score_a: int | None,
    score_b: int | None,
    expected: str,
) -> None:
    a = _row(P1, inferred_team=team_a, functional_domain=domain_a, seniority_score=score_a)
    b = _row(P2, inferred_team=team_b, functional_domain=domain_b, seniority_score=score_b)
    assert job._classify_overlap_kind(a, b) == expected


# ── _overlap_years ──────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "a_start,a_end,b_start,b_end,expected",
    [
        # Exact same window.
        (2018, 2022, 2018, 2022, 4),
        # Partial overlap.
        (2015, 2020, 2018, 2025, 2),
        # No overlap (a ends before b starts).
        (2010, 2015, 2018, 2022, 0),
        # Open-ended end_year on B → uses NOW_YEAR (2025).
        (2018, 2022, 2020, None, 2),
        # Both open-ended → uses NOW_YEAR for both.
        (2020, None, 2022, None, 3),  # 2025 - 2022
    ],
)
def test_overlap_years(
    a_start: int,
    a_end: int | None,
    b_start: int,
    b_end: int | None,
    expected: int,
) -> None:
    a = _row(P1, start_year=a_start, end_year=a_end)
    b = _row(P2, start_year=b_start, end_year=b_end)
    assert job._overlap_years(a, b) == expected


# ── _pairs_within_company ──────────────────────────────────────────────────


@pytest.mark.unit
def test_pairs_within_company_enforces_id_ordering() -> None:
    rows = [
        _row(P2, start_year=2018, end_year=2022),
        _row(P1, start_year=2019, end_year=2023),
    ]
    pairs = list(job._pairs_within_company(rows))
    assert len(pairs) == 1
    assert pairs[0].a.person_id == P1
    assert pairs[0].b.person_id == P2
    assert pairs[0].a.person_id < pairs[0].b.person_id


@pytest.mark.unit
def test_pairs_within_company_skips_non_overlapping() -> None:
    rows = [
        _row(P1, start_year=2010, end_year=2014),
        _row(P2, start_year=2018, end_year=2022),
    ]
    assert list(job._pairs_within_company(rows)) == []


@pytest.mark.unit
def test_pairs_within_company_overlap_window_correct() -> None:
    rows = [
        _row(P1, start_year=2015, end_year=2022, inferred_team="memory"),
        _row(P2, start_year=2018, end_year=2025, inferred_team="memory"),
    ]
    pairs = list(job._pairs_within_company(rows))
    assert len(pairs) == 1
    p = pairs[0]
    assert p.overlap_start_year == 2018
    assert p.overlap_end_year == 2022
    assert p.overlap_years == 4
    assert p.signal_type == "career_overlap_same_team"


@pytest.mark.unit
def test_pairs_within_company_three_prospect_group_yields_three_pairs() -> None:
    rows = [
        _row(P1, start_year=2018, end_year=2024),
        _row(P2, start_year=2018, end_year=2024),
        _row(P3, start_year=2018, end_year=2024),
    ]
    pairs = list(job._pairs_within_company(rows))
    assert len(pairs) == 3
    pair_ids = {(p.a.person_id, p.b.person_id) for p in pairs}
    assert pair_ids == {(P1, P2), (P1, P3), (P2, P3)}


# ── _pairs_from_index ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_pairs_from_index_skips_singleton_companies() -> None:
    idx = {COMPANY_A: [_row(P1)]}
    assert list(job._pairs_from_index(idx)) == []


@pytest.mark.unit
def test_pairs_from_index_dedupes_same_person_multiple_tenures() -> None:
    """Person P1 with two stints at COMPANY_A — should pair P1 with P2 once."""
    idx = {
        COMPANY_A: [
            _row(P1, start_year=2010, end_year=2013),
            _row(P1, start_year=2018, end_year=2022),
            _row(P2, start_year=2011, end_year=2014),
        ]
    }
    pairs = list(job._pairs_from_index(idx))
    # Earliest P1 tenure (2010-2013) overlaps with P2 (2011-2014) for 2y.
    assert len(pairs) == 1
    assert pairs[0].overlap_years == 2


# ── _signal_exists ──────────────────────────────────────────────────────────


class _RecordingConn:
    def __init__(self, fetchval_return: Any = None) -> None:
        self._fetchval_return = fetchval_return
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []

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

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.fetchrow_calls.append((sql, args))
        return None


@pytest.mark.unit
async def test_signal_exists_query_well_formed() -> None:
    conn = _RecordingConn(fetchval_return=None)
    result = await job._signal_exists(
        conn,  # type: ignore[arg-type]
        P1,
        "career_overlap_same_team",
        str(COMPANY_A),
        str(P2),
    )
    assert result is False
    assert len(conn.fetchval_calls) == 1
    sql, args = conn.fetchval_calls[0]
    assert "FROM signals" in sql
    assert "value->>'company_id'" in sql
    assert "value->>'connected_to'" in sql
    assert "LIMIT 1" in sql
    assert args == (P1, "career_overlap_same_team", str(COMPANY_A), str(P2))


@pytest.mark.unit
async def test_signal_exists_returns_true_when_present() -> None:
    conn = _RecordingConn(fetchval_return=1)
    assert await job._signal_exists(
        conn,  # type: ignore[arg-type]
        P1, "career_overlap_general", str(COMPANY_A), str(P2),
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
    state: dict[str, Any] = {
        "conns": [],
        "exists_keys": set(),
        "rows": [],
        "id_assumption_ok": True,
        "persons_in_scope": 4,
        "matched": 4,
    }

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
            if "FROM employment_periods" in sql:
                return state["rows"]
            if "FROM companies" in sql:
                return [
                    {"id": COMPANY_A, "canonical_name": "Acme Corp"},
                    {"id": COMPANY_B, "canonical_name": "Beta LLC"},
                ]
            return []

        async def fake_fetchrow(sql: str, *args: Any) -> Any:
            conn.fetchrow_calls.append((sql, args))
            if "persons_in_scope" in sql:
                if state["id_assumption_ok"]:
                    return {
                        "persons_in_scope": state["persons_in_scope"],
                        "matched": state["persons_in_scope"],
                    }
                return {
                    "persons_in_scope": state["persons_in_scope"],
                    "matched": state["matched"],
                }
            return None

        conn.fetch = fake_fetch  # type: ignore[method-assign]
        conn.fetchrow = fake_fetchrow  # type: ignore[method-assign]
        state["conns"].append(conn)
        return conn

    def fake_acquire() -> _FakeAcquire:
        return _FakeAcquire(make_conn())

    monkeypatch.setattr(job, "acquire", fake_acquire)
    return state


def _ep_record(
    person_id: UUID,
    company_id: UUID = COMPANY_A,
    *,
    start_year: int = 2018,
    end_year: int | None = 2022,
    inferred_team: str | None = None,
    functional_domain: str | None = "hardware_engineering",
    seniority_score: int | None = 50,
) -> dict[str, Any]:
    """asyncpg-row-shaped dict mirroring SELECT_EMPLOYMENT_PERIODS_SQL."""
    return {
        "person_id": person_id,
        "company_id": company_id,
        "title": "Engineer",
        "functional_domain": functional_domain,
        "seniority_score": seniority_score,
        "start_year": start_year,
        "end_year": end_year,
        "is_current": False,
        "inferred_team": inferred_team,
        # Default test source_prospect_id to person_id; SQL JOIN to persons
        # already filters out unresolved rows in production, so all rows
        # reaching the runner have a non-null source_prospect_id.
        "source_prospect_id": person_id,
    }


@pytest.mark.unit
async def test_end_to_end_emits_same_team_pair(patched_acquire: dict[str, Any]) -> None:
    patched_acquire["rows"] = [
        _ep_record(P1, inferred_team="memory"),
        _ep_record(P2, inferred_team="memory"),
    ]
    rollup = await job.bulk_career_overlap_signals_account(ACCOUNT_ID)
    assert rollup.employment_periods_read == 2
    assert rollup.company_groups == 1
    assert rollup.pairs_emitted == 1
    assert rollup.signals_inserted == 1
    assert rollup.signals_skipped_dedup == 0

    # Find the write conn (3rd: assumption-check + read both happen on first
    # acquire; companies-fetch on second; writes on third).
    inserts: list[tuple[str, tuple[Any, ...]]] = []
    for c in patched_acquire["conns"]:
        inserts.extend(call for call in c.execute_calls if "INSERT INTO signals" in call[0])
    assert len(inserts) == 1
    _, args = inserts[0]
    # args: prospect_id, account_id, source, signal_type, value, confidence
    assert args[0] == P1
    assert args[1] == ACCOUNT_ID
    assert args[2] == job.SIGNAL_SOURCE
    assert args[3] == "career_overlap_same_team"
    # CRITICAL: value passed as dict (not json.dumps-string).
    assert isinstance(args[4], dict), (
        "value must be a dict for asyncpg's jsonb codec — passing json.dumps "
        "would double-encode and break value->>'key' subscripts."
    )
    assert args[4]["company_id"] == str(COMPANY_A)
    assert args[4]["connected_to"] == str(P2)
    assert args[4]["company_name"] == "Acme Corp"
    assert args[4]["overlap_years"] == 4
    assert isinstance(args[5], float)
    assert 0.0 < args[5] < 1.0


@pytest.mark.unit
async def test_end_to_end_three_prospect_group_emits_three_pairs(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["rows"] = [
        _ep_record(P1, inferred_team="memory"),
        _ep_record(P2, inferred_team="memory"),
        _ep_record(P3, inferred_team="memory"),
    ]
    rollup = await job.bulk_career_overlap_signals_account(ACCOUNT_ID)
    assert rollup.pairs_emitted == 3
    assert rollup.signals_inserted == 3


@pytest.mark.unit
async def test_end_to_end_dry_run_writes_nothing(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["rows"] = [
        _ep_record(P1, inferred_team="memory"),
        _ep_record(P2, inferred_team="memory"),
    ]
    rollup = await job.bulk_career_overlap_signals_account(ACCOUNT_ID, dry_run=True)
    assert rollup.dry_run is True
    assert rollup.pairs_emitted == 1
    assert rollup.signals_inserted == 0
    # No INSERT statements at all.
    for c in patched_acquire["conns"]:
        for sql, _ in c.execute_calls:
            assert "INSERT INTO signals" not in sql


@pytest.mark.unit
async def test_end_to_end_rerun_dedupes(patched_acquire: dict[str, Any]) -> None:
    patched_acquire["rows"] = [
        _ep_record(P1, inferred_team="memory"),
        _ep_record(P2, inferred_team="memory"),
    ]
    first = await job.bulk_career_overlap_signals_account(ACCOUNT_ID)
    second = await job.bulk_career_overlap_signals_account(ACCOUNT_ID)
    assert first.signals_inserted == 1
    assert second.signals_inserted == 0
    assert second.signals_skipped_dedup == 1


@pytest.mark.unit
async def test_end_to_end_no_overlap_emits_nothing(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["rows"] = [
        _ep_record(P1, start_year=2010, end_year=2014),
        _ep_record(P2, start_year=2018, end_year=2022),
    ]
    rollup = await job.bulk_career_overlap_signals_account(ACCOUNT_ID)
    assert rollup.pairs_emitted == 0
    assert rollup.signals_inserted == 0


@pytest.mark.unit
async def test_end_to_end_id_assumption_failure_aborts(
    patched_acquire: dict[str, Any],
) -> None:
    # Guard now aborts ONLY when matched == 0 across non-empty scope (the
    # signature of "linkage migration not applied"). Partial-match (some
    # persons unresolved, some resolved) is normal — those rows are filtered
    # by the SELECT WHERE source_prospect_id IS NOT NULL clause.
    patched_acquire["id_assumption_ok"] = False
    patched_acquire["persons_in_scope"] = 4
    patched_acquire["matched"] = 0  # zero matches → migration unapplied signal
    patched_acquire["rows"] = [
        _ep_record(P1, inferred_team="memory"),
        _ep_record(P2, inferred_team="memory"),
    ]
    rollup = await job.bulk_career_overlap_signals_account(ACCOUNT_ID)
    assert rollup.signals_inserted == 0
    assert rollup.pairs_emitted == 0
    assert rollup.pairs_skipped_id_assumption_fail == 4
    assert rollup.errors
    assert "ID assumption FAILED" in rollup.errors[0]


@pytest.mark.unit
async def test_end_to_end_classifies_same_domain_when_no_team(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["rows"] = [
        _ep_record(P1, functional_domain="hardware_engineering", seniority_score=50),
        _ep_record(P2, functional_domain="hardware_engineering", seniority_score=55),
    ]
    rollup = await job.bulk_career_overlap_signals_account(ACCOUNT_ID)
    assert rollup.signals_inserted == 1
    inserts: list[tuple[str, tuple[Any, ...]]] = []
    for c in patched_acquire["conns"]:
        inserts.extend(call for call in c.execute_calls if "INSERT INTO signals" in call[0])
    _, args = inserts[0]
    assert args[3] == "career_overlap_same_domain"


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
