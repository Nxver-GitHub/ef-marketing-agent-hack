"""Bulk company enrichment — scrape leadership + press pages via Firecrawl.

COMPANY_ENRICHMENT_PLAN.md Step 3.

Processes every company with `enrichment_status IN ('pending', 'error')`,
constructs candidate `/leadership` and `/press` URLs from the company's
domain, calls the existing `company_site.scrape_company_site` Firecrawl
extractor, and writes the results to `company_signals` (one row per
executive_profile + one per press_release).

## Why this is its own job and not a per-company endpoint

Backfill is a one-shot operation; concurrent calls to Firecrawl's API are
capped (default 10) and burning that budget on per-prospect-page-load
requests would starve the demo's interactive flows. The bulk job runs
out-of-band, gates on the cap, and is safe to re-run because the
`enrichment_status` flag prevents repeat work.

## Cost model

Per company we hit 2 pages × $0.03/page = ~$0.06. Across 585 companies
that's ~$35. Adjustable via `--limit` for a smaller pilot.

## Idempotency

`enrichment_status='done'` excludes the company from future runs.
Re-running on `--limit 5` won't double-process companies already marked
done. Operators wanting a forced refresh use `refresh_company_enrichment.py`
which resets stale rows to 'pending'.

## Concurrency

`asyncio.Semaphore(--concurrency, default 10)` — matches Firecrawl's
default rate limit. The DB pool can serve all 10 concurrent writes
comfortably (each write is one INSERT + one UPDATE per company).

## Usage

    cd server
    DATABASE_URL=...  FIRECRAWL_API_KEY=...  uv run python -m \\
      credence.enrichment.bulk_company_enrichment --limit 50

Add `--dry-run` to see which companies would be enriched without
hitting Firecrawl.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

# Allow `python -m credence.enrichment.bulk_company_enrichment` from server/
# AND `python server/credence/enrichment/bulk_company_enrichment.py` from
# repo root. The latter needs the sys.path hop.
if __package__ in (None, ""):
    _SERVER_DIR = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    if _SERVER_DIR not in sys.path:
        sys.path.insert(0, _SERVER_DIR)

from credence.db import acquire, close_pool, fetch  # noqa: E402
from credence.enrichment.company_site import (  # noqa: E402
    CompanySiteSignals,
    scrape_company_site,
)

log = logging.getLogger(__name__)


DEFAULT_CONCURRENCY: int = 10
DEFAULT_LIMIT: int = 500


# ── Pure URL construction ──────────────────────────────────────────────────


# Common /leadership-style paths in priority order. We try the first one
# that responds with usable content per Firecrawl's page-fetch fallback.
# Empirical priority list, ranked by hit rate against the Tier-1 semi/defense
# universe (see Firecrawl probes 2026-05-02). Companies that don't hit any of
# the first 4 are unlikely to hit a deeper path either; truncating to the
# top 4 caps the per-company cost at 4 leadership + 4 press = 8 page-extracts
# (~40 credits) when nothing works.
_LEADERSHIP_PATHS: tuple[str, ...] = (
    "/leadership",                  # NVIDIA, GE, Boeing
    "/about/leadership",            # AMD, Honeywell, Lockheed Martin
    "/about/our-leadership",        # Qualcomm, RTX
    "/about-us/leadership-team",    # Microchip, NXP variants
    "/about-micron/leadership",     # Micron-specific
    "/company/leadership",          # Intel-style legacy
    "/who-we-are/leadership",
)
_PRESS_PATHS: tuple[str, ...] = (
    "/news",                        # most enterprises (works as a hub)
    "/press-releases",              # Intel, AMD, NVIDIA legacy
    "/newsroom",                    # GE, Honeywell, Boeing
    "/about/news",                  # generic fallback
    "/press",
    "/about/press",
    "/company/news",
)


def _normalize_domain(domain: str | None) -> str | None:
    """Strip protocol/www/trailing-slash so we can rebuild known-good URLs."""
    if not domain:
        return None
    d = domain.strip().lower()
    for prefix in ("http://", "https://"):
        if d.startswith(prefix):
            d = d[len(prefix) :]
    if d.startswith("www."):
        d = d[4:]
    d = d.rstrip("/")
    return d or None


def candidate_urls(domain: str | None) -> tuple[str | None, str | None, str | None]:
    """Return (root_url, leadership_url, press_url) for a domain.

    Picks the first path from each priority list — kept for backward
    compatibility with single-URL callers + tests. The bulk job uses
    `candidate_url_lists` below to try multiple paths per company.
    """
    norm = _normalize_domain(domain)
    if norm is None:
        return None, None, None
    root = f"https://{norm}"
    leadership = f"https://{norm}{_LEADERSHIP_PATHS[0]}"
    press = f"https://{norm}{_PRESS_PATHS[0]}"
    return root, leadership, press


def candidate_url_lists(
    domain: str | None,
    *,
    leadership_paths: tuple[str, ...] = _LEADERSHIP_PATHS[:4],
    press_paths: tuple[str, ...] = _PRESS_PATHS[:4],
) -> tuple[str | None, list[str], list[str]]:
    """Return (root_url, leadership_url_candidates, press_url_candidates).

    Production sites rarely host their leadership page at exactly `/leadership`
    — Intel uses `/content/www/us/en/corporate/leadership.html`, ASML uses
    `/about/leadership`, Honeywell hides it under `/company/leadership`. The
    bulk caller iterates the candidate list and stops at the first scrape
    that returns ≥1 executive (or all 400/404 attempts exhausted).

    Default truncates to 3 paths per kind to bound the worst-case cost at
    6 page-extracts per company (3 leadership + 3 press) when nothing
    works. Most companies hit a real page on the first or second try.
    """
    norm = _normalize_domain(domain)
    if norm is None:
        return None, [], []
    root = f"https://{norm}"
    leadership_urls = [f"https://{norm}{path}" for path in leadership_paths]
    press_urls = [f"https://{norm}{path}" for path in press_paths]
    return root, leadership_urls, press_urls


# ── DB-facing types ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CompanyRow:
    """Slice of `companies` we need to drive a single enrichment."""

    id: UUID
    account_id: UUID
    canonical_name: str
    domains: list[str]


@dataclass(slots=True)
class BulkRollup:
    """Per-run summary for the operator + tests."""

    candidates: int = 0
    enriched: int = 0
    skipped_no_domain: int = 0
    errors: int = 0
    signals_written: int = 0
    errors_by_company: dict[str, str] = field(default_factory=dict)


# ── DB I/O ──────────────────────────────────────────────────────────────────


async def _load_pending_companies(
    *, limit: int, all_companies: bool = False
) -> list[CompanyRow]:
    """Companies that haven't been enriched yet (or that errored last time).

    By default we filter to "companies with ≥1 current employee" — the
    operationally-useful set. Without that filter the `companies` table
    pulls in 35k+ rows of past-employer / acquired / defunct entities
    (LinkedIn employment history blows the row count up), and burning
    Firecrawl on those is wasted spend.

    Pass `all_companies=True` to disable the filter — useful for ops
    that want to enrich a specific set of unfilled rows after the
    operational set is done.

    Order is by current-employee count descending so the highest-value
    companies (most prospects we can sell into) get enriched first when
    `--limit` is used as a budget cap.
    """
    if all_companies:
        rows = await fetch(
            """
            SELECT id, account_id, canonical_name, COALESCE(domains, ARRAY[]::TEXT[]) AS domains
            FROM companies
            WHERE enrichment_status IS NULL
               OR enrichment_status IN ('pending', 'error')
            ORDER BY canonical_name
            LIMIT $1
            """,
            limit,
        )
    else:
        rows = await fetch(
            """
            SELECT c.id, c.account_id, c.canonical_name,
                   COALESCE(c.domains, ARRAY[]::TEXT[]) AS domains,
                   pp.n AS current_count
            FROM companies c
            JOIN (
                SELECT current_company_id AS company_id, COUNT(*) AS n
                FROM persons
                WHERE current_company_id IS NOT NULL
                GROUP BY current_company_id
            ) pp ON pp.company_id = c.id
            WHERE c.enrichment_status IS NULL
               OR c.enrichment_status IN ('pending', 'error')
            ORDER BY pp.n DESC
            LIMIT $1
            """,
            limit,
        )
    return [
        CompanyRow(
            id=row["id"],
            account_id=row["account_id"],
            canonical_name=row["canonical_name"],
            domains=list(row["domains"] or []),
        )
        for row in rows
    ]


async def _mark_status(
    company_id: UUID, status: str, *, last_run: datetime | None = None
) -> None:
    """Idempotent status flip + timestamp bump."""
    await fetch(
        """
        UPDATE companies
           SET enrichment_status   = $2,
               enrichment_last_run = COALESCE($3, enrichment_last_run),
               updated_at          = now()
         WHERE id = $1
        """,
        company_id,
        status,
        last_run,
    )


async def _write_signals(
    company: CompanyRow,
    signals: list[CompanySiteSignals],
) -> int:
    """Bulk-INSERT the per-page extracted rows into `company_signals`.

    Each `CompanySiteSignals` carries 0..N executives + 0..N press releases.
    We flatten into one DB row per executive + one per press release. JSONB
    payload mirrors the shape `explain_company` will return so the read
    side doesn't have to re-shape.
    """
    if not signals:
        return 0
    rows: list[tuple[Any, ...]] = []
    fetched_at = datetime.now(UTC)
    for sig in signals:
        for exec_ in sig.executives:
            rows.append(
                (
                    company.account_id,
                    company.id,
                    "executive_profile",
                    "firecrawl_leadership",
                    {
                        "name": exec_.name,
                        "title": exec_.title,
                        "bio": exec_.bio,
                        "image_url": exec_.image_url,
                        "page_url": sig.page_url,
                    },
                    0.85,
                    fetched_at,
                )
            )
        for pr in sig.press_releases:
            rows.append(
                (
                    company.account_id,
                    company.id,
                    "press_release",
                    "firecrawl_press",
                    {
                        "headline": pr.headline,
                        "published_at": pr.published_at,
                        "url": pr.url,
                        "summary": pr.summary,
                        "mentioned_executives": pr.mentioned_executives,
                        "reporting_phrases": pr.reporting_phrases,
                        "page_url": sig.page_url,
                    },
                    0.90,
                    fetched_at,
                )
            )
    if not rows:
        return 0

    # Use the same COPY-into-temp pattern as scope/clustering bulk inserts —
    # asyncpg can't bind jsonb in COPY binary, so we serialize structured_value
    # to text and cast on the way out.
    import json
    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                CREATE TEMP TABLE IF NOT EXISTS _company_signal_chunk (
                  account_id            uuid NOT NULL,
                  company_id            uuid NOT NULL,
                  signal_type           text NOT NULL,
                  source                text NOT NULL,
                  structured_value_json text NOT NULL,
                  confidence            numeric NOT NULL,
                  fetched_at            timestamptz NOT NULL
                ) ON COMMIT DROP
                """
            )
            await conn.execute("TRUNCATE _company_signal_chunk")
            await conn.copy_records_to_table(
                "_company_signal_chunk",
                records=[
                    (
                        r[0], r[1], r[2], r[3],
                        json.dumps(r[4]),
                        r[5], r[6],
                    )
                    for r in rows
                ],
                columns=[
                    "account_id",
                    "company_id",
                    "signal_type",
                    "source",
                    "structured_value_json",
                    "confidence",
                    "fetched_at",
                ],
            )
            await conn.execute(
                """
                INSERT INTO company_signals
                  (account_id, company_id, signal_type, source,
                   structured_value, confidence, fetched_at)
                SELECT
                  account_id, company_id, signal_type, source,
                  structured_value_json::jsonb, confidence, fetched_at
                FROM _company_signal_chunk
                """
            )
    return len(rows)


