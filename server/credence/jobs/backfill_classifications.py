"""Backfill ``functional_domain`` + ``seniority_score`` from titles.

Pure inference job — no API calls. Reads rows where the title column is set
but one (or both) classification columns are NULL, applies
:func:`credence.taxonomy.domain_from_title` and
:func:`credence.taxonomy.seniority_from_title`, then UPDATEs the rows in
batches of 1k via ``UPDATE ... FROM (VALUES ...) AS t(id, fd, ss) ...``.

## Why a separate job

Title classification was always a runtime fallback (orgchart/clustering.py
falls through to the taxonomy when the canonical column is NULL). That's
fine for one-off reads but produces a 65–83% NULL ceiling on
``employment_periods.functional_domain`` and ``persons.current_functional_domain``,
which:

1. Suppresses orgchart clustering — DarkBeaver's audit (msg 217) found 35
   zero-cluster companies because every person resolved to ``domain=None``.
2. Forces every read site to redo the classification work, repeatedly.
3. Hides genuinely unknown titles in the same NULL bucket as classifiable
   ones — we lose the signal that something needs human triage.

Backfilling once writes the cached classification to the canonical column;
read sites become trivial COALESCE-from-column reads with no Python work.

## Idempotency

Strict ``WHERE column IS NULL`` filter — never overwrites a value an
operator (or a Phase-1 enrichment) has already set. Re-runs only touch
rows that arrived since the last run.

## CLI

::

    cd server && uv run python -m credence.jobs.backfill_classifications \\
        --account-id <uuid> --limit 100 --dry-run

    cd server && uv run python -m credence.jobs.backfill_classifications \\
        --all --batch-size 1000

The ``--targets`` flag is comma-separated subset selection over the four
columns we touch — useful for staged rollouts (e.g., backfill seniority
first, audit, then domain).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Iterable
from uuid import UUID

import asyncpg

from ..db import acquire, close_pool
from ..taxonomy import domain_from_title, seniority_from_title

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


DEFAULT_BATCH_SIZE = 1000

# Targetable columns. Each entry maps a CLI key to the table + canonical
# columns the backfill writes. Keep this in sync with the CHECK constraint
# keyspace (taxonomy.FUNCTIONAL_DOMAINS) — anything else triggers a
# constraint violation at UPDATE time.
TARGET_EMP_DOMAIN = "emp_domain"
TARGET_EMP_SENIORITY = "emp_seniority"
TARGET_PERSON_DOMAIN = "person_domain"
TARGET_PERSON_SENIORITY = "person_seniority"
ALL_TARGETS: tuple[str, ...] = (
    TARGET_EMP_DOMAIN,
    TARGET_EMP_SENIORITY,
    TARGET_PERSON_DOMAIN,
    TARGET_PERSON_SENIORITY,
)


# ── SQL ──────────────────────────────────────────────────────────────────────


# Each SELECT pulls (id, title) pairs eligible for classification in the
# current account scope. The ``LIMIT $N`` form is appended at call time.
SELECT_EMP_DOMAIN_NEEDED_SQL = """
SELECT id, title
FROM employment_periods
WHERE account_id = $1
  AND title IS NOT NULL
  AND title <> ''
  AND functional_domain IS NULL
ORDER BY id
"""

SELECT_EMP_SENIORITY_NEEDED_SQL = """
SELECT id, title
FROM employment_periods
WHERE account_id = $1
  AND title IS NOT NULL
  AND title <> ''
  AND seniority_score IS NULL
ORDER BY id
"""

SELECT_PERSON_DOMAIN_NEEDED_SQL = """
SELECT id, current_title AS title
FROM persons
WHERE account_id = $1
  AND current_title IS NOT NULL
  AND current_title <> ''
  AND current_functional_domain IS NULL
ORDER BY id
"""

SELECT_PERSON_SENIORITY_NEEDED_SQL = """
SELECT id, current_title AS title
FROM persons
WHERE account_id = $1
  AND current_title IS NOT NULL
  AND current_title <> ''
  AND current_seniority_score IS NULL
