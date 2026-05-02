"""Bridge B — extract co-mention edges from press releases.

For each ``company_signals`` row with ``signal_type='press_release'`` and
``structured_value->'mentioned_executives'`` containing ≥2 names, emit a
``person_connections`` row of type ``mentioned_in_same_release`` for every
pair of executives that resolve to known persons in the same company.

Design choices:

- **Weakest signal class.** Co-mention is correlational, not collaborative.
  ``base_strength = 0.55`` (weaker than ``career_overlap_general`` at 0.60)
  so it never displaces a stronger edge. The job refuses to insert a
  co-mention edge if the same pair already has any connection_type with
  ``base_strength >= 0.55``.
- **Faster recency decay.** Press relevance is short-lived;
  ``recency_factor = exp(-0.10 * years_since_release)``.
- **Lower frequency coefficient.** Repeat co-mentions corroborate but only
  modestly — coefficient 0.10 (vs. the standard 0.15).
- **Deterministic pair ordering.** ``LEAST(a,b), GREATEST(a,b)`` per
  CLAUDE.md Decision 1.

Idempotency: the writer uses ``ON CONFLICT (person_a_id, person_b_id,
connection_type) DO UPDATE`` to bump corroboration_count + recompute
computed_strength on re-emit. Safe under concurrent writers.

CLI::

    uv run python -m credence.jobs.bulk_press_co_mention_edges --dry-run --all-accounts
    uv run python -m credence.jobs.bulk_press_co_mention_edges --account-id <uuid>
    uv run python -m credence.jobs.bulk_press_co_mention_edges --all-accounts --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import Any
from uuid import UUID

import asyncpg

from ..db import acquire, close_pool

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


CONNECTION_TYPE = "mentioned_in_same_release"
BASE_STRENGTH: float = 0.55
DECAY_RATE: float = 0.10  # press relevance is short-lived
FREQUENCY_COEFFICIENT: float = 0.10  # weaker than the standard 0.15
SOURCE_TYPE_COUNT: int = 1
STRENGTH_CAP: float = 0.99
CURRENT_YEAR: int = datetime.now(tz=timezone.utc).year


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PressRelease:
    """One press_release company_signals row."""

    signal_id: UUID
    account_id: UUID
    company_id: UUID
    fetched_at: datetime
    mentioned_executives: tuple[str, ...]


@dataclass(slots=True)
class CoMentionRollup:
    """Aggregate counters for one ``run_press_co_mention_clustering`` call."""

    releases_scanned: int = 0
    releases_qualifying: int = 0
    pairs_considered: int = 0
    pairs_emitted: int = 0
    pairs_updated: int = 0
    pairs_skipped_unmatched: int = 0
    pairs_skipped_stronger_exists: int = 0
    dry_run: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)


# ── Pure helpers ─────────────────────────────────────────────────────────────


def _pair_executives(names: list[str]) -> list[tuple[str, str]]:
    """Return all unordered (n choose 2) pairs of distinct, non-empty names.

    Names are stripped; empty strings are dropped. Duplicates (case-insensitive,
    whitespace-collapsed) collapse to one — a single press release shouldn't
    co-mention "Jane Smith" twice and inflate pair counts.
    """
    seen: dict[str, str] = {}
    for raw in names:
        if not isinstance(raw, str):
            continue
        cleaned = " ".join(raw.split())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen[key] = cleaned
    canonical = list(seen.values())
    return list(combinations(canonical, 2))


def _recency_factor(years_ago: float, decay_rate: float = DECAY_RATE) -> float:
    """Exponential decay: ``exp(-decay_rate * max(0, years_ago))``."""
    return math.exp(-decay_rate * max(0.0, years_ago))


def _frequency_factor(
    corroboration_count: int,
    coefficient: float = FREQUENCY_COEFFICIENT,
) -> float:
    """``1 + log(max(1, corroboration_count)) * coefficient``."""
    return 1.0 + math.log(max(1, corroboration_count)) * coefficient


def _compute_strength(
    base: float,
    recency: float,
    frequency: float,
    corroboration: float,
    cap: float = STRENGTH_CAP,
) -> float:
    """Strength formula identical in shape to ``credence.strength``: capped product."""
    return min(cap, base * recency * frequency * corroboration)


def _split_first_last(full_name: str) -> tuple[str, str] | None:
    """Crude first / last token split; matches scripts/backfill_persons convention."""
    tokens = [t for t in full_name.strip().split() if t.strip()]
    if len(tokens) < 2:
        return None
    return tokens[0], tokens[-1]


def _years_since(release_at: datetime, now_year: int = CURRENT_YEAR) -> float:
    """Fractional years between the release date and the current year."""
    if release_at.tzinfo is None:
        release_at = release_at.replace(tzinfo=timezone.utc)
    now = datetime(now_year, 1, 1, tzinfo=timezone.utc)
    delta_days = (now - release_at).total_seconds() / 86400.0
    return max(0.0, delta_days / 365.25)


# ── DB readers ───────────────────────────────────────────────────────────────


def _build_releases_query(
    account_id: UUID | None,
    limit: int | None,
) -> tuple[str, list[Any]]:
    """Planner SQL for qualifying press_release rows."""
    args: list[Any] = []
    where_parts = [
        "cs.signal_type = 'press_release'",
        "jsonb_typeof(cs.structured_value -> 'mentioned_executives') = 'array'",
        "jsonb_array_length(cs.structured_value -> 'mentioned_executives') >= 2",
    ]
    if account_id is not None:
        args.append(account_id)
        where_parts.append(f"cs.account_id = ${len(args)}")
    where_clause = " AND ".join(where_parts)

    sql = f"""
        SELECT cs.id            AS signal_id,
               cs.account_id    AS account_id,
               cs.company_id    AS company_id,
               cs.fetched_at    AS fetched_at,
               cs.structured_value -> 'mentioned_executives' AS mentioned_executives
        FROM company_signals cs
        WHERE {where_clause}
        ORDER BY cs.fetched_at DESC
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql, args


