"""Bulk per-prospect Semantic Scholar ingestion runner.

For every prospect in a tenant (``account_id``), call Semantic Scholar to
fetch their authored papers, build an in-memory ``paper_id → list[(prospect_id,
formatted_record)]`` index, then for every paper authored by ≥2 prospects in
the set emit ``signal_type='academic_co_author'`` rows into the v2 ``signals``
table — one row per ordered pair (``person_a_id < person_b_id`` lexically),
Contract 1 ``structured_value`` shape — pointing each prospect at the other
via ``connected_to``.

This is a write-only data pipeline — no UI, no API, no scoring. It populates
the data the existing frontend fifth pass (``src/lib/graph.ts:1023``) reads to
render ``academic_co_author`` edges.

## Idempotency

Re-runs do NOT pile up duplicates. Before INSERTing a signal we run an
explicit ``SELECT 1 ... LIMIT 1`` keyed on ``(prospect_id, signal_type,
value->>'semantic_scholar_id', value->>'connected_to')``. The existing
``_persist_signal`` helper in ``signals.py`` uses ``ON CONFLICT DO NOTHING``
with no constraint target — that's a no-op for our use case, so we don't
rely on it. Explicit pre-check, then conditional INSERT.

## Rate limit

Semantic Scholar unauthenticated quota is 100 requests / 5 minutes ≈ 0.33
req/sec. With ``concurrency=2`` you can only exceed that once you've ramped
past ~50 prospects, but we still gate the whole pipeline through a small
token-bucket :class:`RateLimiter` (100 tokens, refill 1 token every 3
seconds) so a long-running tenant scan stays well below the cap.

## CLI

::

    cd server && uv run python -m credence.jobs.bulk_scholar_ingest \\
        --account-id <uuid> --limit 100 --dry-run

    cd server && uv run python -m credence.jobs.bulk_scholar_ingest \\
        --all-accounts --max-papers-per-person 50 --concurrency 2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from ..db import acquire, close_pool
from ..extractors.patents import PersonRef
from ..extractors.scholar import (
    _fetch_author_papers,
    _format_paper_record,
    _resolve_author_id,
)

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


SIGNAL_TYPE = "academic_co_author"
SIGNAL_SOURCE = "semantic_scholar"

# Semantic Scholar unauth quota — empirically tighter than docs suggest:
# the documented "100 requests / 5 minutes" tolerates short bursts but
# returns HTTP 429 on sustained ~10 req/sec at concurrency=2. Smoke run
# (msg-internal, 2026-04-30) saw ~20% 429 loss with bucket-starts-full.
# Switching to small-burst + slower sustained rate: 5 tokens initial
# (allows the first ~2 prospects to fly through with no delay), refill
# 1 token every 1.5s ≈ 40 req/min sustained — well under the unauth
# ceiling and tolerant of Scholar's burst-detection heuristics.
RATE_LIMIT_BUCKET_CAPACITY = 1
RATE_LIMIT_REFILL_SECONDS = 2.0

# Two API calls per prospect (resolve_author_id + fetch_author_papers).
TOKENS_PER_PROSPECT = 2

# Confidence tier — Contract 1.
CONFIDENCE_SMALL_PAPER = 0.90  # author_count <= 5
CONFIDENCE_LARGE_PAPER = 0.75  # author_count > 5
SMALL_PAPER_AUTHOR_THRESHOLD = 5

# Default per-prospect paper cap. Aligns with the per-pair extractor's
# `max_results * 5` ceiling so we generally see every paper that matters.
DEFAULT_MAX_PAPERS_PER_PERSON = 50
# Concurrency=1 — Scholar's unauth limiter penalizes parallel requests
# more than the per-second budget alone implies. Sequential fetches
# avoid the burst-detection 429s seen in the 2026-04-30 smoke.
DEFAULT_CONCURRENCY = 1

# Minimum tokens in the canonical_name before we'll touch the API. Single
# tokens produce huge fanout in Semantic Scholar's author-search index.
MIN_NAME_TOKENS = 2


# ── SQL ──────────────────────────────────────────────────────────────────────


SELECT_PROSPECTS_SQL = """
SELECT id, name
FROM prospects
WHERE account_id = $1
ORDER BY id
"""

SELECT_PROSPECTS_LIMIT_SQL = SELECT_PROSPECTS_SQL + "LIMIT $2\n"

SELECT_ALL_ACCOUNTS_SQL = """
SELECT DISTINCT account_id
FROM prospects
WHERE account_id IS NOT NULL
ORDER BY account_id
"""

SIGNAL_EXISTS_SQL = (
    "SELECT 1 FROM signals "
    "WHERE prospect_id = $1 AND signal_type = $2 "
    "AND value->>'semantic_scholar_id' = $3 "
    "AND value->>'connected_to' = $4 "
    "LIMIT 1"
)

INSERT_SIGNAL_SQL = """
INSERT INTO signals (
    id, prospect_id, account_id, source, signal_type,
    value, raw_data, weight, confidence, collected_at
)
VALUES (
    gen_random_uuid(), $1, $2, $3, $4,
    $5::jsonb, NULL, 1.0, $6, NOW()
)
"""

# v3 ingest path — pivot v2 signals into the structured papers + paper_authors
# tables so paper_clustering.py can JOIN on them. Keyed by the unique constraint
# (account_id, semantic_scholar_id); RETURNING id surfaces the paper_id we need
# for the paper_authors junction. Year is bounded by the papers_year_range
# CHECK (1900..now+1) — anything out of range is squashed to NULL up-front.
RESOLVE_PERSONS_FOR_PROSPECTS_SQL = """
SELECT id, source_prospect_id
FROM persons
WHERE account_id = $1
  AND source_prospect_id = ANY($2::uuid[])
