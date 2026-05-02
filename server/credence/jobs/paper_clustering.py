"""Academic co-author clustering — v3 person_connections writer.

For every pair of persons who co-authored one or more papers in the
``papers`` / ``paper_authors`` tables, emit one ``academic_co_author_multi``
or ``academic_co_author_single`` edge into ``person_connections``.

Connection type is determined by shared-paper count per pair:
  - ≥ 3 shared papers → ``academic_co_author_multi``  (base 0.90)
  - 1–2 shared papers → ``academic_co_author_single`` (base 0.85)

## Data source

Reads from the v3 ``papers`` + ``paper_authors`` tables populated by
``bulk_scholar_ingest.py`` (which writes Semantic Scholar data into
``paper_authors`` via an intermediate ``papers`` upsert).

## Evidence

One ``connection_evidence`` row per (pair, paper), keyed by
``"{person_a_id}:{person_b_id}:{semantic_scholar_id}"``. The highest-cited
shared paper is always the first in evidence_ids[0] on the connection row.

## Idempotency

Same find-or-create + UPDATE pattern as the other clustering jobs. Re-running
is a no-op except for ``updated_at`` bumps.

## Tenancy

``account_id`` from ``paper_authors``; planner SQL constrains
``a.account_id = b.account_id``.

## CLI

::

    uv run python -m credence.jobs.paper_clustering --dry-run
    uv run python -m credence.jobs.paper_clustering --paper-id <uuid>
    uv run python -m credence.jobs.paper_clustering --all
    uv run python -m credence.jobs.paper_clustering --limit 500
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
import uuid
from uuid import UUID

import asyncpg

from ..db import acquire, close_pool
from ..strength import DECAY_RATES, STRENGTH_CAP, STRENGTH_TABLE

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


EVIDENCE_SOURCE_TYPE = "semantic_scholar"
CURRENT_YEAR = 2026
_SOURCE_TYPE_COUNT_DEFAULT = 1

# Threshold: ≥ MULTI_THRESHOLD shared papers → academic_co_author_multi
MULTI_THRESHOLD = 3


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PaperRow:
    """One co-authorship row from the planner SQL."""

    person_a_id: UUID
    person_b_id: UUID
    paper_id: UUID
    semantic_scholar_id: str | None
    title: str | None
    venue: str | None
    year: int | None
    citation_count: int
    account_id: UUID


@dataclass(slots=True)
class PaperPair:
    """Aggregated view of all shared papers for a (person_a, person_b) pair."""

    person_a_id: UUID
    person_b_id: UUID
    account_id: UUID
    papers: list[PaperRow] = field(default_factory=list)

    @property
    def connection_type(self) -> str:
        return (
            "academic_co_author_multi"
            if len(self.papers) >= MULTI_THRESHOLD
            else "academic_co_author_single"
        )

    @property
    def last_active_year(self) -> int:
        years = [p.year for p in self.papers if p.year]
        return max(years) if years else CURRENT_YEAR

    def papers_sorted_by_citations(self) -> list[PaperRow]:
        """Highest-cited paper first — used to order evidence_ids."""
        return sorted(self.papers, key=lambda p: p.citation_count, reverse=True)


@dataclass(slots=True)
class PaperRollup:
    """Aggregate counters for one ``cluster_paper_co_authors`` call."""

    pairs_found: int = 0
    pairs_inserted: int = 0
    pairs_updated: int = 0
    evidence_inserted: int = 0
    evidence_reused: int = 0
    dry_run: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)


# ── Pure planner ─────────────────────────────────────────────────────────────


def _build_query(
    paper_id: UUID | None,
    limit: int | None,
) -> tuple[str, list]:
    args: list = []
    paper_clause = ""
    if paper_id is not None:
        args.append(paper_id)
        paper_clause = f"AND a.paper_id = ${len(args)} "

    sql = f"""
SELECT
    LEAST(a.person_id, b.person_id)     AS person_a_id,
    GREATEST(a.person_id, b.person_id)  AS person_b_id,
    p.id                                AS paper_id,
    p.semantic_scholar_id,
    p.title,
    p.venue,
    p.year,
    COALESCE(p.citation_count, 0)       AS citation_count,
    a.account_id
FROM paper_authors a
JOIN paper_authors b
    ON a.paper_id = b.paper_id
    AND a.person_id < b.person_id
    AND a.account_id = b.account_id
    {paper_clause}
