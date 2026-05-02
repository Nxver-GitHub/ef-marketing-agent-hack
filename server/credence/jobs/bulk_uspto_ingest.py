"""Bulk per-prospect USPTO patent ingestion runner — Wave 7.

For every prospect in a tenant (``account_id``), call USPTO Open Data Portal
to fetch their authored patents, build an in-memory ``patent_id → list[(prospect_id,
formatted_record)]`` index, then for every patent listing ≥2 prospects in the
set emit ``signal_type='patent_co_inventor'`` rows into the v2 ``signals`` table
— one row per ordered pair (``person_a_id < person_b_id`` lexically), Contract 1
``structured_value`` shape — pointing each prospect at the other via ``connected_to``.

Mirrors ``bulk_scholar_ingest.py`` exactly. The data source is USPTO ODP at
``api.uspto.gov`` (free, but requires registration via ``data.uspto.gov`` for an
API key). The per-pair extractor (``extractors/patents.py``) has the parsing
primitives — we reuse ``_build_query_for_person``, ``_fetch_patents``, and
``_format_patent_record`` so name parsing + JSON shape stay in lock-step.

This runner produces the highest-strength edges in STRENGTH_TABLE
(``patent_co_inventor`` base 0.95 — top of the chart per CLAUDE.md).

## Auth requirement

The USPTO ODP endpoint requires both ``USPTO_USE_ODP=1`` and
``USPTO_ODP_API_KEY=<key>`` in the environment. If either is missing the
extractor's ``_resolve_endpoint_config()`` raises ``RuntimeError`` (we catch
it once at orchestrator entry and abort with an informative message rather
than silently producing zero rows).

## Idempotency

Re-runs do NOT pile up duplicates. Before INSERTing a signal we run an
explicit ``SELECT 1 ... LIMIT 1`` keyed on ``(prospect_id, signal_type,
value->>'patent_number', value->>'connected_to')``. Same shape as
``bulk_scholar_ingest`` keyed on ``semantic_scholar_id`` instead of
``patent_number``.

## Rate limit

USPTO ODP free tier is documented at "45 requests/minute" (CLAUDE.md L344).
Token-bucket: 10-token initial burst, refill 1 token / 1.5s ≈ 40 req/min
sustained — well under the cap and tolerant of momentary bursts during the
prospect scan.

## CLI

::

    cd server && uv run python -m credence.jobs.bulk_uspto_ingest \\
        --account-id <uuid> --limit 100 --dry-run

    cd server && USPTO_USE_ODP=1 USPTO_ODP_API_KEY=<key> uv run python \\
        -m credence.jobs.bulk_uspto_ingest --account-id <uuid>
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from ..db import acquire, close_pool
from ..extractors.patents import (
    PersonRef,
    _build_query_for_person,
    _fetch_patents,
    _format_patent_record,
    _resolve_endpoint_config,
)

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


SIGNAL_TYPE = "patent_co_inventor"
SIGNAL_SOURCE = "uspto_odp"

# USPTO ODP free-tier docs say "45 requests/minute". Burst of 10 with refill
# every 1.5s gives ~40 sustained req/min, leaves headroom for the
# burst-detection USPTO documents on its free tier.
RATE_LIMIT_BUCKET_CAPACITY = 10
RATE_LIMIT_REFILL_SECONDS = 1.5

# One API call per prospect — patents extractor calls _fetch_patents once
# for the inventor query (response includes the inventors[] array we need
# for cross-referencing).
TOKENS_PER_PROSPECT = 1

# Confidence — STRENGTH_TABLE.patent_co_inventor base. The signal layer
# applies its own decay/corroboration math when cross-referencing into
# person_connections, but the per-row signal carries the headline base.
CONFIDENCE_PATENT_CO_INVENTOR = 0.95

# How many candidate patents to pull per prospect. The extractor uses
# max_results * 5 internally; for bulk we pre-set a reasonable cap so a
# patent-prolific person doesn't blow out the index.
DEFAULT_MAX_PATENTS_PER_PERSON = 50
# USPTO ODP tolerates parallel requests less than Scholar — keep
# concurrency=1 by default. Smoke runs can bump to 2 with the ratelimiter
# still bounding total req/sec.
DEFAULT_CONCURRENCY = 1

# Same name-token gate as Scholar — single-token names produce huge
# fanout in the USPTO inventor index.
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
    "AND value->>'patent_number' = $3 "
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


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProspectRow:
    """One row from ``prospects`` — same shape as the Scholar ingest."""

    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class PatentEntry:
    """One (prospect, formatted-record) pair indexed under a patent_number."""

    prospect_id: UUID
    record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Emission:
    """One ordered-pair structured_value ready to persist."""

    prospect_a: UUID
    prospect_b: UUID
    structured_value: dict[str, Any]


@dataclass(slots=True)
class UsptoIngestRollup:
    """Aggregate counters for one ``bulk_uspto_ingest_account`` call."""

    account_id: UUID
    prospects_scanned: int = 0
    prospects_with_patents: int = 0
    prospects_skipped_short_name: int = 0
    patents_indexed: int = 0
    pairs_emitted: int = 0
    signals_inserted: int = 0
    signals_skipped_dedup: int = 0
    dry_run: bool = False
    aborted_no_api_key: bool = False
    errors: list[tuple[str, str]] = field(default_factory=list)


# ── Token-bucket rate limiter ───────────────────────────────────────────────


class RateLimiter:
    """Simple async token bucket — copy of bulk_scholar_ingest.RateLimiter.

    Lifted verbatim so this module is self-contained; future shared-utility
    extraction can DRY both. Time is injectable for deterministic tests.
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


