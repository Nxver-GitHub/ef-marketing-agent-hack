"""Patent co-inventor clustering — Phase 2 Job 2 of the connection-edge sprint.

For every pair of persons who share a USPTO patent as co-inventors, emit one
``patent_co_inventor`` edge into ``person_connections`` with the
``person_a_id < person_b_id`` invariant (CLAUDE.md Decision 1).

``patent_co_inventor`` has the highest base strength in the STRENGTH_TABLE
(0.95) — a shared patent is a hard documented signal of deep collaboration.
Decay rate is 0.01/year, so even a 20-year-old co-invention retains ~0.78
base strength.

## Data source

Reads from the v3 ``patents`` + ``patent_inventors`` junction tables populated
by the USPTO extractor (``server/credence/extractors/patents.py``). Each
``patent_inventors`` row maps a ``(patent_id, person_id)`` pair; this job
self-joins to find all co-inventor pairs per patent.

## Evidence

One ``connection_evidence`` row per (pair, patent), keyed by the deterministic
``source_id = "{person_a_id}:{person_b_id}:{patent_id}"``. ``structured_value``
carries the Contract-1 ``patent_co_inventor`` shape so the frontend's
``evidenceFromSignal`` bridge can render rich tooltips and warm-path openers.

## Idempotency

Same find-or-create + recompute pattern as ``career_overlap_clustering``.
Re-running over the same ``patent_inventors`` rows is safe:
- ``connection_evidence``: find-by-source_id first, insert only if absent.
- ``person_connections``: INSERT … ON CONFLICT DO NOTHING + UPDATE to merge
  evidence_ids and recompute computed_strength.

## Tenancy

``account_id`` is read from the ``patent_inventors`` row; the planner SQL
constrains ``a.account_id = b.account_id``.

## CLI

::

    uv run python -m credence.jobs.patent_clustering --dry-run
    uv run python -m credence.jobs.patent_clustering --patent-id <uuid>
    uv run python -m credence.jobs.patent_clustering --all
    uv run python -m credence.jobs.patent_clustering --limit 500
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


CONNECTION_TYPE = "patent_co_inventor"
EVIDENCE_SOURCE_TYPE = "uspto"
CURRENT_YEAR = 2026
_SOURCE_TYPE_COUNT_DEFAULT = 1


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CoInventorPair:
    """One co-inventor pair as returned by the planner SQL."""

    person_a_id: UUID
    person_b_id: UUID
    patent_id: UUID
    patent_number: str | None
    patent_title: str | None
    filing_date: str | None
    grant_date: str | None
    assignee: str | None
    grant_year: int
    account_id: UUID


@dataclass(slots=True)
class PatentRollup:
    """Aggregate counters for one ``cluster_patent_co_inventors`` call."""

    pairs_found: int = 0
    pairs_inserted: int = 0
    pairs_updated: int = 0
    evidence_inserted: int = 0
    evidence_reused: int = 0
    dry_run: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)


# ── Pure planner ─────────────────────────────────────────────────────────────


def _build_query(
    patent_id: UUID | None,
    limit: int | None,
) -> tuple[str, list]:
    args: list = []
    patent_clause = ""
    if patent_id is not None:
        args.append(patent_id)
        patent_clause = f"AND a.patent_id = ${len(args)} "

    sql = f"""
SELECT
    LEAST(a.person_id, b.person_id)    AS person_a_id,
    GREATEST(a.person_id, b.person_id) AS person_b_id,
    p.id                               AS patent_id,
    p.patent_number,
    p.title                            AS patent_title,
    p.filing_date::text                AS filing_date,
    p.grant_date::text                 AS grant_date,
    c.canonical_name                   AS assignee,
    COALESCE(
        EXTRACT(YEAR FROM p.grant_date)::int,
        EXTRACT(YEAR FROM p.filing_date)::int,
        {CURRENT_YEAR}
    )                                  AS grant_year,
    a.account_id
FROM patent_inventors a
JOIN patent_inventors b
    ON a.patent_id = b.patent_id
    AND a.person_id < b.person_id
    AND a.account_id = b.account_id
JOIN patents p ON p.id = a.patent_id
LEFT JOIN companies c ON c.id = p.assignee_company_id
WHERE TRUE
  {patent_clause}
