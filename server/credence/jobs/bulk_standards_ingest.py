"""Bulk per-tenant standards-roster ingestion runner.

Scrapes the public membership rosters of standards bodies (JEDEC, IEEE SA,
SEMI, Wi-Fi Alliance, RISC-V International, MLCommons) once per process,
then for every ``persons`` row in the tenant whose canonical_name (or
name_variants) matches a roster entry, UPSERTs one ``standards_memberships``
row.

Downstream, ``standards_clustering.py`` self-joins ``standards_memberships``
on ``(organization, committee)`` to emit ``standards_committee_peer`` edges
into ``person_connections``.

## Why a tenant-scope bulk runner instead of the per-pair extractor

``credence.extractors.standards.find_standards_roster_memberships`` answers
"do these two specific people share a committee?" Useful at warm-path-fan-out
time, but if you call it pairwise across N persons you do O(N²) name matches
when the natural query is O(N) — fan in once, then let SQL self-join.

This runner does the O(N) version: scrape rosters once, build a folded-name
index, sweep every tenant person against the index, write the resulting
memberships. Subsequent paper_clustering / standards_clustering reads run
on a fully-materialized join table.

## Idempotency

UPSERT keyed on ``(person_id, organization, committee, COALESCE(start_year, 0))``
matching the unique index ``standards_memberships_uniq``. Re-runs are no-ops
unless a roster has changed (e.g. years updated). Year refresh is intentional —
``end_year`` may flip from NULL to a concrete year as someone leaves a
committee.

## Cost

Firecrawl ``/v1/scrape`` is ~1¢/scrape. Six bodies = ~6¢ per process. The
extractor's module-level ``_ROSTER_CACHE`` keeps subsequent same-process
calls free.

## CLI

::

    cd server && uv run python -m credence.jobs.bulk_standards_ingest \\
        --account-id <uuid> --dry-run

    cd server && uv run python -m credence.jobs.bulk_standards_ingest \\
        --all-accounts

The ``--bodies`` flag accepts a comma-separated subset for staged rollouts:

::

    --bodies "JEDEC,IEEE SA"

## Defensive defaults

- No ``FIRECRAWL_API_KEY`` → returns immediately with a logged warning.
- A roster fetch fails → that body is skipped; other bodies still contribute.
- A name appears in multiple committees within one body → multiple rows
  written (one per committee).
- A name in the roster doesn't match any tenant person → silently dropped
  (it's not a person we care about).
- A tenant person matches multiple rosters/committees → one row per
  (body, committee). The ``standards_memberships_uniq`` constraint enforces
  no duplicates within a (person, org, committee, start_year) tuple.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from ..db import acquire, close_pool
from ..extractors.standards import (
    STANDARDS_BODIES,
    _ensure_roster_cached,
    _fold_name,
)

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


DEFAULT_FIRECRAWL_TIMEOUT = 30.0

# CHECK constraint on standards_memberships requires year ∈ [1950, 2100] or
# NULL. Years outside the range are squashed to NULL so the UPSERT never
# trips the constraint.
_YEAR_MIN = 1950
_YEAR_MAX = 2100

# Match the year-string formats the extractor produces in `_extract_years`:
#   "2018-2022"     → start=2018, end=2022
#   "2015-present"  → start=2015, end=None (open-ended)
#   "2020"          → start=2020, end=None  (single year, can't tell tenure)
#   "unknown"       → start=None, end=None
_YEAR_RANGE_RE = re.compile(r"^(\d{4})\s*[-–—]\s*(\d{4})$")
_YEAR_PRESENT_RE = re.compile(r"^(\d{4})\s*[-–—]\s*present$", re.IGNORECASE)
_YEAR_SINGLE_RE = re.compile(r"^(\d{4})$")


# ── SQL ──────────────────────────────────────────────────────────────────────


SELECT_PERSONS_SQL = """
SELECT id, canonical_name, name_variants
FROM persons
WHERE account_id = $1
  AND canonical_name IS NOT NULL
  AND canonical_name <> ''
