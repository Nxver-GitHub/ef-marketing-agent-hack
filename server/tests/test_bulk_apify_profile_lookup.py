"""Tests for ``bulk_apify_profile_lookup`` — profile-by-URL Apify runner.

Covers the new ``fetch_profile_by_url`` extension to the apify module
plus the bulk runner (selector SQL, fetch fan-out, persist path, dedupe
marker, dry-run, failure handling, cost accounting). All tests are
fully mocked — no live Apify calls, no real DB.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from credence.enrichment import apify as apify_mod
from credence.enrichment.apify import (
    MODE_FULL,
    MODE_SHORT,
    PROFILE_ACTOR_ID,
    ApifyProfile,
)
from credence.jobs import bulk_apify_profile_lookup as job


# ── Fixture data ─────────────────────────────────────────────────────────────


ACCOUNT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
P1 = UUID("00000000-0000-0000-0000-000000000001")
P2 = UUID("00000000-0000-0000-0000-000000000002")
P3 = UUID("00000000-0000-0000-0000-000000000003")
P4 = UUID("00000000-0000-0000-0000-000000000004")
P5 = UUID("00000000-0000-0000-0000-000000000005")


def _mk_raw_profile(
    *,
    public_identifier: str = "rhonda-whitney-28183b28",
    first: str = "Rhonda",
    last: str = "Whitney",
    company: str = "Marvell Technology",
    title: str = "GSOC Manager",
) -> dict[str, Any]:
    """Real harvestapi/linkedin-profile-scraper item shape (mirrors the
    company-employees actor — same vendor)."""
    return {
        "id": f"ACoAA{public_identifier}",
        "publicIdentifier": public_identifier,
        "linkedinUrl": f"https://www.linkedin.com/in/{public_identifier}",
        "firstName": first,
        "lastName": last,
        "headline": "Test headline",
        "location": {
            "linkedinText": "Hayward, CA, US",
            "countryCode": "US",
        },
        "emails": [],
        "currentPosition": [
            {
                "position": title,
                "companyName": company,
                "startDate": {"month": "Jan", "year": 2026},
                "endDate": {"text": "Present"},
            }
        ],
        "experience": [
            {
                "position": title,
                "companyName": company,
                "startDate": {"month": "Jan", "year": 2026},
                "endDate": {"text": "Present"},
            }
        ],
        "education": [
            {
                "schoolName": "MIT",
                "degree": "BS",
                "fieldOfStudy": "EECS",
                "startDate": {"year": 2018},
                "endDate": {"year": 2022},
            }
        ],
        "skills": [{"name": "Leadership"}],
        "certifications": [],
        "languages": [],
        "connectionsCount": 100,
        "followerCount": 100,
        "premium": False,
        "verified": True,
        "openToWork": False,
        "hiring": False,
        "registeredAt": "2020-01-01T00:00:00Z",
        "publications": [],
        "patents": [],
        "honorsAndAwards": [],
        "organizations": [],
    }


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _apify_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "fake-test-token")


# ── _build_marker_value ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_build_marker_value_shape() -> None:
    val = job._build_marker_value()
    assert val["method"] == "apify_profile_by_url"
    assert "fetched_at" in val
    assert isinstance(val["fetched_at"], str)
    # ISO-format-ish (just ensure it's not empty)
    assert "T" in val["fetched_at"]


# ── _per_profile_cost_cents ──────────────────────────────────────────────────


@pytest.mark.unit
def test_per_profile_cost_cents_full_default() -> None:
    """0.8¢ per full profile → ceiling = 1¢."""
    assert job._per_profile_cost_cents(MODE_FULL) == 1


@pytest.mark.unit
def test_per_profile_cost_cents_short() -> None:
    """0.4¢ per short profile → ceiling = 1¢."""
    assert job._per_profile_cost_cents(MODE_SHORT) == 1


@pytest.mark.unit
def test_per_profile_cost_cents_unknown_falls_back_to_full() -> None:
    assert job._per_profile_cost_cents("garbage") == 1


# ── fetch_profile_by_url (the new apify module function) ─────────────────────


@pytest.mark.unit
async def test_fetch_profile_by_url_happy_path() -> None:
    """Mock returns one item; we get an EnrichmentResult with one ApifyProfile."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(201, json=[_mk_raw_profile()])

    async with _client_with(handler) as client:
        result = await apify_mod.fetch_profile_by_url(
            "https://www.linkedin.com/in/rhonda-whitney-28183b28",
            client=client,
        )

    assert result is not None
    assert len(result.profiles) == 1
    p = result.profiles[0]
    assert isinstance(p, ApifyProfile)
    assert p.linkedin_url == "https://www.linkedin.com/in/rhonda-whitney-28183b28"
    # 1 item × 0.4¢ (no_email) = 0.4 → ceil = 1¢
    assert result.cost_cents == 1
    # The actor and queries arg landed correctly in the request
    assert PROFILE_ACTOR_ID in captured["url"]
    assert "run-sync-get-dataset-items" in captured["url"]
    assert captured["body"]["queries"] == [
        "https://www.linkedin.com/in/rhonda-whitney-28183b28"
    ]
    # Default mode for the profile-by-url call is PROFILE_MODE_NO_EMAIL
    from credence.enrichment.apify import PROFILE_MODE_NO_EMAIL
    assert captured["body"]["profileScraperMode"] == PROFILE_MODE_NO_EMAIL