JOIN papers p ON p.id = a.paper_id
ORDER BY p.year DESC NULLS LAST, p.citation_count DESC, a.paper_id
"""
    if limit is not None:
        sql += f"LIMIT {int(limit)}\n"
    return sql, args


# Deterministic namespace for synthesizing UUIDs from semantic_scholar_id
# strings. The signals path doesn't have a row in the v3 `papers` table to
# borrow an `id` from, but the rest of the clustering pipeline keys on
# ``PaperRow.paper_id: UUID``. Use uuid5 so the same ssid always maps to
# the same synthetic UUID — keeps `_evidence_source_id` stable across re-runs.
_SSID_UUID_NAMESPACE = uuid.UUID("c2c5b9a4-8c01-4b2f-9e2a-1a1b1c1d1e1f")


def _synthetic_paper_id(ssid: str) -> UUID:
    """Deterministic UUID for a semantic_scholar_id string."""
    return uuid.uuid5(_SSID_UUID_NAMESPACE, ssid)


# v2 → v3 pivot: read existing ``signals`` rows (signal_type='academic_co_author')
# and resolve both endpoints to ``persons.id`` via ``source_prospect_id``. Each
# signal is one directional emission, so a pair (a, b) typically appears twice
# (once from each side). DISTINCT + LEAST/GREATEST normalizes them down to one
# canonical row per (person_a, person_b, paper) triple.
SELECT_FROM_SIGNALS_SQL = """
WITH paired AS (
    SELECT
        pa.id                                         AS person_a_raw,
        pb.id                                         AS person_b_raw,
        s.value->>'semantic_scholar_id'               AS semantic_scholar_id,
        NULLIF(s.value->>'paper_title', '')           AS title,
        NULLIF(s.value->>'venue', '')                 AS venue,
        NULLIF(s.value->>'year', '')                  AS year_str,
        NULLIF(s.value->>'citation_count', '')        AS citation_count_str,
        s.account_id
    FROM signals s
    JOIN persons pa
        ON pa.source_prospect_id = s.prospect_id
       AND pa.account_id = s.account_id
    JOIN persons pb
        ON pb.id <> pa.id
       AND pb.account_id = s.account_id
       AND pb.source_prospect_id::text = s.value->>'connected_to'
    WHERE s.signal_type = 'academic_co_author'
      AND s.value ? 'semantic_scholar_id'
      AND s.value ? 'connected_to'
)
SELECT DISTINCT
    LEAST(person_a_raw, person_b_raw)    AS person_a_id,
    GREATEST(person_a_raw, person_b_raw) AS person_b_id,
    semantic_scholar_id,
    title,
    venue,
    CASE WHEN year_str ~ '^[0-9]+$'
         THEN year_str::int ELSE NULL END                  AS year,
    COALESCE(
        CASE WHEN citation_count_str ~ '^[0-9]+$'
             THEN citation_count_str::int ELSE 0 END, 0
    )                                                      AS citation_count,
    account_id
