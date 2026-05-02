"""Career-overlap clustering — Phase 2 Job 1 of the connection-edge sprint.

For every pair of persons who shared an employer for ≥1 overlapping year,
emit one row to ``person_connections`` keyed by connection_type and the
``person_a_id < person_b_id`` invariant (CLAUDE.md Decision 1). Three
``connection_type`` values, lifted from CLAUDE.md "Connection Priority for
YC Demo" SQL CASE expression:

- ``career_overlap_same_team`` (base 0.88)
- ``career_overlap_same_domain`` (base 0.72)
- ``career_overlap_general``     (base 0.60)

Reads from ``employment_periods`` (Phase 1.2 backfilled ``start_year`` /
``end_year`` from v2 ``career_history`` signals). Writes one
``connection_evidence`` row per (pair, company) keyed by a deterministic
``source_id`` so re-runs find-or-create rather than duplicate.

## Idempotency

The function is safe to re-run. Two write paths preserve idempotency:

1. ``connection_evidence`` — find-by-source_id first, insert only if absent.
2. ``person_connections`` — INSERT … ON CONFLICT DO NOTHING + a follow-up
   UPDATE that recomputes ``computed_strength`` from the current state.

The strength formula matches ``credence.strength.compute_strength`` —
the same formula the TypeScript sibling implements. ``base`` and
``decay_rate`` come from the canonical lookup tables; ``corroboration_count``
is derived from ``len(evidence_ids)`` so it never drifts even if the row
was previously updated by a different ingestion path.

## Tenancy

``account_id`` for each connection + evidence row is read from the
``employment_periods`` row. The CTE constrains ``a.account_id = b.account_id``
so we can't accidentally form cross-tenant edges.

## CLI

::

    uv run python -m credence.jobs.career_overlap_clustering --dry-run
    uv run python -m credence.jobs.career_overlap_clustering --company-id <uuid>
    uv run python -m credence.jobs.career_overlap_clustering --all
    uv run python -m credence.jobs.career_overlap_clustering --limit 100
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from ..db import acquire, close_pool
from ..strength import DECAY_RATES, STRENGTH_CAP, STRENGTH_TABLE

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


SAME_TEAM = "career_overlap_same_team"
SAME_DOMAIN = "career_overlap_same_domain"
GENERAL = "career_overlap_general"

# All three are present in BOTH person_connections type CHECK constraints
# (the redundant ``_type_valid`` covers them; the cohort kinds are flagged
# blocked elsewhere).
EMITTED_TYPES: frozenset[str] = frozenset({SAME_TEAM, SAME_DOMAIN, GENERAL})

EVIDENCE_SOURCE_TYPE = "employment_overlap"

# Treat all "currently employed" rows as ending in the current year for the
# overlap math. Lining up with CLAUDE.md SQL which uses 2025; we bump to the
# session date because newer migrations push CURRENT_YEAR forward.
CURRENT_YEAR = 2026

# source_type_count = 1 for this job (a single extractor source). Connection
# strength rises only when *additional* extractors corroborate the same pair.
_SOURCE_TYPE_COUNT_DEFAULT = 1


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OverlapPair:
    """One overlapping employment pair as returned by the planner SQL.

    ``overlap_start`` and ``overlap_end`` may be ``None`` when
    ``allow_missing_years`` mode is on and at least one row's year data is
    NULL — in that case ``overlap_years`` is 0 and the strength formula uses
    ``CURRENT_YEAR`` as the fallback ``last_active_year``.
    """

    person_a_id: UUID
    person_b_id: UUID
    company_id: UUID
    company_name: str
    connection_type: str
    overlap_start: int | None
    overlap_end: int | None
    overlap_years: int
    seniority_gap: int | None
    team_a: str | None
    team_b: str | None
    domain_a: str | None
    domain_b: str | None
    account_id: UUID


@dataclass(slots=True)
class ClusterRollup:
    """Aggregate counters for one ``cluster_career_overlaps`` call."""

    pairs_found: int = 0
    pairs_inserted: int = 0
    pairs_updated: int = 0
    evidence_inserted: int = 0
    evidence_reused: int = 0
    dry_run: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)


# ── Pure planner: build the SQL + parse rows ────────────────────────────────


# CLAUDE.md "Connection Priority for YC Demo" — verbatim with three
# parameterizations: ``$1`` is the current year sentinel for COALESCE on
# end_year, ``$2`` (optional) is the company filter, ``$3`` (optional) is the
# limit. The CASE expression converts the three boolean partitions into the
# canonical ``connection_type`` string.
def _build_query(
    company_id: UUID | None,
    limit: int | None,
    *,
    allow_missing_years: bool = False,
    skip_current_pairs: bool = True,
    require_source_prospect: bool = True,
) -> tuple[str, list[Any]]:
    """Build the planner SQL.

    Default mode is the CLAUDE.md "Connection Priority for YC Demo" SQL:
    requires both start_years to be non-NULL and applies the date-overlap
    predicates.

    ``allow_missing_years=True`` drops the year filters so past employments
    without parsed years can still produce edges. The fallback edges land as
    ``career_overlap_general`` (the weakest of the three kinds) since we can't
    promote them to same-team / same-domain without temporal evidence. Useful
    for sparse data where most ``employment_periods.start_year`` is NULL.

    ``skip_current_pairs=True`` (default) excludes pairs where BOTH rows are
    ``is_current=TRUE`` — those are the "everyone at $BIGCO right now" cases
    that explode pair counts and aren't meaningfully warm by themselves.
    Past↔current and past↔past still pass.
    """
    args: list[Any] = [CURRENT_YEAR]
    company_clause = ""
    if company_id is not None:
        args.append(company_id)
        company_clause = f"AND a.company_id = ${len(args)} "

    # Year filter — strict by default, can be lifted for sparse-data mode.
    if allow_missing_years:
        year_join_clause = ""
        year_where_clause = ""
        overlap_years_floor = 0  # accept zero-year overlaps when years unknown
    else:
        year_join_clause = (
            "AND a.start_year <= COALESCE(b.end_year, $1::int)\n"
            "        AND b.start_year <= COALESCE(a.end_year, $1::int)"
        )
        year_where_clause = (
            "AND a.start_year IS NOT NULL\n      AND b.start_year IS NOT NULL"
        )
        overlap_years_floor = 1

    current_pair_clause = (
        "AND NOT (a.is_current AND b.is_current)" if skip_current_pairs else ""
    )

    # Both endpoints must trace back to a v2 prospects row, otherwise the
    # frontend can't render the edge (graph nodes are keyed on prospect_id).
    # Persons created from career_history role entries without a linkedin_url
    # are placeholders with `source_prospect_id IS NULL`. Filter them out to
    # avoid materializing edges that the UI would silently drop. Per
    # SunnyRidge msg 190 — Option A.
    person_join_clause = ""
    source_prospect_where = ""
    if require_source_prospect:
        person_join_clause = (
            "JOIN persons pa ON pa.id = a.person_id "
            "AND pa.source_prospect_id IS NOT NULL\n    "
            "JOIN persons pb ON pb.id = b.person_id "
            "AND pb.source_prospect_id IS NOT NULL"
        )

    sql = f"""