def _build_structured_value(
    record: dict[str, Any],
    connected_to: UUID,
) -> dict[str, Any]:
    """Add ``connected_to`` to a formatted patent record without mutating it."""
    out = dict(record)
    out["connected_to"] = str(connected_to)
    return out


def _pair_index_to_emissions(
    patent_index: dict[str, list[PatentEntry]],
) -> list[Emission]:
    """Yield one Emission per ordered (prospect_a < prospect_b) pair per patent.

    Pure function — same algorithm as ``bulk_scholar_ingest._pair_index_to_emissions``.
    Single-inventor patents are skipped. Same prospect appearing twice under
    one patent (defensive) collapses to one entry. Pair ordering uses
    Python's UUID comparison which matches Postgres's ``person_a_id <
    person_b_id`` invariant.
    """
    emissions: list[Emission] = []
    for entries in patent_index.values():
        by_prospect: dict[UUID, dict[str, Any]] = {}
        for entry in entries:
            if entry.prospect_id not in by_prospect:
                by_prospect[entry.prospect_id] = entry.record
        if len(by_prospect) < 2:
            continue
        ordered = sorted(by_prospect.keys())
        for i, prospect_a in enumerate(ordered):
            for prospect_b in ordered[i + 1:]:
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
    patent_number: str,
    connected_to: str,
) -> bool:
    """Explicit pre-check — see module docstring."""
    row = await conn.fetchval(
        SIGNAL_EXISTS_SQL,
        prospect_id,
        signal_type,
        patent_number,
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
    # Pass dict directly — asyncpg's JSONB codec handles encoding. Passing
    # ``json.dumps(dict)`` here would double-encode (we hit this in
    # bulk_bio_extraction; comments in that module document the gotcha).
    await conn.execute(
        INSERT_SIGNAL_SQL,
        prospect_id,
        account_id,
        SIGNAL_SOURCE,
        SIGNAL_TYPE,
        structured_value,
        confidence,
    )


# ── Per-prospect API fetch ──────────────────────────────────────────────────


async def _fetch_patents_for_prospect(
    client: httpx.AsyncClient,
    prospect: ProspectRow,
    *,
    max_patents: int,
    rate_limiter: RateLimiter,
    semaphore: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """Build a USPTO query for the prospect and fetch their patents.

    Single API call per prospect; gated through the shared ``RateLimiter``
    so a wide tenant doesn't blow past the ODP free-tier cap. Returns ``[]``
    on any failure — Contract 1 partial-results semantics, mirroring the
    extractor's own swallow-on-error contract.
    """
    async with semaphore:
        await rate_limiter.acquire(TOKENS_PER_PROSPECT)
        person = PersonRef(
            person_id=str(prospect.id),
            canonical_name=prospect.name,
        )
        query = _build_query_for_person(person)
        if query is None:
            return []
        try:
            return await _fetch_patents(
                client, query, page_size=max(max_patents, 25),
            )
        except Exception as exc:  # noqa: BLE001 — extractor is the trust boundary
            log.warning(
                "uspto fetch failed for %s (%s): %r",
                prospect.id, prospect.name, exc,
            )
            return []


# ── Public orchestrator ──────────────────────────────────────────────────────


async def bulk_uspto_ingest_account(
    account_id: UUID,
    *,
    limit: int | None = None,
    max_patents_per_person: int = DEFAULT_MAX_PATENTS_PER_PERSON,
    concurrency: int = DEFAULT_CONCURRENCY,
    dry_run: bool = False,
    client: httpx.AsyncClient | None = None,
    rate_limiter: RateLimiter | None = None,
) -> UsptoIngestRollup:
    """Build the per-account USPTO patent index and emit co-inventor signals.

    Args:
        account_id: tenant scope.
        limit: optional cap on prospects scanned.
        max_patents_per_person: max patents fetched per prospect.
        concurrency: max in-flight USPTO requests.
        dry_run: log emissions without writing to ``signals``.
        client: optional injected ``httpx.AsyncClient`` (tests pass a
            ``MockTransport``-backed client — same shape as Scholar).
        rate_limiter: optional injected limiter (tests skip waiting).
    """
    rollup = UsptoIngestRollup(account_id=account_id, dry_run=dry_run)

    # Step 0 — verify the ODP endpoint is configured. The extractor raises
    # if USPTO_USE_ODP / USPTO_ODP_API_KEY are unset; we catch once and
    # abort with an informative rollup so the caller sees the gap.
    try:
        _resolve_endpoint_config()
    except RuntimeError as exc:
        log.error("uspto_ingest aborted: %s", exc)
        rollup.aborted_no_api_key = True
        rollup.errors.append((str(account_id), repr(exc)))
        return rollup

    # Step 1 — load prospects.
    async with acquire() as conn:
        prospects = await _fetch_prospects(conn, account_id, limit)
    rollup.prospects_scanned = len(prospects)
    log.info(
        "uspto_ingest start account=%s prospects=%d max_patents=%d concurrency=%d dry_run=%s",
        account_id, rollup.prospects_scanned, max_patents_per_person,
        concurrency, dry_run,
    )

    eligible: list[ProspectRow] = []
    for p in prospects:
        if _has_enough_name_tokens(p.name):
            eligible.append(p)
        else:
            rollup.prospects_skipped_short_name += 1

    own_client = client is None
    http = client if client is not None else httpx.AsyncClient()
    limiter = rate_limiter or RateLimiter()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    # Step 2 — per-prospect USPTO fetch + patent indexing.
    patent_index: dict[str, list[PatentEntry]] = {}
    try:
        tasks = [
            _fetch_patents_for_prospect(
                http,
                prospect,
                max_patents=max_patents_per_person,
                rate_limiter=limiter,
                semaphore=semaphore,
            )
            for prospect in eligible
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
    finally:
        if own_client:
            await http.aclose()

    for prospect, patents in zip(eligible, results):
        if not patents:
            continue
        added_any = False
        for patent in patents:
            record = _format_patent_record(patent)
            if record is None:
                continue
            patent_id = record.get("patent_number") or ""
            if not patent_id:
                continue
            patent_index.setdefault(patent_id, []).append(
                PatentEntry(prospect_id=prospect.id, record=record)
            )
            added_any = True
        if added_any:
            rollup.prospects_with_patents += 1
    rollup.patents_indexed = len(patent_index)

    # Step 3 — cross-reference & emit.
    emissions = _pair_index_to_emissions(patent_index)
    rollup.pairs_emitted = len(emissions)
    log.info(
        "uspto_ingest indexed account=%s patents=%d pairs=%d (with_patents=%d, skipped_short_name=%d)",
        account_id, rollup.patents_indexed, rollup.pairs_emitted,
        rollup.prospects_with_patents, rollup.prospects_skipped_short_name,
    )

    if dry_run:
        for emission in emissions:
            log.info(
                "[dry-run] would emit %s↔%s patent=%s",
                emission.prospect_a, emission.prospect_b,
                emission.structured_value.get("patent_number"),
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
                    "uspto_ingest persist failed for %s↔%s",
                    emission.prospect_a, emission.prospect_b,
                )

    log.info(
        "uspto_ingest done account=%s inserted=%d skipped_dedup=%d errors=%d",
        account_id, rollup.signals_inserted, rollup.signals_skipped_dedup,
        len(rollup.errors),
    )
    return rollup


async def _persist_emission(
    conn: asyncpg.Connection,
    account_id: UUID,
    emission: Emission,
    rollup: UsptoIngestRollup,
) -> None:
    """One emission = one signal row pointing prospect_a → prospect_b."""
    structured = emission.structured_value
    patent_number = str(structured.get("patent_number") or "")
    connected_to = str(structured.get("connected_to") or "")
    if not patent_number or not connected_to:
        rollup.errors.append(
            (str(emission.prospect_a), "missing patent_number or connected_to")
        )
        return
    if await _signal_exists(
        conn, emission.prospect_a, SIGNAL_TYPE, patent_number, connected_to
    ):
        rollup.signals_skipped_dedup += 1
        return
    await _insert_signal(
        conn,
        emission.prospect_a,
        account_id,
        structured,
        CONFIDENCE_PATENT_CO_INVENTOR,
    )
    rollup.signals_inserted += 1


async def bulk_uspto_ingest_all_accounts(
    *,
    limit: int | None = None,
    max_patents_per_person: int = DEFAULT_MAX_PATENTS_PER_PERSON,
    concurrency: int = DEFAULT_CONCURRENCY,
    dry_run: bool = False,
) -> list[UsptoIngestRollup]:
    """Iterate every account in ``prospects`` and ingest each in turn."""
    async with acquire() as conn:
        account_ids = await _fetch_all_account_ids(conn)
    log.info("uspto_ingest all-accounts: %d accounts", len(account_ids))
    rollups: list[UsptoIngestRollup] = []
    for account_id in account_ids:
        rollup = await bulk_uspto_ingest_account(
            account_id,
            limit=limit,
            max_patents_per_person=max_patents_per_person,
            concurrency=concurrency,
            dry_run=dry_run,
        )
        rollups.append(rollup)
    return rollups


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.bulk_uspto_ingest",
        description=(
            "Bulk per-prospect USPTO patent ingestion → emits "
            "patent_co_inventor signal rows (strength 0.95)."
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
        "--max-patents-per-person",
        type=int,
        default=DEFAULT_MAX_PATENTS_PER_PERSON,
        help=f"Max patents fetched per prospect (default {DEFAULT_MAX_PATENTS_PER_PERSON}).",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Max in-flight USPTO requests (default {DEFAULT_CONCURRENCY}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log emissions without writing to the signals table.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return p


def _print_rollup(rollup: UsptoIngestRollup) -> None:
    if rollup.aborted_no_api_key:
        print(
            f"uspto_ingest account={rollup.account_id} ABORTED — "
            "USPTO_ODP_API_KEY missing. Register at data.uspto.gov."
        )
        return
    msg = (
        f"uspto_ingest account={rollup.account_id} "
        f"prospects_scanned={rollup.prospects_scanned} "
        f"prospects_with_patents={rollup.prospects_with_patents} "
        f"patents_indexed={rollup.patents_indexed} "
        f"pairs_emitted={rollup.pairs_emitted} "
        f"signals_inserted={rollup.signals_inserted} "
        f"signals_skipped_dedup={rollup.signals_skipped_dedup} "
        f"skipped_short_name={rollup.prospects_skipped_short_name} "
        f"errors={len(rollup.errors)} "
        f"dry_run={rollup.dry_run}"
    )
    print(msg)


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    async def _go() -> list[UsptoIngestRollup]:
        try:
            if args.all_accounts:
                return await bulk_uspto_ingest_all_accounts(
                    limit=args.limit,
                    max_patents_per_person=args.max_patents_per_person,
                    concurrency=args.concurrency,
                    dry_run=args.dry_run,
                )
            return [
                await bulk_uspto_ingest_account(
                    args.account_id,
                    limit=args.limit,
                    max_patents_per_person=args.max_patents_per_person,
                    concurrency=args.concurrency,
                    dry_run=args.dry_run,
                )
            ]
        finally:
            await close_pool()

    rollups = asyncio.run(_go())
    for rollup in rollups:
        _print_rollup(rollup)
    if any(r.aborted_no_api_key for r in rollups):
        return 2
    return 0 if all(not r.errors for r in rollups) else 1


if __name__ == "__main__":
    sys.exit(main())