FROM paired
WHERE semantic_scholar_id IS NOT NULL AND semantic_scholar_id <> ''
ORDER BY year DESC NULLS LAST, citation_count DESC
"""


def _build_signals_query(limit: int | None) -> tuple[str, list]:
    sql = SELECT_FROM_SIGNALS_SQL
    if limit is not None:
        sql += f"\nLIMIT {int(limit)}\n"
    return sql, []


def _row_from_signals_to_paper_row(row: asyncpg.Record) -> PaperRow:
    """Parallel to ``_row_to_paper_row`` but synthesizes ``paper_id`` from ssid."""
    ssid = row["semantic_scholar_id"]
    return PaperRow(
        person_a_id=row["person_a_id"],
        person_b_id=row["person_b_id"],
        paper_id=_synthetic_paper_id(ssid),
        semantic_scholar_id=ssid,
        title=row["title"],
        venue=row["venue"],
        year=int(row["year"]) if row["year"] is not None else None,
        citation_count=int(row["citation_count"]),
        account_id=row["account_id"],
    )


def _row_to_paper_row(row: asyncpg.Record) -> PaperRow:
    return PaperRow(
        person_a_id=row["person_a_id"],
        person_b_id=row["person_b_id"],
        paper_id=row["paper_id"],
        semantic_scholar_id=row["semantic_scholar_id"],
        title=row["title"],
        venue=row["venue"],
        year=int(row["year"]) if row["year"] is not None else None,
        citation_count=int(row["citation_count"]),
        account_id=row["account_id"],
    )


def _group_into_pairs(rows: list[PaperRow]) -> list[PaperPair]:
    """Aggregate flat paper rows into one PaperPair per (a, b) person pair."""
    buckets: dict[tuple[UUID, UUID], PaperPair] = {}
    for r in rows:
        key = (r.person_a_id, r.person_b_id)
        if key not in buckets:
            buckets[key] = PaperPair(
                person_a_id=r.person_a_id,
                person_b_id=r.person_b_id,
                account_id=r.account_id,
            )
        buckets[key].papers.append(r)
    return list(buckets.values())


# ── Pure strength math ───────────────────────────────────────────────────────


def _compute_factors(
    connection_type: str,
    last_active_year: int,
    corroboration_count: int,
    source_type_count: int = _SOURCE_TYPE_COUNT_DEFAULT,
) -> tuple[float, float, float, float, float]:
    base = STRENGTH_TABLE[connection_type]
    decay = DECAY_RATES[connection_type]
    years = max(0, CURRENT_YEAR - last_active_year)
    recency = math.exp(-decay * years)
    frequency = 1.0 + math.log(max(1, corroboration_count)) * 0.15
    corroboration = 1.0 + source_type_count * 0.10
    computed = min(STRENGTH_CAP, base * recency * frequency * corroboration)
    return base, recency, frequency, corroboration, computed


# ── DB write helpers ─────────────────────────────────────────────────────────


def _evidence_source_id(pair: PaperPair, paper: PaperRow) -> str:
    ss_id = paper.semantic_scholar_id or str(paper.paper_id)
    return f"{pair.person_a_id}:{pair.person_b_id}:{ss_id}"


def _evidence_payload(paper: PaperRow) -> str:
    return json.dumps(
        {
            "paper_id": str(paper.paper_id),
            "semantic_scholar_id": paper.semantic_scholar_id,
            "title": paper.title,
            "venue": paper.venue,
            "year": paper.year,
            "citation_count": paper.citation_count,
        },
        separators=(",", ":"),
    )


async def _find_or_create_evidence(
    conn: asyncpg.Connection,
    pair: PaperPair,
    paper: PaperRow,
) -> tuple[UUID, bool]:
    source_id = _evidence_source_id(pair, paper)
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
        _evidence_payload(paper),
        pair.account_id,
    )
    return new_id, True


async def _upsert_connection(
    conn: asyncpg.Connection,
    pair: PaperPair,
    evidence_ids: list[UUID],
) -> bool:
    connection_type = pair.connection_type
    last_active = pair.last_active_year
    corroboration_count = len(evidence_ids)

    base, recency, frequency, corroboration, computed = _compute_factors(
        connection_type, last_active, corroboration_count
    )

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
            $5, $6, $7, $8,
            $9, $10,
            $11, $12, $13
        )
        ON CONFLICT (person_a_id, person_b_id, connection_type) DO NOTHING
        RETURNING id
        """,
        pair.person_a_id,
        pair.person_b_id,
        connection_type,
        pair.account_id,
        base,
        recency,
        frequency,
        corroboration,
        computed,
        last_active,
        corroboration_count,
        _SOURCE_TYPE_COUNT_DEFAULT,
        evidence_ids,
    )
    is_new = inserted_id is not None

    # Load the canonical row (whether just inserted or pre-existing) and
    # apply the full update so corroboration accumulates correctly.
    row = await conn.fetchrow(
        """
        SELECT id, evidence_ids, last_active_year
        FROM person_connections
        WHERE person_a_id = $1 AND person_b_id = $2 AND connection_type = $3
        """,
        pair.person_a_id,
        pair.person_b_id,
        connection_type,
    )
    if row is None:
        raise RuntimeError(
            f"person_connection vanished for {pair.person_a_id}→{pair.person_b_id}"
        )

    merged_ids: set[UUID] = set(row["evidence_ids"] or ())
    merged_ids.update(evidence_ids)
    new_evidence_ids = sorted(merged_ids)
    new_corroboration = len(new_evidence_ids)
    new_last_active = max(int(row["last_active_year"] or last_active), last_active)

    # Re-derive connection_type from merged corroboration count in case this
    # pair has accumulated enough papers to cross the multi threshold.
    new_connection_type = (
        "academic_co_author_multi"
        if new_corroboration >= MULTI_THRESHOLD
        else "academic_co_author_single"
    )

    base2, recency2, frequency2, corroboration2, computed2 = _compute_factors(
        new_connection_type, new_last_active, new_corroboration
    )

    await conn.execute(
        """
        UPDATE person_connections SET
            connection_type      = $2,
            evidence_ids         = $3,
            base_strength        = $4,
            recency_factor       = $5,
            frequency_factor     = $6,
            corroboration_factor = $7,
            computed_strength    = $8,
            last_active_year     = $9,
            corroboration_count  = $10,
            updated_at           = now()
        WHERE id = $1
        """,
        row["id"],
        new_connection_type,
        new_evidence_ids,
        base2,
        recency2,
        frequency2,
        corroboration2,
        computed2,
        new_last_active,
        new_corroboration,
    )
    return is_new


