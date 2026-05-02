"""Bulk per-account conference co-presenter signal runner.

Reads existing v2 ``signal_type='conference_talk'`` rows (already populated
from the v2 conference talk extraction pipeline), groups prospects by
``(event_normalized, year)``, and emits ``conference_co_presenter``
signals — one row per ordered prospect pair (``person_a < person_b``
lexically) per group.

This is a **write-only** data pipeline using only data we already have:
zero external API calls. The frontend's existing fifth pass
(``src/lib/graph.ts:1023``) already reads ``conference_co_presenter``
signals and renders the co-presenter edges.

## Idempotency

Re-runs do not pile up duplicates. Before INSERTing a signal we run an
explicit ``SELECT 1 ... LIMIT 1`` keyed on
``(prospect_id, signal_type, value->>'event_normalized',
value->>'year', value->>'connected_to')``.

## CLI

::

    cd server && uv run python -m credence.jobs.bulk_conference_talk_extraction \\
        --account-id <uuid> --limit 100 --dry-run

    cd server && uv run python -m credence.jobs.bulk_conference_talk_extraction \\
        --all-accounts
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from ..db import acquire, close_pool
from ..strength import compute_strength_for_type

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


SIGNAL_SOURCE = "v2_conference_talk_extraction"
SOURCE_SIGNAL_TYPE = "conference_talk"
EMITTED_SIGNAL_TYPE = "conference_co_presenter"

DEFAULT_CORROBORATION_COUNT = 1
CURRENT_YEAR = 2025
MIN_VALID_YEAR = 1990
MAX_VALID_YEAR = 2030


# ── SQL ──────────────────────────────────────────────────────────────────────


SELECT_TALK_SIGNALS_SQL = """
SELECT id, prospect_id, value
FROM signals
WHERE signal_type = 'conference_talk'
  AND account_id = $1
  AND value ? 'event'
  AND value ? 'year'
ORDER BY prospect_id
"""

SELECT_TALK_SIGNALS_LIMIT_SQL = SELECT_TALK_SIGNALS_SQL + "LIMIT $2\n"

SELECT_ALL_ACCOUNTS_SQL = """
SELECT DISTINCT account_id
FROM signals
WHERE signal_type = 'conference_talk'
  AND account_id IS NOT NULL
