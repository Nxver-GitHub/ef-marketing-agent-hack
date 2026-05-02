"""Education cohort clustering — Phase 2 Job 3 of the connection-edge sprint.

For every pair of persons who attended the same institution in overlapping
years at the same degree level, emit an education-cohort edge into
``person_connections``. The specific ``connection_type`` depends on degree:

- ``same_mba_cohort``        (base 0.85) — MBA / M.B.A. / Master of Business
- ``same_phd_program``       (base 0.78) — PhD / Ph.D. / Doctorate
- ``executive_education``    (base 0.70) — exec ed / certificate program
- ``same_undergrad_cohort``  (base 0.62) — BS / BA / BEng / BASc or unknown

Degree classification is applied per-row by ``_classify_degree`` using a
simple regex ruleset ordered most-specific first. Ties default to undergrad.

## Data source

Reads from the v3 ``education_periods`` table populated by the PDL enrichment
pipeline. Self-joins on (institution_id, degree_level) with an optional year
overlap filter (the same ``allow_missing_years`` escape hatch as career
overlap). Also reads ``education_overlaps`` when that table exists — it stores
pre-computed overlap metadata from the B3 extractor, but the job degrades
gracefully to a self-join if the table is empty.

## Overlap semantics

``education_periods.start_year``/``end_year`` carry graduation cohort years.
Two persons overlap in the same cohort if:
    GREATEST(a.start_year, b.start_year) <= LEAST(
        COALESCE(a.end_year, a.start_year + 4),
        COALESCE(b.end_year, b.start_year + 4)
    )

A 4-year window default is used when end_year is NULL so we don't silently
drop every record that only has a graduation year.

## Idempotency

Same find-or-create + UPDATE pattern as the other clustering jobs.

## Tenancy

``account_id`` is read from ``education_periods``; the planner SQL constrains
``a.account_id = b.account_id``.

## CLI

::

    uv run python -m credence.jobs.education_cohort_clustering --dry-run
    uv run python -m credence.jobs.education_cohort_clustering --institution-id <uuid>
    uv run python -m credence.jobs.education_cohort_clustering --all
    uv run python -m credence.jobs.education_cohort_clustering --limit 200
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import re
import sys
from dataclasses import dataclass, field
from uuid import UUID

import asyncpg

from ..db import acquire, close_pool
from ..strength import DECAY_RATES, STRENGTH_CAP, STRENGTH_TABLE

log = logging.getLogger(__name__)


# ── Degree classification ────────────────────────────────────────────────────
#
# Maps raw degree strings to connection_type keys. Rules ordered most-specific
# first so "MBA" wins over "Master" and "PhD" wins over "Doctoral".

_DEGREE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(mba|m\.b\.a|master of business)\b", re.I), "same_mba_cohort"),
    (re.compile(r"\b(ph\.?d|d\.phil|doctorate|doctoral)\b", re.I), "same_phd_program"),
    (re.compile(r"\b(exec(utive)?\s+ed(ucation)?|certificate|executive\s+program|pgp)\b", re.I), "executive_education"),
    # BSc / BA / BEng / BASc / Bachelor's / any remaining degree → undergrad
]

_UNDERGRAD_CONNECTION_TYPE = "same_undergrad_cohort"

# MBA and PhD are the strong cohort signals; executive ed and undergrad are
# weaker because cohort sizes are much larger. All four map to warm edges.
EMITTED_TYPES: frozenset[str] = frozenset({
    "same_mba_cohort",
    "same_phd_program",
    "executive_education",
    "same_undergrad_cohort",
})

EVIDENCE_SOURCE_TYPE = "education_overlap"
CURRENT_YEAR = 2026
_DEFAULT_DEGREE_YEARS = 4   # fallback window when end_year is NULL
_SOURCE_TYPE_COUNT_DEFAULT = 1


def classify_degree(degree: str | None) -> str:
    """Map a raw degree string to a connection_type key.

    Returns ``same_undergrad_cohort`` for None, empty, or unrecognized values.
    """
    if not degree:
        return _UNDERGRAD_CONNECTION_TYPE
    for pattern, conn_type in _DEGREE_RULES:
        if pattern.search(degree):
            return conn_type
    return _UNDERGRAD_CONNECTION_TYPE


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CohortPair:
    """One education-cohort pair as returned by the planner SQL."""

    person_a_id: UUID
    person_b_id: UUID
    institution_id: str  # NB: holds school_canonical_name (text), not UUID — see _build_query
    institution_name: str | None
    program: str | None
    connection_type: str
    overlap_start_year: int | None
    overlap_end_year: int | None
    account_id: UUID


@dataclass(slots=True)
class EducationRollup:
    """Aggregate counters for one ``cluster_education_cohorts`` call."""

    pairs_found: int = 0
    pairs_inserted: int = 0
    pairs_updated: int = 0
    evidence_inserted: int = 0
    evidence_reused: int = 0
    dry_run: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)


# ── Pure planner ─────────────────────────────────────────────────────────────


def _build_query(
    institution_id: str | None,
    limit: int | None,
    *,
    allow_missing_years: bool = False,
) -> tuple[str, list]:
    """Build the cohort planner SQL.

    Note: ``education_periods`` is keyed on ``school_canonical_name`` (text),
    not a foreign-key ``institution_id``. The argname is preserved for CLI
    backwards-compat but the value is treated as the school's canonical name.
    The ``institutions`` table only carries 30 seed rows so we don't JOIN it
    — we use ``school_canonical_name`` as the institution display value.

    Degree-level filtering is left to ``classify_degree`` post-fetch instead
    of an in-SQL filter (the schema lacks a ``degree_level`` column, and
    classifier logic is identical anyway). Mismatched-degree pairs at the
    same school still produce a connection_type via person_a's degree.
    """
    args: list = [_DEFAULT_DEGREE_YEARS]
    inst_clause = ""
    if institution_id is not None:
        args.append(institution_id)
        inst_clause = f"AND a.school_canonical_name = ${len(args)} "

    if allow_missing_years:
        year_join_clause = ""
        year_where_clause = ""
    else:
        year_join_clause = f"""
        AND GREATEST(a.start_year, b.start_year)
              <= LEAST(
                    COALESCE(a.end_year, a.start_year + $1),
                    COALESCE(b.end_year, b.start_year + $1)
                 )"""
        year_where_clause = "AND a.start_year IS NOT NULL AND b.start_year IS NOT NULL"

    sql = f"""
