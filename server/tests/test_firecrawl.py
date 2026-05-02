"""Tests for `credence.enrichment.firecrawl` — Wave 5 Phase 4 vendor.

Uses `httpx.MockTransport` to return canned Firecrawl /v1/scrape responses
so the parsing logic is exercised without network access. Live API
integration is in `tests/test_firecrawl_live.py` (`@pytest.mark.integration`,
opt-in via `pytest -m integration`).

Coverage:
1. Happy path — full markdown + metadata → FirecrawlFields populated
2. Missing API key → None, no API call
3. Cost cap below per-call cost → None, no API call
4. Network error → None
5. Auth failure (401, 403) → None
6. Rate limit (429) → None
7. Billing failure (402) → None (distinct from rate limit — needs human action)
8. Non-200 status → None
9. Non-JSON body → None
10. `success: false` Firecrawl payload → None
11. Empty data (no markdown, no title) → None — guards login-wall / SPA case
12. Short markdown → confidence 0.7 (under 500 chars threshold)
13. Metadata-only (no markdown but has title) → confidence 0.5
14. `include_links=True` populates `fields["links"]`
15. `include_links=False` drops links even if Firecrawl returns them
16. Invalid url input (None / empty) → None
17. `only_main_content=False` propagates into payload
18. Format deduplication — caller passes ("markdown", "markdown") → wire payload has 1 entry
19. `Authorization: Bearer <key>` header sent
20. `metadata.sourceURL` overrides `request.url` in `fields["url"]` when present
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from credence.enrichment.firecrawl import (
    FIRECRAWL_BASE_URL,
    FIRECRAWL_SCRAPE_CENTS,
    ScrapeRequest,
    scrape,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


REQ_TSMC = ScrapeRequest(
    url="https://www.tsmc.com/about/leadership",
    person_id="p:lin",
)

REQ_GITHUB = ScrapeRequest(
    url="https://github.com/orgs/intel/people",
    person_id="p:marcus",
    include_links=True,
)


def _full_firecrawl_data() -> dict[str, Any]:
    """Canned response — Firecrawl wraps successful scrapes in `data`."""
    return {
        "markdown": "# TSMC Leadership\n\nDr. C.C. Wei serves as Chairman and CEO. "
                    + ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 20),
        "metadata": {
            "title": "TSMC Leadership",
            "description": "Executive leadership at Taiwan Semiconductor Manufacturing Company",
            "language": "en",
            "sourceURL": "https://www.tsmc.com/about/leadership",
            "statusCode": 200,
            "ogTitle": "TSMC | Leadership",
        },
        "links": [
            "https://www.tsmc.com/about",
            "https://www.tsmc.com/news",
        ],
    }


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_happy_path_full_scrape() -> None:
    """Full markdown + metadata → FirecrawlFields populated, confidence 0.9."""
    canned = {"success": True, "data": _full_firecrawl_data()}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _client_with(handler) as client:
        result = await scrape(
            REQ_TSMC, client=client, api_key="fc-fake", max_cost_cents=100
        )

    assert result is not None
    assert result.fields["url"] == "https://www.tsmc.com/about/leadership"
    assert result.fields["title"] == "TSMC Leadership"
    assert "Dr. C.C. Wei" in (result.fields["markdown"] or "")
    assert result.fields["language"] == "en"
    assert result.fields["status_code"] == 200
    assert "links" not in result.fields  # include_links default False
    assert result.confidence == 0.9
    assert result.cost_cents == FIRECRAWL_SCRAPE_CENTS
    assert result.cache_hit is False


@pytest.mark.unit
async def test_missing_api_key_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither api_key= nor FIRECRAWL_API_KEY env var is present, scrape
    short-circuits to None without an HTTP call. Explicitly deletes the env
    var because `.env.local` (loaded by uv --env-file at the dev shell) can
    populate it in the test environment otherwise."""
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"success": True, "data": {}})

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key=None, max_cost_cents=100)

    assert result is None
    assert called is False, "no HTTP call should be made without API key"


@pytest.mark.unit
async def test_cost_cap_below_per_call_skips() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"success": True, "data": _full_firecrawl_data()})

    async with _client_with(handler) as client:
        result = await scrape(
            REQ_TSMC, client=client, api_key="fc-fake",
            max_cost_cents=FIRECRAWL_SCRAPE_CENTS - 1,
        )

    assert result is None
    assert called is False, "cap below per-call cost must skip the API call"


@pytest.mark.unit
async def test_network_error_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key="fc-fake")

    assert result is None


@pytest.mark.unit
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_returns_none(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "unauthorized"})

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key="fc-bad")

    assert result is None


@pytest.mark.unit
async def test_rate_limit_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limit"})

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key="fc-fake")

    assert result is None


@pytest.mark.unit
async def test_billing_failure_returns_none() -> None:
    """402 is distinct from rate limit — out of credits, needs human action."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": "out of credits"})

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key="fc-fake")

    assert result is None


@pytest.mark.unit
async def test_unexpected_status_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream timeout")

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key="fc-fake")

    assert result is None


@pytest.mark.unit
async def test_non_json_body_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key="fc-fake")

    assert result is None


@pytest.mark.unit
async def test_explicit_success_false_returns_none() -> None:
    """Firecrawl can return 200 with {success: false, error: ...} — soft failure."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": False, "error": "URL blocked by robots.txt"},
        )

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key="fc-fake")

    assert result is None


