"""Tests for `credence.extractors.patents` — USPTO PatentsView extractor.

Uses `httpx.MockTransport` to return canned PatentsView v1 JSON responses
so the parsing logic is exercised without hitting the network. A live
integration smoke test against the real API is **deferred to J.4.5** — the
sandbox this code was written in had no DNS resolution available.

Coverage:
1. Happy path — patent with both inventors → 1 record
2. Patent with only person_a → 0 records (filtered out)
3. Multiple co-invented patents → multiple records
4. Single-name persons (can't split first/last) → empty list, no API call
5. uspto_inventor_id used when present (precision over name match)
6. Network error → empty list (partial-results contract)
7. HTTP 5xx → empty list
8. Non-JSON response body → empty list
9. Missing patents[] key → empty list
10. max_results cap honored
11. structured_value shape matches Contract 1
12. Defensive parsing — missing assignees, missing dates, etc.
13. Case-insensitive inventor name match
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from credence.extractors.patents import (
    PATENTSVIEW_BASE_URL,
    PersonRef,
    find_patent_co_inventions,
)


@pytest.fixture(autouse=True)
def _odp_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-set ODP env vars so existing tests exercise the ODP path with a
    test key. Tests that specifically verify env-gating behavior override
    these via their own monkeypatches."""
    monkeypatch.setenv("USPTO_USE_ODP", "1")
    monkeypatch.setenv("USPTO_ODP_API_KEY", "test-odp-key-default")


# ── Fixtures ────────────────────────────────────────────────────────────────


def _patent(
    patent_id: str,
    title: str,
    filing_date: str,
    grant_date: str | None,
    assignee: str,
    inventors: list[tuple[str, str, str | None]],
) -> dict[str, Any]:
    """Construct a PatentsView-shaped patent dict."""
    return {
        "patent_id": patent_id,
        "patent_title": title,
        "patent_date": grant_date,
        "patent_filing_date": filing_date,
        "inventors": [
            {
                "inventor_name_first": first,
                "inventor_name_last": last,
                **({"inventor_id": inv_id} if inv_id else {}),
            }
            for first, last, inv_id in inventors
        ],
        "assignees": [{"assignee_organization": assignee}],
    }


