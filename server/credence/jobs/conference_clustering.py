"""Conference clustering — Phase 2 Job 4 of the connection-edge sprint.

For every pair of persons who appeared at the same conference in the same
year in a *speaking* role (presenter / panelist / keynote / session_chair),
emit one ``conference_co_presenter`` edge into ``person_connections``. Pure
attendees are skipped — they don't count as a meaningful warm signal under
the STRENGTH_TABLE rules (CLAUDE.md: ``conference_co_attendee`` exists but
sits at strength 0.20; we save its emission for a separate ``conference_co_attendee``
job that the warm-path engine can deprioritize).

## Roles considered "speaking"

Per CLAUDE.md "Connection Graph" → STRENGTH_TABLE:
``conference_co_presenter`` is base strength 0.80. We treat the
following ``conference_attendances.role`` values as speaking:

- ``speaker`` (matches event_appearances.presenter)
- ``panelist``
- ``keynote``
- ``session_chair``

``attendee`` is excluded (would produce ``conference_co_attendee``, base 0.20,
not this job's responsibility).

## Idempotency

Same find-or-create + UPDATE pattern as ``career_overlap_clustering``.
Re-running over the same conference_attendances rows is a no-op except for
``updated_at`` bumps.

## Tenancy

``account_id`` is read from the ``conference_attendances`` row; the planner
SQL constrains ``a.account_id = b.account_id`` so cross-tenant edges can't
form.

## CLI

::

    uv run python -m credence.jobs.conference_clustering --dry-run
    uv run python -m credence.jobs.conference_clustering --event-id <uuid>
    uv run python -m credence.jobs.conference_clustering --all
    uv run python -m credence.jobs.conference_clustering --limit 100
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


CONNECTION_TYPE = "conference_co_presenter"
EVIDENCE_SOURCE_TYPE = "conference_program"

# attendees are deliberately excluded — they belong to the lower-strength
# ``conference_co_attendee`` kind which is a separate job.
SPEAKING_ROLES: frozenset[str] = frozenset(
    {"speaker", "panelist", "keynote", "session_chair"}
)

CURRENT_YEAR = 2026
_SOURCE_TYPE_COUNT_DEFAULT = 1


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CoPresenterPair:
    """One speaking-role co-appearance as returned by the planner SQL."""

    person_a_id: UUID
    person_b_id: UUID
    event_id: UUID
    event_name: str | None
    year: int
    role_a: str
    role_b: str
    account_id: UUID


@dataclass(slots=True)
class ConferenceRollup:
    """Aggregate counters for one ``cluster_conference_co_presenters`` call."""

    pairs_found: int = 0
    pairs_inserted: int = 0
    pairs_updated: int = 0
    evidence_inserted: int = 0
    evidence_reused: int = 0
    dry_run: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)


# ── Pure planner ────────────────────────────────────────────────────────────


def _build_query(
    event_id: UUID | None,
    limit: int | None,
) -> tuple[str, list[Any]]:
    args: list[Any] = [list(SPEAKING_ROLES)]
    event_clause = ""
    if event_id is not None:
        args.append(event_id)
        event_clause = f"AND a.event_id = ${len(args)} "
    sql = f"""
SELECT
    LEAST(a.person_id, b.person_id)    AS person_a_id,
    GREATEST(a.person_id, b.person_id) AS person_b_id,
    a.event_id,
    e.name AS event_name,
    a.year,
    a.role AS role_a_raw,
    b.role AS role_b_raw,
    -- Normalize so the role attached to person_a is the role of the
    -- LEAST(person_a, person_b) UUID; mirrors the swap above.
    CASE WHEN a.person_id < b.person_id THEN a.role ELSE b.role END AS role_a,
    CASE WHEN a.person_id < b.person_id THEN b.role ELSE a.role END AS role_b,
    a.account_id
FROM conference_attendances a
JOIN conference_attendances b
    ON a.event_id = b.event_id
    AND a.year = b.year
    AND a.person_id < b.person_id
    AND a.account_id = b.account_id
JOIN events e ON e.id = a.event_id
WHERE a.role = ANY($1::text[])
  AND b.role = ANY($1::text[])
  {event_clause}
ORDER BY a.year DESC, a.event_id
"""
    if limit is not None:
        sql += f"LIMIT {int(limit)}\n"
    return sql, args


def _row_to_pair(row: asyncpg.Record) -> CoPresenterPair:
    return CoPresenterPair(
        person_a_id=row["person_a_id"],
        person_b_id=row["person_b_id"],
        event_id=row["event_id"],
        event_name=row["event_name"],
        year=int(row["year"]),
        role_a=row["role_a"],
        role_b=row["role_b"],
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


def _evidence_source_id(pair: CoPresenterPair) -> str:
    """Deterministic key — same pair + event + year always points at one row."""
    return f"{pair.person_a_id}:{pair.person_b_id}:{pair.event_id}:{pair.year}"


def _evidence_payload(pair: CoPresenterPair) -> str:
    return json.dumps(
        {
            "event_id": str(pair.event_id),
            "event_name": pair.event_name,
            "year": pair.year,
            "role_a": pair.role_a,
            "role_b": pair.role_b,
        },
        separators=(",", ":"),
    )


async def _find_or_create_evidence(
    conn: asyncpg.Connection,
    pair: CoPresenterPair,
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
    pair: CoPresenterPair,
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
        pair.year,
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
    new_last_active = max(int(row["last_active_year"] or pair.year), pair.year)

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


async def cluster_conference_co_presenters(
    event_id: UUID | None = None,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> ConferenceRollup:
    """Materialize ``conference_co_presenter`` edges into ``person_connections``."""
    rollup = ConferenceRollup(dry_run=dry_run)
    sql, args = _build_query(event_id, limit)

    async with acquire() as conn:
        rows = await conn.fetch(sql, *args)
    rollup.pairs_found = len(rows)
    if dry_run:
        log.info(
            "[dry-run] conference_co_presenter pairs found: %d (event=%s, limit=%s)",
            rollup.pairs_found,
            event_id,
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
                    "conference upsert failed for %s↔%s @ event %s (%d)",
                    pair.person_a_id, pair.person_b_id, pair.event_id, pair.year,
                )

    log.info(
        "conference rollup — pairs_found=%d inserted=%d updated=%d "
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
        prog="credence.jobs.conference_clustering",
        description="Materialize conference_co_presenter edges into person_connections.",
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="Process every event.")
    scope.add_argument(
        "--event-id",
        type=UUID,
        help="Scope to a single events.id UUID.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap number of pairs processed (after ordering by year DESC).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Planner only: count pairs, write nothing.",
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
    event_id = None if args.all else args.event_id

    async def _go() -> ConferenceRollup:
        try:
            return await cluster_conference_co_presenters(
                event_id=event_id,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        finally:
            await close_pool()

    rollup = asyncio.run(_go())
    print(
        f"conference rollup — pairs_found={rollup.pairs_found} "
        f"inserted={rollup.pairs_inserted} updated={rollup.pairs_updated} "
        f"evidence[new={rollup.evidence_inserted}, reused={rollup.evidence_reused}] "
        f"failures={len(rollup.failures)}"
    )
    return 0 if not rollup.failures else 1


if __name__ == "__main__":
    sys.exit(main())