@pytest.mark.unit
async def test_empty_data_returns_none() -> None:
    """No markdown AND no title → treated as miss (login wall / SPA case)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": True, "data": {"metadata": {"sourceURL": "...", "statusCode": 200}}},
        )

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key="fc-fake")

    assert result is None


@pytest.mark.unit
async def test_short_markdown_yields_lower_confidence() -> None:
    """Markdown under 500 chars → confidence 0.7."""
    short_data = {
        "markdown": "# Short page\n\nBrief content.",
        "metadata": {"title": "Short page", "statusCode": 200, "sourceURL": REQ_TSMC.url},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": short_data})

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key="fc-fake")

    assert result is not None
    assert result.confidence == 0.7


@pytest.mark.unit
async def test_metadata_only_yields_lowest_confidence() -> None:
    """Title but no markdown → confidence 0.5 (still a hit, but thin)."""
    metadata_only = {
        "metadata": {
            "title": "TSMC About",
            "statusCode": 200,
            "sourceURL": REQ_TSMC.url,
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": metadata_only})

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key="fc-fake")

    assert result is not None
    assert result.confidence == 0.5
    assert result.fields.get("markdown") is None


@pytest.mark.unit
async def test_include_links_populates_links_field() -> None:
    canned = {"success": True, "data": _full_firecrawl_data()}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _client_with(handler) as client:
        result = await scrape(REQ_GITHUB, client=client, api_key="fc-fake")

    assert result is not None
    assert "links" in result.fields
    assert result.fields["links"] == [
        "https://www.tsmc.com/about",
        "https://www.tsmc.com/news",
    ]


@pytest.mark.unit
async def test_links_dropped_when_not_requested() -> None:
    """Even if Firecrawl returns a links array, drop it when caller didn't ask."""
    canned = {"success": True, "data": _full_firecrawl_data()}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key="fc-fake")

    assert result is not None
    assert "links" not in result.fields


@pytest.mark.unit
@pytest.mark.parametrize("bad_url", [None, "", "   "])
async def test_invalid_url_returns_none(bad_url: Any) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"success": True, "data": {}})

    bad_request = ScrapeRequest(url=bad_url) if bad_url is not None else ScrapeRequest(url="")  # type: ignore[arg-type]

    async with _client_with(handler) as client:
        result = await scrape(bad_request, client=client, api_key="fc-fake")

    assert result is None
    assert called is False


@pytest.mark.unit
async def test_only_main_content_propagated_into_payload() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = request.read()
        return httpx.Response(
            200, json={"success": True, "data": _full_firecrawl_data()}
        )

    async with _client_with(handler) as client:
        await scrape(
            ScrapeRequest(url=REQ_TSMC.url, only_main_content=False),
            client=client, api_key="fc-fake",
        )

    import json as _json
    body = _json.loads(captured["payload"])
    assert body["onlyMainContent"] is False


@pytest.mark.unit
async def test_format_deduplication() -> None:
    """Caller passes ('markdown', 'markdown') → wire payload has one 'markdown'."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = request.read()
        return httpx.Response(
            200, json={"success": True, "data": _full_firecrawl_data()}
        )

    async with _client_with(handler) as client:
        await scrape(
            ScrapeRequest(url=REQ_TSMC.url, formats=("markdown", "markdown")),
            client=client, api_key="fc-fake",
        )

    import json as _json
    body = _json.loads(captured["payload"])
    assert body["formats"] == ["markdown"], "duplicate formats must be deduped"


@pytest.mark.unit
async def test_bearer_auth_header_sent() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(
            200, json={"success": True, "data": _full_firecrawl_data()}
        )

    async with _client_with(handler) as client:
        await scrape(REQ_TSMC, client=client, api_key="fc-secret-key")

    assert captured["auth"] == "Bearer fc-secret-key"


@pytest.mark.unit
async def test_source_url_from_metadata_overrides_request_url() -> None:
    """If Firecrawl follows redirects, metadata.sourceURL is the canonical URL."""
    redirected = {
        "markdown": "# Redirected page",
        "metadata": {
            "title": "Redirected",
            "sourceURL": "https://www.tsmc.com/canonical-url",
            "statusCode": 200,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": redirected})

    async with _client_with(handler) as client:
        result = await scrape(REQ_TSMC, client=client, api_key="fc-fake")

    assert result is not None
    assert result.fields["url"] == "https://www.tsmc.com/canonical-url"


@pytest.mark.unit
async def test_request_targets_v1_scrape_endpoint() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200, json={"success": True, "data": _full_firecrawl_data()}
        )

    async with _client_with(handler) as client:
        await scrape(REQ_TSMC, client=client, api_key="fc-fake")

    assert captured["url"] == FIRECRAWL_BASE_URL + "scrape"