"""

SELECT_ALL_ACCOUNTS_SQL = """
SELECT DISTINCT account_id
FROM persons
WHERE account_id IS NOT NULL
ORDER BY account_id
"""

UPSERT_MEMBERSHIP_SQL = """
INSERT INTO standards_memberships (
    person_id, organization, committee, role,
    start_year, end_year, source_url, account_id
)
VALUES ($1, $2, $3, NULL, $4, $5, $6, $7)
ON CONFLICT (person_id, organization, committee, COALESCE(start_year, 0))
DO UPDATE SET
    end_year   = COALESCE(EXCLUDED.end_year, standards_memberships.end_year),
    source_url = COALESCE(EXCLUDED.source_url, standards_memberships.source_url),
    updated_at = NOW()
"""


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PersonRow:
    """Folded view of one ``persons`` row used for the name index."""

    id: UUID
    canonical_name: str
    folded_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One (body, committee, member_name, years) tuple from the extractor."""

    body: str
    committee: str
    member_name: str
    years: str
    source_url: str


@dataclass(frozen=True, slots=True)
class MembershipEmission:
    """One ``standards_memberships`` row ready to UPSERT."""

    person_id: UUID
    organization: str
    committee: str
    start_year: int | None
    end_year: int | None
    source_url: str


@dataclass(slots=True)
class StandardsIngestRollup:
    """Aggregate counters for one ``bulk_standards_ingest_account`` call."""

    account_id: UUID
    persons_indexed: int = 0
    bodies_scraped: int = 0
    bodies_empty: int = 0
    roster_entries: int = 0
    matched_emissions: int = 0
    inserts_or_updates: int = 0
    dry_run: bool = False
    errors: list[tuple[str, str]] = field(default_factory=list)


# ── Pure planning helpers ───────────────────────────────────────────────────


def _build_name_index(rows: list[PersonRow]) -> dict[str, UUID]:
    """``folded_name → person_id``. Last-write-wins on collisions.

    Two people sharing a folded name (e.g., common Anglo-Saxon names) is a
    real entity-resolution problem, not solved by this runner — the
    extractor's documented limitation. The naive collision behavior is
    safer than emitting both: a wrong attribution is one bad signal, while
    a duplicate emission floods clustering with phantom co-memberships.
    """
    index: dict[str, UUID] = {}
    for row in rows:
        for folded in row.folded_names:
            if not folded:
                continue
            index[folded] = row.id
    return index


def _safe_year(raw: int | str | None) -> int | None:
    """Squash years outside [1950, 2100] to NULL (matches CHECK constraint)."""
    if raw is None:
        return None
    try:
        y = int(raw)
    except (TypeError, ValueError):
        return None
    if y < _YEAR_MIN or y > _YEAR_MAX:
        return None
    return y


def _parse_years(years_str: str | None) -> tuple[int | None, int | None]:
    """Map the extractor's years string → (start_year, end_year)."""
    if not years_str or years_str.lower() == "unknown":
        return None, None
    s = years_str.strip()
    m = _YEAR_RANGE_RE.match(s)
    if m:
        return _safe_year(m.group(1)), _safe_year(m.group(2))
    m = _YEAR_PRESENT_RE.match(s)
    if m:
        return _safe_year(m.group(1)), None  # open-ended → end is NULL
    m = _YEAR_SINGLE_RE.match(s)
    if m:
        # Single year: can't infer tenure; record start, leave end NULL.
        return _safe_year(m.group(1)), None
    return None, None


def _emissions_from_rosters(
    roster_entries: list[RosterEntry],
    name_index: dict[str, UUID],
) -> list[MembershipEmission]:
    """Pure: cross-reference roster entries against the tenant person index.

    Returns one MembershipEmission per matched (person, body, committee)
    triple. Multiple entries for the same person + same committee under
    different years collapse into multiple emissions — the UPSERT key
    includes ``COALESCE(start_year, 0)`` so distinct years produce
    distinct rows.
    """
    out: list[MembershipEmission] = []
    seen: set[tuple[UUID, str, str, int]] = set()
    for entry in roster_entries:
        pid = name_index.get(_fold_name(entry.member_name))
        if pid is None:
            continue
        start_year, end_year = _parse_years(entry.years)
        # Match the UPSERT key shape — same `start_year=0` collapse Postgres
        # uses on the unique index.
        dedup_key = (pid, entry.body, entry.committee, start_year or 0)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        out.append(
            MembershipEmission(
                person_id=pid,
                organization=entry.body,
                committee=entry.committee,
                start_year=start_year,
                end_year=end_year,
                source_url=entry.source_url,
            )
        )
    return out