@pytest.mark.unit
async def test_fetch_profile_by_url_empty_dataset_returns_no_profiles() -> None:
    """Actor returned 200 with empty list — caller treats as no-match."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async with _client_with(handler) as client:
        result = await apify_mod.fetch_profile_by_url(
            "https://www.linkedin.com/in/missing", client=client,
        )
    assert result is not None
    assert result.profiles == []
    assert result.cost_cents == 0


@pytest.mark.unit
async def test_fetch_profile_by_url_500_returns_none() -> None:
    """Actor 5xx → None. Bulk runner counts that as ``profiles_failed``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with _client_with(handler) as client:
        result = await apify_mod.fetch_profile_by_url(
            "https://www.linkedin.com/in/x", client=client,
        )
    assert result is None


@pytest.mark.unit
async def test_fetch_profile_by_url_malformed_json_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    async with _client_with(handler) as client:
        result = await apify_mod.fetch_profile_by_url(
            "https://www.linkedin.com/in/x", client=client,
        )
    assert result is None


@pytest.mark.unit
async def test_fetch_profile_by_url_no_token_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    async with _client_with(handler) as client:
        result = await apify_mod.fetch_profile_by_url(
            "https://www.linkedin.com/in/x", client=client,
        )
    assert result is None
    assert called is False


@pytest.mark.unit
async def test_fetch_profile_by_url_blank_url_returns_none() -> None:
    """Empty URL short-circuits before any HTTP — caller-side guard."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP should not be called for blank URL")

    async with _client_with(handler) as client:
        result = await apify_mod.fetch_profile_by_url("   ", client=client)
    assert result is None


# ── start_profile_by_url_run (the new batched-async submitter) ───────────────


@pytest.mark.unit
async def test_start_profile_by_url_run_returns_run_data() -> None:
    """Submits to /acts/{PROFILE_ACTOR_ID}/runs with queries+mode and
    returns the run document."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(
            201,
            json={
                "data": {
                    "id": "run-xyz-1",
                    "status": "READY",
                    "defaultDatasetId": "ds-xyz-1",
                }
            },
        )

    urls = [
        "https://www.linkedin.com/in/a",
        "https://www.linkedin.com/in/b",
        "https://www.linkedin.com/in/c",
    ]
    async with _client_with(handler) as client:
        run_data = await apify_mod.start_profile_by_url_run(urls, client=client)

    assert run_data is not None
    assert run_data["id"] == "run-xyz-1"
    assert run_data["defaultDatasetId"] == "ds-xyz-1"
    # Payload landed correctly
    assert PROFILE_ACTOR_ID in captured["url"]
    assert "/runs" in captured["url"] and "run-sync" not in captured["url"]
    assert captured["body"]["queries"] == urls
    assert captured["body"]["maxItems"] == 3
    assert "profileScraperMode" in captured["body"]