ORDER BY id
"""

# Batched UPDATE via UNNEST — one round-trip per batch. The
# ``COALESCE(target, EXCLUDED)`` pattern is unnecessary because the SELECT
# already filters to NULL rows; the WHERE clause guards against TOCTOU
# races where another writer fills the column in the brief window between
# SELECT and UPDATE.
# Note: employment_periods has no updated_at column (only created_at);
# persons has a trg_persons_touch_updated_at BEFORE UPDATE trigger that
# auto-bumps updated_at. So we only need to write the data column itself.
UPDATE_EMP_DOMAIN_SQL = """
UPDATE employment_periods AS ep
SET functional_domain = u.fd
FROM UNNEST($1::uuid[], $2::text[]) AS u(id, fd)
WHERE ep.id = u.id
  AND ep.functional_domain IS NULL
"""

UPDATE_EMP_SENIORITY_SQL = """
UPDATE employment_periods AS ep
SET seniority_score = u.ss
FROM UNNEST($1::uuid[], $2::smallint[]) AS u(id, ss)
WHERE ep.id = u.id
  AND ep.seniority_score IS NULL
"""

UPDATE_PERSON_DOMAIN_SQL = """
UPDATE persons AS p
SET current_functional_domain = u.fd
FROM UNNEST($1::uuid[], $2::text[]) AS u(id, fd)
WHERE p.id = u.id
  AND p.current_functional_domain IS NULL
"""

UPDATE_PERSON_SENIORITY_SQL = """
UPDATE persons AS p
SET current_seniority_score = u.ss
FROM UNNEST($1::uuid[], $2::smallint[]) AS u(id, ss)
WHERE p.id = u.id
  AND p.current_seniority_score IS NULL
"""

# Distinct-account discovery for ``--all``. Iterates whatever account scopes
# have any eligible row across the four targets so we don't waste a pass on
# empty tenants.
SELECT_ALL_ACCOUNTS_SQL = """
SELECT DISTINCT account_id FROM (
    SELECT account_id FROM employment_periods
        WHERE title IS NOT NULL AND title <> ''
          AND (functional_domain IS NULL OR seniority_score IS NULL)
    UNION
    SELECT account_id FROM persons
        WHERE current_title IS NOT NULL AND current_title <> ''
          AND (current_functional_domain IS NULL OR current_seniority_score IS NULL)
) s
ORDER BY account_id
"""


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class BackfillRollup:
    """Per-account counters for one ``backfill_classifications_account`` run."""

    account_id: UUID
    emp_domain_candidates: int = 0
    emp_domain_classified: int = 0
    emp_domain_updated: int = 0
    emp_seniority_candidates: int = 0
    emp_seniority_classified: int = 0
    emp_seniority_updated: int = 0
    person_domain_candidates: int = 0
    person_domain_classified: int = 0
    person_domain_updated: int = 0
    person_seniority_candidates: int = 0
    person_seniority_classified: int = 0
    person_seniority_updated: int = 0
    dry_run: bool = False
    errors: list[tuple[str, str]] = field(default_factory=list)


# ── Pure planning helpers ───────────────────────────────────────────────────


def _classify_domain_rows(
    rows: Iterable[tuple[UUID, str | None]],
) -> tuple[list[UUID], list[str]]:
    """Apply ``domain_from_title`` to each row; drop unclassifiables."""
    ids: list[UUID] = []
    domains: list[str] = []
    for rid, title in rows:
        domain = domain_from_title(title)
        if domain is None:
            continue
        ids.append(rid)
        domains.append(domain)
    return ids, domains


def _classify_seniority_rows(
    rows: Iterable[tuple[UUID, str | None]],
) -> tuple[list[UUID], list[int]]:
    """Apply ``seniority_from_title`` to each row; drop unclassifiables."""
    ids: list[UUID] = []
    scores: list[int] = []
    for rid, title in rows:
        score = seniority_from_title(title)
        if score is None:
            continue
        ids.append(rid)
        scores.append(score)
    return ids, scores


def _chunked(seq: list, size: int) -> Iterable[list]:
    """Yield ``size``-bounded chunks. Pure helper for batched UPDATEs."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# ── DB helpers ───────────────────────────────────────────────────────────────


