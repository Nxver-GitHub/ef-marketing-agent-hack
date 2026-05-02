"""Tier-1 enrichment: per-company website crawl.

Catches signals that LinkedIn doesn't cover:
- ``/about/leadership`` pages — exec roster (overlap with LinkedIn but
  with role-narrative context — "leads our 5-year roadmap to…")
- ``/press`` and ``/news`` — company-issued press releases mentioning
  named executives, often with reporting-line context ("she will report
  to the COO")
- ``/investor-relations`` — for public cos, exec changes announcements
- Blog posts authored by employees — engagement signals beyond LinkedIn

## Architecture

Per-company. Runs ONCE per company in the bulk Apify pass (after we
know the company's primary URL). Output feeds:

- ``signals.value->>signal_type`` ∈ {``leadership_listing``,
  ``press_mention``, ``blog_post_authored``}
- Reporting-line phrases from press releases feed
  ``orgchart/hierarchy.ingest_explicit_edge`` per CLAUDE.md Decision 3

## Cost

Firecrawl LLM-extract: ~$0.03 per scrape. Default URLs per company:
3-5 pages × $0.03 = $0.09-0.15 per company.

For 60 target companies: 60 × ~$0.10 = **$6 total** (well under any
realistic budget).

## URL discovery

This module assumes the caller provides candidate URLs. A future
``url_discovery.py`` could derive URLs from a company name (e.g.,
"Lockheed Martin" → ``lockheedmartin.com/en-us/who-we-are/leadership.html``)
via either a hand-curated map or Parallel agentic. For v1, the caller
passes URLs explicitly — keeps the module focused on the scrape contract.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

FIRECRAWL_BASE_URL = "https://api.firecrawl.dev/v1/"
DEFAULT_TIMEOUT_SECONDS = 90.0


SitePageKind = Literal["leadership", "press", "blog", "investor_relations", "about"]


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CompanyExecutive:
    """One executive extracted from a /leadership-style page."""

    name: str
    title: str
    bio: str | None = None
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class PressRelease:
    """One press release / news item mentioning company executives."""

    headline: str
    published_at: str | None      # ISO date when extractable
    url: str | None
    summary: str | None
    mentioned_executives: list[str] = field(default_factory=list)
    reporting_phrases: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CompanySiteSignals:
    """Per-page scrape result; one of these per (company, page_url)."""

    company_url: str
    page_url: str
    page_kind: SitePageKind
    executives: list[CompanyExecutive] = field(default_factory=list)
    press_releases: list[PressRelease] = field(default_factory=list)
    cost_cents: int = 0


# ─── Extraction schema per page kind ────────────────────────────────────────


_LEADERSHIP_SCHEMA = {
    "type": "object",
    "properties": {
        "executives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "title": {"type": "string"},
                    "bio": {"type": ["string", "null"]},
                    "image_url": {"type": ["string", "null"]},
                },
                "required": ["name", "title"],
            },
        }
    },
    "required": ["executives"],
}

_LEADERSHIP_PROMPT = (
    "Extract every executive listed on this leadership page. For each, "
    "return: {name: full name, title: exact title text, bio: 1-2 sentence "
    "summary if present, image_url: profile photo URL if present}."
)

_PRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "press_releases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "published_at": {"type": ["string", "null"]},
                    "url": {"type": ["string", "null"]},
                    "summary": {"type": ["string", "null"]},
                    "mentioned_executives": {"type": "array", "items": {"type": "string"}},
                    "reporting_phrases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["headline"],
            },
        }
    },
    "required": ["press_releases"],
}

_PRESS_PROMPT = (
    "Extract press releases / news items from this company news page. "
    "For each, return: {headline, published_at: ISO date if shown, url: "
    "permalink if available, summary: 1-2 sentence preview, "
    "mentioned_executives: list of named executives mentioned, "
    "reporting_phrases: any phrases like 'X reports to Y' or 'will report to' "
    "found in the release}."
)


def _schema_and_prompt_for(kind: SitePageKind) -> tuple[dict[str, Any], str]:
    if kind == "leadership" or kind == "about":
        return _LEADERSHIP_SCHEMA, _LEADERSHIP_PROMPT
    # press / blog / investor_relations all use the press release schema
    return _PRESS_SCHEMA, _PRESS_PROMPT


# ─── Field-extraction helpers ──────────────────────────────────────────────


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _list_of_str(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [s.strip() for s in v if isinstance(s, str) and s.strip()]


def _parse_executive(raw: Any) -> CompanyExecutive | None:
    if not isinstance(raw, dict):
        return None
    name = _str_or_none(raw.get("name"))
    title = _str_or_none(raw.get("title"))
    if not name or not title:
        return None
    return CompanyExecutive(
        name=name,
        title=title,
        bio=_str_or_none(raw.get("bio")),
        image_url=_str_or_none(raw.get("image_url")),
    )


def _parse_press(raw: Any) -> PressRelease | None:
    if not isinstance(raw, dict):
        return None
    headline = _str_or_none(raw.get("headline"))
    if not headline:
        return None
    return PressRelease(
        headline=headline,
        published_at=_str_or_none(raw.get("published_at")),
        url=_str_or_none(raw.get("url")),
        summary=_str_or_none(raw.get("summary")),
        mentioned_executives=_list_of_str(raw.get("mentioned_executives")),
        reporting_phrases=_list_of_str(raw.get("reporting_phrases")),
    )


# ─── HTTP / Firecrawl ──────────────────────────────────────────────────────


def _resolve_key(api_key: str | None) -> str | None:
    return api_key or os.environ.get("FIRECRAWL_API_KEY")


async def scrape_company_page(
    *,
    company_url: str,
    page_url: str,
    page_kind: SitePageKind,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> CompanySiteSignals | None:
    """Firecrawl-scrape one page of a company website.

    Returns None when Firecrawl is unavailable or the call fails.
    Partial-result data is returned when the LLM extract returns
    rows for some categories but not others.
    """
    key = _resolve_key(api_key)
    if not key:
        logger.info("company_site: no FIRECRAWL_API_KEY — skipping %s", page_url)
        return None

    schema, prompt = _schema_and_prompt_for(page_kind)
    payload = {
        "url": page_url,
        "formats": [{"type": "json", "prompt": prompt, "schema": schema}],
    }

    own_client = client is None
    http = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        try:
            r = await http.post(
                f"{FIRECRAWL_BASE_URL}scrape",
                json=payload,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("company_site: scrape failed for %s: %s", page_url, exc)
            return None
        if r.status_code != 200:
            logger.warning("company_site: HTTP %d for %s", r.status_code, page_url)
            return None
        try:
            body = r.json()
        except ValueError:
            return None
    finally:
        if own_client:
            await http.aclose()

    extracted = (((body or {}).get("data") or {}).get("json") or {})

    executives: list[CompanyExecutive] = []
    if page_kind in ("leadership", "about"):
        for raw in (extracted.get("executives") or []):
            parsed = _parse_executive(raw)
            if parsed is not None:
                executives.append(parsed)

    press_releases: list[PressRelease] = []
    if page_kind in ("press", "blog", "investor_relations"):
        for raw in (extracted.get("press_releases") or []):
            parsed = _parse_press(raw)
            if parsed is not None:
                press_releases.append(parsed)

    return CompanySiteSignals(
        company_url=company_url,
        page_url=page_url,
        page_kind=page_kind,
        executives=executives,
        press_releases=press_releases,
        cost_cents=3,
    )


async def scrape_company_site(
    company_url: str,
    *,
    leadership_url: str | None = None,
    press_url: str | None = None,
    investor_url: str | None = None,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[CompanySiteSignals]:
    """Convenience: hit the standard 1-3 pages for a company.

    Caller passes whichever URLs they have. Missing ones are skipped.
    Returns a list of ``CompanySiteSignals`` — one per successful scrape.
    """
    results: list[CompanySiteSignals] = []
    targets: tuple[tuple[str | None, SitePageKind], ...] = (
        (leadership_url, "leadership"),
        (press_url, "press"),
        (investor_url, "investor_relations"),
    )
    for url, kind in targets:
        if not url:
            continue
        result = await scrape_company_page(
            company_url=company_url,
            page_url=url,
            page_kind=kind,
            api_key=api_key,
            client=client,
        )
        if result is not None:
            results.append(result)
    return results


__all__ = [
    "CompanyExecutive",
    "PressRelease",
    "CompanySiteSignals",
    "SitePageKind",
    "scrape_company_page",
    "scrape_company_site",
]