# ── DB helpers ───────────────────────────────────────────────────────────────


async def _fetch_persons(
    conn: asyncpg.Connection, account_id: UUID
) -> list[PersonRow]:
    rows = await conn.fetch(SELECT_PERSONS_SQL, account_id)
    out: list[PersonRow] = []
    for r in rows:
        canonical = r["canonical_name"] or ""
        variants = r["name_variants"] or []
        folded = {_fold_name(canonical)}
        folded.update(_fold_name(v) for v in variants if v)
        folded.discard("")
        out.append(
            PersonRow(
                id=r["id"],
                canonical_name=canonical,
                folded_names=frozenset(folded),
            )
        )
    return out


async def _fetch_all_account_ids(conn: asyncpg.Connection) -> list[UUID]:
    rows = await conn.fetch(SELECT_ALL_ACCOUNTS_SQL)
    return [r["account_id"] for r in rows]


async def _upsert_membership(
    conn: asyncpg.Connection,
    account_id: UUID,
    emission: MembershipEmission,
) -> None:
    await conn.execute(
        UPSERT_MEMBERSHIP_SQL,
        emission.person_id,
        emission.organization,
        emission.committee,
        emission.start_year,
        emission.end_year,
        emission.source_url,
        account_id,
    )


# ── Public orchestrator ──────────────────────────────────────────────────────


async def bulk_standards_ingest_account(
    account_id: UUID,
    *,
    bodies: dict[str, str] | None = None,
    dry_run: bool = False,
    client: httpx.AsyncClient | None = None,
) -> StandardsIngestRollup:
    """Scrape rosters → match → UPSERT for one tenant scope.

    Args:
        account_id: tenant scope.
        bodies: optional override of the standards-body URL map. Defaults to
            :data:`credence.extractors.standards.STANDARDS_BODIES`.
        dry_run: log emissions without writing to ``standards_memberships``.
        client: optional injected ``httpx.AsyncClient`` (tests pass a
            ``MockTransport``-backed client).
    """
    rollup = StandardsIngestRollup(account_id=account_id, dry_run=dry_run)
    target_bodies = bodies if bodies is not None else STANDARDS_BODIES

    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        log.warning(
            "bulk_standards_ingest: FIRECRAWL_API_KEY not configured; "
            "skipping account=%s",
            account_id,
        )
        return rollup

    # Step 1 — load the tenant person index.
    async with acquire() as conn:
        persons = await _fetch_persons(conn, account_id)
    rollup.persons_indexed = len(persons)
    name_index = _build_name_index(persons)
    log.info(
        "standards_ingest start account=%s persons=%d folded_names=%d",
        account_id, len(persons), len(name_index),
    )

    # Step 2 — scrape rosters (cached per process by the extractor).
    own_client = client is None
    http = client if client is not None else httpx.AsyncClient(
        timeout=DEFAULT_FIRECRAWL_TIMEOUT
    )
    try:
        scrape_tasks = [
            _ensure_roster_cached(body, url, client=http, api_key=api_key)
            for body, url in target_bodies.items()
        ]
        roster_lists = await asyncio.gather(*scrape_tasks, return_exceptions=False)
    finally:
        if own_client:
            await http.aclose()

    # Flatten parsed rosters into RosterEntry tuples carrying the source URL.
    roster_entries: list[RosterEntry] = []
    for body, roster in zip(target_bodies.keys(), roster_lists):
        if not roster:
            rollup.bodies_empty += 1
            continue
        rollup.bodies_scraped += 1
        source_url = target_bodies[body]
        for entry in roster:
            roster_entries.append(
                RosterEntry(
                    body=body,
                    committee=str(entry.get("committee") or "").strip(),
                    member_name=str(entry.get("member_name") or "").strip(),
                    years=str(entry.get("years") or "unknown"),
                    source_url=source_url,
                )
            )
    rollup.roster_entries = len(roster_entries)
    log.info(
        "standards_ingest scraped account=%s scraped=%d empty=%d entries=%d",
        account_id, rollup.bodies_scraped, rollup.bodies_empty,
        rollup.roster_entries,
    )

    # Step 3 — match against the tenant index.
    emissions = _emissions_from_rosters(roster_entries, name_index)
    rollup.matched_emissions = len(emissions)
    log.info(
        "standards_ingest matched account=%s emissions=%d",
        account_id, rollup.matched_emissions,
    )

    if dry_run:
        for emission in emissions:
            log.info(
                "[dry-run] would emit person=%s body=%s committee=%s years=%s-%s",
                emission.person_id, emission.organization,
                emission.committee, emission.start_year, emission.end_year,
            )
        return rollup

    # Step 4 — UPSERT one row per emission.
    async with acquire() as conn:
        for emission in emissions:
            try:
                await _upsert_membership(conn, account_id, emission)
                rollup.inserts_or_updates += 1
            except Exception as exc:  # noqa: BLE001
                rollup.errors.append((str(emission.person_id), repr(exc)))
                log.exception(
                    "standards UPSERT failed for person=%s body=%s",
                    emission.person_id, emission.organization,
                )

    log.info(
        "standards_ingest done account=%s upserts=%d errors=%d",
        account_id, rollup.inserts_or_updates, len(rollup.errors),
    )
    return rollup


