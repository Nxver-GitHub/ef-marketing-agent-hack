"""Tests for `credence.extractors.scholar` — Semantic Scholar extractor.

Uses `httpx.MockTransport` to return canned Semantic Scholar v1 JSON
responses so the parsing logic is exercised without network access. Live
API integration is **deferred to J.5.5**.

Coverage:
1. Happy path — author search + papers list, both authored → 1 record
2. Paper missing person_b → filtered out
3. Multiple co-authored papers → multiple records
4. Single-name persons → empty (no API call beyond search)
5. Author search returns no hits → empty result
6. Author search returns malformed body → empty result
7. Network error on author search → empty
8. Network error on papers fetch → empty
9. HTTP 5xx on either call → empty
10. Non-JSON body → empty
11. Missing data[] key → empty
12. max_results cap honored
13. Defensive parsing — missing venue, missing year, missing citations
14. structured_value includes author_count for signals.py confidence tier
15. DOI extracted from externalIds
16. Case-insensitive author name match
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from credence.extractors.patents import PersonRef
from credence.extractors.scholar import (
    SEMANTIC_SCHOLAR_BASE_URL,
    find_paper_co_authorships,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


PERSON_WEI = PersonRef(person_id="p:wei", canonical_name="Wei Chen")
PERSON_MARCUS = PersonRef(person_id="p:marcus", canonical_name="Marcus Hale")


def _author_search_body(author_id: str = "1741101", name: str = "Wei Chen") -> dict[str, Any]:
    return {
        "total": 1,
        "data": [
            {
                "authorId": author_id,
                "name": name,
                "affiliations": ["Test Institution"],
                "paperCount": 100,
            }
        ],
    }


def _paper(
    paper_id: str,
    title: str,
    venue: str,
    year: int,
    citation_count: int,
    authors: list[tuple[str, str]],
    *,
    doi: str | None = None,
) -> dict[str, Any]:
    return {
        "paperId": paper_id,
        "title": title,
        "venue": venue,
        "year": year,
        "citationCount": citation_count,
        "authors": [
            {"authorId": author_id, "name": name}
            for author_id, name in authors
        ],
        "externalIds": ({"DOI": doi} if doi else {}),
    }


def _route_handler(
    *,
    author_search: httpx.Response | None = None,
    papers: httpx.Response | None = None,
):
    """Return a MockTransport handler that dispatches by URL path."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/author/search" in path:
            return author_search or httpx.Response(200, json={"data": []})
        if "/papers" in path:
            return papers or httpx.Response(200, json={"data": []})
        return httpx.Response(404, text="not handled")

    return handler


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_happy_path_single_co_authorship() -> None:
    """One paper authored by both Wei and Marcus → 1 record."""
    handler = _route_handler(
        author_search=httpx.Response(200, json=_author_search_body()),
        papers=httpx.Response(
            200,
            json={
                "data": [
                    _paper(
                        "p1",
                        "Accelerator design for LLM training",
                        "NeurIPS",
                        2023,
                        42,
                        [("1741101", "Wei Chen"), ("9999", "Marcus Hale")],
                        doi="10.1234/example",
                    )
                ]
            },
        ),
    )
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )

    assert len(result) == 1
    rec = result[0]
    assert rec["paper_title"] == "Accelerator design for LLM training"
    assert rec["venue"] == "NeurIPS"
    assert rec["year"] == 2023
    assert rec["citation_count"] == 42
    assert rec["semantic_scholar_id"] == "p1"
    assert rec["doi"] == "10.1234/example"
    assert rec["author_count"] == 2


@pytest.mark.unit
async def test_filters_papers_without_person_b() -> None:
    """Papers authored only by Wei → not returned."""
    handler = _route_handler(
        author_search=httpx.Response(200, json=_author_search_body()),
        papers=httpx.Response(
            200,
            json={
                "data": [
                    _paper(
                        "p1",
                        "Solo paper",
                        "ICML",
                        2022,
                        5,
                        [("1741101", "Wei Chen")],
                    )
                ]
            },
        ),
    )
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert result == []


@pytest.mark.unit
async def test_multiple_co_authored_papers() -> None:
    """3 papers by both → 3 records."""
    handler = _route_handler(
        author_search=httpx.Response(200, json=_author_search_body()),
        papers=httpx.Response(
            200,
            json={
                "data": [
                    _paper(
                        f"p{i}",
                        f"Paper {i}",
                        "Venue",
                        2020 + i,
                        i,
                        [("1741101", "Wei Chen"), ("9999", "Marcus Hale")],
                    )
                    for i in range(3)
                ]
            },
        ),
    )
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert len(result) == 3


@pytest.mark.unit
async def test_max_results_cap_honored() -> None:
    handler = _route_handler(
        author_search=httpx.Response(200, json=_author_search_body()),
        papers=httpx.Response(
            200,
            json={
                "data": [
                    _paper(
                        f"p{i}",
                        f"Paper {i}",
                        "V",
                        2020,
                        1,
                        [("1741101", "Wei Chen"), ("9999", "Marcus Hale")],
                    )
                    for i in range(5)
                ]
            },
        ),
    )
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=2, client=client
        )
    assert len(result) == 2


@pytest.mark.unit
async def test_single_name_skips_search() -> None:
    """Single-token canonical_name → empty result, no API call."""
    person_lin = PersonRef(person_id="p:lin", canonical_name="Lin")
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"data": []})

    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            person_lin, PERSON_MARCUS, max_results=10, client=client
        )
    assert result == []
    assert call_count["n"] == 0


