"""Tests for bulk_scholar_ingest — the per-account Semantic Scholar runner.

Pure-function unit coverage + a fake-conn integration that drives the full
algorithm with a stubbed httpx transport. No live Scholar calls; no live DB.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from credence.extractors.scholar import SEMANTIC_SCHOLAR_BASE_URL
from credence.jobs import bulk_scholar_ingest as job


# ── Fixtures ────────────────────────────────────────────────────────────────


ACCOUNT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
P_WEI = UUID("00000000-0000-0000-0000-000000000001")
P_MARCUS = UUID("00000000-0000-0000-0000-000000000002")
P_LIN = UUID("00000000-0000-0000-0000-000000000003")


def _record(
    paper_id: str,
    *,
    title: str = "Paper",
    venue: str = "V",
    year: int = 2023,
    citation_count: int = 1,
    author_count: int = 2,
    doi: str | None = None,
) -> dict[str, Any]:
    return {
        "paper_title": title,
        "venue": venue,
        "year": year,
        "citation_count": citation_count,
        "semantic_scholar_id": paper_id,
        "doi": doi,
        "author_count": author_count,
    }


# ── RateLimiter ─────────────────────────────────────────────────────────────


class _FakeClock:
    """Deterministic monotonic clock + sleep for RateLimiter tests."""

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
    await limiter.acquire(2)  # bucket empty
    assert clock.slept == []
    await limiter.acquire(1)  # must wait 3s for one token
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


# ── Confidence tier ─────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("count", [1, 2, 5])
def test_confidence_high_for_small_papers(count: int) -> None:
    assert job._confidence_for_author_count(count) == job.CONFIDENCE_SMALL_PAPER


@pytest.mark.unit
@pytest.mark.parametrize("count", [6, 10, 100])
def test_confidence_low_for_large_papers(count: int) -> None:
    assert job._confidence_for_author_count(count) == job.CONFIDENCE_LARGE_PAPER


# ── Pure planner: paper_index → emissions ───────────────────────────────────


@pytest.mark.unit
class TestPairIndexToEmissions:

    def test_single_author_paper_skipped(self) -> None:
        index = {
            "p1": [job.PaperEntry(prospect_id=P_WEI, record=_record("p1"))]
        }
        assert job._pair_index_to_emissions(index) == []

    def test_two_author_paper_emits_one_ordered_pair(self) -> None:
        rec = _record("p1", title="Co-authored")
        index = {
            "p1": [
                job.PaperEntry(prospect_id=P_MARCUS, record=rec),
                job.PaperEntry(prospect_id=P_WEI, record=rec),
            ]
        }
        emissions = job._pair_index_to_emissions(index)
        assert len(emissions) == 1
        # P_WEI < P_MARCUS lexically (UUIDs ending 001 vs 002).
        assert emissions[0].prospect_a == P_WEI
        assert emissions[0].prospect_b == P_MARCUS
        assert emissions[0].structured_value["connected_to"] == str(P_MARCUS)
        assert emissions[0].structured_value["semantic_scholar_id"] == "p1"
        assert emissions[0].structured_value["paper_title"] == "Co-authored"

    def test_three_author_paper_emits_three_pairs(self) -> None:
        rec = _record("p1")
        index = {
            "p1": [
                job.PaperEntry(prospect_id=P_WEI, record=rec),
                job.PaperEntry(prospect_id=P_MARCUS, record=rec),
                job.PaperEntry(prospect_id=P_LIN, record=rec),
            ]
        }
        emissions = job._pair_index_to_emissions(index)
        assert len(emissions) == 3
        # All ordered (a<b).
        for e in emissions:
            assert e.prospect_a < e.prospect_b
        pairs = {(e.prospect_a, e.prospect_b) for e in emissions}
        assert pairs == {
            (P_WEI, P_MARCUS),
            (P_WEI, P_LIN),
            (P_MARCUS, P_LIN),
        }

    def test_duplicate_prospect_under_one_paper_collapses(self) -> None:
        rec = _record("p1")
        index = {
            "p1": [
                job.PaperEntry(prospect_id=P_WEI, record=rec),
                job.PaperEntry(prospect_id=P_WEI, record=rec),  # dup
                job.PaperEntry(prospect_id=P_MARCUS, record=rec),
            ]
        }
        assert len(job._pair_index_to_emissions(index)) == 1

    def test_does_not_mutate_input_record(self) -> None:
        rec = _record("p1")
        before = dict(rec)
        index = {
            "p1": [
                job.PaperEntry(prospect_id=P_WEI, record=rec),
                job.PaperEntry(prospect_id=P_MARCUS, record=rec),
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
        "p1",
        str(P_MARCUS),
    )
    assert result is False
    assert len(conn.fetchval_calls) == 1
    sql, args = conn.fetchval_calls[0]
    assert "FROM signals" in sql
    assert "value->>'semantic_scholar_id'" in sql
    assert "value->>'connected_to'" in sql
    assert "LIMIT 1" in sql
    assert args == (P_WEI, job.SIGNAL_TYPE, "p1", str(P_MARCUS))


@pytest.mark.unit
async def test_signal_exists_returns_true_when_row_present() -> None:
    conn = _RecordingConn(fetchval_return=1)
    assert await job._signal_exists(
        conn,  # type: ignore[arg-type]
        P_WEI, job.SIGNAL_TYPE, "p1", str(P_MARCUS),
    ) is True


# ── End-to-end with stubbed transport + fake conn ───────────────────────────


def _author_search_body(author_id: str, name: str) -> dict[str, Any]:
    return {
        "total": 1,
        "data": [
            {
                "authorId": author_id,
                "name": name,
                "affiliations": [],
                "paperCount": 5,
            }
        ],
    }


def _papers_body(papers: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": papers}


def _scholar_paper(paper_id: str, year: int = 2023) -> dict[str, Any]:
    """Raw Semantic Scholar paper dict (NOT a formatted record)."""
    return {
        "paperId": paper_id,
        "title": f"Paper {paper_id}",
        "venue": "V",
        "year": year,
        "citationCount": 3,
        "externalIds": {"DOI": f"10.1/{paper_id}"},
        "authors": [
            {"authorId": "auth-x", "name": "Author X"},
            {"authorId": "auth-y", "name": "Author Y"},
        ],
    }


def _make_handler(per_author_papers: dict[str, list[dict[str, Any]]]):
    """Mock transport: dispatches by URL.

    ``/author/search?query=<name>`` → resolves to authorId = name slug.
    ``/author/{authorId}/papers`` → returns papers from per_author_papers.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/author/search" in path:
            query = request.url.params.get("query", "")
            slug = query.lower().replace(" ", "_")
            return httpx.Response(200, json=_author_search_body(slug, query))
        if "/papers" in path:
            # /author/<id>/papers
            parts = path.strip("/").split("/")
            try:
                author_id = parts[parts.index("author") + 1]
            except (ValueError, IndexError):
                return httpx.Response(404, text="bad path")
            return httpx.Response(
                200,
                json=_papers_body(per_author_papers.get(author_id, [])),
            )
        return httpx.Response(404, text=f"unhandled: {path}")

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
        # First conn: prospects fetch. Subsequent conns: signal writes.
        def fetchval_router(sql: str, args: tuple[Any, ...]) -> Any:
            if "FROM signals" in sql:
                key = (args[0], args[2], args[3])  # prospect_id, paper_id, connected_to
                if key in state["exists_keys"]:
                    return 1
                state["exists_keys"].add(key)
                return None
            return None

        conn = _RecordingConn(fetchval_return=fetchval_router)
        # Stash prospect rows for fetch() — set by the test below.
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
    # Two prospects, both "co-author" the same paper p1.
    patched_acquire["prospects"] = [
        {"id": P_WEI, "name": "Wei Chen"},
        {"id": P_MARCUS, "name": "Marcus Hale"},
    ]
    handler = _make_handler({
        "wei_chen": [_scholar_paper("p1")],
        "marcus_hale": [_scholar_paper("p1")],
    })
    no_wait_limiter = job.RateLimiter(
        capacity=1000, refill_seconds=0.001,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rollup = await job.bulk_scholar_ingest_account(
            ACCOUNT_ID,
            client=client,
            rate_limiter=no_wait_limiter,
        )

    assert rollup.prospects_scanned == 2
    assert rollup.prospects_with_papers == 2
    assert rollup.papers_indexed == 1
    assert rollup.pairs_emitted == 1
    assert rollup.signals_inserted == 1
    assert rollup.signals_skipped_dedup == 0
    assert rollup.errors == []

    # Verify INSERT happened with the right shape.
    write_conn = patched_acquire["conns"][1]
    inserts = [c for c in write_conn.execute_calls if "INSERT INTO signals" in c[0]]
    assert len(inserts) == 1
    sql, args = inserts[0]
    # Args: prospect_id, account_id, source, signal_type, json_value, confidence
    assert args[0] == P_WEI  # prospect_a
    assert args[1] == ACCOUNT_ID
    assert args[2] == job.SIGNAL_SOURCE
    assert args[3] == job.SIGNAL_TYPE
    assert args[5] == job.CONFIDENCE_SMALL_PAPER
    # args[4] is the dict passed directly to asyncpg's jsonb codec.
    assert isinstance(args[4], dict)
    assert args[4]["connected_to"] == str(P_MARCUS)


@pytest.mark.unit
async def test_end_to_end_rerun_dedupes(
    patched_acquire: dict[str, Any],
) -> None:
    patched_acquire["prospects"] = [
        {"id": P_WEI, "name": "Wei Chen"},
        {"id": P_MARCUS, "name": "Marcus Hale"},
    ]
    handler = _make_handler({
        "wei_chen": [_scholar_paper("p1")],
        "marcus_hale": [_scholar_paper("p1")],
    })
    no_wait_limiter = job.RateLimiter(capacity=1000, refill_seconds=0.001)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await job.bulk_scholar_ingest_account(
            ACCOUNT_ID, client=client, rate_limiter=no_wait_limiter,
        )
        second = await job.bulk_scholar_ingest_account(
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
    handler = _make_handler({
        "wei_chen": [_scholar_paper("p1")],
        "marcus_hale": [_scholar_paper("p1")],
    })
    no_wait_limiter = job.RateLimiter(capacity=1000, refill_seconds=0.001)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rollup = await job.bulk_scholar_ingest_account(
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
    captured: dict[str, int] = {"author_search_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/author/search" in request.url.path:
            captured["author_search_calls"] += 1
            return httpx.Response(200, json=_author_search_body("wei_chen", "Wei Chen"))
        return httpx.Response(200, json=_papers_body([]))

    no_wait_limiter = job.RateLimiter(capacity=1000, refill_seconds=0.001)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rollup = await job.bulk_scholar_ingest_account(
            ACCOUNT_ID, client=client, rate_limiter=no_wait_limiter,
        )

    assert rollup.prospects_scanned == 2
    assert rollup.prospects_skipped_short_name == 1
    # Only Wei Chen triggered author/search.
    assert captured["author_search_calls"] == 1


@pytest.mark.unit
async def test_no_co_authored_papers_emits_nothing(
    patched_acquire: dict[str, Any],
) -> None:
    """Each prospect has their own papers; no overlap → no emissions."""
    patched_acquire["prospects"] = [
        {"id": P_WEI, "name": "Wei Chen"},
        {"id": P_MARCUS, "name": "Marcus Hale"},
    ]
    handler = _make_handler({
        "wei_chen": [_scholar_paper("paper-wei")],
        "marcus_hale": [_scholar_paper("paper-marcus")],
    })
    no_wait_limiter = job.RateLimiter(capacity=1000, refill_seconds=0.001)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rollup = await job.bulk_scholar_ingest_account(
            ACCOUNT_ID, client=client, rate_limiter=no_wait_limiter,
        )
    assert rollup.papers_indexed == 2
    assert rollup.pairs_emitted == 0
    assert rollup.signals_inserted == 0


# ── v3 helpers (papers + paper_authors) ─────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("not-a-year", None),
        (1899, None),
        (1900, 1900),
        (2024, 2024),
    ],
)
def test_safe_year_squashes_garbage(raw: Any, expected: int | None) -> None:
    record = {"year": raw}
    assert job._safe_year(record) == expected


class _V3Conn:
    """Recording conn with separate fetchval routing for SELECT id RETURNING."""

    def __init__(
        self,
        *,
        person_rows: list[dict[str, Any]] | None = None,
        paper_id_for_ssid: dict[str, UUID] | None = None,
    ) -> None:
        self._person_rows = person_rows or []
        self._paper_id_for_ssid = paper_id_for_ssid or {}
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        # By default every paper_authors INSERT reports 1 row affected.
        self._execute_status = "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.fetch_calls.append((sql, args))
        if "FROM persons" in sql:
            return self._person_rows
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.fetchval_calls.append((sql, args))
        if "INSERT INTO papers" in sql:
            ssid = args[0]
            return self._paper_id_for_ssid.get(ssid, uuid4())
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return self._execute_status


@pytest.mark.unit
async def test_resolve_persons_for_prospects_maps_rows() -> None:
    person_id_a = uuid4()
    person_id_b = uuid4()
    conn = _V3Conn(
        person_rows=[
            {"id": person_id_a, "source_prospect_id": P_WEI},
            {"id": person_id_b, "source_prospect_id": P_MARCUS},
        ]
    )
    result = await job._resolve_persons_for_prospects(
        conn,  # type: ignore[arg-type]
        ACCOUNT_ID,
        [P_WEI, P_MARCUS, P_LIN],
    )
    assert result == {P_WEI: person_id_a, P_MARCUS: person_id_b}
    sql, args = conn.fetch_calls[0]
    assert "FROM persons" in sql
    assert "source_prospect_id = ANY" in sql
    assert args[0] == ACCOUNT_ID


@pytest.mark.unit
async def test_resolve_persons_for_prospects_empty_returns_empty() -> None:
    conn = _V3Conn()
    assert (
        await job._resolve_persons_for_prospects(
            conn,  # type: ignore[arg-type]
            ACCOUNT_ID,
            [],
        )
        == {}
    )
    assert conn.fetch_calls == []


@pytest.mark.unit
async def test_upsert_paper_passes_record_fields() -> None:
    paper_id = uuid4()
    conn = _V3Conn(paper_id_for_ssid={"p1": paper_id})
    record = _record("p1", year=2023, citation_count=5, doi="10.1/p1")
    out = await job._upsert_paper(conn, ACCOUNT_ID, record)  # type: ignore[arg-type]
    assert out == paper_id
    sql, args = conn.fetchval_calls[0]
    assert "INSERT INTO papers" in sql
    assert "ON CONFLICT (account_id, semantic_scholar_id)" in sql
    # 8 args: ssid, title, venue, year, citation_count, doi, url, account_id
    assert args[0] == "p1"
    assert args[1] == "Paper"  # _record default
    assert args[2] == "V"
    assert args[3] == 2023
    assert args[4] == 5
    assert args[5] == "10.1/p1"
    assert args[6] is None
    assert args[7] == ACCOUNT_ID


@pytest.mark.unit
async def test_upsert_paper_skips_when_missing_required_fields() -> None:
    conn = _V3Conn()
    assert await job._upsert_paper(conn, ACCOUNT_ID, {"semantic_scholar_id": ""}) is None  # type: ignore[arg-type]
    assert await job._upsert_paper(conn, ACCOUNT_ID, {"paper_title": "Only"}) is None  # type: ignore[arg-type]
    assert conn.fetchval_calls == []


@pytest.mark.unit
async def test_upsert_paper_author_returns_true_when_inserted() -> None:
    conn = _V3Conn()
    paper_id = uuid4()
    person_id = uuid4()
    out = await job._upsert_paper_author(
        conn, ACCOUNT_ID, paper_id, person_id, 2  # type: ignore[arg-type]
    )
    assert out is True
    sql, args = conn.execute_calls[0]
    assert "INSERT INTO paper_authors" in sql
    assert "ON CONFLICT (paper_id, person_id) DO NOTHING" in sql
    assert args == (paper_id, person_id, 2, ACCOUNT_ID)


@pytest.mark.unit
async def test_upsert_paper_author_returns_false_on_conflict() -> None:
    conn = _V3Conn()
    conn._execute_status = "INSERT 0 0"
    out = await job._upsert_paper_author(
        conn, ACCOUNT_ID, uuid4(), uuid4(), None  # type: ignore[arg-type]
    )
    assert out is False


@pytest.mark.unit
async def test_materialize_v3_skips_prospects_without_persons_rows() -> None:
    """Prospects without an enriched persons row must NOT produce paper_authors."""
    paper_id = uuid4()
    person_for_wei = uuid4()
    conn = _V3Conn(
        person_rows=[{"id": person_for_wei, "source_prospect_id": P_WEI}],
        paper_id_for_ssid={"p1": paper_id},
    )
    rollup = job.ScholarIngestRollup(account_id=ACCOUNT_ID, write_v3=True)
    paper_index = {
        "p1": [
            job.PaperEntry(prospect_id=P_WEI, record=_record("p1")),
            job.PaperEntry(prospect_id=P_MARCUS, record=_record("p1")),
        ]
    }
    await job._materialize_v3_for_paper_index(
        conn, ACCOUNT_ID, paper_index, rollup  # type: ignore[arg-type]
    )
    assert rollup.papers_upserted == 1
    assert rollup.paper_authors_upserted == 1  # only P_WEI had a person row
    assert rollup.prospects_without_person_row == 1
    # Confirm exactly one paper_authors INSERT executed (for P_WEI).
    inserts = [c for c in conn.execute_calls if "paper_authors" in c[0]]
    assert len(inserts) == 1
    assert inserts[0][1][1] == person_for_wei


@pytest.mark.unit
async def test_materialize_v3_dedups_same_prospect_twice_under_one_paper() -> None:
    paper_id = uuid4()
    person_id = uuid4()
    conn = _V3Conn(
        person_rows=[{"id": person_id, "source_prospect_id": P_WEI}],
        paper_id_for_ssid={"p1": paper_id},
    )
    rollup = job.ScholarIngestRollup(account_id=ACCOUNT_ID, write_v3=True)
    paper_index = {
        "p1": [
            job.PaperEntry(prospect_id=P_WEI, record=_record("p1")),
            job.PaperEntry(prospect_id=P_WEI, record=_record("p1")),
        ]
    }
    await job._materialize_v3_for_paper_index(
        conn, ACCOUNT_ID, paper_index, rollup  # type: ignore[arg-type]
    )
    assert rollup.papers_upserted == 1
    # Same prospect twice → one paper_author insert, not two.
    assert rollup.paper_authors_upserted == 1


# ── CLI parser ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cli_requires_scope() -> None:
    parser = job._build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


@pytest.mark.unit
def test_cli_parses_account_id_path() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(["--account-id", str(ACCOUNT_ID), "--limit", "10", "--dry-run"])
    assert args.account_id == ACCOUNT_ID
    assert args.limit == 10
    assert args.dry_run is True
    assert args.all_accounts is False
    assert args.write_v3 is False


@pytest.mark.unit
def test_cli_parses_write_v3_flag() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(
        ["--account-id", str(ACCOUNT_ID), "--write-v3"]
    )
    assert args.write_v3 is True


@pytest.mark.unit
def test_cli_parses_all_accounts_path() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(["--all-accounts", "--max-papers-per-person", "25", "--concurrency", "4"])
    assert args.all_accounts is True
    assert args.max_papers_per_person == 25
    assert args.concurrency == 4


# ── Smoke: SEMANTIC_SCHOLAR_BASE_URL is the expected production URL ────────


@pytest.mark.unit
def test_semantic_scholar_base_url_unchanged() -> None:
    """Lock the base URL — if Scholar moves, this fails fast."""
    assert SEMANTIC_SCHOLAR_BASE_URL.startswith("https://api.semanticscholar.org/graph/v1/")