ORDER BY account_id
"""

SIGNAL_EXISTS_SQL = (
    "SELECT 1 FROM signals "
    "WHERE prospect_id = $1 AND signal_type = $2 "
    "AND value->>'event_normalized' = $3 "
    "AND value->>'year' = $4 "
    "AND value->>'connected_to' = $5 "
    "LIMIT 1"
)

INSERT_SIGNAL_SQL = """
INSERT INTO signals (
    id, prospect_id, account_id, source, signal_type,
    value, raw_data, weight, confidence, collected_at
)
VALUES (
    gen_random_uuid(), $1, $2, $3, $4,
    $5::jsonb, NULL, 1.0, $6, NOW()
)
"""


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ConferenceTalkRow:
    """One v2 row from ``signals WHERE signal_type='conference_talk'``."""

    id: UUID
    prospect_id: UUID
    event_raw: str
    year_raw: Any
    title: str
    url: str | None


@dataclass(frozen=True, slots=True)
class TalkEntry:
    """One classified (prospect, event, year) tuple, indexable."""

    prospect_id: UUID
    event_canonical: str
    year: int
    event_raw: str
    title: str


@dataclass(frozen=True, slots=True)
class ConferenceCoPresenterRollup:
    """Aggregate counters for one ``bulk_conference_talk_extraction_account`` call."""

    account_id: UUID
    talks_read: int = 0
    talks_indexed: int = 0
    event_groups: int = 0
    pairs_emitted: int = 0
    signals_inserted: int = 0
    signals_skipped_dedup: int = 0
    signals_skipped_unparseable: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


# ── Pure planning helpers ───────────────────────────────────────────────────


_YEAR_TRAILING = re.compile(r"\b(19|20)\d{2}\b")
_PUNCT_TRAIL = re.compile(r"[\s,.;:!\?\-_/\\]+$")
_WHITESPACE = re.compile(r"\s+")


def _normalize_event_name(event: str) -> str:
    """Strip year suffix + lowercase + collapse whitespace.

    ``'RSA Conference 2022'`` → ``'rsa conference'``
    ``'Black Hat USA 2023'`` → ``'black hat usa'``
    ``'NeurIPS 2024'`` → ``'neurips'``
    Empty / None → ``''``
    """
    if not event:
        return ""
    s = event.strip().lower()
    if not s:
        return ""
    # Strip ALL year tokens (1900-2099) embedded anywhere in the name.
    s = _YEAR_TRAILING.sub(" ", s)
    s = _PUNCT_TRAIL.sub("", s)
    s = _WHITESPACE.sub(" ", s).strip()
    return s


def _parse_year(year: Any) -> int | None:
    """Parse year from string or int. Returns None for invalid/missing.

    Range guard: ``MIN_VALID_YEAR <= year <= MAX_VALID_YEAR``.
    """
    if year is None:
        return None
    try:
        if isinstance(year, bool):
            return None
        if isinstance(year, int):
            y = year
        elif isinstance(year, float):
            y = int(year)
        else:
            s = str(year).strip()
            if not s:
                return None
            # Pull first 4-digit year-shaped substring.
            m = re.search(r"(19|20)\d{2}", s)
            if m:
                y = int(m.group(0))
            else:
                y = int(s)
    except (TypeError, ValueError):
        return None
    if y < MIN_VALID_YEAR or y > MAX_VALID_YEAR:
        return None
    return y


def _index_key(event_canonical: str, year: int) -> tuple[str, int]:
    return (event_canonical, year)


def _entry_from_row(row: ConferenceTalkRow) -> TalkEntry | None:
    """Pure: turn one talk row into an entry, or None if unparseable."""
    event_raw = (row.event_raw or "").strip()
    event_canonical = _normalize_event_name(event_raw)
    if not event_canonical:
        return None
    year = _parse_year(row.year_raw)
    if year is None:
        return None
    return TalkEntry(
        prospect_id=row.prospect_id,
        event_canonical=event_canonical,
        year=year,
        event_raw=event_raw,
        title=(row.title or "").strip(),
    )


def _build_index(
    rows: list[ConferenceTalkRow],
) -> tuple[dict[tuple[str, int], list[TalkEntry]], int]:
    """Build the (event_normalized, year) → [entries] index.

    Returns ``(index, unparseable_count)``. Within a group we dedupe by
    ``prospect_id`` so the same prospect doesn't pair against itself.
    """
    index: dict[tuple[str, int], list[TalkEntry]] = {}
    seen_in_group: dict[tuple[str, int], set[UUID]] = {}
    unparseable = 0
    for row in rows:
        entry = _entry_from_row(row)
        if entry is None:
            unparseable += 1
            continue
        key = _index_key(entry.event_canonical, entry.year)
        seen_set = seen_in_group.setdefault(key, set())
        if entry.prospect_id in seen_set:
            continue
        seen_set.add(entry.prospect_id)
        index.setdefault(key, []).append(entry)
    return index, unparseable


def _pairs_from_index(
    index: dict[tuple[str, int], list[TalkEntry]],
) -> Iterator[tuple[TalkEntry, TalkEntry]]:
    """Yield (a, b) for every ordered pair in every group ≥2.

    Order is enforced via UUID lexical compare so emissions match the
    Postgres ``person_a_id < person_b_id`` invariant.
    """
    for _key, entries in index.items():
        if len(entries) < 2:
            continue
        by_prospect: dict[UUID, TalkEntry] = {}
        for e in entries:
            by_prospect.setdefault(e.prospect_id, e)
        if len(by_prospect) < 2:
            continue
        ordered = sorted(by_prospect.keys())
        for i, pid_a in enumerate(ordered):
            for pid_b in ordered[i + 1:]:
                yield by_prospect[pid_a], by_prospect[pid_b]


def _build_structured_value(
    entry_a: TalkEntry,
    entry_b: TalkEntry,
    *,
    other_prospect_id: UUID,
) -> dict[str, Any]:
    """structured_value for one direction of a co-presenter emission."""
    return {
        "connected_to": str(other_prospect_id),
        "event": entry_a.event_raw or entry_b.event_raw,
        "event_normalized": entry_a.event_canonical,
        "year": entry_a.year,
        "title_a": entry_a.title,
        "title_b": entry_b.title,
    }


# ── DB helpers ───────────────────────────────────────────────────────────────


def _coerce_value(value: Any) -> dict[str, Any]:
    """value may arrive as a JSON-encoded string from asyncpg."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    return value