@pytest.mark.unit
async def test_start_profile_by_url_run_4xx_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad token")

    async with _client_with(handler) as client:
        run_data = await apify_mod.start_profile_by_url_run(
            ["https://www.linkedin.com/in/a"], client=client,
        )
    assert run_data is None


@pytest.mark.unit
async def test_start_profile_by_url_run_empty_urls_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP must not be called for empty url list")

    async with _client_with(handler) as client:
        run_data = await apify_mod.start_profile_by_url_run([], client=client)
    assert run_data is None
    # Also: list of blanks
    async with _client_with(handler) as client:
        run_data = await apify_mod.start_profile_by_url_run(["", "  "], client=client)
    assert run_data is None


@pytest.mark.unit
async def test_start_profile_by_url_run_no_token_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201, json={"data": {"id": "x"}})

    async with _client_with(handler) as client:
        run_data = await apify_mod.start_profile_by_url_run(
            ["https://www.linkedin.com/in/a"], client=client,
        )
    assert run_data is None
    assert called is False


# ── End-to-end runner with patched acquire + MockTransport ──────────────────


class _RecordingConn:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self._fetch_handler = None

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.fetch_calls.append((sql, args))
        if self._fetch_handler is not None:
            return self._fetch_handler(sql, args)
        return []


class _FakeAcquire:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _RecordingConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _row(prospect_id: UUID, url: str | None, name: str = "Test Person") -> dict[str, Any]:
    return {
        "id": prospect_id,
        "name": name,
        "linkedin_url": url,
        "account_id": ACCOUNT_ID,
    }


@pytest.fixture
def patched_runner(monkeypatch: pytest.MonkeyPatch):
    """Patch ``acquire`` + ``write_canonical_persons`` so the runner can
    execute end-to-end without a real DB.

    State exposed:
      - ``rows``: list of dict-rows the SELECT will return
      - ``conns``: every _RecordingConn created (so tests can inspect
        marker INSERTs)
      - ``persisted``: list of (canonical_persons, account_id) calls to
        write_canonical_persons
      - ``write_result``: the WriteResult returned by the patched writer
    """
    from credence.enrichment.writer import WriteResult

    state: dict[str, Any] = {
        "rows": [],
        "conns": [],
        "persisted": [],
        "write_result": WriteResult(
            persons_inserted=1,
            employment_periods_inserted=1,
            education_periods_inserted=1,
        ),
    }

    def make_conn() -> _RecordingConn:
        conn = _RecordingConn()

        def fetch_handler(sql: str, args: tuple[Any, ...]) -> list[Any]:
            if "FROM prospects" in sql:
                return state["rows"]
            return []

        conn._fetch_handler = fetch_handler  # type: ignore[assignment]
        state["conns"].append(conn)
        return conn

    def fake_acquire() -> _FakeAcquire:
        return _FakeAcquire(make_conn())

    monkeypatch.setattr(job, "acquire", fake_acquire)

    async def fake_write(persons: list[Any], *, account_id: UUID, **_kw: Any):
        state["persisted"].append((list(persons), account_id))
        return state["write_result"]

    monkeypatch.setattr(job, "write_canonical_persons", fake_write)
    return state


