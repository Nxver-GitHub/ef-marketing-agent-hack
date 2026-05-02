"""Mock-only tests for ``server/scripts/bulk_press_co_mention_edges.py``.

The script reads from the v2 ``signals`` table and writes ``person_connections``
edges of type ``co_mentioned_in_press``. These tests exercise the pure helpers
(name extraction, pair generation, resolver) plus the orchestrator with
mocked DB I/O.

The companion v3 job (``credence.jobs.bulk_press_co_mention_edges``) has its
own test suite — see ``test_bulk_press_co_mention_edges.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# asyncpg's connection.transaction() is a *sync* method that returns an async
# context manager. AsyncMock attribute access produces coroutine factories,
# which break ``async with``. Bind a MagicMock that hands back a fresh
# ``_FakeTxn`` on each call so the real code's ``async with conn.transaction():``
# works under test.
def _make_conn() -> AsyncMock:
    conn = AsyncMock()
    conn.transaction = MagicMock(side_effect=lambda: _FakeTxn())
    return conn
from uuid import UUID

import pytest

# Ensure ``server/scripts/`` is on sys.path so ``import scripts.…`` works
# from the repo root regardless of pytest's rootdir.
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from scripts import bulk_press_co_mention_edges as script  # noqa: E402


# ── Shared async-context fakes ─────────────────────────────────────────────


class _FakeTxn:
    """Async context manager that mimics asyncpg's transaction()."""

    async def __aenter__(self) -> "_FakeTxn":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FakeFKError(Exception):
    """Stand-in for asyncpg.exceptions.ForeignKeyViolationError."""


class _FakeCheckError(Exception):
    """Stand-in for asyncpg.exceptions.CheckViolationError."""


class _FakePostgresError(Exception):
    """Stand-in for asyncpg.exceptions.PostgresError."""


# ── _extract_names_from_value ──────────────────────────────────────────────


@pytest.mark.unit
class TestExtractNames:
    def test_parses_mentioned_executives_key(self) -> None:
        value = {"mentioned_executives": ["Jane Smith", "John Doe"]}
        assert script._extract_names_from_value(value) == ["Jane Smith", "John Doe"]

    def test_falls_through_to_persons_key(self) -> None:
        value = {"persons": ["Jane Smith", "John Doe"]}
        assert script._extract_names_from_value(value) == ["Jane Smith", "John Doe"]

    def test_unknown_shape_returns_empty(self) -> None:
        assert script._extract_names_from_value({"foo": "bar"}) == []
        assert script._extract_names_from_value(None) == []
        assert script._extract_names_from_value("a string") == []

    def test_dedupes_and_strips(self) -> None:
        value = {"mentioned_executives": ["  Jane  Smith ", "JANE SMITH", "John Doe"]}
        assert script._extract_names_from_value(value) == ["Jane Smith", "John Doe"]

    def test_drops_non_strings(self) -> None:
        value = {"mentioned_executives": ["Jane Smith", None, 42, ""]}
        assert script._extract_names_from_value(value) == ["Jane Smith"]


# ── _pair_names ────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestPairNames:
    def test_single_name_emits_zero_pairs(self) -> None:
        assert script._pair_names(["Jane Smith"]) == []

    def test_two_names_emit_one_pair(self) -> None:
        assert script._pair_names(["Jane Smith", "John Doe"]) == [
            ("Jane Smith", "John Doe"),
        ]

    def test_three_names_emit_three_pairs(self) -> None:
        pairs = script._pair_names(["A A", "B B", "C C"])
        assert len(pairs) == 3
        assert ("A A", "B B") in pairs
        assert ("A A", "C C") in pairs
        assert ("B B", "C C") in pairs

    def test_empty_yields_empty(self) -> None:
        assert script._pair_names([]) == []


# ── _resolve_name (case-insensitive in-memory index) ───────────────────────


@pytest.mark.unit
class TestResolveName:
    def test_case_insensitive_hit(self) -> None:
        pid = UUID("11111111-1111-1111-1111-111111111111")
        index = {"jane smith": pid}
        assert script._resolve_name("Jane Smith", index) == pid
        assert script._resolve_name("JANE SMITH", index) == pid
        assert script._resolve_name("  jane   smith ", index) == pid

    def test_missing_name_returns_none(self) -> None:
        index = {"jane smith": UUID(int=1)}
        assert script._resolve_name("Madonna", index) is None

    def test_empty_string_returns_none(self) -> None:
        assert script._resolve_name("", {"jane smith": UUID(int=1)}) is None


# ── _order_pair ────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestOrderPair:
    def test_lower_uuid_first(self) -> None:
        a = UUID(int=1)
        b = UUID(int=2)
        assert script._order_pair(a, b) == (a, b)
        assert script._order_pair(b, a) == (a, b)


# ── _build_releases_query ──────────────────────────────────────────────────


@pytest.mark.unit
class TestBuildReleasesQuery:
    def test_no_limit_omits_limit_clause(self) -> None:
        sql = script._build_releases_query(limit=None)
        assert "LIMIT" not in sql
        assert "signals" in sql
        assert "$1::text[]" in sql

    def test_with_limit_appends_clause(self) -> None:
        sql = script._build_releases_query(limit=42)
        assert "LIMIT 42" in sql


# ── orchestrator (dry-run path) ────────────────────────────────────────────


def _make_signal_row(value: dict, *, signal_id: UUID | None = None) -> dict:
    return {
        "signal_id": signal_id or UUID(int=99),
        "prospect_id": UUID(int=100),
        "signal_type": "press_release",
        "value": value,
        "collected_at": datetime(script.CURRENT_YEAR, 1, 1, tzinfo=timezone.utc),
    }