ORDER BY grant_year DESC, p.patent_number
"""
    if limit is not None:
        sql += f"LIMIT {int(limit)}\n"
    return sql, args


def _row_to_pair(row: asyncpg.Record) -> CoInventorPair:
    return CoInventorPair(
        person_a_id=row["person_a_id"],
        person_b_id=row["person_b_id"],
        patent_id=row["patent_id"],
        patent_number=row["patent_number"],
        patent_title=row["patent_title"],
        filing_date=row["filing_date"],
        grant_date=row["grant_date"],
        assignee=row["assignee"],
        grant_year=int(row["grant_year"]),
        account_id=row["account_id"],
    )


# ── Pure strength math ───────────────────────────────────────────────────────


def _compute_factors(
    last_active_year: int,
    corroboration_count: int,
    source_type_count: int = _SOURCE_TYPE_COUNT_DEFAULT,
) -> tuple[float, float, float, float, float]:
    """Return (base, recency, frequency, corroboration, computed)."""
    base = STRENGTH_TABLE[CONNECTION_TYPE]
    decay = DECAY_RATES[CONNECTION_TYPE]
    years = max(0, CURRENT_YEAR - last_active_year)
    recency = math.exp(-decay * years)
    frequency = 1.0 + math.log(max(1, corroboration_count)) * 0.15
    corroboration = 1.0 + source_type_count * 0.10
    computed = min(STRENGTH_CAP, base * recency * frequency * corroboration)
    return base, recency, frequency, corroboration, computed


# ── DB write helpers ─────────────────────────────────────────────────────────


def _evidence_source_id(pair: CoInventorPair) -> str:
    """Deterministic key — same pair + patent always points at the same row."""
    return f"{pair.person_a_id}:{pair.person_b_id}:{pair.patent_id}"


def _evidence_payload(pair: CoInventorPair) -> str:
    return json.dumps(
        {
            "patent_id": str(pair.patent_id),
            "patent_number": pair.patent_number,
            "patent_title": pair.patent_title,
            "filing_date": pair.filing_date,
            "grant_date": pair.grant_date,
            "assignee": pair.assignee,
            # connected_to is not set here — it's filled by the signals layer.
            # The evidence shape matches Contract 1 §"patent_co_inventor".
        },
        separators=(",", ":"),
    )


async def _find_or_create_evidence(
    conn: asyncpg.Connection,
    pair: CoInventorPair,
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
    pair: CoInventorPair,
    evidence_id: UUID,
) -> bool:
    """Insert or refresh one person_connections row. Returns True if newly inserted."""
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
        pair.grant_year,
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
    new_last_active = max(int(row["last_active_year"] or pair.grant_year), pair.grant_year)

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


async def cluster_patent_co_inventors(
    patent_id: UUID | None = None,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> PatentRollup:
    """Materialize ``patent_co_inventor`` edges into ``person_connections``.

    Args:
        patent_id: scope to one patent; ``None`` means all patents.
        limit: cap the number of pairs processed (after ordering by grant_year DESC).
        dry_run: planner-only — no rows written, summary still returned.
    """
    rollup = PatentRollup(dry_run=dry_run)
    sql, args = _build_query(patent_id, limit)

    async with acquire() as conn:
        rows = await conn.fetch(sql, *args)
    rollup.pairs_found = len(rows)

    if dry_run:
        log.info(
            "[dry-run] patent_co_inventor pairs found: %d (patent=%s, limit=%s)",
            rollup.pairs_found,
            patent_id,
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
                    "patent upsert failed for %s↔%s @ patent %s",
                    pair.person_a_id,
                    pair.person_b_id,
                    pair.patent_id,
                )

    log.info(
        "patent rollup — pairs_found=%d inserted=%d updated=%d "
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
        prog="credence.jobs.patent_clustering",
        description="Materialize patent_co_inventor edges into person_connections.",
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="Process every patent.")
    scope.add_argument(
        "--patent-id",
        type=UUID,
        help="Scope to a single patents.id UUID.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap number of pairs processed (after ordering by grant_year DESC).",
    )
    p.add_argument("--dry-run", action="store_true", help="Planner only: count pairs, write nothing.")
    p.add_argument("--log-level", default="INFO", help="Python logging level (default INFO).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    patent_id = None if args.all else args.patent_id

    async def _go() -> PatentRollup:
        try:
            return await cluster_patent_co_inventors(
                patent_id=patent_id,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        finally:
            await close_pool()

    rollup = asyncio.run(_go())
    print(
        f"patent rollup — pairs_found={rollup.pairs_found} "
        f"inserted={rollup.pairs_inserted} updated={rollup.pairs_updated} "
        f"evidence[new={rollup.evidence_inserted}, reused={rollup.evidence_reused}] "
        f"failures={len(rollup.failures)}"
    )
    return 0 if not rollup.failures else 1


if __name__ == "__main__":
    sys.exit(main())