def _profile_handler(
    url_to_response: dict[str, httpx.Response],
    *,
    run_id: str = "run-batched-1",
    dataset_id: str = "ds-batched-1",
    fail_start: bool = False,
):
    """Build a MockTransport handler that simulates the *batched async*
    profile-by-URL flow.

    The runner now does three calls per chunk:
      1. POST /acts/{actor}/runs            → returns ``{"data": {"id":..., "defaultDatasetId":...}}``
      2. GET  /actor-runs/{id}              → polled until status=SUCCEEDED
      3. GET  /datasets/{id}/items          → returns the merged dataset

    For test fixtures, callers supply a ``url → httpx.Response`` map of
    *intended* per-URL responses. The handler unpacks each canned
    response's JSON body and unions them into a single dataset (so the
    fake batched run returns the same set of profiles a per-URL call
    would have). Non-200 entries in the map cause that URL to be
    excluded from the dataset (no-match), and any URL whose response
    had status >= 500 makes the run as a whole fail (status=FAILED) —
    matching the brief's "if a chunk's run never reaches SUCCEEDED,
    every prospect in it is profiles_failed."
    """
    # Pre-compute the dataset items + per-run status from the canned
    # per-URL responses.
    items: list[dict[str, Any]] = []
    has_5xx = fail_start
    for url, resp in url_to_response.items():
        if resp.status_code >= 500:
            has_5xx = True
            continue
        if resp.status_code in (200, 201):
            try:
                body = resp.json()
            except Exception:
                continue
            if isinstance(body, list):
                items.extend(body)

    run_status = "FAILED" if has_5xx else "SUCCEEDED"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        method = request.method
        # Step 1: start run
        if method == "POST" and "/runs?" in url and "/run-sync" not in url:
            if fail_start:
                return httpx.Response(500, text="boom")
            return httpx.Response(
                201,
                json={
                    "data": {
                        "id": run_id,
                        "status": "READY",
                        "defaultDatasetId": dataset_id,
                    }
                },
            )
        # Step 2: poll
        if method == "GET" and f"actor-runs/{run_id}" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": run_id,
                        "status": run_status,
                        "defaultDatasetId": dataset_id,
                        "chargedEventCounts": {
                            # 0.4¢/profile-no-email × items → cost
                            "full-profile": len(items),
                        },
                    }
                },
            )
        # Step 3: dataset items
        if method == "GET" and f"datasets/{dataset_id}/items" in url:
            return httpx.Response(200, json=items)
        # Legacy run-sync-get-dataset-items (kept so existing
        # apify-module tests for fetch_profile_by_url still pass when
        # they share the helper — though they don't currently).
        if method == "POST" and "run-sync-get-dataset-items" in url:
            return httpx.Response(200, json=items)
        return httpx.Response(404, text=f"unhandled: {method} {url}")

    return handler


@pytest.mark.unit
async def test_runner_fetches_and_persists_one_prospect(patched_runner: dict[str, Any]) -> None:
    patched_runner["rows"] = [
        _row(P1, "https://www.linkedin.com/in/p1"),
    ]
    handler = _profile_handler({
        "https://www.linkedin.com/in/p1": httpx.Response(
            201, json=[_mk_raw_profile(public_identifier="p1")]
        ),
    })
    async with _client_with(handler) as client:
        rollup = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, client=client,
        )

    assert rollup.prospects_targeted == 1
    assert rollup.profiles_fetched == 1
    assert rollup.profiles_failed == 0
    assert rollup.profiles_no_match == 0
    assert rollup.persons_inserted == 1
    assert rollup.employment_periods_inserted == 1
    assert rollup.education_periods_inserted == 1
    # 1 fetch × 1¢ ceiling
    assert rollup.cost_cents_total == 1
    # write_canonical_persons was called once with the canonical mapped from apify
    assert len(patched_runner["persisted"]) == 1
    canonicals, acct = patched_runner["persisted"][0]
    assert acct == ACCOUNT_ID
    assert len(canonicals) == 1
    # Marker insert ran on a separate acquire — find it across conns
    marker_inserts = [
        call
        for conn in patched_runner["conns"]
        for call in conn.execute_calls
        if "INSERT INTO signals" in call[0]
    ]
    assert len(marker_inserts) == 1
    _, args = marker_inserts[0]
    assert args[0] == P1
    assert args[1] == ACCOUNT_ID
    assert args[2] == job.MARKER_SIGNAL_SOURCE
    assert args[3] == job.MARKER_SIGNAL_TYPE
    # value is the dict (not json string) — matches the write convention
    assert isinstance(args[4], dict)
    assert args[4]["method"] == job.MARKER_METHOD