def _row_to_release(row: asyncpg.Record) -> PressRelease:
    raw = row["mentioned_executives"] or []
    if not isinstance(raw, list):
        raw = []
    names = tuple(str(x) for x in raw if isinstance(x, str))
    return PressRelease(
        signal_id=row["signal_id"],
        account_id=row["account_id"],
        company_id=row["company_id"],
        fetched_at=row["fetched_at"],
        mentioned_executives=names,
    )


async def _resolve_person(
    conn: asyncpg.Connection,
    name: str,
    company_id: UUID,
) -> UUID | None:
    """Find the persons.id matching ``name`` scoped to ``company_id``.

    Match precedence (linkedin_url is rarely available in press text, so name
    match is the primary path):
      1. ``lower(canonical_name) ILIKE %first% AND %last%`` AND
         ``current_company_id = $company_id``
      2. Returns None when 0 matches OR when ≥2 matches (ambiguous).
    """
    split = _split_first_last(name)
    if split is None:
        return None
    first, last = split
    rows = await conn.fetch(
        """
        SELECT id
        FROM persons
        WHERE current_company_id = $1
          AND canonical_name ILIKE $2
          AND canonical_name ILIKE $3
        ORDER BY canonical_name
        LIMIT 2
        """,
        company_id,
        f"%{first}%",
        f"%{last}%",
    )
    if len(rows) == 1:
        return rows[0]["id"]
    return None


# ── DB writer ────────────────────────────────────────────────────────────────


async def _stronger_edge_exists(
    conn: asyncpg.Connection,
    person_a_id: UUID,
    person_b_id: UUID,
) -> bool:
    """True if (a, b) already has ANY connection_type with base_strength ≥ 0.55.

    Co-mention is the weakest signal in the system — never displace stronger
    edges. We DO permit upserts of an existing ``mentioned_in_same_release``
    row (handled in the upsert SQL via ON CONFLICT) — the gate skips fresh
    competing edges of OTHER types.
    """
    row = await conn.fetchrow(
        """
        SELECT 1
        FROM person_connections
        WHERE person_a_id = $1
          AND person_b_id = $2
          AND connection_type <> $3
          AND base_strength >= $4
        LIMIT 1
        """,
        person_a_id,
        person_b_id,
        CONNECTION_TYPE,
        BASE_STRENGTH,
    )
    return row is not None


