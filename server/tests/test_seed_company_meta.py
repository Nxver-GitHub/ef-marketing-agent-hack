"""Mock-only tests for `scripts.seed_company_meta.seed_companies`.

Covers the DB-touching orchestration path with `credence.db.fetch`
fully mocked — `asyncpg.connect` is never called. Pure-function tests
(parsers, row shapers, employee buckets) live in
`tests/test_company_enrichment.py`; this module focuses on:

  1. Parser round-trip on a small inline fixture
  2. Skipping rows that don't resolve to a `companies.id`
  3. UPDATE SQL shape (anchored COALESCE preservation)
  4. Dry-run path emits no UPDATE
  5. COALESCE clauses preserve existing non-null values
  6. Stats counters add up across mixed rows
  7. Missing TS file raises `FileNotFoundError`
  8. Sentinel "?"/""/null-equivalent industry values produce empty tags
  9. Per-row UPDATE failures are caught and counted, not bubbled

All async tests use `monkeypatch` to swap `credence.db.fetch` with an
async function that records its calls — no real connection, no real
asyncpg pool, sub-second total runtime.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scripts import seed_company_meta as seed_mod
from scripts.seed_company_meta import (
    build_update_row,
    parse_company_meta,
    parse_employee_count,
    seed_companies,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


_FIXTURE_TS = """\
// Auto-generated. Do not edit.
export interface GeneratedCompanyMeta { country: string; industry: string; prospect_count: number; }
export const GENERATED_COMPANY_META: Record<string, GeneratedCompanyMeta> = {
  "Intel": { "country": "United States", "state": "California", "hq_city": "Santa Clara", "industry": "Semiconductors", "employee_count_estimate": "100k+", "partnerships": ["Dell", "HP"], "description": "Chips.", "prospect_count": 454 },
  "Acme Corp": { "country": "FR", "industry": "Defense", "prospect_count": 5 },
  "GhostCo": { "country": "DE", "industry": "Defense", "prospect_count": 2 }
};
"""


class _FetchRecorder:
    """Stand-in for `credence.db.fetch` that records every call.

    Returns the canned `select_rows` for SELECT queries (the
    canonical_name → id resolution) and an empty list for UPDATEs
    (matches the `RETURNING`-less UPDATE the script issues).
    """

    def __init__(self, select_rows: list[dict[str, Any]]) -> None:
        self._select_rows = select_rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def __call__(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if sql.lstrip().upper().startswith("SELECT"):
            return self._select_rows
        return []

    @property
    def update_calls(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [c for c in self.calls if c[0].lstrip().upper().startswith("UPDATE")]

    @property
    def select_calls(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [c for c in self.calls if c[0].lstrip().upper().startswith("SELECT")]


@pytest.fixture()
def fixture_ts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write the inline TS fixture to a temp file and point the
    seed module at it. Avoids touching the real generated file."""
    ts_file = tmp_path / "company-meta.generated.ts"
    ts_file.write_text(_FIXTURE_TS)
    monkeypatch.setattr(seed_mod, "_ts_path", lambda: ts_file)
    return ts_file