@pytest.mark.unit
async def test_runner_handles_empty_target_list(patched_runner: dict[str, Any]) -> None:
    patched_runner["rows"] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP should not be called when no targets")

    async with _client_with(handler) as client:
        rollup = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, client=client,
        )
    assert rollup.prospects_targeted == 0
    assert rollup.profiles_fetched == 0


@pytest.mark.unit
async def test_runner_dry_run_makes_no_http_or_writes(patched_runner: dict[str, Any]) -> None:
    patched_runner["rows"] = [
        _row(P1, "https://www.linkedin.com/in/p1"),
        _row(P2, "https://www.linkedin.com/in/p2"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP must not be called in dry-run")

    async with _client_with(handler) as client:
        rollup = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, dry_run=True, client=client,
        )
    assert rollup.dry_run is True
    assert rollup.prospects_targeted == 2
    assert rollup.profiles_fetched == 0
    assert patched_runner["persisted"] == []


@pytest.mark.unit
async def test_runner_500_response_counts_as_failed(patched_runner: dict[str, Any]) -> None:
    """One prospect whose chunk's Apify run returns 5xx → profiles_failed=1."""
    patched_runner["rows"] = [_row(P1, "https://www.linkedin.com/in/p1")]
    # Any 5xx in canned per-URL responses cascades to a FAILED run.
    handler = _profile_handler({
        "https://www.linkedin.com/in/p1": httpx.Response(500, text="boom"),
    })
    async with _client_with(handler) as client:
        rollup = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, client=client,
        )
    assert rollup.profiles_failed == 1
    assert rollup.profiles_fetched == 0
    assert rollup.persons_inserted == 0
    # No marker written for failures (so re-run will retry)
    marker_inserts = [
        call
        for conn in patched_runner["conns"]
        for call in conn.execute_calls
        if "INSERT INTO signals" in call[0]
    ]
    assert marker_inserts == []


@pytest.mark.unit
async def test_runner_empty_dataset_counts_as_no_match(patched_runner: dict[str, Any]) -> None:
    """Actor returned 200 but no items — neither failed nor fetched.

    ``profiles_no_match`` exists so we can monitor for upstream-data
    quality issues (broken URLs, deleted profiles)."""
    patched_runner["rows"] = [_row(P1, "https://www.linkedin.com/in/missing")]
    handler = _profile_handler({
        "https://www.linkedin.com/in/missing": httpx.Response(200, json=[]),
    })
    async with _client_with(handler) as client:
        rollup = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, client=client,
        )
    assert rollup.profiles_no_match == 1
    assert rollup.profiles_fetched == 0
    assert rollup.profiles_failed == 0
    assert rollup.persons_inserted == 0


@pytest.mark.unit
async def test_runner_mixed_success_and_no_match(patched_runner: dict[str, Any]) -> None:
    """Batched semantics: one chunk submits all URLs in a single Apify
    run. URLs whose profile is returned in the dataset are matched;
    URLs absent from the dataset are no-match. (The old per-URL 500
    case is now covered by the chunk-level fail test below.)"""
    patched_runner["rows"] = [
        _row(P1, "https://www.linkedin.com/in/p1"),
        _row(P2, "https://www.linkedin.com/in/p2"),
        _row(P3, "https://www.linkedin.com/in/p3"),
    ]
    handler = _profile_handler({
        # p1 + p3 returned, p2 absent → no-match
        "https://www.linkedin.com/in/p1": httpx.Response(
            201, json=[_mk_raw_profile(public_identifier="p1")]
        ),
        "https://www.linkedin.com/in/p3": httpx.Response(
            201, json=[_mk_raw_profile(public_identifier="p3")]
        ),
    })
    async with _client_with(handler) as client:
        rollup = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, client=client,
        )
    assert rollup.prospects_targeted == 3
    assert rollup.profiles_fetched == 2
    assert rollup.profiles_failed == 0
    assert rollup.profiles_no_match == 1