async def _fetch_eligible(
    conn: asyncpg.Connection,
    sql: str,
    account_id: UUID,
    limit: int | None,
) -> list[tuple[UUID, str | None]]:
    if limit is not None:
        rows = await conn.fetch(sql + f"LIMIT {int(limit)}", account_id)
    else:
        rows = await conn.fetch(sql, account_id)
    return [(r["id"], r["title"]) for r in rows]


async def _execute_update(
    conn: asyncpg.Connection,
    sql: str,
    ids: list[UUID],
    values: list,
    batch_size: int,
) -> int:
    """Execute the UPDATE in chunks; return total updated row count."""
    total = 0
    for chunk_ids, chunk_vals in zip(_chunked(ids, batch_size), _chunked(values, batch_size)):
        status = await conn.execute(sql, chunk_ids, chunk_vals)
        # asyncpg execute returns "UPDATE N" — extract the count.
        parts = (status or "").split()
        try:
            total += int(parts[-1]) if parts else 0
        except ValueError:
            pass
    return total


async def _fetch_all_account_ids(conn: asyncpg.Connection) -> list[UUID]:
    rows = await conn.fetch(SELECT_ALL_ACCOUNTS_SQL)
    return [r["account_id"] for r in rows]


# ── Public orchestrator ─────────────────────────────────────────────────────


