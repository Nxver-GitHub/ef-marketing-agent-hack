"""Bulk Apify profile-by-URL enrichment runner.

Mines unenriched prospects (those with ``linkedin_url`` populated but no
``apify_linkedin_apimaestro`` marker signal yet), calls the
``harvestapi/linkedin-profile-scraper`` Apify actor one URL at a time,
and writes the returned profile shape into ``persons`` /
``employment_periods`` / ``education_periods`` via the canonical
:func:`credence.enrichment.writer.write_canonical_persons` write-path.

This is the largest unblock for edge coverage: ~18k untouched
prospects can't generate any career/education overlap edges until they
have rich profile data. After this runs, the existing
``bulk_career_overlap_signals`` + ``bulk_education_signals`` runners
discover thousands of new edges from the freshly-enriched data.

## Idempotency

Each successful fetch writes a marker signal
``signal_type='apify_linkedin_apimaestro'`` keyed on
``(account_id, prospect_id)``. The selector in
:data:`SELECT_UNENRICHED_PROSPECTS_SQL` excludes any prospect that
already has one — so re-runs only touch prospects that haven't been
fetched yet. Failed fetches do *not* write a marker so they're
retried.

## Cost accounting

Apify's run-sync endpoint doesn't return ``chargedEventCounts`` in the
body, so cost is approximated as ``len(profiles) × per-mode-rate``
(0.4¢ short / 0.8¢ full / 1.2¢ full+email). For a typical
single-profile run that's 1¢ per prospect after rounding-up.

## CLI

::

    cd server && APIFY_TOKEN=$APIFY_TOKEN \\
        uv run python -m credence.jobs.bulk_apify_profile_lookup \\
        --account-id <uuid> --limit 5 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from ..db import acquire, close_pool
from ..enrichment import apify as apify_mod
from ..enrichment.apify import (
    MODE_FULL,
    MODE_FULL_EMAIL,
    MODE_SHORT,
    PROFILE_MODE_NO_EMAIL,
    PROFILE_MODE_WITH_EMAIL,
    ApifyProfile,
    EnrichmentResult,
    ScrapeMode,
)
from ..enrichment.normalizer import from_apify
from ..enrichment.writer import write_canonical_persons

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


DEFAULT_CONCURRENCY = 4
# Per-chunk URL ceiling. Smaller chunks = (a) more chunks running
# concurrently → more proxy-IP diversity (anti-throttle), (b) faster
# completion of a single chunk → faster persist + bulk-runner cascade.
# Was 5000 (3 chunks for 15k); empirical: 2 of 3 chunks got stuck at
# ~3 items/min (LinkedIn-side throttle on a single proxy IP) while the
# third hit ~23/min. Smaller chunks let bad-IP chunks die faster +
# resubmit takes a fresh proxy assignment. 1500 → ~10 chunks per 15k
# run; with concurrency=8 most run truly in parallel.
DEFAULT_CHUNK_SIZE = 1500
# Max wall time we'll wait for any one batched run to finish. The
# original 3600s (1hr) was too short — slow-proxy chunks routinely take
# 90-120 min to chew through 1500 URLs (LinkedIn anti-bot throttle keeps
# per-actor rate at ~3-7 items/min for 5-of-8 IP assignments). When the
# wait expires, the runner marks the chunk failed and never fetches the
# (eventually-SUCCEEDED) dataset — see `scripts/apify_recover_datasets.py`
# which exists to clean up after this exact failure mode. 4hr ceiling is
# defensive; chunks that genuinely stall this long are throwaway.
DEFAULT_RUN_MAX_WAIT_SECONDS = 14400.0
# How often to poll an in-flight run.
DEFAULT_RUN_POLL_INTERVAL_SECONDS = 10.0
MARKER_SIGNAL_TYPE = "apify_linkedin_apimaestro"
MARKER_SIGNAL_SOURCE = "apify_individual"
MARKER_METHOD = "apify_profile_by_url"

# Per-mode default cost approximation. The profile-scraper actor uses
# different mode strings + cheaper rates than company-employees. The runner
# defaults to PROFILE_MODE_NO_EMAIL ($4/1k = 0.4¢/profile).
_MODE_COST_CENTS: dict[str, float] = {
    # Company-employees actor (legacy / not the path used here):
    MODE_SHORT: 0.4,
    MODE_FULL: 0.8,
    MODE_FULL_EMAIL: 1.2,
    # Profile-scraper actor — what fetch_profile_by_url actually runs:
    PROFILE_MODE_NO_EMAIL: 0.4,
    PROFILE_MODE_WITH_EMAIL: 1.0,
}


# ── SQL ──────────────────────────────────────────────────────────────────────


SELECT_UNENRICHED_PROSPECTS_SQL = """
SELECT id, name, linkedin_url, account_id
FROM prospects
WHERE account_id = $1
  AND linkedin_url IS NOT NULL
  AND linkedin_url <> ''
  AND NOT EXISTS (
    SELECT 1 FROM signals s
    WHERE s.prospect_id = prospects.id
      AND s.signal_type = $2
  )