def _make_client(handler) -> httpx.AsyncClient:
    """Async client with a MockTransport routing every request through `handler`."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


PERSON_WEI = PersonRef(person_id="p:wei", canonical_name="Wei Chen")
PERSON_MARCUS = PersonRef(person_id="p:marcus", canonical_name="Marcus Hale")


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_happy_path_single_co_invention() -> None:
    """One patent with both Wei and Marcus listed as inventors → 1 record."""
    canned = {
        "patents": [
            _patent(
                "10234567",
                "Yield optimization method",
                "2017-08-12",
                "2018-04-21",
                "Intel Corporation",
                inventors=[
                    ("Wei", "Chen", "fl:we_ln:chen-1"),
                    ("Marcus", "Hale", "fl:ma_ln:hale-1"),
                ],
            ),
        ],
        "count": 1,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert len(result) == 1
    rec = result[0]
    assert rec["patent_number"] == "10234567"
    assert rec["patent_title"] == "Yield optimization method"
    assert rec["filing_date"] == "2017-08-12"
    assert rec["grant_date"] == "2018-04-21"
    assert rec["assignee"] == "Intel Corporation"
    assert rec["uspto_url"].startswith("https://patents.google.com/patent/US")


@pytest.mark.unit
async def test_filters_out_patents_missing_person_b() -> None:
    """Patent with only Wei → not returned (Marcus not in inventors)."""
    canned = {
        "patents": [
            _patent(
                "10000001",
                "Solo invention",
                "2019-01-01",
                "2020-06-01",
                "Apple",
                inventors=[("Wei", "Chen", None)],
            ),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert result == []


@pytest.mark.unit
async def test_returns_only_patents_with_both_inventors() -> None:
    """Mix of patents — only the one with both inventors is returned."""
    canned = {
        "patents": [
            _patent(
                "10000001",
                "Solo Wei",
                "2019-01-01",
                "2020-06-01",
                "Apple",
                inventors=[("Wei", "Chen", None)],
            ),
            _patent(
                "10234567",
                "Both",
                "2017-08-12",
                "2018-04-21",
                "Intel",
                inventors=[
                    ("Wei", "Chen", None),
                    ("Marcus", "Hale", None),
                ],
            ),
            _patent(
                "10999999",
                "Wei + someone else",
                "2018-03-01",
                None,
                "AMD",
                inventors=[
                    ("Wei", "Chen", None),
                    ("Sarah", "Kim", None),
                ],
            ),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert len(result) == 1
    assert result[0]["patent_number"] == "10234567"


@pytest.mark.unit
async def test_max_results_cap_honored() -> None:
    """3 matching patents but max_results=2 → 2 records returned."""
    canned = {
        "patents": [
            _patent(
                f"P{i:05d}",
                f"Patent {i}",
                "2020-01-01",
                "2020-12-31",
                "X",
                inventors=[("Wei", "Chen", None), ("Marcus", "Hale", None)],
            )
            for i in range(5)
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=2, client=client
        )

    assert len(result) == 2


@pytest.mark.unit
async def test_single_name_person_skips_query() -> None:
    """Person with single-token canonical_name → empty list, no API call."""
    person_lin = PersonRef(person_id="p:lin", canonical_name="Lin")
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"patents": []})

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            person_lin, PERSON_MARCUS, max_results=10, client=client
        )

    assert result == []
    assert call_count["n"] == 0


@pytest.mark.unit
async def test_uspto_inventor_id_used_in_query_when_present() -> None:
    """When person.uspto_inventor_id is set, query targets it directly."""
    captured_q: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        captured_q["q"] = json.loads(qs["q"][0])
        return httpx.Response(200, json={"patents": []})

    person_with_id = PersonRef(
        person_id="p:wei",
        canonical_name="Wei Chen",
        uspto_inventor_id="fl:we_ln:chen-1",
    )
    async with _make_client(handler) as client:
        await find_patent_co_inventions(
            person_with_id, PERSON_MARCUS, max_results=10, client=client
        )

    assert captured_q["q"] == {
        "_contains": {"inventors.inventor_id": "fl:we_ln:chen-1"}
    }


@pytest.mark.unit
async def test_inventor_id_match_short_circuits_name_check() -> None:
    """When person_b has an inventor_id and a patent's inventor matches that
    id, presence is confirmed even if names mismatch."""
    canned = {
        "patents": [
            _patent(
                "11111111",
                "Match by id",
                "2020-01-01",
                "2021-01-01",
                "X",
                inventors=[
                    ("Wei", "Chen", "fl:we_ln:chen-1"),
                    # Person B's inventor_id matches but names differ (e.g.,
                    # PatentsView uses a different romanization)
                    ("Markus", "Halé", "fl:ma_ln:hale-1"),
                ],
            ),
        ],
    }
    person_b = PersonRef(
        person_id="p:marcus",
        canonical_name="Marcus Hale",
        uspto_inventor_id="fl:ma_ln:hale-1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, person_b, max_results=10, client=client
        )

    assert len(result) == 1


@pytest.mark.unit
async def test_network_error_returns_empty_list() -> None:
    """httpx ConnectError → empty list, no exception (partial-results)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failed", request=request)

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert result == []


@pytest.mark.unit
async def test_http_5xx_returns_empty_list() -> None:
    """5xx responses are absorbed silently."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert result == []


@pytest.mark.unit
async def test_non_json_body_returns_empty_list() -> None:
    """Malformed JSON body → empty list, no crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>error page</html>")

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert result == []


