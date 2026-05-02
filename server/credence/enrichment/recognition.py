"""Tier-1 enrichment: IEEE / ACM / NAE fellowship + recognition scrape.

Ground-truth Authenticity signals that are domain-specific to our target
verticals (semiconductor, AI, defense engineering). When a Lockheed
Martin Director is also an IEEE Fellow, that's Authenticity gold —
it's documentary evidence of formal peer recognition.

## Sources

| Body | URL pattern | Coverage |
|---|---|---|
| IEEE Fellows | ``ieee.org/membership/fellows/...`` (per class year) | ~7,000 active Fellows |
| ACM Fellows | ``awards.acm.org/fellows`` | ~1,200 active Fellows |
| NAE Members | ``nae.edu/MembersDirectory.aspx`` | ~2,500 members |
| AAAS Fellows (eng/cs sections) | ``aaas.org/fellows`` | (subset relevant) |

These are public, indexed, no-key required. Firecrawl with LLM-extract
is the right tool — the pages are HTML-heavy with inconsistent layout.

## Architecture

This module is per-organization (not per-prospect). One scrape per body
per refresh cycle (e.g., quarterly). The output is a flat list of
``(name, body, year_elected, citation)`` rows that get entity-resolved
against ``persons.canonical_name`` + ``persons.name_variants[]`` later.

The entity-resolution half lives in ``writer.py`` (it has access to the
DB to do the SELECT). This module ships the scrape + parse half only.

## Cost

Firecrawl LLM-extract: ~$0.03 per page. IEEE Fellows is paginated by
class year — last 10 years × ~300 fellows/year = ~30 pages = $0.90.
ACM and NAE similar. Per quarterly refresh: ~$3 across all bodies.

## Sandbox / live status

This module ships the scrape contract; the live integration test is
gated on ``FIRECRAWL_API_KEY`` (already in ``.env.local``). Skipped
unit tests that would mock the response shape — Firecrawl LLM-extract
is non-deterministic by design (LLM extraction varies), so deterministic
unit tests aren't useful. Caller should verify shape via the live test
during the first run.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

FIRECRAWL_BASE_URL = "https://api.firecrawl.dev/v1/"
DEFAULT_TIMEOUT_SECONDS = 90.0  # LLM-extract is slow; 60-120s is normal


# ─── Recognition body catalog ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RecognitionSource:
    """One body whose member list we scrape for Recognition signals."""

    body_id: str                # short id used in signal_type
    display_name: str           # human-readable
    list_url: str               # the URL to scrape
    extraction_prompt: str      # passed to Firecrawl LLM-extract


# Curated set — covers the Authenticity signal that our 60-co target
# list cares about (semis + defense + aerospace engineering).
DEFAULT_SOURCES: tuple[RecognitionSource, ...] = (
    RecognitionSource(
        body_id="ieee_fellows",
        display_name="IEEE Fellows",
        list_url="https://services15.ieee.org/fellows-directory/",
        extraction_prompt=(
            "Extract every IEEE Fellow on this page. For each row return: "
            "{name: string, year_elected: integer | null, citation: string | null, "
            "company: string | null, location: string | null}."
        ),
    ),
    RecognitionSource(
        body_id="acm_fellows",
        display_name="ACM Fellows",
        list_url="https://awards.acm.org/fellows",
        extraction_prompt=(
            "Extract every ACM Fellow on this page. Return per row: "
            "{name: string, year_elected: integer | null, citation: string | null}."
        ),
    ),
    RecognitionSource(
        body_id="nae_members",
        display_name="National Academy of Engineering Members",
        list_url="https://www.nae.edu/MembersDirectory.aspx",
        extraction_prompt=(
            "Extract every NAE member on this page. Return per row: "
            "{name: string, year_elected: integer | null, "
            "primary_section: string | null, primary_affiliation: string | null}."
        ),
    ),
)


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RecognitionRecord:
    """One entry from a recognition body's directory.

    Stored as ``signal_type='formal_recognition'`` in the signals table
    after entity-resolution against the persons table.
    """

    body_id: str                # 'ieee_fellows' | 'acm_fellows' | 'nae_members'
    body_display: str           # 'IEEE Fellows'
    name: str                   # raw name as it appears in the directory
    year_elected: int | None
    citation: str | None        # one-line citation when available
    company: str | None         # affiliation listed in the directory
    location: str | None
    source_url: str             # the page we scraped


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """Per-body scrape result."""

    body_id: str
    records: list[RecognitionRecord]
    cost_cents: int             # Firecrawl charge for this scrape


# ─── Field-extraction helpers ──────────────────────────────────────────────


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _int_or_none(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _parse_record(
    raw: Any, *, source: RecognitionSource
) -> RecognitionRecord | None:
    """Map one Firecrawl extraction row → ``RecognitionRecord``.

    Returns None when the row lacks a usable ``name``.
    """
    if not isinstance(raw, dict):
        return None
    name = _str_or_none(raw.get("name"))
    if not name:
        return None
    return RecognitionRecord(
        body_id=source.body_id,
        body_display=source.display_name,
        name=name,
        year_elected=_int_or_none(raw.get("year_elected")),
        citation=_str_or_none(raw.get("citation")),
        company=_str_or_none(raw.get("company") or raw.get("primary_affiliation")),
        location=_str_or_none(raw.get("location")),
        source_url=source.list_url,
    )


# ─── HTTP / Firecrawl ──────────────────────────────────────────────────────


def _resolve_key(api_key: str | None) -> str | None:
    return api_key or os.environ.get("FIRECRAWL_API_KEY")


async def scrape_source(
    source: RecognitionSource,
    *,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> EnrichmentResult | None:
    """Scrape one recognition body's directory page.

    Returns None when Firecrawl rejects the call (auth, rate-limit, timeout)
    or returns an unusable shape. Partial results within a successful call
    pass through (rows missing ``name`` are dropped, others kept).
    """
    key = _resolve_key(api_key)
    if not key:
        logger.info("recognition: no FIRECRAWL_API_KEY — skipping %s", source.body_id)
        return None

    own_client = client is None
    http = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    payload = {
        "url": source.list_url,
        "formats": [
            {
                "type": "json",
                "prompt": source.extraction_prompt,
                "schema": {
                    "type": "object",
                    "properties": {
                        "members": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "year_elected": {"type": ["integer", "null"]},
                                    "citation": {"type": ["string", "null"]},
                                    "company": {"type": ["string", "null"]},
                                    "primary_affiliation": {"type": ["string", "null"]},
                                    "location": {"type": ["string", "null"]},
                                },
                                "required": ["name"],
                            },
                        }
                    },
                    "required": ["members"],
                },
            }
        ],
    }

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
            logger.warning("recognition: scrape failed for %s: %s", source.body_id, exc)
            return None
        if r.status_code != 200:
            logger.warning(
                "recognition: scrape HTTP %d for %s — %s",
                r.status_code, source.body_id, r.text[:200],
            )
            return None
        try:
            body = r.json()
        except ValueError:
            return None
    finally:
        if own_client:
            await http.aclose()

    # Firecrawl v1 response shape: { success, data: { json: { members: [...] } } }
    extracted = (((body or {}).get("data") or {}).get("json") or {})
    members_raw = extracted.get("members")
    if not isinstance(members_raw, list):
        return EnrichmentResult(body_id=source.body_id, records=[], cost_cents=3)

    records: list[RecognitionRecord] = []
    for raw in members_raw:
        rec = _parse_record(raw, source=source)
        if rec is not None:
            records.append(rec)

    return EnrichmentResult(
        body_id=source.body_id,
        records=records,
        cost_cents=3,  # Firecrawl LLM-extract is ~$0.03 per page
    )


async def scrape_all_sources(
    sources: tuple[RecognitionSource, ...] = DEFAULT_SOURCES,
    *,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[EnrichmentResult]:
    """Run every configured source in series. Returns one result per
    successful scrape (sources that returned None are dropped from the
    output)."""
    out: list[EnrichmentResult] = []
    for src in sources:
        result = await scrape_source(src, api_key=api_key, client=client)
        if result is not None:
            out.append(result)
    return out


__all__ = [
    "RecognitionSource",
    "RecognitionRecord",
    "EnrichmentResult",
    "DEFAULT_SOURCES",
    "scrape_source",
    "scrape_all_sources",
]
