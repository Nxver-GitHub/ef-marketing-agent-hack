"""Tests for bulk_uspto_ingest — the per-account USPTO patent runner.

Pure-function unit coverage + a fake-conn integration that drives the full
algorithm with a stubbed httpx transport. No live USPTO calls; no live DB.

Mirrors `test_bulk_scholar_ingest.py` shape with patent-shaped data and the
extractor's auth gate ('USPTO_USE_ODP=1' + 'USPTO_ODP_API_KEY=<key>').
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from credence.jobs import bulk_uspto_ingest as job


# ── Fixtures ────────────────────────────────────────────────────────────────


ACCOUNT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
P_WEI = UUID("00000000-0000-0000-0000-000000000001")
P_MARCUS = UUID("00000000-0000-0000-0000-000000000002")
P_LIN = UUID("00000000-0000-0000-0000-000000000003")


def _record(
    patent_id: str,
    *,
    title: str = "Patent",
    grant_date: str = "2020-01-01",
    filing_date: str = "2018-06-01",
    assignee: str = "Acme",
) -> dict[str, Any]:
    """A formatted-record dict — what _format_patent_record returns."""
    return {
        "patent_number": patent_id,
        "patent_title": title,
        "filing_date": filing_date,
        "grant_date": grant_date,
        "assignee": assignee,
        "uspto_url": f"https://patents.google.com/patent/US{patent_id}/",
    }


@pytest.fixture(autouse=True)
def _odp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests need the ODP endpoint to be configured. The 'aborted'
    test undoes this fixture explicitly with `monkeypatch.delenv`."""
    monkeypatch.setenv("USPTO_USE_ODP", "1")
    monkeypatch.setenv("USPTO_ODP_API_KEY", "test-key-shhh")


# ── RateLimiter ─────────────────────────────────────────────────────────────


