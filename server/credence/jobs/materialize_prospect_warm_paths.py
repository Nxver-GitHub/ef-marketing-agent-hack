"""Materialize ``prospect_warm_paths`` from ``person_connections``.

User asks (this iteration):

1. Materialized table ``prospect_warm_paths`` keyed on (prospect_id, rank).
2. Refreshed by a job triggered after each clustering / enrichment run.
3. Per-prospect top-K cap (default K=20) at write time so the long tail of
   weak edges doesn't leak.
4. Keep ``person_connections`` as the canonical write target; this is a
   read-cache only.

## Algorithm

For every (prospect_id) reachable through person_connections via
``persons.source_prospect_id``, take the top-K edges by ``computed_strength``
DESC, denormalize a few partner display fields, and write to
``prospect_warm_paths`` in a single transaction:

1. ``DELETE FROM prospect_warm_paths WHERE account_id = $1`` — wipe the
   tenant's slice (transaction-bounded so a failed refresh doesn't leave
   the table half-written).
2. ``INSERT … SELECT … FROM person_connections JOIN persons …`` — one
   server-side query that does the whole materialization. This avoids the
   per-prospect roundtrip the naive Python approach would take.

The query handles BOTH directions of each person_connections row
(person_a → person_b AND person_b → person_a) so each prospect sees their
own top-K rather than only the half they happen to land on by UUID
ordering.

## Idempotency

Re-running is a no-op modulo refreshed_at. The transaction-scoped
DELETE + INSERT pattern means concurrent refreshes on different accounts
don't collide; same-account concurrent refresh is bounded by the
transaction.

## CLI

::

    cd server && uv run python -m credence.jobs.materialize_prospect_warm_paths \\
        --account-id 00000000-0000-0000-0000-000000000001 --dry-run

    cd server && uv run python -m credence.jobs.materialize_prospect_warm_paths --all-accounts
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from ..db import acquire, close_pool

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


DEFAULT_TOP_K = 20

# Hard upper bound on top-K (matches the CHECK constraint in the migration).
TOP_K_MAX = 20

# Defaults for the optional ``--watch`` daemon mode (msg 207 #6 ask):
# poll person_connections count every N seconds and re-materialize when the
# delta crosses a configurable threshold. 60s + 100-row threshold matches a
# typical clustering-job cadence; tunable via CLI flags.
DEFAULT_WATCH_POLL_SECONDS = 60
DEFAULT_WATCH_THRESHOLD = 100
WATCH_MIN_POLL_SECONDS = 5
WATCH_MIN_THRESHOLD = 1


# ── SQL ──────────────────────────────────────────────────────────────────────


# The materialization query — one server-side pass that:
# 1. Unions both directions of each person_connections row, attaching
#    source_prospect_id from persons (a → b AND b → a).
# 2. Filters to rows where BOTH endpoints have a non-null source_prospect_id
#    (otherwise the partner isn't renderable in /discover anyway).
# 3. Deduplicates same-pair-different-direction by keeping the row with
#    higher computed_strength, then ranks within each prospect_id by
#    computed_strength DESC.
# 4. Caps to top-K per prospect.
# 5. Joins prospects to denormalize partner_name / partner_company /
#    partner_title for the read path.
#
# `$1` is the account_id, `$2` is the K cap.
MATERIALIZE_SQL = """
WITH bidirectional AS (
    -- person_a → person_b direction
    SELECT
        pa.source_prospect_id        AS prospect_id,
        pb.source_prospect_id        AS partner_prospect_id,
        pc.connection_type,
        pc.computed_strength,
        pc.evidence_ids,
        pc.account_id
    FROM person_connections pc
    JOIN persons pa ON pa.id = pc.person_a_id
    JOIN persons pb ON pb.id = pc.person_b_id
    WHERE pa.source_prospect_id IS NOT NULL
      AND pb.source_prospect_id IS NOT NULL
      AND pa.source_prospect_id <> pb.source_prospect_id
      AND pc.account_id = $1

    UNION ALL

    -- person_b → person_a direction (so each prospect sees their own top-K)
    SELECT
        pb.source_prospect_id        AS prospect_id,
        pa.source_prospect_id        AS partner_prospect_id,
        pc.connection_type,
        pc.computed_strength,
        pc.evidence_ids,
        pc.account_id
    FROM person_connections pc
    JOIN persons pa ON pa.id = pc.person_a_id
    JOIN persons pb ON pb.id = pc.person_b_id
    WHERE pa.source_prospect_id IS NOT NULL
      AND pb.source_prospect_id IS NOT NULL
      AND pa.source_prospect_id <> pb.source_prospect_id
      AND pc.account_id = $1
),
deduped AS (
    -- The same (prospect, partner) can appear with multiple connection_types
    -- (e.g. career_overlap_general AND career_overlap_same_domain for the
    -- same pair). The unique constraint on (prospect_id, partner_prospect_id)
    -- forces one row per partner — keep only the strongest connection_type.
    -- Tiebreak on connection_type alphabetically for determinism.
    SELECT DISTINCT ON (prospect_id, partner_prospect_id)
        prospect_id, partner_prospect_id, connection_type,
        computed_strength, evidence_ids, account_id
    FROM bidirectional
    ORDER BY prospect_id, partner_prospect_id, computed_strength DESC, connection_type
),
ranked AS (
    SELECT
        prospect_id,
        partner_prospect_id,
        connection_type,
        computed_strength,
        evidence_ids,
        account_id,
        ROW_NUMBER() OVER (
            PARTITION BY prospect_id
            ORDER BY computed_strength DESC, partner_prospect_id, connection_type
        ) AS rank
    FROM deduped
),
capped AS (
    SELECT * FROM ranked WHERE rank <= $2
),
-- Pull the strongest evidence row per (prospect, partner, connection_type)
-- so the denormalized `evidence` column has something useful. We use the
-- first evidence_id in the array (insertion order within the connection)
-- as a stable choice; if that lookup misses we fall back to '{}'::jsonb.
with_evidence AS (
    SELECT
        c.prospect_id,
        c.rank::smallint,
        c.partner_prospect_id,
        c.connection_type,
        c.computed_strength,
        COALESCE(ce.structured_value, '{}'::jsonb) AS evidence,
        c.account_id
    FROM capped c
    LEFT JOIN connection_evidence ce
        ON ce.id = (CASE
                       WHEN array_length(c.evidence_ids, 1) > 0
                       THEN c.evidence_ids[1]
                       ELSE NULL
                   END)
)
INSERT INTO prospect_warm_paths (
    prospect_id, rank, partner_prospect_id, connection_type,
    computed_strength, evidence,
    partner_name, partner_company, partner_title,
    account_id, refreshed_at
)
SELECT
    we.prospect_id,
    we.rank,
    we.partner_prospect_id,
    we.connection_type,
    we.computed_strength,
    we.evidence,
    pr.name        AS partner_name,
    pr.company     AS partner_company,
    pr.role        AS partner_title,
    we.account_id,
    now()