WITH overlapping_pairs AS (
    SELECT
        LEAST(a.person_id, b.person_id)    AS person_a_id,
        GREATEST(a.person_id, b.person_id) AS person_b_id,
        a.company_id,
        c.canonical_name AS company_name,
        GREATEST(a.start_year, b.start_year) AS overlap_start,
        LEAST(COALESCE(a.end_year, $1::int), COALESCE(b.end_year, $1::int)) AS overlap_end,
        COALESCE(
            LEAST(COALESCE(a.end_year, $1::int), COALESCE(b.end_year, $1::int))
                - GREATEST(a.start_year, b.start_year),
            0
        ) AS overlap_years,
        a.inferred_team AS team_a,
        b.inferred_team AS team_b,
        a.functional_domain AS domain_a,
        b.functional_domain AS domain_b,
        ABS(a.seniority_score - b.seniority_score) AS seniority_gap,
        a.account_id AS account_id,
        (a.start_year IS NULL OR b.start_year IS NULL) AS years_unknown
    FROM employment_periods a
    JOIN employment_periods b
        ON b.company_id = a.company_id
        AND a.person_id < b.person_id
        {year_join_clause}
        AND a.account_id = b.account_id
        {current_pair_clause}
    JOIN companies c ON c.id = a.company_id
    {person_join_clause}
    WHERE TRUE
      {year_where_clause}
      {company_clause}
)
SELECT *,
    CASE
        -- Functional similarity wins when present, even without temporal
        -- evidence: shared team / domain is independent of overlap years.
        WHEN team_a IS NOT NULL AND team_a = team_b
            THEN '{SAME_TEAM}'
        WHEN domain_a IS NOT NULL AND domain_a = domain_b
             AND seniority_gap IS NOT NULL AND seniority_gap < 10
            THEN '{SAME_DOMAIN}'
        ELSE '{GENERAL}'
    END AS connection_type
