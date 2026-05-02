"""Tier-2 enrichment: per-prospect news mentions via Parallel agentic search.

Runs ONLY on prospects promoted to ``persons.enrichment_tier = 3``
(top 10% by seniority — see ``prioritize.py``). Bulk-running this on
every prospect would cost ~$15k+ at $0.50/Parallel task; the user's
top-10% gate brings it to ~$1,000 for the full run.

## Why news mentions matter

A press release mentioning "<name>" announcing a product launch, exec
appointment, or acquisition is **Authenticity gold** — far stronger
than LinkedIn self-attribution. It also surfaces:

- **Reporting-line phrases** ("she will report to the COO") → fed to
  ``orgchart/hierarchy.ingest_explicit_edge`` per CLAUDE.md Decision 3
- **Acquisition / IPO context** that LinkedIn lags on
- **Conference keynote announcements** that miss the per-pair
  ``conference.py`` extractor's window

## Why Parallel and not Firecrawl

Firecrawl is URL-driven — you tell it where to scrape. For news mentions
we don't know the URL up front; we want to find recent articles
mentioning a specific person. Parallel's agentic search runs an LLM
that crawls + reasons + returns structured JSON — exactly the shape
this task needs.

## Cost

Parallel charges per task (~$0.30-1.00 depending on depth). At default
config (5 articles per prospect, low-depth crawl), expect $0.50/prospect.

For 2,000 Tier-3 prospects: 2,000 × $0.50 = **~$1,000**.

## Result shape

Per-prospect, the output is a flat list of ``NewsMention`` records:
``{title, source, url, published_at, summary, sentiment, kind}``.
Caller persists each as ``signals.signal_type='news_mention'`` with
``structured_value`` carrying the fields.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PARALLEL_API_BASE = "https://api.parallel.ai/v1/"
DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_ARTICLES = 5
DEFAULT_LOOKBACK_MONTHS = 12

# Per-task cost in cents — Parallel low-depth tasks. Update if pricing
# changes or we switch to high-depth.
COST_PER_TASK_CENTS = 50


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NewsMention:
    """One news article mentioning a prospect.

    Stored as ``signals.signal_type='news_mention'`` with these fields
    in ``structured_value``.
    """

    title: str
    source: str | None         # publication name — "Reuters" / "TechCrunch"
    url: str | None
    published_at: str | None   # ISO date when extractable
    summary: str | None        # 1-2 sentence summary of the mention
    sentiment: str | None      # "positive" | "neutral" | "negative" | None
    kind: str | None           # "press_release" | "interview" | "feature" | "obituary" | …


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """Per-prospect result. ``mentions`` is empty when Parallel found nothing."""

    prospect_name: str
    company_name: str | None
    mentions: list[NewsMention] = field(default_factory=list)
    cost_cents: int = 0
    task_id: str | None = None  # Parallel run id for audit / reconciliation


# ─── Field-extraction helpers ──────────────────────────────────────────────


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _parse_mention(raw: Any) -> NewsMention | None:
    if not isinstance(raw, dict):
        return None
    title = _str_or_none(raw.get("title"))
    if not title:
        return None
    return NewsMention(
        title=title,
        source=_str_or_none(raw.get("source") or raw.get("publication")),
        url=_str_or_none(raw.get("url") or raw.get("link")),
        published_at=_str_or_none(raw.get("published_at") or raw.get("date")),
        summary=_str_or_none(raw.get("summary") or raw.get("snippet")),
        sentiment=_str_or_none(raw.get("sentiment")),
        kind=_str_or_none(raw.get("kind") or raw.get("article_type")),
    )


# ─── HTTP / Parallel ───────────────────────────────────────────────────────


def _resolve_key(api_key: str | None) -> str | None:
    return api_key or os.environ.get("PARALLEL_API_KEY")


def _build_query(prospect_name: str, company_name: str | None, lookback_months: int) -> str:
    """The natural-language query we hand to Parallel."""
    co_clause = f" at {company_name}" if company_name else ""
    return (
        f"Find recent news articles (last {lookback_months} months) mentioning "
        f"{prospect_name}{co_clause}. For each article return: title, source "
        f"(publication name), url, published_at (ISO date), summary (1-2 "
        f"sentences), sentiment (positive/neutral/negative), kind "
        f"(press_release / interview / feature / acquisition / appointment)."
    )


_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "source": {"type": ["string", "null"]},
                    "url": {"type": ["string", "null"]},
                    "published_at": {"type": ["string", "null"]},
                    "summary": {"type": ["string", "null"]},
                    "sentiment": {"type": ["string", "null"]},
                    "kind": {"type": ["string", "null"]},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["mentions"],
}


async def find_news_mentions(
    prospect_name: str,
    *,
    company_name: str | None = None,
    max_articles: int = DEFAULT_MAX_ARTICLES,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> EnrichmentResult | None:
    """Run a Parallel agentic search for news mentioning the prospect.

    Returns None when Parallel is unavailable (no key, network failure,
    rate-limited). Partial results: when Parallel returns successfully
    but with zero mentions, returns ``EnrichmentResult`` with empty list.

    The caller is responsible for paying — every successful task call
    costs ``COST_PER_TASK_CENTS`` regardless of how many mentions
    are returned.
    """
    if not isinstance(prospect_name, str) or not prospect_name.strip():
        return None
    key = _resolve_key(api_key)
    if not key:
        logger.info("news: no PARALLEL_API_KEY — skipping %s", prospect_name)
        return None

    payload = {
        "input": _build_query(prospect_name, company_name, lookback_months),
        "output_schema": _OUTPUT_SCHEMA,
        "search_options": {
            "max_results": max_articles,
            "freshness": f"{lookback_months}mo",
        },
    }

    own_client = client is None
    http = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        try:
            r = await http.post(
                f"{PARALLEL_API_BASE}tasks/runs",
                json=payload,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("news: parallel task failed for %s: %s", prospect_name, exc)
            return None
        if r.status_code not in (200, 201):
            logger.warning(
                "news: parallel HTTP %d for %s — %s",
                r.status_code, prospect_name, r.text[:200],
            )
            return None
        try:
            body = r.json()
        except ValueError:
            return None
    finally:
        if own_client:
            await http.aclose()

    # Parallel response shape: { task_id, output: { mentions: [...] }, ... }
    output = (body or {}).get("output") or {}
    mentions_raw = output.get("mentions") or []
    if not isinstance(mentions_raw, list):
        mentions_raw = []

    mentions: list[NewsMention] = []
    for raw in mentions_raw:
        parsed = _parse_mention(raw)
        if parsed is not None:
            mentions.append(parsed)

    return EnrichmentResult(
        prospect_name=prospect_name,
        company_name=company_name,
        mentions=mentions,
        cost_cents=COST_PER_TASK_CENTS,  # Parallel charges per successful task
        task_id=_str_or_none((body or {}).get("task_id") or (body or {}).get("id")),
    )


__all__ = [
    "NewsMention",
    "EnrichmentResult",
    "DEFAULT_MAX_ARTICLES",
    "DEFAULT_LOOKBACK_MONTHS",
    "find_news_mentions",
]