async def backfill_classifications_account(
    account_id: UUID,
    *,
    targets: frozenset[str] = frozenset(ALL_TARGETS),
    limit: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> BackfillRollup:
    """Classify + UPDATE one tenant scope; return per-target rollup."""
    rollup = BackfillRollup(account_id=account_id, dry_run=dry_run)

    async with acquire() as conn:
        # ── employment_periods.functional_domain ────────────────────────
        if TARGET_EMP_DOMAIN in targets:
            emp_rows = await _fetch_eligible(
                conn, SELECT_EMP_DOMAIN_NEEDED_SQL, account_id, limit
            )
            rollup.emp_domain_candidates = len(emp_rows)
            ids, domains = _classify_domain_rows(emp_rows)
            rollup.emp_domain_classified = len(ids)
            if ids and not dry_run:
                rollup.emp_domain_updated = await _execute_update(
                    conn, UPDATE_EMP_DOMAIN_SQL, ids, domains, batch_size
                )
            log.info(
                "emp_domain account=%s candidates=%d classified=%d updated=%d",
                account_id, rollup.emp_domain_candidates,
                rollup.emp_domain_classified, rollup.emp_domain_updated,
            )

        # ── employment_periods.seniority_score ──────────────────────────
        if TARGET_EMP_SENIORITY in targets:
            emp_rows = await _fetch_eligible(
                conn, SELECT_EMP_SENIORITY_NEEDED_SQL, account_id, limit
            )
            rollup.emp_seniority_candidates = len(emp_rows)
            ids, scores = _classify_seniority_rows(emp_rows)
            rollup.emp_seniority_classified = len(ids)
            if ids and not dry_run:
                rollup.emp_seniority_updated = await _execute_update(
                    conn, UPDATE_EMP_SENIORITY_SQL, ids, scores, batch_size
                )
            log.info(
                "emp_seniority account=%s candidates=%d classified=%d updated=%d",
                account_id, rollup.emp_seniority_candidates,
                rollup.emp_seniority_classified, rollup.emp_seniority_updated,
            )

        # ── persons.current_functional_domain ──────────────────────────
        if TARGET_PERSON_DOMAIN in targets:
            person_rows = await _fetch_eligible(
                conn, SELECT_PERSON_DOMAIN_NEEDED_SQL, account_id, limit
            )
            rollup.person_domain_candidates = len(person_rows)
            ids, domains = _classify_domain_rows(person_rows)
            rollup.person_domain_classified = len(ids)
            if ids and not dry_run:
                rollup.person_domain_updated = await _execute_update(
                    conn, UPDATE_PERSON_DOMAIN_SQL, ids, domains, batch_size
                )
            log.info(
                "person_domain account=%s candidates=%d classified=%d updated=%d",
                account_id, rollup.person_domain_candidates,
                rollup.person_domain_classified, rollup.person_domain_updated,
            )

        # ── persons.current_seniority_score ────────────────────────────
        if TARGET_PERSON_SENIORITY in targets:
            person_rows = await _fetch_eligible(
                conn, SELECT_PERSON_SENIORITY_NEEDED_SQL, account_id, limit
            )
            rollup.person_seniority_candidates = len(person_rows)
            ids, scores = _classify_seniority_rows(person_rows)
            rollup.person_seniority_classified = len(ids)
            if ids and not dry_run:
                rollup.person_seniority_updated = await _execute_update(
                    conn, UPDATE_PERSON_SENIORITY_SQL, ids, scores, batch_size
                )
            log.info(
                "person_seniority account=%s candidates=%d classified=%d updated=%d",
                account_id, rollup.person_seniority_candidates,
                rollup.person_seniority_classified, rollup.person_seniority_updated,
            )

    return rollup


async def backfill_classifications_all_accounts(
    *,
    targets: frozenset[str] = frozenset(ALL_TARGETS),
    limit: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> list[BackfillRollup]:
    async with acquire() as conn:
        account_ids = await _fetch_all_account_ids(conn)
    log.info("backfill all-accounts: %d tenants", len(account_ids))
    rollups: list[BackfillRollup] = []
    for account_id in account_ids:
        rollup = await backfill_classifications_account(
            account_id,
            targets=targets,
            limit=limit,
            batch_size=batch_size,
            dry_run=dry_run,
        )
        rollups.append(rollup)
    return rollups


# ── CLI ──────────────────────────────────────────────────────────────────────


def _parse_targets(raw: str | None) -> frozenset[str]:
    if not raw or raw.strip().lower() in ("all", ""):
        return frozenset(ALL_TARGETS)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    invalid = [p for p in parts if p not in ALL_TARGETS]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"invalid target(s): {invalid}. Allowed: {', '.join(ALL_TARGETS)}"
        )
    return frozenset(parts)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.backfill_classifications",
        description=(
            "Backfill functional_domain + seniority_score from existing titles. "
            "Pure inference, no API calls, idempotent (only fills NULL)."
        ),
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--account-id",
        type=UUID,
        help="Scope to a single accounts.id UUID.",
    )
    scope.add_argument(
        "--all",
        action="store_true",
        help="Iterate every account with eligible NULLs.",
    )
    p.add_argument(
        "--targets",
        type=_parse_targets,
        default=frozenset(ALL_TARGETS),
        help=(
            "Comma-separated subset of targets to backfill. "
            f"Allowed: {', '.join(ALL_TARGETS)} (or 'all'). "
            "Default: all four targets."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap candidate rows scanned per target per account (default: no cap).",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"UPDATE batch size (default {DEFAULT_BATCH_SIZE}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify but skip the UPDATEs; logs the counts only.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return p


def _print_rollup(rollup: BackfillRollup) -> None:
    msg = (
        f"backfill account={rollup.account_id} "
        f"emp_domain[cand={rollup.emp_domain_candidates} "
        f"cls={rollup.emp_domain_classified} upd={rollup.emp_domain_updated}] "
        f"emp_sen[cand={rollup.emp_seniority_candidates} "
        f"cls={rollup.emp_seniority_classified} upd={rollup.emp_seniority_updated}] "
        f"person_domain[cand={rollup.person_domain_candidates} "
        f"cls={rollup.person_domain_classified} upd={rollup.person_domain_updated}] "
        f"person_sen[cand={rollup.person_seniority_candidates} "
        f"cls={rollup.person_seniority_classified} upd={rollup.person_seniority_updated}] "
        f"errors={len(rollup.errors)} dry_run={rollup.dry_run}"
    )
    print(msg)


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    async def _go() -> list[BackfillRollup]:
        try:
            if args.all:
                return await backfill_classifications_all_accounts(
                    targets=args.targets,
                    limit=args.limit,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                )
            return [
                await backfill_classifications_account(
                    args.account_id,
                    targets=args.targets,
                    limit=args.limit,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                )
            ]
        finally:
            await close_pool()

    rollups = asyncio.run(_go())
    for rollup in rollups:
        _print_rollup(rollup)
    return 0 if all(not r.errors for r in rollups) else 1


if __name__ == "__main__":
    raise SystemExit(main())
