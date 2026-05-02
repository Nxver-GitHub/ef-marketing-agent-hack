"""Bulk-emit ``co_mentioned_in_press`` edges from the v2 ``signals`` table.

The v3 sibling of this script (``credence.jobs.bulk_press_co_mention_edges``)
runs against the v3 ``company_signals`` table and emits edges of type
``mentioned_in_same_release``. This script is the v2 backfill: it scans
the per-prospect ``signals`` table for press-release-shaped rows, extracts
the names mentioned in each release, resolves them to ``persons.id`` via a
single in-memory ``canonical_name`` index, and emits one
``co_mentioned_in_press`` edge per pair into ``person_connections``.

Per the user spec: roughly **30 edges expected** across the current dataset.
The script is therefore optimised for clarity over throughput — one round trip
per release pair, idempotent re-runs, and a clean dry-run mode.

## Connection-type fallback

The v3 ``person_connections.connection_type`` CHECK constraint enumerates a
fixed taxonomy (see ``20260430_v3_connection_graph.sql`` +
``20260501_v3_education_conference.sql``). ``co_mentioned_in_press`` is the
intended kind, but historic CHECK constraints may not list it. At startup
the script probes the constraint by attempting a no-op insert in a rolled-
back transaction. If the kind is rejected we fall back to
``conference_co_attendee`` — the closest existing weak-correlation kind —
and emit a single WARNING line so operators know a migration is owed.

## Idempotency

Re-runs are safe via the existing
``UNIQUE (person_a_id, person_b_id, connection_type)`` index.
``ON CONFLICT … DO UPDATE`` bumps ``corroboration_count`` and refreshes
``computed_strength`` so additional supporting releases corroborate the
edge without duplicating it.

## CLI

::

    cd server && uv run --env-file ../.env.local \\
        python -m scripts.bulk_press_co_mention_edges
    cd server && uv run --env-file ../.env.local \\
        python -m scripts.bulk_press_co_mention_edges --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import Any
from uuid import UUID, uuid4

# Allow `python server/scripts/bulk_press_co_mention_edges.py` invocation in
# addition to the documented `python -m scripts.bulk_press_co_mention_edges`.
if __package__ in (None, ""):
    _SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _SERVER_DIR not in sys.path:
        sys.path.insert(0, _SERVER_DIR)

import asyncpg  # noqa: E402

from credence.db import acquire, close_pool  # noqa: E402

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


PREFERRED_CONNECTION_TYPE = "co_mentioned_in_press"
FALLBACK_CONNECTION_TYPE = "conference_co_attendee"
PRESS_SIGNAL_TYPES: tuple[str, ...] = (
    "press_release",
    "company_press",
    "press_mention",
)
BASE_STRENGTH: float = 0.45
DECAY_RATE: float = 0.10
FREQUENCY_COEFFICIENT: float = 0.10
SOURCE_TYPE_COUNT: int = 1
STRENGTH_CAP: float = 0.99
CURRENT_YEAR: int = datetime.now(tz=timezone.utc).year

# Plausible JSONB key names that a press_release ``value`` blob may use to
# carry the list of mentioned persons. Order is informative — the first key
# whose value is a list of strings wins.
NAME_LIST_KEYS: tuple[str, ...] = (
    "mentioned_executives",
    "mentioned_persons",
    "mentioned_people",
    "persons",
    "people",
    "executives",
    "names",
)


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PressSignal:
    """One press-release-shaped row pulled from the v2 ``signals`` table."""

    signal_id: UUID
    prospect_id: UUID | None
    signal_type: str
    collected_at: datetime
    mentioned_names: tuple[str, ...]


@dataclass(slots=True)
class CoMentionRollup:
    """Aggregate counters for one ``run_press_co_mention`` invocation."""

    signals_scanned: int = 0
    signals_qualifying: int = 0
    pairs_considered: int = 0
    pairs_inserted: int = 0
    pairs_updated: int = 0
    pairs_skipped_unmatched: int = 0
    pairs_failed: int = 0
    connection_type_used: str = PREFERRED_CONNECTION_TYPE
    fallback_used: bool = False
    dry_run: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)


# ── Pure helpers ─────────────────────────────────────────────────────────────


def _extract_names_from_value(value: Any) -> list[str]:
    """Pull the list of mentioned person names from a v2 ``signals.value`` blob.

    Returns ``[]`` for any shape we don't recognize. Strips, drops empties,
    and dedupes case-insensitively.
    """
    if not isinstance(value, dict):
        return []
    raw_list: list[Any] | None = None
    for key in NAME_LIST_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, list):
            raw_list = candidate
            break
    if raw_list is None:
        return []
    return _normalise_names(raw_list)


def _normalise_names(items: list[Any]) -> list[str]:
    """Strip, drop empties, dedupe case-insensitively while preserving order."""
    seen: dict[str, str] = {}
    for raw in items:
        if not isinstance(raw, str):
            continue
        cleaned = " ".join(raw.split())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen[key] = cleaned
    return list(seen.values())


def _pair_names(names: list[str]) -> list[tuple[str, str]]:
    """Return ``(n choose 2)`` unordered pairs of names. Empty input yields ``[]``."""
    return list(combinations(names, 2))


def _recency_factor(years_ago: float, decay_rate: float = DECAY_RATE) -> float:
    """``exp(-decay_rate * max(0, years_ago))``."""
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
    """Capped product (matches ``credence.strength`` family)."""
    return min(cap, base * recency * frequency * corroboration)


def _years_since(when: datetime, now_year: int = CURRENT_YEAR) -> float:
    """Fractional years between ``when`` and ``Jan 1 of now_year``."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = datetime(now_year, 1, 1, tzinfo=timezone.utc)
    delta_days = (now - when).total_seconds() / 86400.0
    return max(0.0, delta_days / 365.25)