async def _fetch_talk_signals(
    conn: asyncpg.Connection,
    account_id: UUID,
    limit: int | None,
) -> list[ConferenceTalkRow]:
    if limit is None:
        rows = await conn.fetch(SELECT_TALK_SIGNALS_SQL, account_id)
    else:
        rows = await conn.fetch(
            SELECT_TALK_SIGNALS_LIMIT_SQL, account_id, int(limit)
        )
    out: list[ConferenceTalkRow] = []
    for r in rows:
        value = _coerce_value(r["value"])
        out.append(
            ConferenceTalkRow(
                id=r["id"],
                prospect_id=r["prospect_id"],
                event_raw=str(value.get("event") or ""),
                year_raw=value.get("year"),
                title=str(value.get("title") or ""),
                url=str(value.get("url")) if value.get("url") else None,
            )
        )
    return out


async def _fetch_all_account_ids(conn: asyncpg.Connection) -> list[UUID]:
    rows = await conn.fetch(SELECT_ALL_ACCOUNTS_SQL)
    return [r["account_id"] for r in rows]


async def _signal_exists(
    conn: asyncpg.Connection,
    prospect_id: UUID,
    signal_type: str,
    event_normalized: str,
    year: int,
    connected_to: str,
) -> bool:
    row = await conn.fetchval(
        SIGNAL_EXISTS_SQL,
        prospect_id,
        signal_type,
        event_normalized,
        str(year),
        connected_to,
    )
    return row is not None


async def _insert_signal(
    conn: asyncpg.Connection,
    prospect_id: UUID,
    account_id: UUID,
    signal_type: str,
    structured_value: dict[str, Any],
    confidence: float,
) -> None:
    # Pass the dict directly. asyncpg's jsonb codec encodes dict → jsonb-object
    # natively. Using ``json.dumps(dict)`` would produce a Python str, which
    # asyncpg would then JSON-encode a second time before the ``::jsonb``
    # cast — Postgres parses the result as a jsonb-typed *string*
    # (jsonb_typeof='string'), opaque to ``value->>'key'`` subscripts and
    # unreadable by the frontend's fifth pass.
    await conn.execute(
        INSERT_SIGNAL_SQL,
        prospect_id,
        account_id,
        SIGNAL_SOURCE,
        signal_type,
        structured_value,
        confidence,
    )


# ── Public orchestrator ──────────────────────────────────────────────────────


async def bulk_conference_talk_extraction_account(
    account_id: UUID,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> ConferenceCoPresenterRollup:
    """Build the per-account conference co-presenter index and emit signals."""

    talks_read = 0
    talks_indexed = 0
    event_groups = 0
    pairs_emitted = 0
    signals_inserted = 0
    signals_skipped_dedup = 0
    signals_skipped_unparseable = 0
    errors: list[str] = []

    # Step 1 — load conference_talk signals.
    async with acquire() as conn:
        rows = await _fetch_talk_signals(conn, account_id, limit)
    talks_read = len(rows)
    log.info(
        "conference_co_presenter start account=%s rows=%d dry_run=%s",
        account_id, talks_read, dry_run,
    )

    # Step 2 — pure-function index build.
    index, unparseable = _build_index(rows)
    talks_indexed = sum(len(v) for v in index.values())
    signals_skipped_unparseable = unparseable

    # Step 3 — emit pair tuples.
    pair_tuples = list(_pairs_from_index(index))
    pairs_emitted = len(pair_tuples)
    event_groups = sum(
        1 for entries in index.values()
        if len({e.prospect_id for e in entries}) >= 2
    )

    log.info(
        "conference_co_presenter indexed account=%s talks_indexed=%d "
        "groups=%d pairs=%d unparseable=%d",
        account_id, talks_indexed, event_groups, pairs_emitted,
        signals_skipped_unparseable,
    )

    if dry_run:
        for entry_a, entry_b in pair_tuples:
            log.info(
                "[dry-run] would emit %s↔%s event=%s year=%d",
                entry_a.prospect_id, entry_b.prospect_id,
                entry_a.event_canonical, entry_a.year,
            )
        return ConferenceCoPresenterRollup(
            account_id=account_id,
            talks_read=talks_read,
            talks_indexed=talks_indexed,
            event_groups=event_groups,
            pairs_emitted=pairs_emitted,
            signals_inserted=0,
            signals_skipped_dedup=0,
            signals_skipped_unparseable=signals_skipped_unparseable,
            errors=errors,
            dry_run=True,
        )

    # Step 4 — persist with explicit dedupe.
    async with acquire() as conn:
        for entry_a, entry_b in pair_tuples:
            try:
                inserted, deduped = await _persist_pair(
                    conn,
                    account_id=account_id,
                    entry_a=entry_a,
                    entry_b=entry_b,
                )
                signals_inserted += inserted
                signals_skipped_dedup += deduped
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"{entry_a.prospect_id}->{entry_b.prospect_id}: {exc!r}"
                )
                log.exception(
                    "conference_co_presenter persist failed for %s↔%s",
                    entry_a.prospect_id, entry_b.prospect_id,
                )

    log.info(
        "conference_co_presenter done account=%s inserted=%d "
        "skipped_dedup=%d errors=%d",
        account_id, signals_inserted, signals_skipped_dedup, len(errors),
    )
    return ConferenceCoPresenterRollup(
        account_id=account_id,
        talks_read=talks_read,
        talks_indexed=talks_indexed,
        event_groups=event_groups,
        pairs_emitted=pairs_emitted,
        signals_inserted=signals_inserted,
        signals_skipped_dedup=signals_skipped_dedup,
        signals_skipped_unparseable=signals_skipped_unparseable,
        errors=errors,
        dry_run=False,
    )