@pytest.mark.unit
async def test_author_search_no_hits_returns_empty() -> None:
    handler = _route_handler(
        author_search=httpx.Response(200, json={"total": 0, "data": []})
    )
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert result == []


@pytest.mark.unit
async def test_author_search_malformed_body_returns_empty() -> None:
    handler = _route_handler(author_search=httpx.Response(200, json={"unexpected": "shape"}))
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert result == []


@pytest.mark.unit
async def test_network_error_on_author_search_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failed", request=request)

    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert result == []


@pytest.mark.unit
async def test_network_error_on_papers_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/author/search" in request.url.path:
            return httpx.Response(200, json=_author_search_body())
        raise httpx.ConnectError("DNS failed mid-call", request=request)

    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert result == []


@pytest.mark.unit
async def test_http_5xx_returns_empty() -> None:
    handler = _route_handler(author_search=httpx.Response(503, text="upstream down"))
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert result == []


@pytest.mark.unit
async def test_non_json_body_returns_empty() -> None:
    handler = _route_handler(author_search=httpx.Response(200, text="<html>not json</html>"))
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert result == []


@pytest.mark.unit
async def test_missing_data_key_returns_empty() -> None:
    handler = _route_handler(
        author_search=httpx.Response(200, json={"total": 0})
    )
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert result == []


@pytest.mark.unit
async def test_paper_without_id_or_title_dropped() -> None:
    """Paper record with neither paperId nor title is silently dropped."""
    handler = _route_handler(
        author_search=httpx.Response(200, json=_author_search_body()),
        papers=httpx.Response(
            200,
            json={
                "data": [
                    {
                        # neither paperId nor title
                        "venue": "V",
                        "year": 2020,
                        "authors": [
                            {"authorId": "1741101", "name": "Wei Chen"},
                            {"authorId": "9999", "name": "Marcus Hale"},
                        ],
                    }
                ]
            },
        ),
    )
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert result == []


@pytest.mark.unit
async def test_missing_optional_fields_render_defaults() -> None:
    """Missing venue / year / citations / DOI render defaults, not crash."""
    handler = _route_handler(
        author_search=httpx.Response(200, json=_author_search_body()),
        papers=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "paperId": "p1",
                        "title": "Sparse paper",
                        "authors": [
                            {"authorId": "1741101", "name": "Wei Chen"},
                            {"authorId": "9999", "name": "Marcus Hale"},
                        ],
                        # no venue, year, citationCount, externalIds
                    }
                ]
            },
        ),
    )
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert len(result) == 1
    rec = result[0]
    assert rec["venue"] == ""
    assert rec["year"] == 0
    assert rec["citation_count"] == 0
    assert rec["doi"] is None


@pytest.mark.unit
async def test_author_count_surfaced_for_confidence_tier() -> None:
    """Per Contract 1: signals.py reads `author_count` to choose between
    0.90 (≤5 authors) and 0.75 (>5 authors) confidence."""
    handler = _route_handler(
        author_search=httpx.Response(200, json=_author_search_body()),
        papers=httpx.Response(
            200,
            json={
                "data": [
                    _paper(
                        "p1",
                        "Many authors",
                        "Big-Conf",
                        2024,
                        100,
                        [("1741101", "Wei Chen"), ("9999", "Marcus Hale")]
                        + [(f"a{i}", f"Author {i} Z") for i in range(8)],
                    )
                ]
            },
        ),
    )
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert len(result) == 1
    assert result[0]["author_count"] == 10


@pytest.mark.unit
async def test_doi_extracted_from_external_ids() -> None:
    handler = _route_handler(
        author_search=httpx.Response(200, json=_author_search_body()),
        papers=httpx.Response(
            200,
            json={
                "data": [
                    _paper(
                        "p1",
                        "Has DOI",
                        "V",
                        2023,
                        5,
                        [("1741101", "Wei Chen"), ("9999", "Marcus Hale")],
                        doi="10.5555/great-paper",
                    )
                ]
            },
        ),
    )
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert result[0]["doi"] == "10.5555/great-paper"


@pytest.mark.unit
async def test_case_insensitive_author_name_match() -> None:
    handler = _route_handler(
        author_search=httpx.Response(200, json=_author_search_body()),
        papers=httpx.Response(
            200,
            json={
                "data": [
                    _paper(
                        "p1",
                        "T",
                        "V",
                        2023,
                        1,
                        [("1741101", "WEI CHEN"), ("9999", "marcus hale")],
                    )
                ]
            },
        ),
    )
    async with _client_with(handler) as client:
        result = await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert len(result) == 1


@pytest.mark.unit
async def test_query_url_targets_semantic_scholar() -> None:
    """The HTTP request hits the documented Semantic Scholar base URL."""
    captured: dict[str, list[str]] = {"urls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["urls"].append(str(request.url))
        if "/author/search" in request.url.path:
            return httpx.Response(200, json=_author_search_body())
        return httpx.Response(200, json={"data": []})

    async with _client_with(handler) as client:
        await find_paper_co_authorships(
            PERSON_WEI, PERSON_MARCUS, max_results=10, client=client
        )
    assert any(SEMANTIC_SCHOLAR_BASE_URL in u for u in captured["urls"])
    assert any("/author/search" in u for u in captured["urls"])
    assert any("/papers" in u for u in captured["urls"])