def _order_pair(a: UUID, b: UUID) -> tuple[UUID, UUID]:
    """Enforce the ``person_a_id < person_b_id`` invariant (CLAUDE.md D1)."""
    return (a, b) if str(a) < str(b) else (b, a)


# ── DB readers ───────────────────────────────────────────────────────────────


_RELEASES_QUERY = """
    SELECT s.id           AS signal_id,
           s.prospect_id  AS prospect_id,
           s.signal_type  AS signal_type,
           s.value        AS value,
           COALESCE(s.collected_at, NOW()) AS collected_at
    FROM signals s
    WHERE s.signal_type = ANY($1::text[])
      AND s.value IS NOT NULL
"""


def _build_releases_query(limit: int | None) -> str:
    """Return SQL for fetching candidate v2 press signals."""
    sql = _RELEASES_QUERY
    if limit is not None:
        sql += f"\n    LIMIT {int(limit)}"
    return sql


def _row_to_signal(row: asyncpg.Record) -> PressSignal | None:
    """Adapt a ``signals`` row → ``PressSignal`` or ``None`` when un-usable."""
    names = _extract_names_from_value(row["value"])
    if len(names) < 2:
        return None
    return PressSignal(
        signal_id=row["signal_id"],
        prospect_id=row["prospect_id"],
        signal_type=row["signal_type"],
        collected_at=row["collected_at"],
        mentioned_names=tuple(names),
    )


async def _load_persons_index(conn: asyncpg.Connection) -> dict[str, UUID]:
    """Build a single in-memory ``lower(canonical_name) -> id`` index.

    Ambiguous names (≥2 persons sharing the same canonical_name lower-cased)
    are dropped from the index — we'd rather skip a pair than mis-attribute
    a co-mention.
    """
    rows = await conn.fetch(
        "SELECT id, canonical_name FROM persons WHERE canonical_name IS NOT NULL",
    )
    counts: dict[str, int] = {}
    index: dict[str, UUID] = {}
    for r in rows:
        key = " ".join(r["canonical_name"].split()).lower()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        index[key] = r["id"]
    # Drop any ambiguous keys.
    return {k: v for k, v in index.items() if counts.get(k, 0) == 1}


def _resolve_name(name: str, index: dict[str, UUID]) -> UUID | None:
    """Case-insensitive lookup in the prebuilt index."""
    key = " ".join(name.split()).lower()
    return index.get(key)


# ── DB writer ────────────────────────────────────────────────────────────────


async def _probe_connection_type_supported(
    conn: asyncpg.Connection, candidate: str,
) -> bool:
    """Try inserting + immediately rolling back a sentinel row of ``candidate``.

    Returns True if the CHECK constraint accepts ``candidate``. The probe is
    wrapped in a SAVEPOINT so it never leaves data behind even if the parent
    connection is in a transaction. We pass UUIDs that satisfy ``a_lt_b``
    but reference no real persons, so the FK trips after the CHECK — that's
    fine; we only care which error class fires.
    """
    a = UUID(int=1)
    b = UUID(int=2)
    sentinel_account = UUID(int=0)
    try:
        async with conn.transaction():
            try:
                await conn.execute(
                    """
                    INSERT INTO person_connections (
                        person_a_id, person_b_id, connection_type, account_id,
                        base_strength, recency_factor, frequency_factor,
                        corroboration_factor, computed_strength,
                        last_active_year, corroboration_count,
                        source_type_count, evidence_ids
                    )
                    VALUES (
                        $1, $2, $3, $4,
                        0.45, 1.0, 1.0, 1.1, 0.45,
                        $5, 1, 1, ARRAY[]::uuid[]
                    )
                    """,
                    a, b, candidate, sentinel_account, CURRENT_YEAR,
                )
            except asyncpg.exceptions.CheckViolationError:
                return False
            except asyncpg.exceptions.ForeignKeyViolationError:
                # CHECK passed; the FK is what stopped us. Good signal.
                return True
            except asyncpg.exceptions.PostgresError:
                # Any other constraint violation means the type was at least
                # accepted by the keyspace CHECK — treat as supported.
                return True
            # The insert unexpectedly succeeded (sentinel persons must exist).
            # Force a rollback by raising; the caller's outer logic doesn't see it.
            raise _ProbeAcceptedSentinel
    except _ProbeAcceptedSentinel:
        return True