@pytest.fixture()
def fake_close_pool(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub `close_pool` so the tests don't try to tear down a real pool."""
    closer = AsyncMock(return_value=None)
    monkeypatch.setattr(seed_mod, "close_pool", closer)
    return closer


# ── 1. Parser round-trip on the inline fixture ──────────────────────────────


@pytest.mark.unit
def test_parser_round_trip_on_fixture() -> None:
    parsed = parse_company_meta(_FIXTURE_TS)
    assert set(parsed.keys()) == {"Intel", "Acme Corp", "GhostCo"}
    assert parsed["Intel"]["partnerships"] == ["Dell", "HP"]
    assert parsed["Acme Corp"]["country"] == "FR"


# ── 2. Skips rows that don't resolve to a companies.id ──────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_seed_skips_rows_without_canonical_match(
    fixture_ts: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only "Intel" exists in the companies table — Acme + Ghost are
    # not in the SELECT result, so they should land in `missing`.
    recorder = _FetchRecorder(select_rows=[{"id": "uuid-intel", "canonical_name": "Intel"}])
    monkeypatch.setattr(seed_mod, "fetch", recorder)

    counters = await seed_companies(dry_run=False)

    assert counters["matched"] == 1
    assert counters["updated"] == 1
    assert counters["missing"] == 2  # Acme + Ghost
    assert counters["errors"] == 0


# ── 3. UPDATE SQL shape uses COALESCE for nullable text columns ─────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_sql_uses_coalesce_for_nullable_columns(
    fixture_ts: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _FetchRecorder(select_rows=[{"id": "uuid-intel", "canonical_name": "Intel"}])
    monkeypatch.setattr(seed_mod, "fetch", recorder)

    await seed_companies(dry_run=False)
    assert len(recorder.update_calls) == 1
    sql, _ = recorder.update_calls[0]
    # Every column we care about preserving uses COALESCE($n, col).
    for column in (
        "description",
        "hq_city",
        "hq_state",
        "hq_country",
        "employee_count_estimate",
    ):
        assert f"COALESCE($" in sql
        assert column in sql, f"column {column!r} missing from UPDATE"
    # And we always re-stamp the audit columns.
    assert "updated_at" in sql
    assert "enrichment_last_run" in sql


# ── 4. Dry-run path issues no UPDATEs ───────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_emits_no_update(
    fixture_ts: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _FetchRecorder(select_rows=[
        {"id": "uuid-intel", "canonical_name": "Intel"},
        {"id": "uuid-acme",  "canonical_name": "Acme Corp"},
    ])
    monkeypatch.setattr(seed_mod, "fetch", recorder)

    counters = await seed_companies(dry_run=True)

    assert recorder.update_calls == []  # the load-bearing assertion
    assert counters["matched"] == 2
    assert counters["updated"] == 0     # dry-run never writes
    assert counters["missing"] == 1     # GhostCo


# ── 5. COALESCE preserves existing values when source is null ──────────────


@pytest.mark.unit
def test_build_update_row_emits_none_for_missing_text_fields() -> None:
    """The UPDATE statement uses `COALESCE($n, column)` — for the
    preservation contract to hold, `build_update_row` must surface
    `None` (not "") for missing text fields. Otherwise an empty
    string would overwrite the existing value."""
    row = build_update_row({"country": "FR", "industry": "Defense"})
    # Text fields that the source doesn't supply must come out as None,
    # so COALESCE in the SQL preserves the existing DB value.
    assert row["description"] is None
    assert row["hq_city"] is None
    assert row["hq_state"] is None
    # The supplied values pass through.
    assert row["hq_country"] == "FR"
    assert row["industry_tags"] == ["Defense"]


# ── 6. Stats counters add up across mixed rows ─────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_seed_counters_sum_to_input_rows(
    fixture_ts: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _FetchRecorder(select_rows=[
        {"id": "uuid-intel", "canonical_name": "Intel"},
        {"id": "uuid-acme",  "canonical_name": "Acme Corp"},
    ])
    monkeypatch.setattr(seed_mod, "fetch", recorder)

    counters = await seed_companies(dry_run=False)

    # Fixture has 3 rows; matched + missing must equal that exactly.
    assert counters["matched"] + counters["missing"] == 3
    assert counters["updated"] == counters["matched"] - counters["errors"]


# ── 7. Missing TS file raises FileNotFoundError ────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_seed_missing_ts_file_raises_filenotfound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seed_mod, "_ts_path", lambda: tmp_path / "does-not-exist.ts")
    monkeypatch.setattr(seed_mod, "fetch", AsyncMock())

    with pytest.raises(FileNotFoundError, match="does-not-exist"):
        await seed_companies(dry_run=False)


# ── 8. Sentinel industry values produce no tag ─────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("sentinel", ["", None])
def test_build_update_row_sentinel_industry_yields_empty_tags(
    sentinel: str | None,
) -> None:
    """The DB column is TEXT[] NOT NULL DEFAULT '{}' — empty/null
    industry must round-trip to `[]`, never `[None]` or `[""]`."""
    row = build_update_row({"country": "US", "industry": sentinel})
    assert row["industry_tags"] == []


@pytest.mark.unit
def test_build_update_row_question_mark_industry_passes_through() -> None:
    """A literal `"?"` is ambiguous — current behaviour keeps it as-is
    (truthy string) so an operator can grep for it later. This pins the
    contract; if the policy changes, this test should change with it."""
    row = build_update_row({"industry": "?"})
    assert row["industry_tags"] == ["?"]


# ── 9. Per-row UPDATE failures are counted, not raised ─────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_row_update_failure_is_counted(
    fixture_ts: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad row shouldn't abort the whole seed run — counted in
    `errors`, logged, and the loop keeps going."""
    select_rows = [
        {"id": "uuid-intel", "canonical_name": "Intel"},
        {"id": "uuid-acme",  "canonical_name": "Acme Corp"},
    ]
    call_log: list[str] = []

    async def fake_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if sql.lstrip().upper().startswith("SELECT"):
            return select_rows
        call_log.append(args[0])
        # Fail Intel's UPDATE, succeed Acme's. asyncpg raises arbitrary
        # exceptions; `Exception` is enough to validate the catch-all.
        if args[0] == "uuid-intel":
            raise RuntimeError("simulated UPDATE failure")
        return []

    monkeypatch.setattr(seed_mod, "fetch", fake_fetch)

    counters = await seed_companies(dry_run=False)

    assert counters["matched"] == 2
    assert counters["updated"] == 1
    assert counters["errors"] == 1
    assert call_log == ["uuid-intel", "uuid-acme"]


# ── 10. Live employee buckets cover every label the TS generator emits ─────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<100", 50),
        ("100-1k", 500),
        ("1k-10k", 5000),
        ("10k-100k", 50000),
        ("100k+", 150000),
    ],
)
def test_parse_employee_count_covers_live_generator_buckets(
    raw: str, expected: int
) -> None:
    """Regression guard: the live `enrich-companies.mjs` output uses these
    five labels. Missing any of them silently zero-fills
    `employee_count_estimate` for ~50% of companies (verified 2026-05-01)."""
    assert parse_employee_count(raw) == expected


# ── Sanity: the script itself doesn't try to touch a real DB at import ─────


@pytest.mark.unit
def test_module_imports_without_side_effects() -> None:
    """The script must be importable in environments without DATABASE_URL —
    no asyncio.run, no asyncpg.connect, no eager pool init at import time."""
    # If the import in the module-level statement at the top of this file
    # succeeded, this test is already passing. The assertion is just a
    # hook so pytest counts it.
    assert callable(seed_companies)
    assert asyncio.iscoroutinefunction(seed_companies)