SELECT
    LEAST(a.person_id, b.person_id)    AS person_a_id,
    GREATEST(a.person_id, b.person_id) AS person_b_id,
    a.school_canonical_name           AS institution_id,
    a.school_canonical_name           AS institution_name,
    CASE WHEN a.person_id < b.person_id THEN a.degree ELSE b.degree END AS program,
    GREATEST(a.start_year, b.start_year)  AS overlap_start_year,
    LEAST(
        COALESCE(a.end_year, a.start_year + $1),
        COALESCE(b.end_year, b.start_year + $1)
    )                                     AS overlap_end_year,
    a.account_id
FROM education_periods a
JOIN education_periods b
    ON a.school_canonical_name = b.school_canonical_name
    AND a.person_id < b.person_id
    AND a.account_id = b.account_id
    {year_join_clause}
WHERE TRUE
  {year_where_clause}
  {inst_clause}
ORDER BY COALESCE(GREATEST(a.start_year, b.start_year), 0) DESC, a.school_canonical_name
"""
    if limit is not None:
        sql += f"LIMIT {int(limit)}\n"
    return sql, args


def _row_to_pair(row: asyncpg.Record) -> CohortPair:
    program = row["program"]
    connection_type = classify_degree(program)
    return CohortPair(
        person_a_id=row["person_a_id"],
        person_b_id=row["person_b_id"],
        institution_id=row["institution_id"],
        institution_name=row["institution_name"],
        program=program,
        connection_type=connection_type,
        overlap_start_year=int(row["overlap_start_year"]) if row["overlap_start_year"] is not None else None,
        overlap_end_year=int(row["overlap_end_year"]) if row["overlap_end_year"] is not None else None,
        account_id=row["account_id"],
    )


# ── Pure strength math ───────────────────────────────────────────────────────


def _compute_factors(
    connection_type: str,
    last_active_year: int,
    corroboration_count: int,
    source_type_count: int = _SOURCE_TYPE_COUNT_DEFAULT,
) -> tuple[float, float, float, float, float]:
    """Return (base, recency, frequency, corroboration, computed)."""
    base = STRENGTH_TABLE[connection_type]
    decay = DECAY_RATES[connection_type]
    years = max(0, CURRENT_YEAR - last_active_year)
    recency = math.exp(-decay * years)
    frequency = 1.0 + math.log(max(1, corroboration_count)) * 0.15
    corroboration = 1.0 + source_type_count * 0.10
    computed = min(STRENGTH_CAP, base * recency * frequency * corroboration)
    return base, recency, frequency, corroboration, computed


# ── DB write helpers ─────────────────────────────────────────────────────────


def _evidence_source_id(pair: CohortPair) -> str:
    """Deterministic key — same pair + institution + type always maps to one row."""
    return f"{pair.person_a_id}:{pair.person_b_id}:{pair.institution_id}:{pair.connection_type}"


def _evidence_payload(pair: CohortPair) -> str:
    return json.dumps(
        {
            "institution_id": str(pair.institution_id),
            "institution": pair.institution_name,
            "program": pair.program,
            "overlap_start_year": pair.overlap_start_year,
            "overlap_end_year": pair.overlap_end_year,
        },
        separators=(",", ":"),
    )


async def _find_or_create_evidence(
    conn: asyncpg.Connection,
    pair: CohortPair,
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
    pair: CohortPair,
    evidence_id: UUID,
) -> bool:
    """Insert or refresh one person_connections row. Returns True if newly inserted."""
    last_active = pair.overlap_end_year if pair.overlap_end_year is not None else CURRENT_YEAR

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
        STRENGTH_TABLE[pair.connection_type],
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
        pair.connection_type,
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
        pair.connection_type, new_last_active, corroboration_count
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


async def cluster_education_cohorts(
    institution_id: str | None = None,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    allow_missing_years: bool = False,
    db_concurrency: int = 1,
) -> EducationRollup:
    """Materialize education-cohort edges into ``person_connections``.

    Args:
        institution_id: scope to one institution; ``None`` means all.
        limit: cap number of pairs (after ordering by overlap_start_year DESC).
        dry_run: planner-only — no rows written, summary still returned.
        allow_missing_years: if True, also pair persons who share an institution
            regardless of year data. Produces weaker ``same_undergrad_cohort``
            edges only (no cohort year evidence).
        db_concurrency: max concurrent UPSERT tasks. Default 1 keeps the legacy
            sequential single-connection behavior so re-runs don't change
            performance characteristics. Bump to 4–8 to ease pool sharing
            with other writers (msg 229).
    """
    if db_concurrency < 1:
        raise ValueError(f"db_concurrency must be >= 1, got {db_concurrency}")
    rollup = EducationRollup(dry_run=dry_run)
    sql, args = _build_query(institution_id, limit, allow_missing_years=allow_missing_years)

    async with acquire() as conn:
        rows = await conn.fetch(sql, *args)
    rollup.pairs_found = len(rows)

    if dry_run:
        log.info(
            "[dry-run] education cohort pairs found: %d (institution=%s, limit=%s)",
            rollup.pairs_found,
            institution_id,
            limit,
        )
        return rollup

    pairs = [_row_to_pair(r) for r in rows]

    if db_concurrency == 1:
        # Legacy single-connection sequential path — preserved bit-for-bit so
        # the established msg 218 cohort numbers reproduce exactly.
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
                        "education cohort upsert failed for %s↔%s @ institution %s",
                        pair.person_a_id,
                        pair.person_b_id,
                        pair.institution_id,
                    )
    else:
        # Concurrent path — same pattern as career_overlap_clustering. One
        # pool conn per task; semaphore caps total in-flight; counter_lock
        # serializes rollup mutation across tasks.
        sem = asyncio.Semaphore(db_concurrency)
        counter_lock = asyncio.Lock()

        async def _process_one_pair(pair: CohortPair) -> None:
            async with sem:
                try:
                    async with acquire() as conn:
                        async with conn.transaction():
                            evidence_id, was_new = await _find_or_create_evidence(
                                conn, pair
                            )
                            is_new = await _upsert_connection(conn, pair, evidence_id)
                    async with counter_lock:
                        if was_new:
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
                        "education cohort upsert failed for %s↔%s @ institution %s",
                        pair.person_a_id,
                        pair.person_b_id,
                        pair.institution_id,
                    )

        BATCH = 500
        for i in range(0, len(pairs), BATCH):
            batch = pairs[i : i + BATCH]
            await asyncio.gather(*(_process_one_pair(p) for p in batch))

    log.info(
        "education cohort rollup — pairs_found=%d inserted=%d updated=%d "
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
        prog="credence.jobs.education_cohort_clustering",
        description="Materialize education-cohort edges into person_connections.",
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="Process every institution.")
    scope.add_argument(
        "--institution-id",
        type=str,
        help=(
            "Scope to a single school by canonical name "
            "(NB: education_periods is keyed on text not UUID)."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap number of pairs processed.",
    )
    p.add_argument("--dry-run", action="store_true", help="Planner only: count pairs, write nothing.")
    p.add_argument(
        "--allow-missing-years",
        action="store_true",
        help=(
            "Also pair persons who share an institution but lack year data. "
            "Produces same_undergrad_cohort edges without cohort year evidence."
        ),
    )
    p.add_argument(
        "--db-concurrency",
        type=int,
        default=1,
        help=(
            "Max concurrent UPSERT tasks. Default 1 keeps the legacy "
            "sequential single-connection behavior. Bump to 4–8 to ease "
            "pool sharing with other heavy writers."
        ),
    )
    p.add_argument("--log-level", default="INFO", help="Python logging level (default INFO).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    institution_id = None if args.all else args.institution_id

    async def _go() -> EducationRollup:
        try:
            return await cluster_education_cohorts(
                institution_id=institution_id,
                limit=args.limit,
                dry_run=args.dry_run,
                allow_missing_years=args.allow_missing_years,
                db_concurrency=args.db_concurrency,
            )
        finally:
            await close_pool()

    rollup = asyncio.run(_go())
    print(
        f"education cohort rollup — pairs_found={rollup.pairs_found} "
        f"inserted={rollup.pairs_inserted} updated={rollup.pairs_updated} "
        f"evidence[new={rollup.evidence_inserted}, reused={rollup.evidence_reused}] "
        f"failures={len(rollup.failures)}"
    )
    return 0 if not rollup.failures else 1


if __name__ == "__main__":
    sys.exit(main())