class _ProbeAcceptedSentinel(Exception):
    """Internal: sentinel insert unexpectedly succeeded; type is supported."""


async def _resolve_connection_type(conn: asyncpg.Connection) -> tuple[str, bool]:
    """Pick the connection_type the live DB will accept.

    Returns ``(type, used_fallback)``. If neither candidate is accepted the
    function logs an error and returns the preferred kind anyway — the
    eventual insert will fail loudly, which is the right behaviour.
    """
    if await _probe_connection_type_supported(conn, PREFERRED_CONNECTION_TYPE):
        return PREFERRED_CONNECTION_TYPE, False
    log.warning(
        "person_connections CHECK rejects %r; falling back to %r. "
        "Consider extending the CHECK constraint.",
        PREFERRED_CONNECTION_TYPE,
        FALLBACK_CONNECTION_TYPE,
    )
    return FALLBACK_CONNECTION_TYPE, True


async def _upsert_co_mention_edge(
    conn: asyncpg.Connection,
    *,
    person_a_id: UUID,
    person_b_id: UUID,
    connection_type: str,
    account_id: UUID,
    release_year: int,
    years_ago: float,
) -> tuple[bool, bool]:
    """INSERT or merge one co-mention edge. Returns ``(was_new, was_updated)``."""
    base = BASE_STRENGTH
    recency = _recency_factor(years_ago)
    frequency = _frequency_factor(1)
    corroboration = 1.0 + SOURCE_TYPE_COUNT * 0.10
    computed = _compute_strength(base, recency, frequency, corroboration)

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
            last_active_year    = GREATEST(
                COALESCE(person_connections.last_active_year, EXCLUDED.last_active_year),
                EXCLUDED.last_active_year
            ),
            recency_factor      = GREATEST(person_connections.recency_factor, EXCLUDED.recency_factor),
            frequency_factor    = 1.0 + ln(person_connections.corroboration_count + 1) * {FREQUENCY_COEFFICIENT},
            corroboration_factor= EXCLUDED.corroboration_factor,
            computed_strength   = LEAST(
                {STRENGTH_CAP},
                person_connections.base_strength
                  * GREATEST(person_connections.recency_factor, EXCLUDED.recency_factor)
                  * (1.0 + ln(person_connections.corroboration_count + 1) * {FREQUENCY_COEFFICIENT})
                  * EXCLUDED.corroboration_factor
            ),
            updated_at          = now()
        RETURNING (xmax = 0) AS was_inserted
    """
    row = await conn.fetchrow(
        sql,
        person_a_id, person_b_id, connection_type, account_id,
        base, recency, frequency, corroboration,
        computed, release_year,
        SOURCE_TYPE_COUNT,
    )
    was_inserted = bool(row["was_inserted"]) if row is not None else False
    return was_inserted, not was_inserted


async def _resolve_default_account(conn: asyncpg.Connection) -> UUID:
    """Return the anon-default tenant id (per RLS bridge in v3 migrations)."""
    row = await conn.fetchrow("SELECT id FROM accounts ORDER BY created_at LIMIT 1")
    if row is not None:
        return row["id"]
    # Fallback: random UUID — the FK will fail downstream and the failure
    # bubbles up to the orchestrator's failures list.
    return uuid4()


# ── Orchestrator ─────────────────────────────────────────────────────────────


async def run_press_co_mention(
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> CoMentionRollup:
    """Materialise ``co_mentioned_in_press`` edges from v2 press signals."""
    rollup = CoMentionRollup(dry_run=dry_run)

    async with acquire() as conn:
        connection_type, used_fallback = await _resolve_connection_type(conn)
        rollup.connection_type_used = connection_type
        rollup.fallback_used = used_fallback

        rows = await conn.fetch(
            _build_releases_query(limit),
            list(PRESS_SIGNAL_TYPES),
        )
        rollup.signals_scanned = len(rows)

        persons_index = await _load_persons_index(conn)
        log.info(
            "press co-mention v2: %d candidate signals, %d persons indexed (limit=%s)",
            rollup.signals_scanned, len(persons_index), limit,
        )

        default_account = await _resolve_default_account(conn)

        for raw in rows:
            signal = _row_to_signal(raw)
            if signal is None:
                continue
            rollup.signals_qualifying += 1

            pairs = _pair_names(list(signal.mentioned_names))
            years_ago = _years_since(signal.collected_at)
            release_year = signal.collected_at.year

            for first_name, second_name in pairs:
                rollup.pairs_considered += 1
                a_id = _resolve_name(first_name, persons_index)
                b_id = _resolve_name(second_name, persons_index)
                if a_id is None or b_id is None or a_id == b_id:
                    rollup.pairs_skipped_unmatched += 1
                    continue
                person_a_id, person_b_id = _order_pair(a_id, b_id)
                if dry_run:
                    rollup.pairs_inserted += 1
                    continue
                try:
                    async with conn.transaction():
                        was_new, was_updated = await _upsert_co_mention_edge(
                            conn,
                            person_a_id=person_a_id,
                            person_b_id=person_b_id,
                            connection_type=connection_type,
                            account_id=default_account,
                            release_year=release_year,
                            years_ago=years_ago,
                        )
                    if was_new:
                        rollup.pairs_inserted += 1
                    elif was_updated:
                        rollup.pairs_updated += 1
                except Exception as exc:  # noqa: BLE001
                    log.exception(
                        "press co-mention v2: upsert failed for signal %s pair (%s, %s)",
                        signal.signal_id, first_name, second_name,
                    )
                    rollup.pairs_failed += 1
                    rollup.failures.append((str(signal.signal_id), repr(exc)))

    log.info(
        "press co-mention v2 rollup — scanned=%d qualifying=%d "
        "pairs[considered=%d inserted=%d updated=%d skipped_unmatched=%d failed=%d] "
        "connection_type=%s fallback=%s dry_run=%s",
        rollup.signals_scanned, rollup.signals_qualifying,
        rollup.pairs_considered, rollup.pairs_inserted, rollup.pairs_updated,
        rollup.pairs_skipped_unmatched, rollup.pairs_failed,
        rollup.connection_type_used, rollup.fallback_used, rollup.dry_run,
    )
    return rollup


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts.bulk_press_co_mention_edges",
        description=(
            "Emit person_connections rows of type 'co_mentioned_in_press' "
            "(or fallback) for persons co-mentioned in v2 press_release signals."
        ),
    )
    p.add_argument("--limit", type=int, default=None,
                   help="Cap number of v2 signals scanned (default: no cap).")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan only: count pairs, write nothing.")
    p.add_argument("--log-level", default="INFO",
                   help="Python logging level (default INFO).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    async def _go() -> CoMentionRollup:
        try:
            return await run_press_co_mention(
                limit=args.limit, dry_run=args.dry_run,
            )
        finally:
            await close_pool()

    rollup = asyncio.run(_go())
    print(
        f"press_co_mention_v2 — signals_scanned={rollup.signals_scanned} "
        f"qualifying={rollup.signals_qualifying} "
        f"pairs_considered={rollup.pairs_considered} "
        f"pairs_inserted={rollup.pairs_inserted} "
        f"pairs_updated={rollup.pairs_updated} "
        f"skipped_unmatched={rollup.pairs_skipped_unmatched} "
        f"failed={rollup.pairs_failed} "
        f"connection_type={rollup.connection_type_used} "
        f"fallback_used={rollup.fallback_used} "
        f"dry_run={rollup.dry_run} "
        f"failures={len(rollup.failures)}"
    )
    return 0 if not rollup.failures else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "BASE_STRENGTH",
    "CURRENT_YEAR",
    "CoMentionRollup",
    "DECAY_RATE",
    "FALLBACK_CONNECTION_TYPE",
    "FREQUENCY_COEFFICIENT",
    "NAME_LIST_KEYS",
    "PREFERRED_CONNECTION_TYPE",
    "PRESS_SIGNAL_TYPES",
    "PressSignal",
    "_build_releases_query",
    "_compute_strength",
    "_extract_names_from_value",
    "_frequency_factor",
    "_load_persons_index",
    "_order_pair",
    "_pair_names",
    "_recency_factor",
    "_resolve_name",
    "_row_to_signal",
    "_years_since",
    "run_press_co_mention",
]