async def _upsert_co_mention_edge(
    conn: asyncpg.Connection,
    *,
    person_a_id: UUID,
    person_b_id: UUID,
    account_id: UUID,
    release_year: int,
    years_ago: float,
) -> tuple[bool, bool]:
    """INSERT or UPDATE the co-mention edge for this pair.

    Returns ``(was_new, was_updated)``. Bumps ``corroboration_count`` on every
    re-emit so the strength formula picks up additional supporting releases.
    """
    base = BASE_STRENGTH
    initial_recency = _recency_factor(years_ago)
    initial_frequency = _frequency_factor(1)
    corroboration_factor = 1.0 + SOURCE_TYPE_COUNT * 0.10
    initial_computed = _compute_strength(
        base, initial_recency, initial_frequency, corroboration_factor,
    )

    # Step 1: INSERT … ON CONFLICT DO UPDATE — race-safe single round trip.
    # On conflict we bump corroboration_count and recompute strength fields
    # using the new count so ``computed_strength`` stays in sync.
    new_recency_expr = "EXCLUDED.recency_factor"
    # Push the ``last_active_year`` forward only if this release is newer than
    # the recorded one; older corroborations shouldn't drag last_active back.
    # Use GREATEST in the UPDATE expression.
    sql = f"""
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
            1, $11, ARRAY[]::uuid[]
        )
        ON CONFLICT (person_a_id, person_b_id, connection_type) DO UPDATE SET
            corroboration_count = person_connections.corroboration_count + 1,
            last_active_year   = GREATEST(
                COALESCE(person_connections.last_active_year, EXCLUDED.last_active_year),
                EXCLUDED.last_active_year
            ),
            recency_factor      = GREATEST(person_connections.recency_factor, {new_recency_expr}),
            frequency_factor    = 1.0 + ln(person_connections.corroboration_count + 1) * {FREQUENCY_COEFFICIENT},
            corroboration_factor= EXCLUDED.corroboration_factor,
            computed_strength   = LEAST(
                {STRENGTH_CAP},
                person_connections.base_strength
                  * GREATEST(person_connections.recency_factor, {new_recency_expr})
                  * (1.0 + ln(person_connections.corroboration_count + 1) * {FREQUENCY_COEFFICIENT})
                  * EXCLUDED.corroboration_factor
            ),
            updated_at          = now()
        RETURNING (xmax = 0) AS was_inserted
    """
    row = await conn.fetchrow(
        sql,
        person_a_id,
        person_b_id,
        CONNECTION_TYPE,
        account_id,
        base,
        initial_recency,
        initial_frequency,
        corroboration_factor,
        initial_computed,
        release_year,
        SOURCE_TYPE_COUNT,
    )
    was_inserted = bool(row["was_inserted"]) if row is not None else False
    return was_inserted, not was_inserted


# ── Orchestrator ─────────────────────────────────────────────────────────────


