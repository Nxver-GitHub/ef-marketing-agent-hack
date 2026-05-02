"""Standards committee clustering — Phase 2 Job 5 of the connection-edge sprint.

For every pair of persons who served on the same standards body committee
(JEDEC, IEEE SA, SEMI, ISO, etc.) in overlapping membership periods, emit one
``standards_committee_peer`` edge into ``person_connections``.

``standards_committee_peer`` has base strength 0.82 — stronger than a
conference co-presentation (0.80) because standards participation implies
sustained multi-year collaboration through working groups, drafting sessions,
and ballot processes.

## Data source

Reads from the v3 ``standards_memberships`` table populated by the standards
scraper (``server/credence/extractors/standards.py``). Each row records
``(person_id, committee_id, start_year, end_year)``. This job self-joins on
``committee_id`` with an optional year-overlap filter.

## Evidence

One ``connection_evidence`` row per (pair, committee), keyed by
``"{person_a_id}:{person_b_id}:{committee_id}"``. ``structured_value`` carries
the committee name and active years window so the warm-path opener template
can interpolate real committee names.

## Idempotency

Same find-or-create + UPDATE pattern as the other clustering jobs.

## Tenancy

``account_id`` from ``standards_memberships``; planner SQL constrains
``a.account_id = b.account_id``.

## CLI

::

    uv run python -m credence.jobs.standards_clustering --dry-run
    uv run python -m credence.jobs.standards_clustering --committee-id <uuid>
    uv run python -m credence.jobs.standards_clustering --all
    uv run python -m credence.jobs.standards_clustering --limit 200
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from uuid import UUID

import asyncpg

from ..db import acquire, close_pool
from ..strength import DECAY_RATES, STRENGTH_CAP, STRENGTH_TABLE

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


CONNECTION_TYPE = "standards_committee_peer"
EVIDENCE_SOURCE_TYPE = "standards_committee"
CURRENT_YEAR = 2026
_SOURCE_TYPE_COUNT_DEFAULT = 1


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CommitteePair:
    """One co-member pair from the planner SQL.

    The live schema uses denormalized text columns ``organization`` +
    ``committee`` rather than a UUID FK to a separate committees table —
    so the natural identifier for a (body, committee) tuple is the pair
    of strings, joined as ``"<organization>:<committee>"`` for evidence
    source_id generation.
    """

    person_a_id: UUID
    person_b_id: UUID
    organization: str
    committee: str
    overlap_start: int | None
    overlap_end: int | None
    account_id: UUID


@dataclass(slots=True)
class StandardsRollup:
    """Aggregate counters for one ``cluster_standards_peers`` call."""

    pairs_found: int = 0
    pairs_inserted: int = 0
    pairs_updated: int = 0
    evidence_inserted: int = 0
    evidence_reused: int = 0
    dry_run: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)


# ── Pure planner ─────────────────────────────────────────────────────────────


def _build_query(
    committee_filter: tuple[str, str] | None,
    limit: int | None,
    *,
    allow_missing_years: bool = False,
) -> tuple[str, list]:
    """Build the planner SQL.

    Live schema (msg 216) uses ``standards_memberships(organization,
    committee, ...)`` denormalized text columns — no ``standards_committees``
    table exists, so the previous version's UUID JOIN was schema drift.
    Self-join on the (organization, committee) pair instead.
    """
    args: list = [CURRENT_YEAR]
    committee_clause = ""
    if committee_filter is not None:
        org, committee = committee_filter
        args.append(org)
        org_arg = len(args)
        args.append(committee)
        comm_arg = len(args)
        committee_clause = (
            f"AND a.organization = ${org_arg} AND a.committee = ${comm_arg} "
        )

    if allow_missing_years:
        year_join_clause = ""
        year_where_clause = ""
    else:
        year_join_clause = f"""
        AND a.start_year <= COALESCE(b.end_year, $1::int)
        AND b.start_year <= COALESCE(a.end_year, $1::int)"""
        year_where_clause = "AND a.start_year IS NOT NULL AND b.start_year IS NOT NULL"

    sql = f"""
SELECT
    LEAST(a.person_id, b.person_id)    AS person_a_id,
    GREATEST(a.person_id, b.person_id) AS person_b_id,
    a.organization,
    a.committee,
    GREATEST(a.start_year, b.start_year) AS overlap_start,
    LEAST(
        COALESCE(a.end_year, $1::int),
        COALESCE(b.end_year, $1::int)
    )                                  AS overlap_end,
    a.account_id
FROM standards_memberships a
JOIN standards_memberships b
    ON a.organization = b.organization
    AND a.committee = b.committee
    AND a.person_id < b.person_id
    AND a.account_id = b.account_id
    {year_join_clause}
WHERE TRUE
  {year_where_clause}
  {committee_clause}