ORDER BY id
"""

SELECT_UNENRICHED_PROSPECTS_LIMIT_SQL = (
    SELECT_UNENRICHED_PROSPECTS_SQL + "LIMIT $3\n"
)

SELECT_ALL_ACCOUNTS_SQL = """
SELECT DISTINCT account_id
FROM prospects
WHERE account_id IS NOT NULL
  AND linkedin_url IS NOT NULL
  AND linkedin_url <> ''
ORDER BY account_id
"""

INSERT_MARKER_SIGNAL_SQL = """
INSERT INTO signals (
    id, prospect_id, account_id, source, signal_type,
    value, raw_data, weight, confidence, collected_at
)
VALUES (
    gen_random_uuid(), $1, $2, $3, $4,
    $5::jsonb, NULL, 1.0, 1.0, NOW()
)
"""


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UnenrichedProspect:
    """One row from :data:`SELECT_UNENRICHED_PROSPECTS_SQL`."""

    id: UUID
    name: str | None
    linkedin_url: str
    account_id: UUID


@dataclass
class ApifyLookupRollup:
    """Aggregate counters for one ``bulk_apify_profile_lookup_account`` call."""

    account_id: UUID
    prospects_targeted: int = 0
    profiles_fetched: int = 0
    profiles_failed: int = 0
    profiles_no_match: int = 0
    persons_inserted: int = 0
    persons_updated: int = 0
    employment_periods_inserted: int = 0
    education_periods_inserted: int = 0
    cost_cents_total: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


# ── Pure helpers ─────────────────────────────────────────────────────────────


def _per_profile_cost_cents(mode: str) -> int:
    """Approximate cost (cents, ceiling-rounded) of one successful fetch."""
    rate = _MODE_COST_CENTS.get(mode, _MODE_COST_CENTS[MODE_FULL])
    return int(rate + 0.999) if rate > 0 else 0


def _normalize_linkedin_url(url: str) -> str:
    """Normalize a LinkedIn URL for cross-side matching.

    Apify echoes back the URL it scraped, but minor differences (trailing
    slash, ``http`` vs ``https``, ``www.`` prefix, query params,
    casing on the slug) would otherwise break a naive dict lookup.
    Conservative: strip protocol, ``www.``, lowercase, drop query +
    fragment + trailing slash.
    """
    if not isinstance(url, str):
        return ""
    s = url.strip()
    if not s:
        return ""
    # Drop scheme
    for prefix in ("https://", "http://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    # Drop www.
    if s.lower().startswith("www."):
        s = s[4:]
    # Drop query / fragment
    for sep in ("?", "#"):
        idx = s.find(sep)
        if idx >= 0:
            s = s[:idx]
    # Drop trailing slash
    s = s.rstrip("/")
    return s.lower()


def _build_marker_value() -> dict[str, Any]:
    """Marker signal payload — minimal, just enough to dedupe re-runs."""
    return {
        "method": MARKER_METHOD,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ── DB helpers ───────────────────────────────────────────────────────────────


async def _fetch_unenriched_prospects(
    conn: asyncpg.Connection,
    account_id: UUID,
    limit: int | None,
) -> list[UnenrichedProspect]:
    if limit is None:
        rows = await conn.fetch(
            SELECT_UNENRICHED_PROSPECTS_SQL, account_id, MARKER_SIGNAL_TYPE,
        )
    else:
        rows = await conn.fetch(
            SELECT_UNENRICHED_PROSPECTS_LIMIT_SQL,
            account_id, MARKER_SIGNAL_TYPE, int(limit),
        )
    out: list[UnenrichedProspect] = []
    for r in rows:
        url = r["linkedin_url"]
        if not isinstance(url, str) or not url.strip():
            continue
        out.append(
            UnenrichedProspect(
                id=r["id"],
                name=r["name"],
                linkedin_url=url,
                account_id=r["account_id"],
            )
        )
    return out


async def _fetch_all_account_ids(conn: asyncpg.Connection) -> list[UUID]:
    rows = await conn.fetch(SELECT_ALL_ACCOUNTS_SQL)
    return [r["account_id"] for r in rows]


async def _insert_marker_signal(
    conn: asyncpg.Connection,
    *,
    prospect_id: UUID,
    account_id: UUID,
) -> None:
    """Insert the dedupe marker. Same dict-not-json-string pattern as
    :func:`bulk_education_signals._insert_signal` — asyncpg's jsonb codec
    encodes a dict natively. Passing ``json.dumps(dict)`` would
    double-encode (Postgres would parse the result as a jsonb-typed
    *string*, opaque to ``value->>'key'`` subscripts).
    """
    await conn.execute(
        INSERT_MARKER_SIGNAL_SQL,
        prospect_id,
        account_id,
        MARKER_SIGNAL_SOURCE,
        MARKER_SIGNAL_TYPE,
        _build_marker_value(),
    )


# ── Per-prospect fetch + persist ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _ProspectFetchOutcome:
    """One worker's output: either a profile to persist or a failure tag."""

    prospect: UnenrichedProspect
    profile: ApifyProfile | None
    failed: bool
    cost_cents: int