FROM with_evidence we
JOIN prospects pr ON pr.id = we.partner_prospect_id
"""

DELETE_TENANT_SQL = (
    "DELETE FROM prospect_warm_paths WHERE account_id = $1"
)

SELECT_ALL_ACCOUNTS_SQL = """
SELECT DISTINCT account_id
FROM person_connections
WHERE account_id IS NOT NULL
ORDER BY account_id
"""

COUNT_TENANT_SQL = (
    "SELECT count(*) FROM prospect_warm_paths WHERE account_id = $1"
)

# Used by ``--watch`` to detect when upstream person_connections has grown
# enough to justify a refresh. Bound to a single tenant.
COUNT_PERSON_CONNECTIONS_TENANT_SQL = (
    "SELECT count(*) FROM person_connections WHERE account_id = $1"
)


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class MaterializeRollup:
    """Aggregate counters for one ``materialize_prospect_warm_paths_account`` call."""

    account_id: UUID
    rows_deleted: int = 0
    rows_inserted: int = 0
    top_k: int = DEFAULT_TOP_K
    dry_run: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)


# ── DB helpers ───────────────────────────────────────────────────────────────


def _validate_top_k(top_k: int) -> int:
    """Coerce top_k into [1, TOP_K_MAX]. Raises ValueError on invalid input."""
    if not isinstance(top_k, int) or isinstance(top_k, bool):
        raise ValueError(f"top_k must be int, got {type(top_k).__name__}")
    if top_k < 1:
        raise ValueError(f"top_k must be >= 1, got {top_k}")
    if top_k > TOP_K_MAX:
        raise ValueError(
            f"top_k must be <= {TOP_K_MAX} (matches CHECK constraint), got {top_k}"
        )
    return top_k


async def _fetch_all_account_ids(conn: asyncpg.Connection) -> list[UUID]:
    rows = await conn.fetch(SELECT_ALL_ACCOUNTS_SQL)
    return [r["account_id"] for r in rows]


def _parse_delete_count(execute_status: str) -> int:
    """asyncpg returns 'DELETE n' / 'INSERT 0 n' status strings. Parse the count."""
    parts = execute_status.split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


def _validate_poll_seconds(seconds: int) -> int:
    """Coerce poll-seconds into [WATCH_MIN_POLL_SECONDS, ∞)."""
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        raise ValueError(f"poll-seconds must be int, got {type(seconds).__name__}")
    if seconds < WATCH_MIN_POLL_SECONDS:
        raise ValueError(
            f"poll-seconds must be >= {WATCH_MIN_POLL_SECONDS}, got {seconds} "
            f"(prevents Supabase pool hammering)"
        )
    return seconds


def _validate_threshold(threshold: int) -> int:
    """Coerce threshold into [WATCH_MIN_THRESHOLD, ∞)."""
    if not isinstance(threshold, int) or isinstance(threshold, bool):
        raise ValueError(f"threshold must be int, got {type(threshold).__name__}")
    if threshold < WATCH_MIN_THRESHOLD:
        raise ValueError(
            f"threshold must be >= {WATCH_MIN_THRESHOLD}, got {threshold}"
        )
    return threshold


# ── Public orchestrator ──────────────────────────────────────────────────────


async def materialize_prospect_warm_paths_account(
    account_id: UUID,
    *,
    top_k: int = DEFAULT_TOP_K,
    dry_run: bool = False,
) -> MaterializeRollup:
    """Refresh the prospect_warm_paths read-cache for one tenant."""
    top_k = _validate_top_k(top_k)
    rollup = MaterializeRollup(account_id=account_id, top_k=top_k, dry_run=dry_run)

    if dry_run:
        async with acquire() as conn:
            rows_before = await conn.fetchval(COUNT_TENANT_SQL, account_id)
        log.info(
            "[dry-run] account=%s top_k=%d rows_currently=%d (would TRUNCATE+INSERT)",
            account_id, top_k, rows_before,
        )
        rollup.rows_deleted = 0
        rollup.rows_inserted = 0
        return rollup

    async with acquire() as conn:
        try:
            async with conn.transaction():
                del_status = await conn.execute(DELETE_TENANT_SQL, account_id)
                rollup.rows_deleted = _parse_delete_count(del_status)
                ins_status = await conn.execute(
                    MATERIALIZE_SQL, account_id, top_k,
                )
                rollup.rows_inserted = _parse_delete_count(ins_status)
        except Exception as exc:  # noqa: BLE001
            rollup.failures.append((str(account_id), repr(exc)))
            log.exception(
                "materialize_prospect_warm_paths failed for account %s",
                account_id,
            )
            return rollup

    log.info(
        "materialize_prospect_warm_paths done account=%s top_k=%d "
        "rows_deleted=%d rows_inserted=%d",
        account_id, top_k, rollup.rows_deleted, rollup.rows_inserted,
    )
    return rollup


async def _count_person_connections(account_id: UUID) -> int:
    """Tiny helper for the watch loop — returns current person_connections row
    count for one tenant. Single roundtrip; safe to call frequently."""
    async with acquire() as conn:
        return await conn.fetchval(COUNT_PERSON_CONNECTIONS_TENANT_SQL, account_id)


async def watch_and_refresh_account(
    account_id: UUID,
    *,
    top_k: int = DEFAULT_TOP_K,
    poll_seconds: int = DEFAULT_WATCH_POLL_SECONDS,
    threshold: int = DEFAULT_WATCH_THRESHOLD,
    max_iterations: int | None = None,
    sleep_func: Any = None,
) -> list[MaterializeRollup]:
    """Daemon-mode watch loop: poll person_connections count, refresh on delta ≥ threshold.

    msg 207 (SunnyRidge) ask: a small daemon that detects upstream growth and
    re-materializes the read-cache so the FE never serves a stale view. Single
    tenant only (the multi-tenant variant would need per-account tracking; the
    demo runs one tenant so single-account is enough for now).

    Behavior:
    - First iteration always materializes (treat startup as a "cold" delta).
    - Every ``poll_seconds`` thereafter, query ``count(person_connections)``
      and compare to the count at the last refresh. If the delta is ≥
      ``threshold``, re-materialize. Otherwise sleep and re-poll.

    Args:
        account_id: tenant scope.
        top_k: per-prospect cap (passed through to the materialization).
        poll_seconds: polling cadence in seconds.
        threshold: row-delta floor that triggers a refresh.
        max_iterations: cap on iterations for tests (None = unbounded).
        sleep_func: injectable sleep for deterministic tests.

    Returns:
        List of rollups, one per refresh fired during the watch.
    """
    poll_seconds = _validate_poll_seconds(poll_seconds)
    threshold = _validate_threshold(threshold)
    sleep = sleep_func or asyncio.sleep
    rollups: list[MaterializeRollup] = []
    last_count: int | None = None
    iteration = 0
    log.info(
        "watch start account=%s poll_seconds=%d threshold=%d top_k=%d max_iter=%s",
        account_id, poll_seconds, threshold, top_k, max_iterations,
    )

    while True:
        if max_iterations is not None and iteration >= max_iterations:
            log.info("watch max_iterations=%d reached; exiting", max_iterations)
            break
        iteration += 1
        try:
            current_count = await _count_person_connections(account_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "watch count query failed (iter=%d): %r — sleeping and retrying",
                iteration, exc,
            )
            await sleep(poll_seconds)
            continue

        delta = (current_count - last_count) if last_count is not None else None
        should_refresh = (last_count is None) or (
            delta is not None and delta >= threshold
        )
        log.info(
            "watch iter=%d account=%s person_connections=%d delta=%s threshold=%d refresh=%s",
            iteration, account_id, current_count,
            "cold" if delta is None else delta,
            threshold, should_refresh,
        )

        if should_refresh:
            rollup = await materialize_prospect_warm_paths_account(
                account_id, top_k=top_k, dry_run=False,
            )
            rollups.append(rollup)
            last_count = current_count
            log.info(
                "watch refreshed account=%s rows_inserted=%d (after person_connections=%d)",
                account_id, rollup.rows_inserted, current_count,
            )

        await sleep(poll_seconds)

    return rollups


async def materialize_prospect_warm_paths_all_accounts(
    *,
    top_k: int = DEFAULT_TOP_K,
    dry_run: bool = False,
) -> list[MaterializeRollup]:
    """Iterate every tenant with person_connections and refresh each."""
    async with acquire() as conn:
        account_ids = await _fetch_all_account_ids(conn)
    log.info(
        "materialize_prospect_warm_paths all-accounts: %d accounts",
        len(account_ids),
    )
    rollups: list[MaterializeRollup] = []
    for account_id in account_ids:
        rollup = await materialize_prospect_warm_paths_account(
            account_id, top_k=top_k, dry_run=dry_run,
        )
        rollups.append(rollup)
    return rollups


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.materialize_prospect_warm_paths",
        description=(
            "Refresh prospect_warm_paths read-cache from person_connections. "
            "Top-K cap per prospect (default 20)."
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
        help="Iterate every tenant with person_connections.",
    )
    p.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"Per-prospect cap on edges (default {DEFAULT_TOP_K}, max {TOP_K_MAX}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print row counts without writing.",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Daemon mode: poll person_connections row count + auto-refresh "
            "on delta. Single-account only (--all-accounts not supported)."
        ),
    )
    p.add_argument(
        "--poll-seconds",
        type=int,
        default=DEFAULT_WATCH_POLL_SECONDS,
        help=(
            "Watch-mode poll cadence in seconds (default "
            f"{DEFAULT_WATCH_POLL_SECONDS}, min {WATCH_MIN_POLL_SECONDS})."
        ),
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_WATCH_THRESHOLD,
        help=(
            "Watch-mode row-delta floor that triggers a refresh "
            f"(default {DEFAULT_WATCH_THRESHOLD})."
        ),
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return p


def _print_rollup(rollup: MaterializeRollup) -> None:
    print(
        f"materialize_prospect_warm_paths account={rollup.account_id} "
        f"top_k={rollup.top_k} "
        f"rows_deleted={rollup.rows_deleted} "
        f"rows_inserted={rollup.rows_inserted} "
        f"failures={len(rollup.failures)} "
        f"dry_run={rollup.dry_run}"
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    if args.watch and args.all_accounts:
        print(
            "ERROR: --watch requires --account-id (per-account daemon only); "
            "--all-accounts not supported in watch mode."
        )
        return 2
    if args.watch and args.dry_run:
        print(
            "ERROR: --watch + --dry-run together is meaningless "
            "(watch implies live refreshes). Pick one."
        )
        return 2

    async def _go() -> list[MaterializeRollup]:
        try:
            if args.watch:
                return await watch_and_refresh_account(
                    args.account_id,
                    top_k=args.top_k,
                    poll_seconds=args.poll_seconds,
                    threshold=args.threshold,
                )
            if args.all_accounts:
                return await materialize_prospect_warm_paths_all_accounts(
                    top_k=args.top_k, dry_run=args.dry_run,
                )
            return [
                await materialize_prospect_warm_paths_account(
                    args.account_id, top_k=args.top_k, dry_run=args.dry_run,
                )
            ]
        finally:
            await close_pool()

    rollups = asyncio.run(_go())
    for rollup in rollups:
        _print_rollup(rollup)
    return 0 if all(not r.failures for r in rollups) else 1


if __name__ == "__main__":
    sys.exit(main())