async def run_press_co_mention_clustering(
    *,
    account_id: UUID | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> CoMentionRollup:
    """Materialize co-mention edges into ``person_connections``."""
    rollup = CoMentionRollup(dry_run=dry_run)
    sql, args = _build_releases_query(account_id, limit)

    async with acquire() as conn:
        rows = await conn.fetch(sql, *args)
    rollup.releases_scanned = len(rows)
    log.info(
        "press co-mention: %d candidate press_release rows (account=%s, limit=%s)",
        rollup.releases_scanned, account_id, limit,
    )

    releases = [_row_to_release(r) for r in rows]

    for release in releases:
        pairs = _pair_executives(list(release.mentioned_executives))
        if not pairs:
            continue
        rollup.releases_qualifying += 1
        years_ago = _years_since(release.fetched_at)
        release_year = release.fetched_at.year

        try:
            async with acquire() as conn:
                # Resolve person IDs for every distinct name once per release.
                resolved: dict[str, UUID | None] = {}
                for first_name, second_name in pairs:
                    for n in (first_name, second_name):
                        if n not in resolved:
                            resolved[n] = await _resolve_person(
                                conn, n, release.company_id,
                            )

                for first_name, second_name in pairs:
                    rollup.pairs_considered += 1
                    a_id = resolved.get(first_name)
                    b_id = resolved.get(second_name)
                    if a_id is None or b_id is None or a_id == b_id:
                        rollup.pairs_skipped_unmatched += 1
                        continue
                    person_a_id, person_b_id = (
                        (a_id, b_id) if str(a_id) < str(b_id) else (b_id, a_id)
                    )
                    if await _stronger_edge_exists(conn, person_a_id, person_b_id):
                        rollup.pairs_skipped_stronger_exists += 1
                        continue
                    if dry_run:
                        rollup.pairs_emitted += 1
                        continue
                    async with conn.transaction():
                        was_new, was_updated = await _upsert_co_mention_edge(
                            conn,
                            person_a_id=person_a_id,
                            person_b_id=person_b_id,
                            account_id=release.account_id,
                            release_year=release_year,
                            years_ago=years_ago,
                        )
                    if was_new:
                        rollup.pairs_emitted += 1
                    elif was_updated:
                        rollup.pairs_updated += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("press co-mention failed for signal %s", release.signal_id)
            rollup.failures.append((str(release.signal_id), repr(exc)))

    log.info(
        "press co-mention rollup — releases_scanned=%d qualifying=%d "
        "pairs[considered=%d emitted=%d updated=%d skipped_unmatched=%d "
        "skipped_stronger=%d] failures=%d",
        rollup.releases_scanned,
        rollup.releases_qualifying,
        rollup.pairs_considered,
        rollup.pairs_emitted,
        rollup.pairs_updated,
        rollup.pairs_skipped_unmatched,
        rollup.pairs_skipped_stronger_exists,
        len(rollup.failures),
    )
    return rollup


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.bulk_press_co_mention_edges",
        description=(
            "Emit person_connections rows of type "
            "'mentioned_in_same_release' for executives co-mentioned in "
            "press_release company_signals."
        ),
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all-accounts", action="store_true",
                       help="Process every account.")
    scope.add_argument("--account-id", type=UUID,
                       help="Scope to a single accounts.id UUID.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap number of press_release rows scanned.")
    p.add_argument("--dry-run", action="store_true",
                   help="Planner only: count pairs, write nothing.")
    p.add_argument("--log-level", default="INFO",
                   help="Python logging level (default INFO).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    account_id = None if args.all_accounts else args.account_id

    async def _go() -> CoMentionRollup:
        try:
            return await run_press_co_mention_clustering(
                account_id=account_id,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        finally:
            await close_pool()

    rollup = asyncio.run(_go())
    print(
        f"press_co_mention rollup — releases_scanned={rollup.releases_scanned} "
        f"qualifying={rollup.releases_qualifying} "
        f"pairs_considered={rollup.pairs_considered} "
        f"pairs_emitted={rollup.pairs_emitted} "
        f"pairs_updated={rollup.pairs_updated} "
        f"skipped_unmatched={rollup.pairs_skipped_unmatched} "
        f"skipped_stronger_exists={rollup.pairs_skipped_stronger_exists} "
        f"failures={len(rollup.failures)} (dry_run={rollup.dry_run})"
    )
    return 0 if not rollup.failures else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "BASE_STRENGTH",
    "CONNECTION_TYPE",
    "CURRENT_YEAR",
    "CoMentionRollup",
    "DECAY_RATE",
    "FREQUENCY_COEFFICIENT",
    "PressRelease",
    "_build_releases_query",
    "_compute_strength",
    "_frequency_factor",
    "_pair_executives",
    "_recency_factor",
    "_resolve_person",
    "_split_first_last",
    "_stronger_edge_exists",
    "_years_since",
    "run_press_co_mention_clustering",
]