async def _run_chunk(
    chunk: list[UnenrichedProspect],
    *,
    mode: str,
    api_token: str,
    client: httpx.AsyncClient,
    poll_interval: float,
    max_wait_seconds: float,
) -> list[_ProspectFetchOutcome]:
    """Submit one batched async run for ``chunk`` and map results back.

    Returns one :class:`_ProspectFetchOutcome` per prospect. On run-level
    failure (start, poll, fetch), every prospect in the chunk is marked
    ``failed=True``. On partial success, matched profiles get
    ``profile`` populated, unmatched prospects get
    ``failed=False, profile=None`` (no-match — same semantics the old
    per-URL path used for a 200/empty response).
    """
    if not chunk:
        return []
    urls = [p.linkedin_url for p in chunk]

    # Build URL → prospect map (normalized) so we can match parsed
    # profiles back to input prospects regardless of return order.
    url_to_prospect: dict[str, UnenrichedProspect] = {}
    for p in chunk:
        key = _normalize_linkedin_url(p.linkedin_url)
        if key:
            # If two prospects in a chunk share the same normalized URL
            # (rare but possible), keep the first; the second goes to
            # no-match.
            url_to_prospect.setdefault(key, p)

    def _all_failed(reason: str) -> list[_ProspectFetchOutcome]:
        log.warning(
            "apify_profile_lookup chunk failed (n=%d): %s",
            len(chunk), reason,
        )
        return [
            _ProspectFetchOutcome(
                prospect=p, profile=None, failed=True, cost_cents=0,
            )
            for p in chunk
        ]

    # Step 1: submit run
    try:
        run_data = await apify_mod.start_profile_by_url_run(
            urls,
            mode=mode,
            api_token=api_token,
            client=client,
        )
    except Exception as exc:  # noqa: BLE001 — extractor is the trust boundary
        return _all_failed(f"start exc: {exc!r}")
    if not run_data or not isinstance(run_data.get("id"), str):
        return _all_failed("start returned no run_id")

    run_id = run_data["id"]
    log.info(
        "apify_profile_lookup chunk submitted run=%s n=%d",
        run_id, len(chunk),
    )

    # Step 2: wait
    try:
        status, finished_data = await apify_mod.wait_for_run(
            run_id,
            poll_interval=poll_interval,
            max_wait_seconds=max_wait_seconds,
            api_token=api_token,
            client=client,
        )
    except Exception as exc:  # noqa: BLE001
        return _all_failed(f"wait exc on run={run_id}: {exc!r}")

    if status != "SUCCEEDED" or finished_data is None:
        # Per brief: any non-SUCCEEDED chunk is fully failed (caller
        # re-runs to retry). Recovery of partial datasets happens at
        # the apify_mod layer (see ``find_company_employees_async``)
        # when one-shot async is used; here we keep the bulk path
        # simple and predictable.
        return _all_failed(f"non-SUCCEEDED status={status}")

    # Step 3: fetch dataset
    try:
        result: EnrichmentResult | None = await apify_mod.fetch_run_dataset(
            finished_data,
            api_token=api_token,
            client=client,
        )
    except Exception as exc:  # noqa: BLE001
        return _all_failed(f"fetch exc on run={run_id}: {exc!r}")
    if result is None:
        return _all_failed(f"fetch returned None for run={run_id}")

    # Step 4: match parsed profiles back to prospects
    matched: dict[UUID, ApifyProfile] = {}
    unmatched_count = 0
    for profile in result.profiles:
        key = _normalize_linkedin_url(profile.linkedin_url)
        prospect = url_to_prospect.get(key)
        if prospect is None:
            unmatched_count += 1
            continue
        # If the same prospect resolves twice, keep the first match.
        matched.setdefault(prospect.id, profile)

    if unmatched_count:
        log.warning(
            "apify_profile_lookup chunk run=%s: %d returned profiles did not "
            "match any input URL",
            run_id, unmatched_count,
        )

    # Per-profile cost: total run cost / number of returned items, ceiling.
    # `result.cost_cents` is the total for the run (from chargedEventCounts).
    # Distribute to matched prospects so the rollup totals stay accurate.
    matched_count = len(matched)
    if matched_count > 0 and result.cost_cents > 0:
        per_profile_cost = max(1, (result.cost_cents + matched_count - 1) // matched_count)
    else:
        per_profile_cost = 0

    outcomes: list[_ProspectFetchOutcome] = []
    for p in chunk:
        prof = matched.get(p.id)
        if prof is None:
            # No matching profile in dataset → no-match (not a failure).
            outcomes.append(
                _ProspectFetchOutcome(
                    prospect=p, profile=None, failed=False, cost_cents=0,
                )
            )
        else:
            outcomes.append(
                _ProspectFetchOutcome(
                    prospect=p, profile=prof, failed=False,
                    cost_cents=per_profile_cost,
                )
            )
    return outcomes


async def _persist_profile(
    outcome: _ProspectFetchOutcome,
    *,
    rollup: ApifyLookupRollup,
) -> None:
    """Write one fetched profile to persons/employment/education + marker.

    Uses the canonical :func:`write_canonical_persons` write-path so we
    never reimplement person/employment/education upserts. Then writes
    the marker signal for dedupe on re-runs.
    """
    profile = outcome.profile
    prospect = outcome.prospect
    assert profile is not None  # caller filters

    canonical = from_apify(profile)
    if canonical is None:
        # Profile lacked first/last name or LinkedIn URL after parse —
        # nothing canonicalizable to persist. Treat like a no-match.
        rollup.profiles_no_match += 1
        return

    try:
        write_result = await write_canonical_persons(
            [canonical],
            account_id=prospect.account_id,
        )
    except Exception as exc:  # noqa: BLE001
        rollup.profiles_failed += 1
        rollup.errors.append(f"persist({prospect.id}): {exc!r}")
        log.exception(
            "apify_profile_lookup persist failed for %s",
            prospect.id,
        )
        return

    rollup.persons_inserted += write_result.persons_inserted
    rollup.persons_updated += write_result.persons_updated
    rollup.employment_periods_inserted += write_result.employment_periods_inserted
    rollup.education_periods_inserted += write_result.education_periods_inserted
    if write_result.errors:
        for e in write_result.errors:
            rollup.errors.append(f"writer({prospect.id}): {e}")

    # The canonical writer (`enrichment/writer.write_canonical_persons`) does
    # not populate `persons.source_prospect_id` — it's a v3.1-late field
    # added by `20260501_v3_persons_prospect_link.sql` (msg 188). Without
    # this UPDATE, every new person Apify lands has source_prospect_id=NULL,
    # which excludes them from `bulk_career_overlap_signals` (filters on
    # `source_prospect_id IS NOT NULL`) and from SwiftElk's clustering
    # (`require_source_prospect=True`). Stamping it here at write-time keeps
    # the loop self-healing without needing a periodic linkage backfill.
    if profile.linkedin_url:
        try:
            async with acquire() as conn:
                await conn.execute(
                    "UPDATE persons SET source_prospect_id = $1 "
                    "WHERE linkedin_url = $2 AND source_prospect_id IS NULL",
                    prospect.id,
                    profile.linkedin_url,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "apify_profile_lookup source_prospect_id stamp failed for %s: %r",
                prospect.id, exc,
            )

    # Marker signal — only after the canonical persist succeeded, so a
    # crash mid-write won't leave a "done" marker on partially-written
    # data.
    try:
        async with acquire() as conn:
            await _insert_marker_signal(
                conn,
                prospect_id=prospect.id,
                account_id=prospect.account_id,
            )
    except Exception as exc:  # noqa: BLE001
        rollup.errors.append(f"marker({prospect.id}): {exc!r}")
        log.warning(
            "apify_profile_lookup marker insert failed for %s: %r",
            prospect.id, exc,
        )


# ── Public orchestrator ──────────────────────────────────────────────────────


async def bulk_apify_profile_lookup_account(
    account_id: UUID,
    *,
    limit: int | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    mode: str = PROFILE_MODE_NO_EMAIL,
    dry_run: bool = False,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    poll_interval: float = DEFAULT_RUN_POLL_INTERVAL_SECONDS,
    max_wait_seconds: float = DEFAULT_RUN_MAX_WAIT_SECONDS,
) -> ApifyLookupRollup:
    """Mine unenriched prospects and fetch their LinkedIn profiles via Apify.

    Args:
        account_id: tenant scope.
        limit: optional cap on prospects fetched per run.
        concurrency: max in-flight Apify requests (default 4 to avoid
            Supabase pool contention with sibling jobs).
        mode: ``"Short"`` / ``"Full"`` / ``"Full + email search"``.
        dry_run: log targets without calling Apify or writing anything.
        api_token: optional override; defaults to ``APIFY_TOKEN`` env.
        client: optional injected ``httpx.AsyncClient`` (tests pass a
            ``MockTransport``-backed client).
    """
    rollup = ApifyLookupRollup(account_id=account_id, dry_run=dry_run)

    # Step 1 — load unenriched prospects.
    async with acquire() as conn:
        prospects = await _fetch_unenriched_prospects(conn, account_id, limit)
    rollup.prospects_targeted = len(prospects)
    log.info(
        "apify_profile_lookup start account=%s targets=%d concurrency=%d "
        "mode=%s dry_run=%s",
        account_id, rollup.prospects_targeted, concurrency, mode, dry_run,
    )

    if dry_run:
        for p in prospects:
            log.info(
                "[dry-run] would fetch prospect=%s url=%s",
                p.id, p.linkedin_url,
            )
        return rollup

    if not prospects:
        return rollup

    # Step 2 — concurrent Apify fetches.
    token = api_token or os.environ.get("APIFY_TOKEN")
    if not token:
        rollup.errors.append("APIFY_TOKEN not set — aborting before any fetch")
        log.error("apify_profile_lookup: APIFY_TOKEN not set; aborting")
        return rollup

    own_client = client is None
    http = client or httpx.AsyncClient()
    try:
        # Chunk prospects into batched async runs. With chunk_size=5000
        # and 15k prospects, that's 3 chunks; ``concurrency`` controls
        # how many chunks run in parallel (each chunk = one Apify run
        # with N urls submitted as ``queries``).
        size = max(1, int(chunk_size))
        chunks = [
            prospects[i:i + size] for i in range(0, len(prospects), size)
        ]
        log.info(
            "apify_profile_lookup chunking %d prospects → %d chunks "
            "(chunk_size=%d, concurrency=%d)",
            len(prospects), len(chunks), size, concurrency,
        )

        # Cap simultaneous in-flight chunks at ``concurrency`` (each
        # chunk is a long-running Apify run, so we use a semaphore
        # rather than spawning all of them at once).
        chunk_sem = asyncio.Semaphore(max(1, concurrency))

        async def _run_chunk_guarded(
            chunk: list[UnenrichedProspect],
        ) -> list[_ProspectFetchOutcome]:
            async with chunk_sem:
                return await _run_chunk(
                    chunk,
                    mode=mode,
                    api_token=token,
                    client=http,
                    poll_interval=poll_interval,
                    max_wait_seconds=max_wait_seconds,
                )

        chunk_results = await asyncio.gather(
            *(_run_chunk_guarded(c) for c in chunks),
            return_exceptions=False,
        )
        outcomes: list[_ProspectFetchOutcome] = [
            outcome for batch in chunk_results for outcome in batch
        ]
    finally:
        if own_client:
            await http.aclose()

    # Step 3 — tally + persist successful fetches.
    for outcome in outcomes:
        if outcome.failed:
            rollup.profiles_failed += 1
            continue
        if outcome.profile is None:
            rollup.profiles_no_match += 1
            continue
        rollup.profiles_fetched += 1
        rollup.cost_cents_total += (
            outcome.cost_cents or _per_profile_cost_cents(mode)
        )
        await _persist_profile(outcome, rollup=rollup)

    log.info(
        "apify_profile_lookup done account=%s fetched=%d failed=%d "
        "no_match=%d persons_ins=%d persons_upd=%d emp_ins=%d edu_ins=%d "
        "cost_cents=%d errors=%d",
        account_id, rollup.profiles_fetched, rollup.profiles_failed,
        rollup.profiles_no_match, rollup.persons_inserted,
        rollup.persons_updated, rollup.employment_periods_inserted,
        rollup.education_periods_inserted, rollup.cost_cents_total,
        len(rollup.errors),
    )
    return rollup


async def bulk_apify_profile_lookup_all_accounts(
    *,
    limit: int | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    mode: str = PROFILE_MODE_NO_EMAIL,
    dry_run: bool = False,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    poll_interval: float = DEFAULT_RUN_POLL_INTERVAL_SECONDS,
    max_wait_seconds: float = DEFAULT_RUN_MAX_WAIT_SECONDS,
) -> list[ApifyLookupRollup]:
    """Iterate every account with prospects and run the lookup."""
    async with acquire() as conn:
        account_ids = await _fetch_all_account_ids(conn)
    log.info(
        "apify_profile_lookup all-accounts: %d accounts", len(account_ids),
    )
    rollups: list[ApifyLookupRollup] = []
    for account_id in account_ids:
        rollup = await bulk_apify_profile_lookup_account(
            account_id,
            limit=limit,
            concurrency=concurrency,
            mode=mode,
            dry_run=dry_run,
            api_token=api_token,
            client=client,
            chunk_size=chunk_size,
            poll_interval=poll_interval,
            max_wait_seconds=max_wait_seconds,
        )
        rollups.append(rollup)
    return rollups


# ── CLI ──────────────────────────────────────────────────────────────────────


_MODE_CHOICES: dict[str, str] = {
    # Profile-scraper actor (the one fetch_profile_by_url calls):
    "no_email": PROFILE_MODE_NO_EMAIL,
    "with_email": PROFILE_MODE_WITH_EMAIL,
    # Aliases / legacy company-employees mode strings (kept so existing
    # invocations still work for the Short/Full/Full+Email enum). When the
    # caller passes one of these, fetch_profile_by_url will get rejected
    # by Apify; the runner will surface that as profiles_failed.
    "short": MODE_SHORT,
    "full": MODE_FULL,
    "full_email": MODE_FULL_EMAIL,
}


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.bulk_apify_profile_lookup",
        description=(
            "Bulk profile-by-URL Apify enrichment runner. Mines prospects "
            "with a linkedin_url but no apify_linkedin_apimaestro signal, "
            "calls harvestapi/linkedin-profile-scraper, persists the "
            "resulting profile to persons/employment_periods/"
            "education_periods, and writes a marker signal for re-run "
            "dedupe."
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
        help="Iterate every account with unenriched prospects.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap prospects fetched per account (default: no cap).",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=(
            f"Max in-flight Apify requests (default {DEFAULT_CONCURRENCY}). "
            "Keep low to avoid Supabase pool contention."
        ),
    )
    p.add_argument(
        "--mode",
        choices=sorted(_MODE_CHOICES.keys()),
        default="no_email",
        help="Apify scraper mode: short ($4/1k), full ($8/1k), full_email ($12/1k).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List targets without calling Apify or writing.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return p


def _print_rollup(rollup: ApifyLookupRollup) -> None:
    msg = (
        f"apify_profile_lookup account={rollup.account_id} "
        f"prospects_targeted={rollup.prospects_targeted} "
        f"profiles_fetched={rollup.profiles_fetched} "
        f"profiles_failed={rollup.profiles_failed} "
        f"profiles_no_match={rollup.profiles_no_match} "
        f"persons_inserted={rollup.persons_inserted} "
        f"persons_updated={rollup.persons_updated} "
        f"employment_periods_inserted={rollup.employment_periods_inserted} "
        f"education_periods_inserted={rollup.education_periods_inserted} "
        f"cost_cents_total={rollup.cost_cents_total} "
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
    mode = _MODE_CHOICES[args.mode]

    async def _go() -> list[ApifyLookupRollup]:
        try:
            if args.all_accounts:
                return await bulk_apify_profile_lookup_all_accounts(
                    limit=args.limit,
                    concurrency=args.concurrency,
                    mode=mode,
                    dry_run=args.dry_run,
                )
            return [
                await bulk_apify_profile_lookup_account(
                    args.account_id,
                    limit=args.limit,
                    concurrency=args.concurrency,
                    mode=mode,
                    dry_run=args.dry_run,
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


# Re-export for tests + ergonomic imports.
__all__ = [
    "ApifyLookupRollup",
    "UnenrichedProspect",
    "DEFAULT_CONCURRENCY",
    "MARKER_SIGNAL_TYPE",
    "MARKER_SIGNAL_SOURCE",
    "MARKER_METHOD",
    "SELECT_UNENRICHED_PROSPECTS_SQL",
    "SELECT_UNENRICHED_PROSPECTS_LIMIT_SQL",
    "INSERT_MARKER_SIGNAL_SQL",
    "bulk_apify_profile_lookup_account",
    "bulk_apify_profile_lookup_all_accounts",
    "_build_arg_parser",
    "_build_marker_value",
    "_per_profile_cost_cents",
]


# Quiet the unused-import linter for ``json`` when we keep it on hand for
# parity with the bulk_education_signals module's payload shape (caller may
# log structured-value as JSON). Cheap to keep, costless to import.
_ = json