@pytest.mark.unit
async def test_runner_chunk_run_failure_marks_all_failed(
    patched_runner: dict[str, Any],
) -> None:
    """When the chunk's Apify run never reaches SUCCEEDED, every
    prospect in that chunk is counted as ``profiles_failed`` and no
    marker signal is written (re-runs will retry)."""
    patched_runner["rows"] = [
        _row(P1, "https://www.linkedin.com/in/p1"),
        _row(P2, "https://www.linkedin.com/in/p2"),
    ]
    # fail_start=True → POST /runs returns 500, the run never starts
    handler = _profile_handler({}, fail_start=True)
    async with _client_with(handler) as client:
        rollup = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, client=client,
        )
    assert rollup.profiles_failed == 2
    assert rollup.profiles_fetched == 0
    # No marker writes for failed runs
    marker_inserts = [
        call
        for conn in patched_runner["conns"]
        for call in conn.execute_calls
        if "INSERT INTO signals" in call[0]
    ]
    assert marker_inserts == []


@pytest.mark.unit
async def test_runner_cost_aggregates_across_five(patched_runner: dict[str, Any]) -> None:
    """5 successful fetches × 1¢ ceiling = 5¢ total.

    (Per the brief: 5 × 0.8¢ = 4¢ in fractional units, but ceiling per
    fetch matches Apify billing — never under-count spend.)"""
    rows = []
    url_map: dict[str, httpx.Response] = {}
    for i, pid in enumerate([P1, P2, P3, P4, P5], start=1):
        url = f"https://www.linkedin.com/in/p{i}"
        rows.append(_row(pid, url))
        url_map[url] = httpx.Response(
            201, json=[_mk_raw_profile(public_identifier=f"p{i}")]
        )
    patched_runner["rows"] = rows
    handler = _profile_handler(url_map)
    async with _client_with(handler) as client:
        rollup = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, client=client, concurrency=2,
        )
    assert rollup.profiles_fetched == 5
    assert rollup.cost_cents_total == 5


@pytest.mark.unit
async def test_runner_batched_run_5_profiles_in_one_chunk(
    patched_runner: dict[str, Any],
) -> None:
    """End-to-end: 5 prospects → 1 chunk → 1 batched Apify run → 5
    matched profiles → 5 marker signals."""
    rows = []
    url_map: dict[str, httpx.Response] = {}
    for i, pid in enumerate([P1, P2, P3, P4, P5], start=1):
        url = f"https://www.linkedin.com/in/p{i}"
        rows.append(_row(pid, url))
        url_map[url] = httpx.Response(
            201, json=[_mk_raw_profile(public_identifier=f"p{i}")]
        )
    patched_runner["rows"] = rows
    handler = _profile_handler(url_map)
    async with _client_with(handler) as client:
        rollup = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, client=client, chunk_size=10,
        )
    assert rollup.profiles_fetched == 5
    assert rollup.profiles_failed == 0
    assert rollup.profiles_no_match == 0
    # 5 marker INSERTs across the conns
    marker_inserts = [
        call
        for conn in patched_runner["conns"]
        for call in conn.execute_calls
        if "INSERT INTO signals" in call[0]
    ]
    assert len(marker_inserts) == 5


@pytest.mark.unit
async def test_runner_chunks_split_when_size_smaller_than_total(
    patched_runner: dict[str, Any],
) -> None:
    """5 prospects with chunk_size=2 → 3 chunks → 3 separate Apify
    runs. Verify all 5 still get persisted."""
    rows = []
    url_map: dict[str, httpx.Response] = {}
    for i, pid in enumerate([P1, P2, P3, P4, P5], start=1):
        url = f"https://www.linkedin.com/in/p{i}"
        rows.append(_row(pid, url))
        url_map[url] = httpx.Response(
            201, json=[_mk_raw_profile(public_identifier=f"p{i}")]
        )
    patched_runner["rows"] = rows
    handler = _profile_handler(url_map)
    async with _client_with(handler) as client:
        rollup = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, client=client, chunk_size=2,
        )
    # All 5 still resolved (across 3 chunks)
    assert rollup.profiles_fetched == 5