@pytest.mark.unit
async def test_missing_patents_key_returns_empty_list() -> None:
    """Response without the `patents` field → empty list."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0})

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert result == []


@pytest.mark.unit
async def test_record_without_patent_id_dropped() -> None:
    """A patent record with no patent_id / patent_number is silently dropped."""
    canned = {
        "patents": [
            {
                # no patent_id / patent_number
                "patent_title": "Mystery patent",
                "patent_date": "2020-01-01",
                "inventors": [
                    {"inventor_name_first": "Wei", "inventor_name_last": "Chen"},
                    {"inventor_name_first": "Marcus", "inventor_name_last": "Hale"},
                ],
                "assignees": [{"assignee_organization": "X"}],
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert result == []


@pytest.mark.unit
async def test_missing_assignees_renders_empty_string() -> None:
    """Patent with no assignees[] yields empty assignee, not crash."""
    canned = {
        "patents": [
            {
                "patent_id": "P1",
                "patent_title": "T",
                "patent_filing_date": "2020-01-01",
                "patent_date": "2020-12-01",
                "inventors": [
                    {"inventor_name_first": "Wei", "inventor_name_last": "Chen"},
                    {"inventor_name_first": "Marcus", "inventor_name_last": "Hale"},
                ],
                # no assignees key
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert len(result) == 1
    assert result[0]["assignee"] == ""


@pytest.mark.unit
async def test_case_insensitive_inventor_name_match() -> None:
    """PatentsView records with inverted case still match canonical_name."""
    canned = {
        "patents": [
            _patent(
                "P1",
                "T",
                "2020-01-01",
                "2020-12-01",
                "X",
                inventors=[
                    ("WEI", "CHEN", None),
                    ("marcus", "hale", None),
                ],
            ),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert len(result) == 1


@pytest.mark.unit
async def test_query_url_targets_uspto_odp_endpoint() -> None:
    """The HTTP request goes to USPTO Open Data Portal — patent search path.

    Renamed 2026-04-30 from `test_query_url_targets_patentsview_endpoint`
    after legacy PatentsView migration. The autouse fixture sets ODP env
    vars, so the resolved endpoint is the new ODP host.
    """
    captured_url: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_url["url"] = str(request.url)
        return httpx.Response(200, json={"patents": []})

    async with _make_client(handler) as client:
        await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert "api.uspto.gov" in captured_url["url"]
    assert "/patent/" in captured_url["url"]


@pytest.mark.unit
async def test_grant_date_null_propagates() -> None:
    """patent_date=null → grant_date=None in output."""
    canned = {
        "patents": [
            {
                "patent_id": "P1",
                "patent_title": "T",
                "patent_filing_date": "2020-01-01",
                "patent_date": None,
                "inventors": [
                    {"inventor_name_first": "Wei", "inventor_name_last": "Chen"},
                    {"inventor_name_first": "Marcus", "inventor_name_last": "Hale"},
                ],
                "assignees": [{"assignee_organization": "X"}],
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _make_client(handler) as client:
        result = await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert len(result) == 1
    assert result[0]["grant_date"] is None


# ── ODP migration scaffold (DarkBeaver) ─────────────────────────────────────
#
# Lock the env-gated endpoint switching so the migration to USPTO Open Data
# Portal lands as a config flip when the new URL+shape are verified. See
# `_resolve_endpoint_config()` in patents.py and msg 122 (LavenderPrairie).


@pytest.mark.unit
async def test_no_env_vars_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ODP env vars → RuntimeError with registration pointer.

    Per user directive 2026-04-30: never silently fall back to the dead
    legacy PatentsView host. Surface the configuration gap loudly so
    operators see it.
    """
    monkeypatch.delenv("USPTO_USE_ODP", raising=False)
    monkeypatch.delenv("USPTO_ODP_API_KEY", raising=False)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"patents": [], "count": 0})

    async with _make_client(handler) as client:
        with pytest.raises(RuntimeError, match=r"USPTO_ODP_API_KEY not set"):
            await find_patent_co_inventions(
                PERSON_WEI, PERSON_MARCUS, max_results=5, client=client
            )


@pytest.mark.unit
async def test_odp_path_used_when_opt_in_and_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """USPTO_USE_ODP=1 + USPTO_ODP_API_KEY=… → ODP URL + X-API-KEY header."""
    monkeypatch.setenv("USPTO_USE_ODP", "1")
    monkeypatch.setenv("USPTO_ODP_API_KEY", "test-odp-key-12345")

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"patents": [], "count": 0})

    async with _make_client(handler) as client:
        await find_patent_co_inventions(
            PERSON_WEI, PERSON_MARCUS, max_results=5, client=client
        )

    assert captured["url"].startswith("https://api.uspto.gov/api/v1/")
    # Auth header sent with the literal key value
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_lower.get("x-api-key") == "test-odp-key-12345"


@pytest.mark.unit
async def test_odp_optin_without_key_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """USPTO_USE_ODP=1 but no key → RuntimeError, no fallback.

    Per user directive 2026-04-30: don't fall back to the dead legacy
    PatentsView host. The error surfaces the misconfig with a registration
    pointer so the operator knows exactly how to fix it.
    """
    monkeypatch.setenv("USPTO_USE_ODP", "1")
    monkeypatch.delenv("USPTO_ODP_API_KEY", raising=False)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"patents": [], "count": 0})

    async with _make_client(handler) as client:
        with pytest.raises(RuntimeError, match=r"USPTO_ODP_API_KEY not set"):
            await find_patent_co_inventions(
                PERSON_WEI, PERSON_MARCUS, max_results=5, client=client
            )