async def bulk_standards_ingest_all_accounts(
    *,
    bodies: dict[str, str] | None = None,
    dry_run: bool = False,
) -> list[StandardsIngestRollup]:
    """Iterate every account in ``persons`` and ingest each in turn."""
    async with acquire() as conn:
        account_ids = await _fetch_all_account_ids(conn)
    log.info("standards_ingest all-accounts: %d accounts", len(account_ids))
    rollups: list[StandardsIngestRollup] = []
    for account_id in account_ids:
        rollup = await bulk_standards_ingest_account(
            account_id,
            bodies=bodies,
            dry_run=dry_run,
        )
        rollups.append(rollup)
    return rollups


# ── CLI ──────────────────────────────────────────────────────────────────────


def _parse_bodies(raw: str | None) -> dict[str, str] | None:
    """``--bodies "JEDEC,IEEE SA"`` → dict subset of ``STANDARDS_BODIES``."""
    if not raw or raw.strip().lower() in ("all", ""):
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    invalid = [p for p in parts if p not in STANDARDS_BODIES]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"invalid body name(s): {invalid}. "
            f"Allowed: {', '.join(STANDARDS_BODIES.keys())}"
        )
    return {b: STANDARDS_BODIES[b] for b in parts}


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.bulk_standards_ingest",
        description=(
            "Bulk per-tenant standards-roster scrape → UPSERT into "
            "standards_memberships. Pairs with standards_clustering for the "
            "person_connections edge emit step."
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
        help="Iterate every account in `persons`.",
    )
    p.add_argument(
        "--bodies",
        type=_parse_bodies,
        default=None,
        help=(
            "Comma-separated subset of standards bodies. "
            f"Allowed: {', '.join(STANDARDS_BODIES.keys())} (or 'all'). "
            "Default: all six."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log emissions without writing to standards_memberships.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return p


def _print_rollup(rollup: StandardsIngestRollup) -> None:
    msg = (
        f"standards_ingest account={rollup.account_id} "
        f"persons_indexed={rollup.persons_indexed} "
        f"bodies_scraped={rollup.bodies_scraped} "
        f"bodies_empty={rollup.bodies_empty} "
        f"roster_entries={rollup.roster_entries} "
        f"matched_emissions={rollup.matched_emissions} "
        f"upserts={rollup.inserts_or_updates} "
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

    async def _go() -> list[StandardsIngestRollup]:
        try:
            if args.all_accounts:
                return await bulk_standards_ingest_all_accounts(
                    bodies=args.bodies,
                    dry_run=args.dry_run,
                )
            return [
                await bulk_standards_ingest_account(
                    args.account_id,
                    bodies=args.bodies,
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
    raise SystemExit(main())