class _FakeAcquire:
    """Async context manager that yields a configurable mock connection."""

    def __init__(self, conn: AsyncMock) -> None:
        self._conn = conn

    async def __aenter__(self) -> AsyncMock:
        return self._conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


@pytest.mark.unit
class TestRunPressCoMention:
    @pytest.mark.asyncio
    async def test_dry_run_makes_no_writes(self) -> None:
        a_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        b_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

        conn = _make_conn()
        # _resolve_connection_type → probe path
        conn.execute = AsyncMock(side_effect=_FakeFKError())
        # First fetch: candidate signals.
        # Second fetch: persons index.
        # accounts SELECT: one fetchrow.
        conn.fetch = AsyncMock(side_effect=[
            [_make_signal_row({"mentioned_executives": ["Jane Smith", "John Doe"]})],
            [
                {"id": a_id, "canonical_name": "Jane Smith"},
                {"id": b_id, "canonical_name": "John Doe"},
            ],
        ])
        conn.fetchrow = AsyncMock(return_value={"id": UUID(int=42)})

        with patch.object(script, "acquire", lambda: _FakeAcquire(conn)):
            rollup = await script.run_press_co_mention(dry_run=True)

        assert rollup.dry_run is True
        assert rollup.signals_scanned == 1
        assert rollup.signals_qualifying == 1
        assert rollup.pairs_considered == 1
        assert rollup.pairs_inserted == 1  # in dry-run we count would-be inserts
        # Critically: no upsert SQL was executed.
        executed_calls = [c.args[0] for c in conn.execute.await_args_list]
        assert not any("ON CONFLICT" in str(s) for s in executed_calls)

    @pytest.mark.asyncio
    async def test_idempotent_rerun_no_new_inserts(self) -> None:
        """Second invocation should bump corroboration but emit zero new rows.

        We simulate this by having the upsert RETURNING report
        ``was_inserted=False`` on the second run.
        """
        a_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        b_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

        conn = _make_conn()
        conn.execute = AsyncMock(side_effect=_FakeFKError())
        conn.fetch = AsyncMock(side_effect=[
            [_make_signal_row({"mentioned_executives": ["Jane Smith", "John Doe"]})],
            [
                {"id": a_id, "canonical_name": "Jane Smith"},
                {"id": b_id, "canonical_name": "John Doe"},
            ],
        ])
        # accounts row + the upsert RETURNING result
        conn.fetchrow = AsyncMock(side_effect=[
            {"id": UUID(int=42)},                 # default account
            {"was_inserted": False},              # upsert ON CONFLICT path
        ])

        with patch.object(script, "acquire", lambda: _FakeAcquire(conn)):
            rollup = await script.run_press_co_mention(dry_run=False)

        assert rollup.pairs_inserted == 0
        assert rollup.pairs_updated == 1

    @pytest.mark.asyncio
    async def test_unmatched_name_skips_pair(self) -> None:
        a_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

        conn = _make_conn()
        conn.execute = AsyncMock(side_effect=_FakeFKError())
        conn.fetch = AsyncMock(side_effect=[
            [_make_signal_row({"mentioned_executives": ["Jane Smith", "Unknown Person"]})],
            # Persons index has only Jane.
            [{"id": a_id, "canonical_name": "Jane Smith"}],
        ])
        conn.fetchrow = AsyncMock(return_value={"id": UUID(int=42)})

        with patch.object(script, "acquire", lambda: _FakeAcquire(conn)):
            rollup = await script.run_press_co_mention(dry_run=True)

        assert rollup.pairs_considered == 1
        assert rollup.pairs_skipped_unmatched == 1
        assert rollup.pairs_inserted == 0


# ── Connection-type fallback ───────────────────────────────────────────────


# Patch script's expected exception types so the probe's except clauses
# actually match these test sentinels at runtime.
@pytest.fixture(autouse=True)
def _stub_asyncpg_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = MagicMock()
    fake_module.exceptions.CheckViolationError = _FakeCheckError
    fake_module.exceptions.ForeignKeyViolationError = _FakeFKError
    fake_module.exceptions.PostgresError = _FakePostgresError
    monkeypatch.setattr(script, "asyncpg", fake_module)


@pytest.mark.unit
class TestProbeConnectionType:
    @pytest.mark.asyncio
    async def test_probe_check_violation_returns_false(self) -> None:
        conn = _make_conn()
        conn.execute = AsyncMock(side_effect=_FakeCheckError("bad type"))
        result = await script._probe_connection_type_supported(
            conn, "co_mentioned_in_press",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_probe_fk_error_means_check_passed(self) -> None:
        conn = _make_conn()
        conn.execute = AsyncMock(side_effect=_FakeFKError("missing person"))
        result = await script._probe_connection_type_supported(
            conn, "co_mentioned_in_press",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_resolve_falls_back_when_preferred_rejected(self) -> None:
        conn = _make_conn()
        # First probe (preferred) → CheckViolation. Fallback isn't probed —
        # the resolver returns it unconditionally with a warning.
        conn.execute = AsyncMock(side_effect=_FakeCheckError("bad type"))
        kind, used_fallback = await script._resolve_connection_type(conn)
        assert kind == script.FALLBACK_CONNECTION_TYPE
        assert used_fallback is True

    @pytest.mark.asyncio
    async def test_resolve_uses_preferred_when_accepted(self) -> None:
        conn = _make_conn()
        conn.execute = AsyncMock(side_effect=_FakeFKError("missing person"))
        kind, used_fallback = await script._resolve_connection_type(conn)
        assert kind == script.PREFERRED_CONNECTION_TYPE
        assert used_fallback is False