class _FakeClock:
    """Deterministic monotonic clock + sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.mark.unit
async def test_rate_limiter_starts_full() -> None:
    clock = _FakeClock()
    limiter = job.RateLimiter(
        capacity=4, refill_seconds=1.0,
        time_func=clock.time, sleep_func=clock.sleep,
    )
    assert limiter.tokens == 4.0
    await limiter.acquire(1)
    assert limiter.tokens == 3.0
    assert clock.slept == []


@pytest.mark.unit
async def test_rate_limiter_drains_then_refills() -> None:
    clock = _FakeClock()
    limiter = job.RateLimiter(
        capacity=2, refill_seconds=3.0,
        time_func=clock.time, sleep_func=clock.sleep,
    )
    await limiter.acquire(2)
    assert clock.slept == []
    await limiter.acquire(1)
    assert clock.slept == [3.0]


@pytest.mark.unit
async def test_rate_limiter_rejects_oversize_request() -> None:
    limiter = job.RateLimiter(capacity=4, refill_seconds=1.0)
    with pytest.raises(ValueError):
        await limiter.acquire(5)


@pytest.mark.unit
async def test_rate_limiter_zero_acquire_is_noop() -> None:
    clock = _FakeClock()
    limiter = job.RateLimiter(
        capacity=2, refill_seconds=1.0,
        time_func=clock.time, sleep_func=clock.sleep,
    )
    await limiter.acquire(0)
    assert limiter.tokens == 2.0
    assert clock.slept == []


# ── Pure planner: patent_index → emissions ───────────────────────────────────


@pytest.mark.unit
class TestPairIndexToEmissions:

    def test_single_inventor_patent_skipped(self) -> None:
        index = {
            "10000001": [job.PatentEntry(prospect_id=P_WEI, record=_record("10000001"))]
        }
        assert job._pair_index_to_emissions(index) == []

    def test_two_inventor_patent_emits_one_ordered_pair(self) -> None:
        rec = _record("10000001", title="Co-invented method")
        index = {
            "10000001": [
                job.PatentEntry(prospect_id=P_MARCUS, record=rec),
                job.PatentEntry(prospect_id=P_WEI, record=rec),
            ]
        }
        emissions = job._pair_index_to_emissions(index)
        assert len(emissions) == 1
        # P_WEI < P_MARCUS lexically (UUIDs ending 001 vs 002).
        assert emissions[0].prospect_a == P_WEI
        assert emissions[0].prospect_b == P_MARCUS
        assert emissions[0].structured_value["connected_to"] == str(P_MARCUS)
        assert emissions[0].structured_value["patent_number"] == "10000001"
        assert emissions[0].structured_value["patent_title"] == "Co-invented method"

    def test_three_inventor_patent_emits_three_pairs(self) -> None:
        rec = _record("10000001")
        index = {
            "10000001": [
                job.PatentEntry(prospect_id=P_WEI, record=rec),
                job.PatentEntry(prospect_id=P_MARCUS, record=rec),
                job.PatentEntry(prospect_id=P_LIN, record=rec),
            ]
        }
        emissions = job._pair_index_to_emissions(index)
        assert len(emissions) == 3
        for e in emissions:
            assert e.prospect_a < e.prospect_b
        pairs = {(e.prospect_a, e.prospect_b) for e in emissions}
        assert pairs == {
            (P_WEI, P_MARCUS),
            (P_WEI, P_LIN),
            (P_MARCUS, P_LIN),
        }

    def test_duplicate_prospect_under_one_patent_collapses(self) -> None:
        rec = _record("10000001")
        index = {
            "10000001": [
                job.PatentEntry(prospect_id=P_WEI, record=rec),
                job.PatentEntry(prospect_id=P_WEI, record=rec),  # dup
                job.PatentEntry(prospect_id=P_MARCUS, record=rec),
            ]
        }
        assert len(job._pair_index_to_emissions(index)) == 1

    def test_does_not_mutate_input_record(self) -> None:
        rec = _record("10000001")
        before = dict(rec)
        index = {
            "10000001": [
                job.PatentEntry(prospect_id=P_WEI, record=rec),
                job.PatentEntry(prospect_id=P_MARCUS, record=rec),
            ]
        }
        job._pair_index_to_emissions(index)
        assert rec == before
        assert "connected_to" not in rec


# ── Name eligibility ────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,expected",
    [
        ("Wei Chen", True),
        ("Wei W. Chen", True),
        ("Wei", False),
        ("", False),
        (None, False),
        ("   ", False),
    ],
)
def test_has_enough_name_tokens(name: str | None, expected: bool) -> None:
    assert job._has_enough_name_tokens(name) is expected


# ── _signal_exists query well-formed ────────────────────────────────────────


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
        P_WEI,
        job.SIGNAL_TYPE,
        "10000001",
        str(P_MARCUS),
    )
    assert result is False
    assert len(conn.fetchval_calls) == 1
    sql, args = conn.fetchval_calls[0]
    assert "FROM signals" in sql
    assert "value->>'patent_number'" in sql
    assert "value->>'connected_to'" in sql
    assert "LIMIT 1" in sql
    assert args == (P_WEI, job.SIGNAL_TYPE, "10000001", str(P_MARCUS))


@pytest.mark.unit
async def test_signal_exists_returns_true_when_row_present() -> None:
    conn = _RecordingConn(fetchval_return=1)
    assert await job._signal_exists(
        conn,  # type: ignore[arg-type]
        P_WEI, job.SIGNAL_TYPE, "10000001", str(P_MARCUS),
    ) is True


# ── End-to-end with stubbed transport + fake conn ───────────────────────────


def _uspto_patent(
    patent_id: str,
    inventors: list[tuple[str, str]],
    *,
    title: str | None = None,
    assignee: str = "Acme",
) -> dict[str, Any]:
    """Raw USPTO/PatentsView patent dict (NOT a formatted record).

    `inventors` is a list of (first, last) tuples.
    """
    return {
        "patent_id": patent_id,
        "patent_title": title or f"Patent {patent_id}",
        "patent_date": "2020-04-15",
        "patent_filing_date": "2018-09-01",
        "inventors": [
            {"inventor_name_first": f, "inventor_name_last": l}
            for f, l in inventors
        ],
        "assignees": [{"assignee_organization": assignee}],
    }


def _make_handler(per_query_patents: dict[tuple[str, str], list[dict[str, Any]]]):
    """Mock transport: returns patents matching the (first, last) name in the query.

    The real ODP query JSON has ``_and: [{_contains: inventor_name_first: <first>},
    {_contains: inventor_name_last: <last>}]``. We extract those tokens from the
    JSON-encoded q parameter and dispatch.
    """

    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        q_param = request.url.params.get("q", "")
        try:
            q = _json.loads(q_param)
        except (TypeError, ValueError):
            return httpx.Response(400, text="bad q")
        clauses = q.get("_and") or []
        first = last = None
        for c in clauses:
            contains = c.get("_contains", {})
            if "inventors.inventor_name_first" in contains:
                first = contains["inventors.inventor_name_first"]
            elif "inventors.inventor_name_last" in contains:
                last = contains["inventors.inventor_name_last"]
        if not first or not last:
            return httpx.Response(200, json={"patents": []})
        patents = per_query_patents.get((first, last), [])
        return httpx.Response(200, json={"patents": patents})

    return handler


class _FakeAcquire:
    """Async context manager yielding a single _RecordingConn."""

    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _RecordingConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


@pytest.fixture
def patched_acquire(monkeypatch: pytest.MonkeyPatch):
    """Replace credence.db.acquire used by the job module with a fake."""
    state: dict[str, Any] = {"conns": [], "exists_keys": set()}

    def make_conn() -> _RecordingConn:
        def fetchval_router(sql: str, args: tuple[Any, ...]) -> Any:
            if "FROM signals" in sql:
                # args: prospect_id, signal_type, patent_number, connected_to
                key = (args[0], args[2], args[3])
                if key in state["exists_keys"]:
                    return 1
                state["exists_keys"].add(key)
                return None
            return None

        conn = _RecordingConn(fetchval_return=fetchval_router)
        conn._prospects = state.get("prospects", [])  # type: ignore[attr-defined]

        async def fake_fetch(sql: str, *args: Any) -> list[Any]:
            conn.fetch_calls.append((sql, args))
            if "FROM prospects" in sql:
                return conn._prospects  # type: ignore[attr-defined]
            return []

        conn.fetch = fake_fetch  # type: ignore[method-assign]
        state["conns"].append(conn)
        return conn

    def fake_acquire() -> _FakeAcquire:
        return _FakeAcquire(make_conn())

    monkeypatch.setattr(job, "acquire", fake_acquire)
    return state


@pytest.mark.unit
async def test_end_to_end_emits_pair_and_inserts_signals(
    patched_acquire: dict[str, Any],
) -> None:
    # Two prospects, both listed as co-inventors on the same patent.
    patched_acquire["prospects"] = [
        {"id": P_WEI, "name": "Wei Chen"},
        {"id": P_MARCUS, "name": "Marcus Hale"},
    ]
    co_invented = _uspto_patent(
        "10000001",
        inventors=[("Wei", "Chen"), ("Marcus", "Hale")],
    )
    handler = _make_handler({
        ("Wei", "Chen"): [co_invented],
        ("Marcus", "Hale"): [co_invented],
    })
    no_wait_limiter = job.RateLimiter(capacity=1000, refill_seconds=0.001)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rollup = await job.bulk_uspto_ingest_account(
            ACCOUNT_ID,
            client=client,
            rate_limiter=no_wait_limiter,
        )

    assert rollup.aborted_no_api_key is False
    assert rollup.prospects_scanned == 2
    assert rollup.prospects_with_patents == 2
    assert rollup.patents_indexed == 1
    assert rollup.pairs_emitted == 1
    assert rollup.signals_inserted == 1
    assert rollup.signals_skipped_dedup == 0
    assert rollup.errors == []

    write_conn = patched_acquire["conns"][1]
    inserts = [c for c in write_conn.execute_calls if "INSERT INTO signals" in c[0]]
    assert len(inserts) == 1
    sql, args = inserts[0]
    # Args: prospect_id, account_id, source, signal_type, dict_value, confidence
    assert args[0] == P_WEI  # prospect_a
    assert args[1] == ACCOUNT_ID
    assert args[2] == job.SIGNAL_SOURCE
    assert args[3] == job.SIGNAL_TYPE
    assert args[5] == job.CONFIDENCE_PATENT_CO_INVENTOR
    # args[4] is the dict passed directly to asyncpg's jsonb codec.
    assert isinstance(args[4], dict)
    assert args[4]["connected_to"] == str(P_MARCUS)
    assert args[4]["patent_number"] == "10000001"


@pytest.mark.unit
async def test_end_to_end_rerun_dedupes(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["prospects"] = [
        {"id": P_WEI, "name": "Wei Chen"},
        {"id": P_MARCUS, "name": "Marcus Hale"},
    ]
    co_invented = _uspto_patent(
        "10000001",
        inventors=[("Wei", "Chen"), ("Marcus", "Hale")],
    )
    handler = _make_handler({
        ("Wei", "Chen"): [co_invented],
        ("Marcus", "Hale"): [co_invented],
    })
    no_wait_limiter = job.RateLimiter(capacity=1000, refill_seconds=0.001)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await job.bulk_uspto_ingest_account(
            ACCOUNT_ID, client=client, rate_limiter=no_wait_limiter,
        )
        second = await job.bulk_uspto_ingest_account(
            ACCOUNT_ID, client=client, rate_limiter=no_wait_limiter,
        )

    assert first.signals_inserted == 1
    assert second.signals_inserted == 0
    assert second.signals_skipped_dedup == 1


@pytest.mark.unit
async def test_dry_run_writes_nothing(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["prospects"] = [
        {"id": P_WEI, "name": "Wei Chen"},
        {"id": P_MARCUS, "name": "Marcus Hale"},
    ]
    co_invented = _uspto_patent(
        "10000001",
        inventors=[("Wei", "Chen"), ("Marcus", "Hale")],
    )
    handler = _make_handler({
        ("Wei", "Chen"): [co_invented],
        ("Marcus", "Hale"): [co_invented],
    })
    no_wait_limiter = job.RateLimiter(capacity=1000, refill_seconds=0.001)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rollup = await job.bulk_uspto_ingest_account(
            ACCOUNT_ID,
            client=client,
            rate_limiter=no_wait_limiter,
            dry_run=True,
        )
    assert rollup.dry_run is True
    assert rollup.pairs_emitted == 1
    assert rollup.signals_inserted == 0
    # Only one acquire (for prospects fetch); no second acquire for writes.
    assert len(patched_acquire["conns"]) == 1


@pytest.mark.unit
async def test_single_token_names_skipped(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["prospects"] = [
        {"id": P_WEI, "name": "Wei Chen"},
        {"id": P_LIN, "name": "Lin"},  # single token — skipped
    ]
    captured: dict[str, int] = {"patent_search_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["patent_search_calls"] += 1
        return httpx.Response(200, json={"patents": []})

    no_wait_limiter = job.RateLimiter(capacity=1000, refill_seconds=0.001)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rollup = await job.bulk_uspto_ingest_account(
            ACCOUNT_ID, client=client, rate_limiter=no_wait_limiter,
        )

    assert rollup.prospects_scanned == 2
    assert rollup.prospects_skipped_short_name == 1
    # Only Wei Chen triggered a USPTO query.
    assert captured["patent_search_calls"] == 1


@pytest.mark.unit
async def test_no_co_invented_patents_emits_nothing(
    patched_acquire: dict[str, Any],
) -> None:
    """Each prospect has their own patents; no overlap → no emissions."""
    patched_acquire["prospects"] = [
        {"id": P_WEI, "name": "Wei Chen"},
        {"id": P_MARCUS, "name": "Marcus Hale"},
    ]
    handler = _make_handler({
        ("Wei", "Chen"): [_uspto_patent("PWEI", inventors=[("Wei", "Chen")])],
        ("Marcus", "Hale"): [_uspto_patent("PMARC", inventors=[("Marcus", "Hale")])],
    })
    no_wait_limiter = job.RateLimiter(capacity=1000, refill_seconds=0.001)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rollup = await job.bulk_uspto_ingest_account(
            ACCOUNT_ID, client=client, rate_limiter=no_wait_limiter,
        )
    assert rollup.patents_indexed == 2
    assert rollup.pairs_emitted == 0
    assert rollup.signals_inserted == 0


@pytest.mark.unit
async def test_aborts_when_no_api_key(
    monkeypatch: pytest.MonkeyPatch,
    patched_acquire: dict[str, Any],
) -> None:
    """Without USPTO_USE_ODP / USPTO_ODP_API_KEY the runner aborts cleanly.

    Mirrors what `_resolve_endpoint_config` raises in extractors/patents.py
    when the environment is incomplete.
    """
    monkeypatch.delenv("USPTO_USE_ODP", raising=False)
    monkeypatch.delenv("USPTO_ODP_API_KEY", raising=False)
    patched_acquire["prospects"] = [{"id": P_WEI, "name": "Wei Chen"}]
    no_wait_limiter = job.RateLimiter(capacity=1000, refill_seconds=0.001)

    async with httpx.AsyncClient() as client:
        rollup = await job.bulk_uspto_ingest_account(
            ACCOUNT_ID, client=client, rate_limiter=no_wait_limiter,
        )

    assert rollup.aborted_no_api_key is True
    assert rollup.prospects_scanned == 0
    assert rollup.signals_inserted == 0
    assert len(rollup.errors) == 1
    # Should have aborted before the prospect fetch — no DB connections.
    assert len(patched_acquire["conns"]) == 0


@pytest.mark.unit
def test_confidence_constant_matches_strength_table() -> None:
    """Sanity: 0.95 is the canonical patent_co_inventor base in CLAUDE.md."""
    from credence.strength import STRENGTH_TABLE

    assert (
        job.CONFIDENCE_PATENT_CO_INVENTOR
        == STRENGTH_TABLE["patent_co_inventor"]
    )


# ── CLI parser ──────────────────────────────────────────────────────────────


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
    args = parser.parse_args(
        ["--all-accounts", "--max-patents-per-person", "25", "--concurrency", "4"]
    )
    assert args.all_accounts is True
    assert args.max_patents_per_person == 25
    assert args.concurrency == 4