# ── Per-company orchestration ───────────────────────────────────────────────


async def enrich_one(company: CompanyRow, *, dry_run: bool = False) -> int:
    """Returns the count of signals written. 0 on no-domain or error.

    Tries each candidate path in `_LEADERSHIP_PATHS[:3]` / `_PRESS_PATHS[:3]`
    until one scrape returns ≥1 row or the candidates are exhausted. Stops
    at the first hit per kind so we don't burn credits scraping every
    fallback when the first one works.
    """
    domain = company.domains[0] if company.domains else None
    root_url, leadership_candidates, press_candidates = candidate_url_lists(domain)

    if not root_url:
        log.info(
            "enrich: skip %s — no usable domain (have=%r)",
            company.canonical_name, company.domains,
        )
        return 0

    if dry_run:
        log.info(
            "enrich: [DRY] would scrape leadership_candidates=%s press_candidates=%s for %s",
            leadership_candidates, press_candidates, company.canonical_name,
        )
        return 0

    await _mark_status(company.id, "running")
    all_signals: list[CompanySiteSignals] = []
    try:
        # Leadership: try each candidate path, stop at first non-empty hit.
        for url in leadership_candidates:
            sigs = await scrape_company_site(
                company_url=root_url, leadership_url=url, press_url=None,
            )
            non_empty = [s for s in sigs if s.executives]
            if non_empty:
                all_signals.extend(non_empty)
                break
        # Press: same fallback walk.
        for url in press_candidates:
            sigs = await scrape_company_site(
                company_url=root_url, leadership_url=None, press_url=url,
            )
            non_empty = [s for s in sigs if s.press_releases]
            if non_empty:
                all_signals.extend(non_empty)
                break
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "enrich: scrape failed for %s — %s", company.canonical_name, exc
        )
        await _mark_status(company.id, "error", last_run=datetime.now(UTC))
        raise
    written = await _write_signals(company, all_signals)
    await _mark_status(company.id, "done", last_run=datetime.now(UTC))
    log.info(
        "enrich: %s — %d signals across %d pages",
        company.canonical_name, written, len(all_signals),
    )
    return written