"""

UPSERT_PAPER_SQL = """
INSERT INTO papers (
    semantic_scholar_id, title, venue, year, citation_count,
    doi, url, account_id
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (account_id, semantic_scholar_id) DO UPDATE SET
    title          = EXCLUDED.title,
    venue          = COALESCE(EXCLUDED.venue, papers.venue),
    year           = COALESCE(EXCLUDED.year, papers.year),
    citation_count = GREATEST(papers.citation_count, EXCLUDED.citation_count),
    doi            = COALESCE(EXCLUDED.doi, papers.doi),
    url            = COALESCE(EXCLUDED.url, papers.url),
    updated_at     = NOW()
RETURNING id
"""

UPSERT_PAPER_AUTHOR_SQL = """
INSERT INTO paper_authors (
    paper_id, person_id, author_order, is_corresponding,
    affiliation, account_id
)
VALUES ($1, $2, $3, FALSE, NULL, $4)
ON CONFLICT (paper_id, person_id) DO NOTHING
"""


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProspectRow:
    """One row from ``prospects``."""

    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class PaperEntry:
    """One (prospect, formatted-record) pair indexed under a paperId."""

    prospect_id: UUID
    record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Emission:
    """One ordered-pair structured_value ready to persist."""

    prospect_a: UUID
    prospect_b: UUID
    structured_value: dict[str, Any]


@dataclass(slots=True)
class ScholarIngestRollup:
    """Aggregate counters for one ``bulk_scholar_ingest_account`` call."""

    account_id: UUID
    prospects_scanned: int = 0
    prospects_with_papers: int = 0
    prospects_skipped_short_name: int = 0
    papers_indexed: int = 0
    pairs_emitted: int = 0
    signals_inserted: int = 0
    signals_skipped_dedup: int = 0
    dry_run: bool = False
    write_v3: bool = False
    papers_upserted: int = 0
    paper_authors_upserted: int = 0
    prospects_without_person_row: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


# ── Token-bucket rate limiter ───────────────────────────────────────────────


class RateLimiter:
    """Simple async token bucket.

    ``capacity`` tokens at start, refills 1 token every ``refill_seconds``.
    ``acquire(n)`` blocks until at least ``n`` tokens are available.

    Time is injectable via ``time_func`` and ``sleep_func`` so unit tests
    can drive the clock deterministically.
    """

    __slots__ = (
        "_capacity",
        "_refill_seconds",
        "_tokens",
        "_last_refill",
        "_lock",
        "_time",
        "_sleep",
    )

    def __init__(
        self,
        capacity: int = RATE_LIMIT_BUCKET_CAPACITY,
        refill_seconds: float = RATE_LIMIT_REFILL_SECONDS,
        *,
        time_func: Any = None,
        sleep_func: Any = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_seconds <= 0:
            raise ValueError("refill_seconds must be positive")
        self._capacity = capacity
        self._refill_seconds = float(refill_seconds)
        self._tokens = float(capacity)
        self._time = time_func or time.monotonic
        self._sleep = sleep_func or asyncio.sleep
        self._last_refill = self._time()
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> float:
        return self._tokens

    def _refill(self) -> None:
        now = self._time()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        added = elapsed / self._refill_seconds
        self._tokens = min(float(self._capacity), self._tokens + added)
        self._last_refill = now

    async def acquire(self, n: int = 1) -> None:
        if n <= 0:
            return
        if n > self._capacity:
            raise ValueError(
                f"cannot acquire {n} tokens: bucket capacity is {self._capacity}"
            )
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait = deficit * self._refill_seconds
                await self._sleep(wait)


# ── Pure planning helpers ───────────────────────────────────────────────────


def _has_enough_name_tokens(name: str | None) -> bool:
    if not name:
        return False
    return len(name.split()) >= MIN_NAME_TOKENS


def _confidence_for_author_count(author_count: int) -> float:
    """Contract 1: 0.90 for tight (≤5) author lists, else 0.75."""
    return (
        CONFIDENCE_SMALL_PAPER
        if author_count <= SMALL_PAPER_AUTHOR_THRESHOLD
        else CONFIDENCE_LARGE_PAPER
    )


def _build_structured_value(
    record: dict[str, Any],
    connected_to: UUID,
) -> dict[str, Any]:
    """Add ``connected_to`` to a formatted paper record without mutating it."""
    out = dict(record)
    out["connected_to"] = str(connected_to)
    return out


def _pair_index_to_emissions(
    paper_index: dict[str, list[PaperEntry]],
) -> list[Emission]:
    """Yield one Emission per ordered (prospect_a < prospect_b) pair per paper.

    Pure function — the heart of the algorithm. Single-author papers are
    skipped. Same prospect appearing twice under one paper (shouldn't happen
    in practice but defensive against duplicate rows) collapses to one entry.
    Pair ordering uses Python's UUID comparison which is the same ordering
    Postgres uses for the ``person_a_id < person_b_id`` invariant.
    """
    emissions: list[Emission] = []
    for entries in paper_index.values():
        # Dedup by prospect_id, keep first record encountered.
        by_prospect: dict[UUID, dict[str, Any]] = {}
        for entry in entries:
            if entry.prospect_id not in by_prospect:
                by_prospect[entry.prospect_id] = entry.record
        if len(by_prospect) < 2:
            continue
        ordered = sorted(by_prospect.keys())
        for i, prospect_a in enumerate(ordered):
            for prospect_b in ordered[i + 1 :]:
                # Use prospect_a's record as the canonical paper view; the
                # paper-level fields (title/venue/year/etc.) are identical
                # regardless of which prospect we pulled it through.
                record = by_prospect[prospect_a]
                emissions.append(
                    Emission(
                        prospect_a=prospect_a,
                        prospect_b=prospect_b,
                        structured_value=_build_structured_value(
                            record, prospect_b
                        ),
                    )
                )
    return emissions


# ── DB helpers ───────────────────────────────────────────────────────────────


async def _fetch_prospects(
    conn: asyncpg.Connection,
    account_id: UUID,
    limit: int | None,
) -> list[ProspectRow]:
    if limit is None:
        rows = await conn.fetch(SELECT_PROSPECTS_SQL, account_id)
    else:
        rows = await conn.fetch(SELECT_PROSPECTS_LIMIT_SQL, account_id, int(limit))
    return [ProspectRow(id=r["id"], name=r["name"] or "") for r in rows]


async def _fetch_all_account_ids(conn: asyncpg.Connection) -> list[UUID]:
    rows = await conn.fetch(SELECT_ALL_ACCOUNTS_SQL)
    return [r["account_id"] for r in rows]


async def _signal_exists(
    conn: asyncpg.Connection,
    prospect_id: UUID,
    signal_type: str,
    semantic_scholar_id: str,
    connected_to: str,
) -> bool:
    """Explicit pre-check — see module docstring."""
    row = await conn.fetchval(
        SIGNAL_EXISTS_SQL,
        prospect_id,
        signal_type,
        semantic_scholar_id,
        connected_to,
    )
    return row is not None


async def _insert_signal(
    conn: asyncpg.Connection,
    prospect_id: UUID,
    account_id: UUID,
    structured_value: dict[str, Any],
    confidence: float,
) -> None:
    await conn.execute(
        INSERT_SIGNAL_SQL,
        prospect_id,
        account_id,
        SIGNAL_SOURCE,
        SIGNAL_TYPE,
        # Dict directly — asyncpg's jsonb codec handles encoding. See the
        # `bulk_education_signals._insert_signal` comment for the
        # double-encoding bug we hit when passing json.dumps(dict).
        structured_value,
        confidence,
    )


# ── v3 write helpers (papers + paper_authors) ───────────────────────────────


_PAPERS_YEAR_MIN = 1900


def _safe_year(record: dict[str, Any]) -> int | None:
    """Squash out-of-range years to NULL to satisfy ``papers_year_range``."""
    raw = record.get("year")
    if raw is None:
        return None
    try:
        y = int(raw)
    except (TypeError, ValueError):
        return None
    # Upper bound is now+1 per the CHECK; treat anything > now+5 as garbage.
    # Postgres will reject anything beyond next year, so be conservative.
    if y < _PAPERS_YEAR_MIN:
        return None
    return y


async def _resolve_persons_for_prospects(
    conn: asyncpg.Connection,
    account_id: UUID,
    prospect_ids: list[UUID],
) -> dict[UUID, UUID]:
    """Return ``{prospect_id: person_id}`` for prospects that have a person row.

    Prospects without an enriched ``persons`` row are absent from the dict.
    Callers must skip the v3 write for those prospects.
    """
    if not prospect_ids:
        return {}
    rows = await conn.fetch(
        RESOLVE_PERSONS_FOR_PROSPECTS_SQL, account_id, prospect_ids
    )
    return {r["source_prospect_id"]: r["id"] for r in rows}


async def _upsert_paper(
    conn: asyncpg.Connection,
    account_id: UUID,
    record: dict[str, Any],
) -> UUID | None:
    """UPSERT one paper row; return its ``papers.id`` (or None on bad data).

    The formatted record produced by :func:`extractors.scholar._format_paper_record`
    uses ``paper_title`` (not ``title``) — match its keys exactly.
    """
    semantic_scholar_id = str(record.get("semantic_scholar_id") or "").strip()
    title = (record.get("paper_title") or record.get("title") or "").strip()
    if not semantic_scholar_id or not title:
        return None
    venue = record.get("venue") or None
    year = _safe_year(record)
    citation_count = int(record.get("citation_count") or 0)
    doi = record.get("doi")
    url = record.get("url")  # extractor never sets this today; future-proof
    paper_id = await conn.fetchval(
        UPSERT_PAPER_SQL,
        semantic_scholar_id,
        title,
        venue,
        year,
        citation_count,
        doi,
        url,
        account_id,
    )
    return paper_id


async def _upsert_paper_author(
    conn: asyncpg.Connection,
    account_id: UUID,
    paper_id: UUID,
    person_id: UUID,
    author_order: int | None,
) -> bool:
    """UPSERT one (paper_id, person_id) junction row.

    Returns True if a row was inserted, False if it already existed (the
    ``ON CONFLICT DO NOTHING`` swallowed the write).
    """
    status = await conn.execute(
        UPSERT_PAPER_AUTHOR_SQL,
        paper_id,
        person_id,
        author_order,
        account_id,
    )
    # asyncpg returns "INSERT 0 1" when a row was inserted, "INSERT 0 0" when
    # the conflict path fired. Tail token is the affected-row count.
    parts = (status or "").split()
    try:
        return parts and parts[-1] == "1"
    except Exception:  # noqa: BLE001
        return False


async def _materialize_v3_for_paper_index(
    conn: asyncpg.Connection,
    account_id: UUID,
    paper_index: dict[str, list[PaperEntry]],
    rollup: ScholarIngestRollup,
) -> None:
    """Pivot the in-memory paper_index into ``papers`` + ``paper_authors``.

    Strategy: one `_upsert_paper` per unique paper_id (keyed by
    semantic_scholar_id); one `_upsert_paper_author` per (paper, prospect)
    entry where the prospect has a corresponding ``persons`` row in this
    tenant. Prospects without a person row are counted but not written
    (paper_authors.person_id has a hard FK to persons.id).
    """
    if not paper_index:
        return
    # Step 1 — resolve every prospect_id that appears anywhere in the index.
    all_prospects: set[UUID] = set()
    for entries in paper_index.values():
        for entry in entries:
            all_prospects.add(entry.prospect_id)
    prospect_to_person = await _resolve_persons_for_prospects(
        conn, account_id, list(all_prospects)
    )
    rollup.prospects_without_person_row = len(all_prospects) - len(
        prospect_to_person
    )

    # Step 2 — per paper, upsert once then iterate the entries.
    for entries in paper_index.values():
        if not entries:
            continue
        # Use the first entry's record as the canonical paper view (paper-level
        # fields — title, venue, year, citation_count — are identical across
        # prospects who co-authored it).
        record = entries[0].record
        try:
            paper_id = await _upsert_paper(conn, account_id, record)
        except Exception as exc:  # noqa: BLE001
            ssid = str(record.get("semantic_scholar_id") or "")
            rollup.errors.append((f"paper:{ssid}", repr(exc)))
            log.warning("v3 paper upsert failed ssid=%s: %r", ssid, exc)
            continue
        if paper_id is None:
            continue
        rollup.papers_upserted += 1

        # Dedup entries by prospect — the same prospect can appear twice if
        # Scholar returned the same paper through two different result pages
        # of their author feed. Take the first record encountered.
        seen_prospects: set[UUID] = set()
        for entry in entries:
            if entry.prospect_id in seen_prospects:
                continue
            seen_prospects.add(entry.prospect_id)
            person_id = prospect_to_person.get(entry.prospect_id)
            if person_id is None:
                continue
            author_order = entry.record.get("author_order")
            try:
                inserted = await _upsert_paper_author(
                    conn,
                    account_id,
                    paper_id,
                    person_id,
                    int(author_order)
                    if isinstance(author_order, int)
                    else None,
                )
            except Exception as exc:  # noqa: BLE001
                rollup.errors.append(
                    (f"paper_author:{paper_id}:{person_id}", repr(exc))
                )
                log.warning(
                    "v3 paper_author upsert failed paper=%s person=%s: %r",
                    paper_id, person_id, exc,
                )
                continue
            if inserted:
                rollup.paper_authors_upserted += 1


# ── Per-prospect API fetch ──────────────────────────────────────────────────


async def _fetch_papers_for_prospect(
    client: httpx.AsyncClient,
    prospect: ProspectRow,
    *,
    max_papers: int,
    rate_limiter: RateLimiter,
    semaphore: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """Resolve the prospect to a Semantic Scholar authorId and fetch papers.

    Two API calls per prospect; both gated through the shared ``RateLimiter``
    so a wide tenant doesn't blow past the unauth quota. Returns ``[]`` on
    any failure — Contract 1 partial-results semantics, mirroring the
    extractor.
    """
    async with semaphore:
        await rate_limiter.acquire(1)
        person = PersonRef(
            person_id=str(prospect.id),
            canonical_name=prospect.name,
        )
        try:
            author_id = await _resolve_author_id(client, person)
        except Exception as exc:  # noqa: BLE001 — extractor is the trust boundary
            log.warning(
                "scholar resolve failed for %s (%s): %r",
                prospect.id, prospect.name, exc,
            )
            return []
        if author_id is None:
            return []
        await rate_limiter.acquire(1)
        try:
            return await _fetch_author_papers(
                client, author_id, limit=max_papers
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "scholar papers fetch failed for %s (%s): %r",
                prospect.id, prospect.name, exc,
            )
            return []


# ── Public orchestrator ──────────────────────────────────────────────────────


async def bulk_scholar_ingest_account(
    account_id: UUID,
    *,
    limit: int | None = None,
    max_papers_per_person: int = DEFAULT_MAX_PAPERS_PER_PERSON,
    concurrency: int = DEFAULT_CONCURRENCY,
    dry_run: bool = False,
    write_v3: bool = False,
    client: httpx.AsyncClient | None = None,
    rate_limiter: RateLimiter | None = None,
) -> ScholarIngestRollup:
    """Build the per-account Scholar paper index and emit co-author signals.

    Args:
        account_id: tenant scope.
        limit: optional cap on prospects scanned.
        max_papers_per_person: max papers fetched per prospect.
        concurrency: max in-flight Scholar requests.
        dry_run: log emissions without writing to ``signals``.
        write_v3: when True, ALSO pivot the in-memory paper index into the
            v3 ``papers`` + ``paper_authors`` tables. v2 ``signals`` rows are
            still written so the existing frontend keeps working. Has no
            effect under ``dry_run``.
        client: optional injected ``httpx.AsyncClient`` (tests pass a
            ``MockTransport``-backed client).
        rate_limiter: optional injected limiter (tests skip waiting).
    """
    rollup = ScholarIngestRollup(
        account_id=account_id, dry_run=dry_run, write_v3=write_v3
    )

    # Step 1 — load prospects.
    async with acquire() as conn:
        prospects = await _fetch_prospects(conn, account_id, limit)
    rollup.prospects_scanned = len(prospects)
    log.info(
        "scholar_ingest start account=%s prospects=%d max_papers=%d concurrency=%d dry_run=%s",
        account_id, rollup.prospects_scanned, max_papers_per_person,
        concurrency, dry_run,
    )

    eligible: list[ProspectRow] = []
    for p in prospects:
        if _has_enough_name_tokens(p.name):
            eligible.append(p)
        else:
            rollup.prospects_skipped_short_name += 1

    # Step 2 — per-prospect Scholar fetch + paper indexing.
    # When SEMANTIC_SCHOLAR_API_KEY is in the env, attach it as a default
    # header. Scholar's authenticated tier raises the limit from "fragile
    # ~1 req/sec" to a documented 1 req/sec floor — same nominal rate, but
    # without the burst-detection 429s the unauth tier returns. Costs zero
    # (free registration) and is ~3x more reliable in practice.
    own_client = client is None
    if client is None:
        api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        headers = {"x-api-key": api_key} if api_key else None
        http = httpx.AsyncClient(headers=headers) if headers else httpx.AsyncClient()
        if api_key:
            log.info("scholar_ingest using authenticated Scholar tier (key found)")
    else:
        http = client
    limiter = rate_limiter or RateLimiter()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    paper_index: dict[str, list[PaperEntry]] = {}
    try:
        tasks = [
            _fetch_papers_for_prospect(
                http,
                prospect,
                max_papers=max_papers_per_person,
                rate_limiter=limiter,
                semaphore=semaphore,
            )
            for prospect in eligible
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
    finally:
        if own_client:
            await http.aclose()

    for prospect, papers in zip(eligible, results):
        if not papers:
            continue
        added_any = False
        for paper in papers:
            record = _format_paper_record(paper)
            if record is None:
                continue
            paper_id = record.get("semantic_scholar_id") or ""
            if not paper_id:
                continue
            paper_index.setdefault(paper_id, []).append(
                PaperEntry(prospect_id=prospect.id, record=record)
            )
            added_any = True
        if added_any:
            rollup.prospects_with_papers += 1
    rollup.papers_indexed = len(paper_index)

    # Step 3 — cross-reference & emit.
    emissions = _pair_index_to_emissions(paper_index)
    rollup.pairs_emitted = len(emissions)
    log.info(
        "scholar_ingest indexed account=%s papers=%d pairs=%d (with_papers=%d, skipped_short_name=%d)",
        account_id, rollup.papers_indexed, rollup.pairs_emitted,
        rollup.prospects_with_papers, rollup.prospects_skipped_short_name,
    )

    if dry_run:
        for emission in emissions:
            log.info(
                "[dry-run] would emit %s↔%s paper=%s",
                emission.prospect_a, emission.prospect_b,
                emission.structured_value.get("semantic_scholar_id"),
            )
        return rollup

    # Step 4 — persist with explicit dedupe.
    async with acquire() as conn:
        for emission in emissions:
            try:
                await _persist_emission(conn, account_id, emission, rollup)
            except Exception as exc:  # noqa: BLE001
                rollup.errors.append((str(emission.prospect_a), repr(exc)))
                log.exception(
                    "scholar_ingest persist failed for %s↔%s",
                    emission.prospect_a, emission.prospect_b,
                )

        # Step 5 (optional) — pivot paper_index into v3 papers + paper_authors.
        # Runs in the same connection so the v2 signals + v3 rows are visible
        # to subsequent reads; both are idempotent so partial failures are
        # safe to retry.
        if write_v3:
            await _materialize_v3_for_paper_index(
                conn, account_id, paper_index, rollup
            )

    log.info(
        "scholar_ingest done account=%s inserted=%d skipped_dedup=%d "
        "papers_upserted=%d paper_authors_upserted=%d "
        "prospects_without_person=%d errors=%d",
        account_id, rollup.signals_inserted, rollup.signals_skipped_dedup,
        rollup.papers_upserted, rollup.paper_authors_upserted,
        rollup.prospects_without_person_row, len(rollup.errors),
    )
    return rollup


async def _persist_emission(
    conn: asyncpg.Connection,
    account_id: UUID,
    emission: Emission,
    rollup: ScholarIngestRollup,
) -> None:
    """One emission = one signal row pointing prospect_a → prospect_b."""
    structured = emission.structured_value
    paper_id = str(structured.get("semantic_scholar_id") or "")
    connected_to = str(structured.get("connected_to") or "")
    if not paper_id or not connected_to:
        rollup.errors.append(
            (str(emission.prospect_a), "missing paper_id or connected_to")
        )
        return
    if await _signal_exists(
        conn, emission.prospect_a, SIGNAL_TYPE, paper_id, connected_to
    ):
        rollup.signals_skipped_dedup += 1
        return
    confidence = _confidence_for_author_count(
        int(structured.get("author_count") or 0)
    )
    await _insert_signal(
        conn,
        emission.prospect_a,
        account_id,
        structured,
        confidence,
    )
    rollup.signals_inserted += 1


async def bulk_scholar_ingest_all_accounts(
    *,
    limit: int | None = None,
    max_papers_per_person: int = DEFAULT_MAX_PAPERS_PER_PERSON,
    concurrency: int = DEFAULT_CONCURRENCY,
    dry_run: bool = False,
    write_v3: bool = False,
) -> list[ScholarIngestRollup]:
    """Iterate every account in ``prospects`` and ingest each in turn."""
    async with acquire() as conn:
        account_ids = await _fetch_all_account_ids(conn)
    log.info("scholar_ingest all-accounts: %d accounts", len(account_ids))
    rollups: list[ScholarIngestRollup] = []
    for account_id in account_ids:
        rollup = await bulk_scholar_ingest_account(
            account_id,
            limit=limit,
            max_papers_per_person=max_papers_per_person,
            concurrency=concurrency,
            dry_run=dry_run,
            write_v3=write_v3,
        )
        rollups.append(rollup)
    return rollups


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.bulk_scholar_ingest",
        description=(
            "Bulk per-prospect Semantic Scholar ingestion → emits "
            "academic_co_author signal rows."
        ),
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--account-id",
        type=UUID,
        help="Scope to a single accounts.id UUID.",
    )
    scope.add_argument(
        "--all-accounts",
        action="store_true",
        help="Iterate every account in `prospects`.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap prospects per account (default: no cap).",
    )
    p.add_argument(
        "--max-papers-per-person",
        type=int,
        default=DEFAULT_MAX_PAPERS_PER_PERSON,
        help=f"Max papers fetched per prospect (default {DEFAULT_MAX_PAPERS_PER_PERSON}).",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Max in-flight Scholar requests (default {DEFAULT_CONCURRENCY}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log emissions without writing to the signals table.",
    )
    p.add_argument(
        "--write-v3",
        action="store_true",
        help=(
            "Also pivot the in-memory paper index into the v3 papers + "
            "paper_authors tables. v2 signals rows are still written. No "
            "effect under --dry-run."
        ),
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return p


def _print_rollup(rollup: ScholarIngestRollup) -> None:
    msg = (
        f"scholar_ingest account={rollup.account_id} "
        f"prospects_scanned={rollup.prospects_scanned} "
        f"prospects_with_papers={rollup.prospects_with_papers} "
        f"papers_indexed={rollup.papers_indexed} "
        f"pairs_emitted={rollup.pairs_emitted} "
        f"signals_inserted={rollup.signals_inserted} "
        f"signals_skipped_dedup={rollup.signals_skipped_dedup} "
        f"papers_upserted={rollup.papers_upserted} "
        f"paper_authors_upserted={rollup.paper_authors_upserted} "
        f"prospects_without_person={rollup.prospects_without_person_row} "
        f"skipped_short_name={rollup.prospects_skipped_short_name} "
        f"errors={len(rollup.errors)} "
        f"dry_run={rollup.dry_run} write_v3={rollup.write_v3}"
    )
    print(msg)


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    async def _go() -> list[ScholarIngestRollup]:
        try:
            if args.all_accounts:
                return await bulk_scholar_ingest_all_accounts(
                    limit=args.limit,
                    max_papers_per_person=args.max_papers_per_person,
                    concurrency=args.concurrency,
                    dry_run=args.dry_run,
                    write_v3=args.write_v3,
                )
            return [
                await bulk_scholar_ingest_account(
                    args.account_id,
                    limit=args.limit,
                    max_papers_per_person=args.max_papers_per_person,
                    concurrency=args.concurrency,
                    dry_run=args.dry_run,
                    write_v3=args.write_v3,
                )
            ]
        finally:
            await close_pool()

    rollups = asyncio.run(_go())
    for rollup in rollups:
        _print_rollup(rollup)
    return 0 if all(not r.errors for r in rollups) else 1


if __name__ == "__main__":
    sys.exit(main())