ORDER BY overlap_start DESC NULLS LAST, a.organization, a.committee
"""
    if limit is not None:
        sql += f"LIMIT {int(limit)}\n"
    return sql, args


def _row_to_pair(row: asyncpg.Record) -> CommitteePair:
    return CommitteePair(
        person_a_id=row["person_a_id"],
        person_b_id=row["person_b_id"],
        organization=row["organization"],
        committee=row["committee"],
        overlap_start=int(row["overlap_start"]) if row["overlap_start"] is not None else None,
        overlap_end=int(row["overlap_end"]) if row["overlap_end"] is not None else None,
        account_id=row["account_id"],
    )


# ── Pure strength math ───────────────────────────────────────────────────────


def _compute_factors(
    last_active_year: int,
    corroboration_count: int,
    source_type_count: int = _SOURCE_TYPE_COUNT_DEFAULT,
) -> tuple[float, float, float, float, float]:
    base = STRENGTH_TABLE[CONNECTION_TYPE]
    decay = DECAY_RATES[CONNECTION_TYPE]
    years = max(0, CURRENT_YEAR - last_active_year)
    recency = math.exp(-decay * years)
    frequency = 1.0 + math.log(max(1, corroboration_count)) * 0.15
    corroboration = 1.0 + source_type_count * 0.10
    computed = min(STRENGTH_CAP, base * recency * frequency * corroboration)
    return base, recency, frequency, corroboration, computed


# ── DB write helpers ─────────────────────────────────────────────────────────


def _evidence_source_id(pair: CommitteePair) -> str:
    return f"{pair.person_a_id}:{pair.person_b_id}:{pair.organization}:{pair.committee}"


def _active_years_str(pair: CommitteePair) -> str:
    """Human-readable years range for the warm-path opener template."""
    if pair.overlap_start and pair.overlap_end:
        if pair.overlap_start == pair.overlap_end:
            return str(pair.overlap_start)
        return f"{pair.overlap_start}–{pair.overlap_end}"
    if pair.overlap_start:
        return f"{pair.overlap_start}–present"
    return "active period unknown"


def _evidence_payload(pair: CommitteePair) -> str:
    return json.dumps(
        {
            "organization": pair.organization,
            "committee": pair.committee,
            "years": _active_years_str(pair),
            "overlap_start": pair.overlap_start,
            "overlap_end": pair.overlap_end,
        },
        separators=(",", ":"),
    )


async def _find_or_create_evidence(
    conn: asyncpg.Connection,
    pair: CommitteePair,
) -> tuple[UUID, bool]:
    source_id = _evidence_source_id(pair)
    existing = await conn.fetchval(
        """
        SELECT id FROM connection_evidence
        WHERE source_type = $1 AND source_id = $2
        """,
        EVIDENCE_SOURCE_TYPE,
        source_id,
    )
    if existing is not None:
        return existing, False

    new_id = await conn.fetchval(
        """
        INSERT INTO connection_evidence (
            source_type, source_id, structured_value, account_id
        )
        VALUES ($1, $2, $3::jsonb, $4)
        RETURNING id
        """,
        EVIDENCE_SOURCE_TYPE,
        source_id,
        _evidence_payload(pair),
        pair.account_id,
    )
    return new_id, True


async def _upsert_connection(
    conn: asyncpg.Connection,
    pair: CommitteePair,
    evidence_id: UUID,
) -> bool:
    last_active = pair.overlap_end if pair.overlap_end is not None else CURRENT_YEAR

    inserted_id = await conn.fetchval(
        """
        INSERT INTO person_connections (
            person_a_id, person_b_id, connection_type, account_id,
            base_strength, recency_factor, frequency_factor, corroboration_factor,
            computed_strength, last_active_year,
            corroboration_count, source_type_count, evidence_ids
        )
        VALUES (
            $1, $2, $3, $4,
            $5, 1.0, 1.0, 1.0,
            $5, $6,
            1, $7, ARRAY[$8::uuid]
        )
        ON CONFLICT (person_a_id, person_b_id, connection_type) DO NOTHING
        RETURNING id
        """,
        pair.person_a_id,
        pair.person_b_id,
        CONNECTION_TYPE,
        pair.account_id,
        STRENGTH_TABLE[CONNECTION_TYPE],
        last_active,
        _SOURCE_TYPE_COUNT_DEFAULT,
        evidence_id,
    )
    is_new = inserted_id is not None

    row = await conn.fetchrow(
        """
        SELECT id, evidence_ids, last_active_year
        FROM person_connections
        WHERE person_a_id = $1 AND person_b_id = $2 AND connection_type = $3
        """,
        pair.person_a_id,
        pair.person_b_id,
        CONNECTION_TYPE,
    )
    if row is None:
        raise RuntimeError(
            f"person_connection vanished for {pair.person_a_id}→{pair.person_b_id}"
        )

    existing_ids: set[UUID] = set(row["evidence_ids"] or ())
    existing_ids.add(evidence_id)
    new_evidence_ids = sorted(existing_ids)
    corroboration_count = len(new_evidence_ids)
    new_last_active = max(int(row["last_active_year"] or last_active), last_active)

    base, recency, frequency, corroboration, computed = _compute_factors(
        new_last_active, corroboration_count
    )

    await conn.execute(
        """
        UPDATE person_connections SET
            evidence_ids         = $2,
            base_strength        = $3,
            recency_factor       = $4,
            frequency_factor     = $5,
            corroboration_factor = $6,
            computed_strength    = $7,
            last_active_year     = $8,
            corroboration_count  = $9,
            updated_at           = now()
        WHERE id = $1
        """,
        row["id"],
        new_evidence_ids,
        base,
        recency,
        frequency,
        corroboration,
        computed,
        new_last_active,
        corroboration_count,
    )
    return is_new


# ── Public orchestrator ──────────────────────────────────────────────────────


async def cluster_standards_peers(
    committee_filter: tuple[str, str] | None = None,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    allow_missing_years: bool = False,
) -> StandardsRollup:
    """Materialize ``standards_committee_peer`` edges into ``person_connections``.

    ``committee_filter`` is a ``(organization, committee)`` tuple — e.g.
    ``("JEDEC", "JC-42 (DRAM)")`` — or None to process every committee.
    """
    rollup = StandardsRollup(dry_run=dry_run)
    sql, args = _build_query(committee_filter, limit, allow_missing_years=allow_missing_years)

    async with acquire() as conn:
        rows = await conn.fetch(sql, *args)
    rollup.pairs_found = len(rows)

    if dry_run:
        log.info(
            "[dry-run] standards_committee_peer pairs found: %d (filter=%s, limit=%s)",
            rollup.pairs_found,
            committee_filter,
            limit,
        )
        return rollup

    pairs = [_row_to_pair(r) for r in rows]
    async with acquire() as conn:
        for pair in pairs:
            try:
                async with conn.transaction():
                    evidence_id, was_new = await _find_or_create_evidence(conn, pair)
                    if was_new:
                        rollup.evidence_inserted += 1
                    else:
                        rollup.evidence_reused += 1

                    if await _upsert_connection(conn, pair, evidence_id):
                        rollup.pairs_inserted += 1
                    else:
                        rollup.pairs_updated += 1
            except Exception as exc:
                rollup.failures.append((str(pair.person_a_id), repr(exc)))
                log.exception(
                    "standards upsert failed for %s↔%s @ %s/%s",
                    pair.person_a_id,
                    pair.person_b_id,
                    pair.organization,
                    pair.committee,
                )

    log.info(
        "standards rollup — pairs_found=%d inserted=%d updated=%d "
        "evidence[new=%d, reused=%d] failures=%d",
        rollup.pairs_found,
        rollup.pairs_inserted,
        rollup.pairs_updated,
        rollup.evidence_inserted,
        rollup.evidence_reused,
        len(rollup.failures),
    )
    return rollup


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.standards_clustering",
        description="Materialize standards_committee_peer edges into person_connections.",
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="Process every committee.")
    scope.add_argument(
        "--committee",
        type=str,
        help=(
            "Scope to a single committee, formatted as "
            "'<organization>::<committee>' "
            "(e.g. 'JEDEC::JC-42 (DRAM)'). The live schema is denormalized "
            "text, not a UUID FK."
        ),
    )
    p.add_argument("--limit", type=int, default=None, help="Cap number of pairs processed.")
    p.add_argument("--dry-run", action="store_true", help="Planner only: count pairs, write nothing.")
    p.add_argument(
        "--allow-missing-years",
        action="store_true",
        help="Also pair members who share a committee but lack year data.",
    )
    p.add_argument("--log-level", default="INFO", help="Python logging level (default INFO).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    committee_filter: tuple[str, str] | None = None
    if not args.all and args.committee:
        if "::" not in args.committee:
            log.error(
                "--committee must be formatted as '<organization>::<committee>'"
            )
            return 2
        org, committee = args.committee.split("::", 1)
        committee_filter = (org.strip(), committee.strip())

    async def _go() -> StandardsRollup:
        try:
            return await cluster_standards_peers(
                committee_filter=committee_filter,
                limit=args.limit,
                dry_run=args.dry_run,
                allow_missing_years=args.allow_missing_years,
            )
        finally:
            await close_pool()

    rollup = asyncio.run(_go())
    print(
        f"standards rollup — pairs_found={rollup.pairs_found} "
        f"inserted={rollup.pairs_inserted} updated={rollup.pairs_updated} "
        f"evidence[new={rollup.evidence_inserted}, reused={rollup.evidence_reused}] "
        f"failures={len(rollup.failures)}"
    )
    return 0 if not rollup.failures else 1


if __name__ == "__main__":
    sys.exit(main())