@pytest.mark.unit
async def test_runner_marker_signal_dedupes_rerun(
    monkeypatch: pytest.MonkeyPatch, patched_runner: dict[str, Any],
) -> None:
    """Re-runs are deduped by the SELECT — the marker NOT EXISTS clause
    is in the SQL, so once a prospect is marked, the test simulates
    that by returning zero rows on the second call."""
    seen: dict[str, int] = {"calls": 0}

    def fetch_handler(sql: str, args: tuple[Any, ...]) -> list[Any]:
        if "FROM prospects" in sql:
            seen["calls"] += 1
            if seen["calls"] == 1:
                return [_row(P1, "https://www.linkedin.com/in/p1")]
            return []  # second run — already-marker-signaled prospects excluded
        return []

    # Re-patch acquire so every conn uses the per-call fetch_handler.
    state = patched_runner

    def make_conn() -> _RecordingConn:
        conn = _RecordingConn()
        conn._fetch_handler = fetch_handler  # type: ignore[assignment]
        state["conns"].append(conn)
        return conn

    monkeypatch.setattr(job, "acquire", lambda: _FakeAcquire(make_conn()))

    handler = _profile_handler({
        "https://www.linkedin.com/in/p1": httpx.Response(
            201, json=[_mk_raw_profile(public_identifier="p1")]
        ),
    })
    async with _client_with(handler) as client:
        first = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, client=client,
        )
        second = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, client=client,
        )
    assert first.prospects_targeted == 1
    assert first.profiles_fetched == 1
    # Second run: SELECT excludes the prospect with a marker → zero targets
    assert second.prospects_targeted == 0
    assert second.profiles_fetched == 0


@pytest.mark.unit
async def test_runner_no_token_aborts_before_fetch(
    monkeypatch: pytest.MonkeyPatch, patched_runner: dict[str, Any],
) -> None:
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    patched_runner["rows"] = [_row(P1, "https://www.linkedin.com/in/p1")]

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP must not be called without APIFY_TOKEN")

    async with _client_with(handler) as client:
        rollup = await job.bulk_apify_profile_lookup_account(
            ACCOUNT_ID, client=client,
        )
    assert rollup.profiles_fetched == 0
    assert any("APIFY_TOKEN" in e for e in rollup.errors)


@pytest.mark.unit
async def test_runner_select_sql_excludes_marker_signal_via_not_exists() -> None:
    """The selector hard-bakes the dedupe — guard against accidental SQL drift."""
    sql = job.SELECT_UNENRICHED_PROSPECTS_SQL
    assert "linkedin_url IS NOT NULL" in sql
    assert "linkedin_url <> ''" in sql
    assert "NOT EXISTS" in sql
    assert "signal_type = $2" in sql
    # Marker signal type is parametrized via $2 — confirm the constant
    assert job.MARKER_SIGNAL_TYPE == "apify_linkedin_apimaestro"


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
        [
            "--account-id", str(ACCOUNT_ID),
            "--limit", "5",
            "--concurrency", "2",
            "--mode", "full",
            "--dry-run",
        ]
    )
    assert args.account_id == ACCOUNT_ID
    assert args.limit == 5
    assert args.concurrency == 2
    assert args.mode == "full"
    assert args.dry_run is True
    assert args.all_accounts is False


@pytest.mark.unit
def test_cli_parses_all_accounts_path() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(["--all-accounts"])
    assert args.all_accounts is True
    assert args.account_id is None
    # Default mode = no_email (cheapest profile-scraper mode)
    assert args.mode == "no_email"
    assert args.concurrency == job.DEFAULT_CONCURRENCY


@pytest.mark.unit
def test_cli_mode_choices_cover_three_pricings() -> None:
    parser = job._build_arg_parser()
    for choice in ("short", "full", "full_email"):
        args = parser.parse_args(["--all-accounts", "--mode", choice])
        assert args.mode == choice