async def _persist_pair(
    conn: asyncpg.Connection,
    *,
    account_id: UUID,
    entry_a: TalkEntry,
    entry_b: TalkEntry,
) -> tuple[int, int]:
    """Persist one signal row pointing entry_a → entry_b. Returns (inserted, deduped)."""
    structured = _build_structured_value(
        entry_a, entry_b, other_prospect_id=entry_b.prospect_id
    )
    if await _signal_exists(
        conn,
        entry_a.prospect_id,
        EMITTED_SIGNAL_TYPE,
        entry_a.event_canonical,
        entry_a.year,
        str(entry_b.prospect_id),
    ):
        return (0, 1)
    years_since_active = max(0, CURRENT_YEAR - entry_a.year)
    confidence = compute_strength_for_type(
        EMITTED_SIGNAL_TYPE,
        years_since_active=years_since_active,
        corroboration_count=DEFAULT_CORROBORATION_COUNT,
    )
    await _insert_signal(
        conn,
        entry_a.prospect_id,
        account_id,
        EMITTED_SIGNAL_TYPE,
        structured,
        confidence,
    )
    return (1, 0)


async def bulk_conference_talk_extraction_all_accounts(
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> list[ConferenceCoPresenterRollup]:
    """Iterate every account with conference_talk signals and emit co-presenter rows."""
    async with acquire() as conn:
        account_ids = await _fetch_all_account_ids(conn)
    log.info("conference_co_presenter all-accounts: %d accounts", len(account_ids))
    rollups: list[ConferenceCoPresenterRollup] = []
    for account_id in account_ids:
        rollup = await bulk_conference_talk_extraction_account(
            account_id, limit=limit, dry_run=dry_run,
        )
        rollups.append(rollup)
    return rollups


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.bulk_conference_talk_extraction",
        description=(
            "Bulk per-account conference co-presenter runner → emits "
            "conference_co_presenter signal rows from existing v2 "
            "conference_talk data."
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
        help="Iterate every account with v2 conference_talk signals.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap conference_talk signals read per account (default: no cap).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log emissions without writing to the signals table.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return p


def _print_rollup(rollup: ConferenceCoPresenterRollup) -> None:
    msg = (
        f"conference_co_presenter account={rollup.account_id} "
        f"talks_read={rollup.talks_read} "
        f"talks_indexed={rollup.talks_indexed} "
        f"event_groups={rollup.event_groups} "
        f"pairs_emitted={rollup.pairs_emitted} "
        f"signals_inserted={rollup.signals_inserted} "
        f"signals_skipped_dedup={rollup.signals_skipped_dedup} "
        f"signals_skipped_unparseable={rollup.signals_skipped_unparseable} "
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

    async def _go() -> list[ConferenceCoPresenterRollup]:
        try:
            if args.all_accounts:
                return await bulk_conference_talk_extraction_all_accounts(
                    limit=args.limit, dry_run=args.dry_run,
                )
            return [
                await bulk_conference_talk_extraction_account(
                    args.account_id, limit=args.limit, dry_run=args.dry_run,
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
