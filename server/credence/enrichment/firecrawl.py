"""Firecrawl enrichment — Wave 5 Phase 4.

API: https://docs.firecrawl.dev — `POST /v1/scrape` is the primary endpoint.
Pricing: ~1 credit/scrape on the paid tier (~$0.001 = 0.1¢). Free tier exists
but rate-limited. We treat each scrape as costing **1 cent** for cap-checking
purposes — small enough that practical caps (~$1/prospect) don't refuse it,
big enough to make spend-runaway visible in the per-tenant budget log.

## What this fetches

Given a known URL (company About page, IEEE 802 working group roster,
conference program page, GitHub profile), Firecrawl returns:

- Cleaned markdown of the main content (`onlyMainContent=true` strips nav,
  footer, cookie banners, ads).
- Page metadata: title, description, language, og:* tags, statusCode.
- Outbound links (useful for crawling related rosters / programs).

The intended call site is **narrow, known-source extraction** — the upstream
caller has already identified a URL that's likely to contain the structured
information they want. Firecrawl is not a search engine and is not a general
people lookup; it's a "fetch this specific page and give me the markdown"
service. For people/firmographic enrichment, prefer Apollo (Phase 1) or
PDL (Phase 2).

## Strategy

Single-step call to `POST /v1/scrape`:

```
{
  "url": "...",
  "formats": ["markdown"],          # html / links / screenshot also available
  "onlyMainContent": true,           # strip nav/footer/ads
  "waitFor": 0                       # we're not waiting for JS-rendered content
}
```

Response shape (truncated to what we extract):

```
{
  "success": true,
  "data": {
    "markdown": "# Page Title\\n...",
    "metadata": {
      "title": "...",
      "description": "...",
      "language": "en",
      "sourceURL": "...",
      "statusCode": 200,
      "ogTitle": "..." (optional)
    },
    "links": ["https://...", ...]  (when "links" requested in formats)
  }
}
```

For v1, `formats=["markdown"]` keeps the response small and the cost
predictable; `links` is only requested when `include_links=True` is passed.

## Sandbox / live status

Implementation is doc-driven against Firecrawl v1. **Live integration test
is `tests/test_firecrawl_live.py`** (marked `@pytest.mark.integration`,
opt-in via `pytest -m integration`). Unit tests in `tests/test_firecrawl.py`
mock the httpx transport.

## Cost handling

We estimate 1 cent per scrape regardless of formats requested. Firecrawl's
billing is reconciled against credit usage on the dashboard; per-call cost
is reported via `ScrapeResult.cost_cents` so the route layer (M4 budget
checks) can compare against `account_settings.firecrawl_monthly_cents`.

## Idempotency / cache

The route layer handles cache — Firecrawl results are deterministic-ish
per URL (the scraped page is upstream of us), so a 24h cache window mirrors
what the route does for Apollo.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, TypedDict
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

FIRECRAWL_BASE_URL = "https://api.firecrawl.dev/v1/"
DEFAULT_TIMEOUT_SECONDS = 30.0  # Firecrawl pages can take 5-15s server-side

# Firecrawl bills per credit; default scrape ≈ 1 credit ≈ 0.1¢. We round up
# to 1¢ for cap-checking so spend trends are visible in the cost log without
# rounding noise. Reconciled monthly against Firecrawl dashboard.
FIRECRAWL_SCRAPE_CENTS = 1


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ScrapeRequest:
    """Inputs to a single Firecrawl scrape.

    `url` is the target page. `person_id` is carried only for traceability
    in logs — Firecrawl itself doesn't see it. `formats` controls what we
    ask Firecrawl to return (markdown is cheapest; html/links cost the
    same credits but inflate response size).
    """

    url: str
    person_id: str | None = None
    formats: tuple[str, ...] = ("markdown",)
    only_main_content: bool = True
    include_links: bool = False


class FirecrawlFields(TypedDict, total=False):
    """Vendor-specific payload for Contract 8's `EnrichmentRecord.fields`."""

    url: str
    title: str | None
    description: str | None
    language: str | None
    status_code: int | None
    markdown: str | None
    links: list[str]


@dataclass(frozen=True, slots=True)
class ScrapeResult:
    """Per-vendor result handed back to the route layer."""

    fields: FirecrawlFields
    confidence: float
    cost_cents: int
    cache_hit: bool = False


# ─── HTTP I/O ───────────────────────────────────────────────────────────────