# ── Public orchestrator ──────────────────────────────────────────────────────


async def cluster_paper_co_authors(
    paper_id: UUID | None = None,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    from_signals: bool = False,
) -> PaperRollup:
    """Materialize ``academic_co_author_*`` edges into ``person_connections``.

    Args:
        paper_id: scope to a single ``papers.id`` UUID (only valid for the
            v3-table source path; ignored when ``from_signals=True``).
        limit: cap rows returned by the planner SQL.
        dry_run: planner-only — count pairs, write nothing.
        from_signals: when True, read pre-existing v2 ``signals`` rows
            (``signal_type='academic_co_author'``) instead of joining on
            ``papers``/``paper_authors``. Used for the v2→v3 pivot when the
            v3 tables haven't been backfilled yet (LP msg 229 Path A).
    """
    rollup = PaperRollup(dry_run=dry_run)
    if from_signals:
        if paper_id is not None:
            raise ValueError(
                "--paper-id is not supported in --from-signals mode "
                "(no papers.id present in the signals path)"
            )
        sql, args = _build_signals_query(limit)
        row_converter = _row_from_signals_to_paper_row
        scope_label = "signals(academic_co_author)"
    else:
        sql, args = _build_query(paper_id, limit)
        row_converter = _row_to_paper_row
        scope_label = f"paper_id={paper_id}"

    async with acquire() as conn:
        rows = await conn.fetch(sql, *args)

    paper_rows = [row_converter(r) for r in rows]
    pairs = _group_into_pairs(paper_rows)
    rollup.pairs_found = len(pairs)

    if dry_run:
        log.info(
            "[dry-run] academic_co_author pairs found: %d (scope=%s, limit=%s)",
            rollup.pairs_found,
            scope_label,
            limit,
        )
        return rollup

    async with acquire() as conn:
        for pair in pairs:
            try:
                async with conn.transaction():
                    # Write one evidence row per shared paper (highest-cited first).
                    ordered_papers = pair.papers_sorted_by_citations()
                    ev_ids: list[UUID] = []
                    for paper in ordered_papers:
                        ev_id, was_new = await _find_or_create_evidence(
                            conn, pair, paper
                        )
                        ev_ids.append(ev_id)
                        if was_new:
                            rollup.evidence_inserted += 1
                        else:
                            rollup.evidence_reused += 1

                    if await _upsert_connection(conn, pair, ev_ids):
                        rollup.pairs_inserted += 1
                    else:
                        rollup.pairs_updated += 1
            except Exception as exc:
                rollup.failures.append((str(pair.person_a_id), repr(exc)))
                log.exception(
                    "paper upsert failed for %s↔%s",
                    pair.person_a_id,
                    pair.person_b_id,
                )

    log.info(
        "paper rollup — pairs_found=%d inserted=%d updated=%d "
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
        prog="credence.jobs.paper_clustering",
        description="Materialize academic_co_author_* edges into person_connections.",
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="Process every paper.")
    scope.add_argument(
        "--paper-id",
        type=UUID,
        help="Scope to a single papers.id UUID.",
    )
    p.add_argument("--limit", type=int, default=None, help="Cap number of pairs processed.")
    p.add_argument("--dry-run", action="store_true", help="Count pairs only, write nothing.")
    p.add_argument(
        "--from-signals",
        action="store_true",
        help=(
            "Pivot existing v2 signals (signal_type='academic_co_author') "
            "into v3 person_connections, instead of joining on the v3 "
            "papers/paper_authors tables. Use when the v3 tables haven't "
            "been backfilled yet (LP msg 229 Path A)."
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
    pid = None if args.all else args.paper_id

    async def _go() -> PaperRollup:
        try:
            return await cluster_paper_co_authors(
                paper_id=pid,
                limit=args.limit,
                dry_run=args.dry_run,
                from_signals=args.from_signals,
            )
        finally:
            await close_pool()

    rollup = asyncio.run(_go())
    print(
        f"paper rollup — pairs_found={rollup.pairs_found} "
        f"inserted={rollup.pairs_inserted} updated={rollup.pairs_updated} "
        f"evidence[new={rollup.evidence_inserted}, reused={rollup.evidence_reused}] "
        f"failures={len(rollup.failures)}"
    )
    return 0 if not rollup.failures else 1


if __name__ == "__main__":
    sys.exit(main())
