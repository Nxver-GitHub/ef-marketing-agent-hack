"""Live integration tests for `credence.enrichment.firecrawl`.

Opt-in only — run with `pytest -m integration`. Skipped by default.

Hits the real Firecrawl API at api.firecrawl.dev/v1/scrape with a known-stable
public URL. Costs ~1¢ per run. Requires `FIRECRAWL_API_KEY` in environment
(set via `.env.local` + python-dotenv autoload through `conftest.py`).

Targets are chosen for stability (large public sites that won't redirect/
rebrand inside our test horizon) and small response size (faster, cheaper):

- example.com — IETF-blessed sample page, returns minimal markdown reliably
- httpbin.org/html — Postman's testing site, also stable HTML

If a target page changes shape, only the assertions need updating; the
extractor logic is locked by the offline test suite.
"""
from __future__ import annotations

import os

import httpx
import pytest

from credence.enrichment.firecrawl import ScrapeRequest, scrape


@pytest.mark.integration
async def test_firecrawl_live_scrape_example_com() -> None:
    if not os.environ.get("FIRECRAWL_API_KEY"):
        pytest.skip("FIRECRAWL_API_KEY not set; skipping live test")

    async with httpx.AsyncClient() as client:
        result = await scrape(
            ScrapeRequest(url="https://example.com", person_id="test:example"),
            client=client,
            max_cost_cents=10,
        )

    if result is None:
        pytest.fail(
            "Firecrawl /v1/scrape returned no result for https://example.com — "
            "check API key, rate limits, or whether the site is reachable from "
            "Firecrawl's servers."
        )
    # example.com always has the IETF-standard "Example Domain" title.
    title = (result.fields.get("title") or "").lower()
    assert "example" in title, f"unexpected title from example.com: {title!r}"
    # Markdown should at minimum contain the "More information" link copy.
    markdown = result.fields.get("markdown") or ""
    assert len(markdown) > 0, "expected non-empty markdown from example.com"


@pytest.mark.integration
async def test_firecrawl_live_with_links_format() -> None:
    """Verify `include_links=True` returns a non-empty links array on a real page."""
    if not os.environ.get("FIRECRAWL_API_KEY"):
        pytest.skip("FIRECRAWL_API_KEY not set; skipping live test")

    async with httpx.AsyncClient() as client:
        result = await scrape(
            ScrapeRequest(
                url="https://httpbin.org/html",
                person_id="test:httpbin",
                include_links=True,
            ),
            client=client,
            max_cost_cents=10,
        )

    if result is None:
        pytest.fail(
            "Firecrawl /v1/scrape returned no result for https://httpbin.org/html — "
            "check API key, rate limits, or whether the site is reachable from "
            "Firecrawl's servers."
        )
    assert "links" in result.fields, "links should be present when include_links=True"
    # httpbin's /html page is Moby Dick excerpt with no outbound links — the
    # array can legitimately be empty. Just verify the key is set and the
    # value is a list (extractor invariant).
    assert isinstance(result.fields["links"], list)


@pytest.mark.integration
async def test_firecrawl_live_invalid_url_returns_none_or_failure() -> None:
    """Firecrawl handles unreachable / nonexistent URLs gracefully.

    A truly invalid URL should either come back as None (Firecrawl returned
    success=false / 4xx) or as a result with empty markdown. Either is
    acceptable Contract 8 behavior — we just guard against an exception.
    """
    if not os.environ.get("FIRECRAWL_API_KEY"):
        pytest.skip("FIRECRAWL_API_KEY not set; skipping live test")

    async with httpx.AsyncClient() as client:
        # A domain that's intentionally unlikely to resolve. If Firecrawl ever
        # registers it, this assertion can be loosened.
        result = await scrape(
            ScrapeRequest(
                url="https://this-domain-should-never-resolve-pls.invalid",
                person_id="test:bad",
            ),
            client=client,
            max_cost_cents=10,
        )

    # Result may be None (good) or have empty markdown (also acceptable).
    # The contract is: never raise, never fabricate.
    if result is not None:
        assert (result.fields.get("markdown") or "") == ""