# ── Public bulk runner ──────────────────────────────────────────────────────


async def run_bulk(
    *,
    limit: int = DEFAULT_LIMIT,
    concurrency: int = DEFAULT_CONCURRENCY,
    dry_run: bool = False,
    all_companies: bool = False,
) -> BulkRollup:
    """Fan out enrichment over `concurrency` workers, return rollup."""
    candidates = await _load_pending_companies(
        limit=limit, all_companies=all_companies,
    )
    rollup = BulkRollup(candidates=len(candidates))
    if not candidates:
        log.info("enrich: no pending companies — nothing to do")
        return rollup

    sem = asyncio.Semaphore(concurrency)

    async def _guarded(company: CompanyRow) -> None:
        async with sem:
            try:
                if not company.domains:
                    rollup.skipped_no_domain += 1
                    return
                written = await enrich_one(company, dry_run=dry_run)
                rollup.signals_written += written
                rollup.enriched += 1
            except Exception as exc:  # noqa: BLE001
                rollup.errors += 1
                rollup.errors_by_company[company.canonical_name] = str(exc)[:200]

    await asyncio.gather(*(_guarded(c) for c in candidates))
    return rollup


# ── CLI ─────────────────────────────────────────────────────────────────────


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if not args.dry_run and not os.environ.get("FIRECRAWL_API_KEY"):
        log.error("FIRECRAWL_API_KEY not set — aborting (use --dry-run for plan-only)")
        return 2
    try:
        rollup = await run_bulk(
            limit=args.limit,
            concurrency=args.concurrency,
            dry_run=args.dry_run,
            all_companies=args.all_companies,
        )
    finally:
        await close_pool()
    print(
        f"bulk_company_enrichment: candidates={rollup.candidates} "
        f"enriched={rollup.enriched} skipped_no_domain={rollup.skipped_no_domain} "
        f"errors={rollup.errors} signals_written={rollup.signals_written} "
        f"(dry_run={args.dry_run})"
    )
    if rollup.errors_by_company:
        print("first 5 errors:")
        for name, msg in list(rollup.errors_by_company.items())[:5]:
            print(f"  {name}: {msg}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk enrich companies via Firecrawl /leadership + /press scrape."
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--all-companies",
        action="store_true",
        help="Include companies with no current employees (otherwise filter to operationally-useful set).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()


__all__ = [
    "BulkRollup",
    "CompanyRow",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_LIMIT",
    "candidate_urls",
    "enrich_one",
    "run_bulk",
]