FROM overlapping_pairs
WHERE overlap_years >= {overlap_years_floor}
ORDER BY overlap_years DESC
"""
    if limit is not None:
        sql += f"LIMIT {int(limit)}\n"
    return sql, args


def _row_to_pair(row: asyncpg.Record) -> OverlapPair:
    return OverlapPair(
        person_a_id=row["person_a_id"],
        person_b_id=row["person_b_id"],
        company_id=row["company_id"],
        company_name=row["company_name"],
        connection_type=row["connection_type"],
        overlap_start=int(row["overlap_start"]) if row["overlap_start"] is not None else None,
        overlap_end=int(row["overlap_end"]) if row["overlap_end"] is not None else None,
        overlap_years=int(row["overlap_years"] or 0),
        seniority_gap=int(row["seniority_gap"]) if row["seniority_gap"] is not None else None,
        team_a=row["team_a"],
        team_b=row["team_b"],
        domain_a=row["domain_a"],
        domain_b=row["domain_b"],
        account_id=row["account_id"],
    )


# ── Pure strength math ───────────────────────────────────────────────────────


def _compute_factors(
    connection_type: str,
    last_active_year: int,
    corroboration_count: int,
    source_type_count: int = _SOURCE_TYPE_COUNT_DEFAULT,
) -> tuple[float, float, float, float, float]:
    """Return (base, recency, frequency, corroboration, computed) factors.

    Mirrors ``credence.strength.compute_strength`` so the field-level breakdown
    in person_connections matches the canonical formula. Kept as a pure
    function so a unit test can pin the math without DB I/O.
    """
    base = STRENGTH_TABLE[connection_type]
    decay = DECAY_RATES[connection_type]
    years = max(0, CURRENT_YEAR - last_active_year)
    recency = math.exp(-decay * years)
    frequency = 1.0 + math.log(max(1, corroboration_count)) * 0.15
    corroboration = 1.0 + source_type_count * 0.10
    computed = min(STRENGTH_CAP, base * recency * frequency * corroboration)
    return base, recency, frequency, corroboration, computed


# ── DB write helpers ─────────────────────────────────────────────────────────


def _evidence_source_id(pair: OverlapPair) -> str:
    """Deterministic key — same pair + company always points at the same row."""
    return f"{pair.person_a_id}:{pair.person_b_id}:{pair.company_id}"


def _evidence_payload(pair: OverlapPair) -> str:
    return json.dumps(
        {
            "company_id": str(pair.company_id),
            "company_name": pair.company_name,
            "overlap_start": pair.overlap_start,
            "overlap_end": pair.overlap_end,
            "overlap_years": pair.overlap_years,
            "team_a": pair.team_a,
            "team_b": pair.team_b,
            "domain_a": pair.domain_a,
            "domain_b": pair.domain_b,
            "seniority_gap": pair.seniority_gap,
        },
        separators=(",", ":"),
    )


async def _find_or_create_evidence(
    conn: asyncpg.Connection,
    pair: OverlapPair,
) -> tuple[UUID, bool]:
    """Return (evidence_id, was_inserted)."""
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
    pair: OverlapPair,
    evidence_id: UUID,
) -> bool:
    """Insert person_connections row or refresh derived fields. Returns True if newly inserted."""
    # Step 1: try to insert the row with placeholder factors. The ON CONFLICT
    # branch is a no-op so we know whether we hit a fresh row.
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
        pair.connection_type,
        pair.account_id,
        STRENGTH_TABLE[pair.connection_type],  # placeholder; recomputed in step 3
        pair.overlap_end if pair.overlap_end is not None else CURRENT_YEAR,
        _SOURCE_TYPE_COUNT_DEFAULT,
        evidence_id,
    )
    is_new = inserted_id is not None

    # Step 2: load whichever row we have now (could be the one we just inserted
    # or a pre-existing one).
    row = await conn.fetchrow(
        """
        SELECT id, evidence_ids, last_active_year, corroboration_count
        FROM person_connections
        WHERE person_a_id = $1 AND person_b_id = $2 AND connection_type = $3
        """,
        pair.person_a_id,
        pair.person_b_id,
        pair.connection_type,
    )
    if row is None:
        # Race we shouldn't see in practice — surface loudly.
        raise RuntimeError(
            f"person_connection vanished for {pair.person_a_id}→{pair.person_b_id}"
        )

    # Step 3: union the evidence_ids, recompute derived fields.
    existing_ids: set[UUID] = set(row["evidence_ids"] or ())
    existing_ids.add(evidence_id)
    new_evidence_ids = sorted(existing_ids)
    corroboration_count = len(new_evidence_ids)
    pair_last_active = pair.overlap_end if pair.overlap_end is not None else CURRENT_YEAR
    new_last_active = max(int(row["last_active_year"] or pair_last_active), pair_last_active)

    base, recency, frequency, corroboration, computed = _compute_factors(
        pair.connection_type,
        new_last_active,
        corroboration_count,
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


async def cluster_career_overlaps(
    company_id: UUID | None = None,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    allow_missing_years: bool = False,
    skip_current_pairs: bool = True,
    require_source_prospect: bool = True,
    db_concurrency: int = 8,
) -> ClusterRollup:
    """Materialize career-overlap edges into ``person_connections``.

    Args:
        company_id: scope to one company; ``None`` means all companies.
        limit: cap the number of pairs processed (after the SQL ORDER BY
            overlap_years DESC, so the longest overlaps land first).
        dry_run: planner-only — no rows written, summary still returned.
        allow_missing_years: if True, also emit ``career_overlap_general`` edges
            for past pairs that share a company but lack year data. Use when the
            extractor pass hasn't populated start_year/end_year yet.
        skip_current_pairs: if True (default), skip pairs where both rows are
            ``is_current=TRUE`` (excludes the "everyone at $BIGCO" explosion).
    """
    rollup = ClusterRollup(dry_run=dry_run)
    sql, args = _build_query(
        company_id, limit,
        allow_missing_years=allow_missing_years,
        skip_current_pairs=skip_current_pairs,
        require_source_prospect=require_source_prospect,
    )

    async with acquire() as conn:
        rows = await conn.fetch(sql, *args)
    rollup.pairs_found = len(rows)
    if dry_run:
        log.info(
            "[dry-run] career_overlap pairs found: %d (company=%s, limit=%s)",
            rollup.pairs_found,
            company_id,
            limit,
        )
        return rollup

    pairs = [_row_to_pair(r) for r in rows]

    # Bounded concurrent pair processing — each task acquires its own pool
    # connection so transactions don't serialize. Semaphore caps concurrency
    # to keep us under Supabase's ~15-conn pool ceiling. Default 8 leaves
    # headroom for other queries (apify recovery / backfill_v3 / Tier-1
    # enrichment may run in parallel — see msg 229 db-coexistence ask).
    # Operators can override via ``--db-concurrency`` to ease/loosen pressure.
    if db_concurrency < 1:
        raise ValueError(f"db_concurrency must be >= 1, got {db_concurrency}")
    sem = asyncio.Semaphore(db_concurrency)
    counter_lock = asyncio.Lock()  # protects rollup mutation across tasks

    async def _process_one_pair(pair: OverlapPair) -> None:
        async with sem:
            try:
                async with acquire() as conn:
                    async with conn.transaction():
                        evidence_id, evidence_was_new = await _find_or_create_evidence(
                            conn, pair
                        )
                        is_new = await _upsert_connection(conn, pair, evidence_id)
                async with counter_lock:
                    if evidence_was_new:
                        rollup.evidence_inserted += 1
                    else:
                        rollup.evidence_reused += 1
                    if is_new:
                        rollup.pairs_inserted += 1
                    else:
                        rollup.pairs_updated += 1
            except Exception as exc:
                async with counter_lock:
                    rollup.failures.append((str(pair.person_a_id), repr(exc)))
                log.exception(
                    "career_overlap upsert failed for %s↔%s @ %s",
                    pair.person_a_id, pair.person_b_id, pair.company_id,
                )

    # Process pairs in batches to bound peak memory + give us periodic
    # log updates on long runs (51k candidates would otherwise be one
    # silent gather call). Batch size 500 = ~5min of work at ~2 pairs/s
    # per worker × 8 workers.
    BATCH = 500
    for i in range(0, len(pairs), BATCH):
        batch = pairs[i : i + BATCH]
        await asyncio.gather(*(_process_one_pair(p) for p in batch))
        log.info(
            "career_overlap progress — %d/%d pairs processed (inserted=%d updated=%d)",
            min(i + BATCH, len(pairs)),
            len(pairs),
            rollup.pairs_inserted,
            rollup.pairs_updated,
        )

    log.info(
        "career_overlap rollup — pairs_found=%d inserted=%d updated=%d "
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
        prog="credence.jobs.career_overlap_clustering",
        description="Materialize career-overlap edges into person_connections.",
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="Process every company.")
    scope.add_argument(
        "--company-id",
        type=UUID,
        help="Scope to a single companies.id UUID.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap number of pairs processed (after ordering by overlap_years DESC).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Planner only: count pairs, write nothing.",
    )
    p.add_argument(
        "--allow-missing-years",
        action="store_true",
        help=(
            "Also emit career_overlap_general for past pairs that share a "
            "company but lack year data. Use when extractor pass is incomplete."
        ),
    )
    p.add_argument(
        "--include-current-current-pairs",
        action="store_true",
        help=(
            "Include pairs where both rows are is_current=TRUE. Off by default "
            "(produces N²/2 'everyone at $BIGCO right now' edges that aren't "
            "meaningfully warm)."
        ),
    )
    p.add_argument(
        "--include-placeholder-persons",
        action="store_true",
        help=(
            "Include persons whose source_prospect_id IS NULL (past-employer "
            "placeholder rows from career_history). Off by default — those edges "
            "won't render in the UI since the graph is keyed on prospect_id."
        ),
    )
    p.add_argument(
        "--db-concurrency",
        type=int,
        default=8,
        help=(
            "Max concurrent pair-processing tasks (each holds one pool conn). "
            "Default 8. Lower (e.g. 4) when sharing the Supabase pool with "
            "other heavy writers (apify ingestion, recovery)."
        ),
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    company_id = None if args.all else args.company_id

    async def _go() -> ClusterRollup:
        try:
            return await cluster_career_overlaps(
                company_id=company_id,
                limit=args.limit,
                dry_run=args.dry_run,
                allow_missing_years=args.allow_missing_years,
                skip_current_pairs=not args.include_current_current_pairs,
                require_source_prospect=not args.include_placeholder_persons,
                db_concurrency=args.db_concurrency,
            )
        finally:
            await close_pool()

    rollup = asyncio.run(_go())
    print(
        f"career_overlap rollup — pairs_found={rollup.pairs_found} "
        f"inserted={rollup.pairs_inserted} updated={rollup.pairs_updated} "
        f"evidence[new={rollup.evidence_inserted}, reused={rollup.evidence_reused}] "
        f"failures={len(rollup.failures)}"
    )
    return 0 if not rollup.failures else 1


if __name__ == "__main__":
    sys.exit(main())
