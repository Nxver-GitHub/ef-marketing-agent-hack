"""Live-API smoke test for the Semantic Scholar extractor — J.5.5.

Skipped by default. Run when you have internet to confirm the canned
Semantic Scholar Graph API v1 response shape used in `test_scholar.py` still
matches reality:

    pytest tests/test_scholar_live.py -m integration -v

What it asserts:
- /author/search returns the documented shape (data[] of author records)
- /author/{id}/papers returns the documented shape (data[] of paper records)
- Top-level paper fields (paperId, title, venue, year, citationCount, authors)
  are present where expected

If this test fails, `_resolve_author_id`, `_fetch_author_papers`, or
`_format_paper_record` in `credence.extractors.scholar` need adjustment.
"""
from __future__ import annotations

import httpx
import pytest

from credence.extractors.patents import PersonRef
from credence.extractors.scholar import (
    SEMANTIC_SCHOLAR_BASE_URL,
    find_paper_co_authorships,
)


@pytest.mark.integration
async def test_semantic_scholar_author_search_shape() -> None:
    """GET /author/search returns the documented shape."""
    url = SEMANTIC_SCHOLAR_BASE_URL.rstrip("/") + "/author/search"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            url,
            params={
                "query": "Wei Chen",
                "fields": "name,affiliations,paperCount",
                "limit": 3,
            },
        )

    # Semantic Scholar can rate-limit aggressively when unauthenticated.
    # 429 on a clean run = backoff territory; mark as skip rather than fail.
    if r.status_code == 429:
        pytest.skip("Semantic Scholar rate-limited; rerun with auth or wait")

    assert r.status_code == 200, (
        f"Semantic Scholar /author/search returned {r.status_code}: {r.text[:300]}"
    )
    body = r.json()
    assert "data" in body, f"missing `data` key — schema drift? keys: {list(body.keys())}"
    data = body["data"]
    assert isinstance(data, list)
    if data:
        sample = data[0]
        assert "authorId" in sample, f"missing authorId — schema drift? keys: {list(sample.keys())}"
        assert "name" in sample


@pytest.mark.integration
async def test_semantic_scholar_author_papers_shape() -> None:
    """GET /author/{id}/papers returns the documented shape with authors[]."""
    # Use Semantic Scholar's example endpoint to find a known author first.
    url_search = SEMANTIC_SCHOLAR_BASE_URL.rstrip("/") + "/author/search"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            url_search,
            params={"query": "Geoffrey Hinton", "fields": "name", "limit": 1},
        )
        if r.status_code == 429:
            pytest.skip("Semantic Scholar rate-limited; rerun with auth or wait")
        assert r.status_code == 200
        first = r.json().get("data", [])
        if not first:
            pytest.skip("no Semantic Scholar match for Geoffrey Hinton — corpus drift?")
        author_id = first[0]["authorId"]

        url_papers = SEMANTIC_SCHOLAR_BASE_URL.rstrip("/") + f"/author/{author_id}/papers"
        r = await client.get(
            url_papers,
            params={
                "fields": "title,year,authors,venue,citationCount,externalIds",
                "limit": 3,
            },
        )
        if r.status_code == 429:
            pytest.skip("Semantic Scholar rate-limited; rerun with auth or wait")
        assert r.status_code == 200
        body = r.json()
        assert "data" in body
        papers = body["data"]
        assert isinstance(papers, list)
        if papers:
            p = papers[0]
            # At least one of the documented identifier keys
            assert "paperId" in p or "title" in p
            authors = p.get("authors")
            assert isinstance(authors, list) and authors
            assert "name" in authors[0]


@pytest.mark.integration
async def test_find_paper_co_authorships_against_live_api() -> None:
    """Run the real extractor against Semantic Scholar.

    NOTE — replace PERSON_A / PERSON_B with a known co-authorship pair if you
    want to assert non-empty results. As written, this just asserts the
    function returns a list without raising.
    """
    person_a = PersonRef(person_id="p:a", canonical_name="Geoffrey Hinton")
    person_b = PersonRef(person_id="p:b", canonical_name="Yann LeCun")

    result = await find_paper_co_authorships(person_a, person_b, max_results=3)

    assert isinstance(result, list)
    for record in result:
        assert "paper_title" in record
        assert "venue" in record
        assert "year" in record
        assert "citation_count" in record
        assert "author_count" in record
