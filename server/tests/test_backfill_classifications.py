"""Tests for backfill_classifications — pure helpers + CLI surface.

Live-DB integration is exercised by the operator via ``--limit N --dry-run``
smoke; this file covers the deterministic Python plumbing only.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from credence.jobs import backfill_classifications as job


# ── Pure-classification helpers ──────────────────────────────────────────────


@pytest.mark.unit
def test_classify_domain_rows_drops_unclassifiable() -> None:
    rows = [
        (uuid4(), "Director of Hardware Engineering"),
        (uuid4(), "Random Title That Matches Nothing 12345"),
        (uuid4(), "VP Sales"),
        (uuid4(), None),
        (uuid4(), ""),
    ]
    ids, domains = job._classify_domain_rows(rows)
    assert len(ids) == 2
    assert len(domains) == 2
    # Order preserved + each classified row matches a CHECK-valid keyspace.
    for d in domains:
        assert d in {
            "hardware_engineering", "software_engineering", "product_management",
            "manufacturing_ops", "sales_marketing", "research", "finance_legal",
            "people_ops", "general_management",
        }


@pytest.mark.unit
def test_classify_seniority_rows_drops_unclassifiable() -> None:
    rows = [
        (uuid4(), "CEO"),
        (uuid4(), "Random nonsense"),
        (uuid4(), "Senior Engineer"),
        (uuid4(), None),
    ]
    ids, scores = job._classify_seniority_rows(rows)
    assert len(ids) == 2
    assert len(scores) == 2
    # All scores in CHECK-valid range.
    for s in scores:
        assert 0 <= s <= 100


@pytest.mark.unit
def test_chunked_yields_size_bounded_chunks() -> None:
    chunks = list(job._chunked(list(range(7)), 3))
    assert chunks == [[0, 1, 2], [3, 4, 5], [6]]


@pytest.mark.unit
def test_chunked_handles_empty_input() -> None:
    assert list(job._chunked([], 3)) == []


@pytest.mark.unit
def test_chunked_handles_size_larger_than_input() -> None:
    assert list(job._chunked([1, 2], 100)) == [[1, 2]]


# ── _parse_targets ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_targets_default_to_all() -> None:
    assert job._parse_targets(None) == frozenset(job.ALL_TARGETS)
    assert job._parse_targets("") == frozenset(job.ALL_TARGETS)
    assert job._parse_targets("all") == frozenset(job.ALL_TARGETS)


@pytest.mark.unit
def test_parse_targets_single_value() -> None:
    assert job._parse_targets(job.TARGET_EMP_DOMAIN) == frozenset({job.TARGET_EMP_DOMAIN})


@pytest.mark.unit
def test_parse_targets_multiple_csv() -> None:
    raw = f"{job.TARGET_EMP_DOMAIN}, {job.TARGET_PERSON_SENIORITY}"
    assert job._parse_targets(raw) == frozenset(
        {job.TARGET_EMP_DOMAIN, job.TARGET_PERSON_SENIORITY}
    )


@pytest.mark.unit
def test_parse_targets_rejects_invalid() -> None:
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        job._parse_targets("not_a_real_target")


# ── CLI parser ──────────────────────────────────────────────────────────────


ACCOUNT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.mark.unit
def test_cli_requires_scope() -> None:
    parser = job._build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


@pytest.mark.unit
def test_cli_account_id_path() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(["--account-id", str(ACCOUNT_ID), "--dry-run"])
    assert args.account_id == ACCOUNT_ID
    assert args.dry_run is True
    assert args.targets == frozenset(job.ALL_TARGETS)
    assert args.batch_size == job.DEFAULT_BATCH_SIZE


@pytest.mark.unit
def test_cli_targets_subset() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(
        ["--all", "--targets", job.TARGET_EMP_DOMAIN]
    )
    assert args.all is True
    assert args.targets == frozenset({job.TARGET_EMP_DOMAIN})


@pytest.mark.unit
def test_cli_custom_batch_size() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(
        ["--all", "--batch-size", "500"]
    )
    assert args.batch_size == 500


# ── End-to-end on a fake conn ────────────────────────────────────────────────


class _RecordingConn:
    """Captures fetch + execute calls; returns canned rows for SELECT, fake
    UPDATE counts for execute. Just enough surface to drive
    backfill_classifications_account end-to-end."""

    def __init__(self, fetch_rows_per_sql: dict[str, list[dict[str, Any]]]) -> None:
        self._fetch_rows = fetch_rows_per_sql
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.fetch_calls.append((sql, args))
        # Match the canned set by the unique fragment in the SQL.
        for marker, rows in self._fetch_rows.items():
            if marker in sql:
                return rows
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        # Pretend each execute affected len(ids) rows.
        ids = args[0] if args else []
        return f"UPDATE {len(ids)}"


class _FakeAcquire:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _RecordingConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


@pytest.mark.unit
async def test_backfill_account_dry_run_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows_emp_dom = [
        {"id": uuid4(), "title": "VP Sales"},
        {"id": uuid4(), "title": "garbage zzzzzz"},
    ]
    conn = _RecordingConn({"functional_domain IS NULL": rows_emp_dom})
    monkeypatch.setattr(job, "acquire", lambda: _FakeAcquire(conn))

    result = await job.backfill_classifications_account(
        ACCOUNT_ID,
        targets=frozenset({job.TARGET_EMP_DOMAIN}),
        dry_run=True,
    )
    assert result.emp_domain_candidates == 2
    assert result.emp_domain_classified == 1  # garbage rejected
    assert result.emp_domain_updated == 0  # dry-run skips UPDATE
    # No execute calls under dry_run.
    assert conn.execute_calls == []


@pytest.mark.unit
async def test_backfill_account_writes_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = uuid4(), uuid4()
    rows = [
        {"id": a, "title": "Director of Hardware Engineering"},
        {"id": b, "title": "Senior Engineer"},
    ]
    conn = _RecordingConn({"functional_domain IS NULL": rows})
    monkeypatch.setattr(job, "acquire", lambda: _FakeAcquire(conn))

    result = await job.backfill_classifications_account(
        ACCOUNT_ID,
        targets=frozenset({job.TARGET_EMP_DOMAIN}),
        dry_run=False,
    )
    assert result.emp_domain_candidates == 2
    assert result.emp_domain_classified == 2
    assert result.emp_domain_updated == 2
    # Confirm UPDATE was issued with both ids + both domains.
    assert len(conn.execute_calls) == 1
    sql, args = conn.execute_calls[0]
    assert "UPDATE employment_periods" in sql
    assert "ep.functional_domain IS NULL" in sql  # TOCTOU guard preserved
    assert set(args[0]) == {a, b}


@pytest.mark.unit
async def test_backfill_account_targets_subset_skips_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting only emp_domain target must skip person_* SELECTs entirely."""
    rows = [{"id": uuid4(), "title": "VP Sales"}]
    conn = _RecordingConn({"functional_domain IS NULL": rows})
    monkeypatch.setattr(job, "acquire", lambda: _FakeAcquire(conn))

    result = await job.backfill_classifications_account(
        ACCOUNT_ID,
        targets=frozenset({job.TARGET_EMP_DOMAIN}),
        dry_run=True,
    )
    # Exactly one SELECT (for emp_domain). No person_* SELECTs fired.
    selects = [c for c in conn.fetch_calls if "FROM" in c[0]]
    assert len(selects) == 1
    assert "FROM employment_periods" in selects[0][0]
    # Other-target counters stay at zero.
    assert result.emp_seniority_candidates == 0
    assert result.person_domain_candidates == 0
    assert result.person_seniority_candidates == 0