async def _firecrawl_post(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any],
    *,
    api_key: str,
) -> dict[str, Any] | None:
    """POST to Firecrawl, return JSON dict on 200, None on any failure mode.

    Firecrawl auths via `Authorization: Bearer <key>`. Network errors,
    non-200, non-JSON, and explicit `success: false` payloads all collapse
    to None — Contract 8 partial-results semantics.
    """
    url = urljoin(FIRECRAWL_BASE_URL, path)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        r = await client.post(
            url, json=payload, headers=headers, timeout=DEFAULT_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        logger.warning("Firecrawl request failed (%s): %s", path, exc)
        return None
    if r.status_code in (401, 403):
        # Auth issues are sticky — log loud so ops sees them in the digest.
        logger.error(
            "Firecrawl auth failure at %s — check FIRECRAWL_API_KEY rotation", path
        )
        return None
    if r.status_code == 429:
        logger.warning("Firecrawl rate-limited at %s", path)
        return None
    if r.status_code == 402:
        # Out of credits / billing issue. Log at error level — distinct
        # from rate-limit because it requires human action, not retry.
        logger.error("Firecrawl billing failure at %s — out of credits or plan downgrade", path)
        return None
    if r.status_code != 200:
        logger.warning("Firecrawl HTTP %d at %s: %s", r.status_code, path, r.text[:200])
        return None
    try:
        body = r.json()
    except ValueError:
        logger.warning("Firecrawl returned non-JSON body")
        return None
    if not isinstance(body, dict):
        return None
    # Firecrawl wraps successful results in {"success": true, "data": {...}}.
    # An explicit "success": false is a soft failure — log + return None.
    if body.get("success") is False:
        logger.warning(
            "Firecrawl reported success=false at %s: %s",
            path,
            body.get("error", "<no error message>"),
        )
        return None
    return body


# ─── Field extraction ───────────────────────────────────────────────────────


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _extract_firecrawl_data(
    data: dict[str, Any], *, request_url: str, include_links: bool
) -> FirecrawlFields:
    """Map a Firecrawl `data` dict to the Contract 8 `FirecrawlFields` shape.

    Missing fields default to None / empty list (Contract 8 invariant — never
    fabricate values). `links` is only populated when the caller requested it
    via `include_links=True`; otherwise we drop the array even if Firecrawl
    returns one (keeps the field set predictable per request).
    """
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}

    out: FirecrawlFields = FirecrawlFields(
        url=_str_or_none(metadata.get("sourceURL")) or request_url,
        title=_str_or_none(metadata.get("title")) or _str_or_none(metadata.get("ogTitle")),
        description=_str_or_none(metadata.get("description"))
        or _str_or_none(metadata.get("ogDescription")),
        language=_str_or_none(metadata.get("language")),
        status_code=_int_or_none(metadata.get("statusCode")),
        markdown=_str_or_none(data.get("markdown")),
    )

    if include_links:
        raw_links = data.get("links")
        if isinstance(raw_links, list):
            out["links"] = [link for link in raw_links if isinstance(link, str) and link]
        else:
            out["links"] = []

    return out


def _calculate_cost(_fields: FirecrawlFields) -> int:
    """Estimate cost in cents.

    Firecrawl bills 1 credit per scrape regardless of which formats are
    requested (markdown vs html vs links all roll up to one scrape). We
    surface 1¢ as the per-call cost; reconciled monthly.
    """
    return FIRECRAWL_SCRAPE_CENTS


# ─── Public API ─────────────────────────────────────────────────────────────


async def scrape(
    request: ScrapeRequest,
    *,
    client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
    max_cost_cents: int = 100,
) -> ScrapeResult | None:
    """Scrape a single URL via Firecrawl `POST /v1/scrape`.

    Returns:
        ScrapeResult on a successful scrape (with `fields`, `cost_cents`,
        `confidence`).
        None when:
        - No `FIRECRAWL_API_KEY` is configured
        - Cost cap exceeded
        - Network / auth / billing failure
        - Firecrawl reports `success: false` (page unreachable, blocked, etc.)
        - Response has no usable markdown or metadata

    The route layer is responsible for writing the `enrichment_cost_log`
    row regardless of outcome.
    """
    key = api_key or os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        logger.info(
            "firecrawl.scrape called without FIRECRAWL_API_KEY — skipping (set env or pass api_key=)"
        )
        return None

    if not isinstance(request.url, str) or not request.url.strip():
        logger.warning("firecrawl.scrape called with invalid url: %r", request.url)
        return None

    # Pre-flight cost cap.
    if FIRECRAWL_SCRAPE_CENTS > max_cost_cents:
        logger.info(
            "firecrawl.scrape: per-call cost %d¢ > cap %d¢ — skipping",
            FIRECRAWL_SCRAPE_CENTS,
            max_cost_cents,
        )
        return None

    # Build the scrape payload. Firecrawl accepts duplicate format requests
    # gracefully but we deduplicate to keep the wire payload tidy.
    formats = list(dict.fromkeys(request.formats))
    if request.include_links and "links" not in formats:
        formats.append("links")

    payload: dict[str, Any] = {
        "url": request.url,
        "formats": formats,
        "onlyMainContent": request.only_main_content,
    }

    own_client = client is None
    http = client if client is not None else httpx.AsyncClient()
    try:
        body = await _firecrawl_post(http, "scrape", payload, api_key=key)
    finally:
        if own_client:
            await http.aclose()

    if not body:
        return None

    data = body.get("data")
    if not isinstance(data, dict):
        return None

    fields = _extract_firecrawl_data(
        data, request_url=request.url, include_links=request.include_links
    )

    # If we got nothing useful — no markdown AND no title — treat as a miss.
    # This catches pages that returned 200 but were empty (login walls, JS
    # SPAs Firecrawl couldn't render in waitFor=0).
    if not fields.get("markdown") and not fields.get("title"):
        logger.info(
            "firecrawl.scrape: %s returned empty content (login wall / SPA?)",
            request.url,
        )
        return None

    # Confidence: high when markdown is non-trivial, lower for metadata-only.
    markdown = fields.get("markdown") or ""
    confidence = 0.9 if len(markdown) >= 500 else 0.7 if markdown else 0.5

    return ScrapeResult(
        fields=fields,
        confidence=confidence,
        cost_cents=_calculate_cost(fields),
        cache_hit=False,
    )
