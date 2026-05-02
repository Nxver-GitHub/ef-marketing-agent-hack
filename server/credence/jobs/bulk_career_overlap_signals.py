"""Bulk per-account career-overlap signal runner.

Reads ``employment_periods`` rows scoped by ``account_id``, groups them by
``company_id``, finds every overlapping ``(person_a, person_b)`` pair (UUID
lexical order: ``person_a < person_b``), classifies the overlap kind, and
emits **v2 signal rows** of type:

- ``career_overlap_same_team``     (shared ``inferred_team``)
- ``career_overlap_same_domain``   (shared ``functional_domain`` AND
                                    ``ABS(seniority_score) < 10``)
- ``career_overlap_general``       (everything else with ``overlap_years ≥ 1``)

This is a **sister** runner to ``career_overlap_clustering.py`` (which writes
the v3 ``person_connections`` table). The two co-exist; this one is a
write-only data pipeline that targets the v2 ``signals`` table so the
frontend's existing fifth pass renders career-overlap edges immediately —
no migration required.

## persons ↔ prospects ID assumption

``employment_periods.person_id`` references ``persons.id`` (v3 UUIDs).
``signals.prospect_id`` references ``prospects.id`` (v2 UUIDs). The two
tables are not formally linked yet (a future migration), but in practice
v3 backfill carried the v2 UUID forward — see
``backfill_v3.py:upsert_person`` which inherits ``prospect.id`` when no
existing person matches.

The assumption ``persons.id == prospects.id`` is **verified at job start**
via a join count check. If the assumption holds we proceed; if it fails
(persons whose ids do NOT match a prospect) we abort with a clear error.

## Idempotency

Re-runs do not duplicate. Before INSERTing a signal we run an explicit
``SELECT 1 ... LIMIT 1`` keyed on
``(prospect_id, signal_type, value->>'company_id', value->>'connected_to')``.

## CLI

::

    cd server && uv run python -m credence.jobs.bulk_career_overlap_signals \\
        --account-id <uuid> --limit 5000 --dry-run

    cd server && uv run python -m credence.jobs.bulk_career_overlap_signals \\
        --all-accounts
"""
from __future__ import annotations

import argparse
import asyncio
import logging
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


SIGNAL_SOURCE = "v2_employment_period_extraction"

SIGNAL_TYPE_SAME_TEAM = "career_overlap_same_team"
SIGNAL_TYPE_SAME_DOMAIN = "career_overlap_same_domain"
SIGNAL_TYPE_GENERAL = "career_overlap_general"

# Sentinel for COALESCE(end_year, NOW_YEAR). Matches CLAUDE.md L657.
NOW_YEAR = 2025

# Minimum overlap to count. < 1y is noise (same-year hires/leavers).
MIN_OVERLAP_YEARS = 1

# Seniority gap threshold for "same_domain" (CLAUDE.md L811).
SAME_DOMAIN_GAP_THRESHOLD = 10

DEFAULT_CORROBORATION_COUNT = 1


# ── SQL ──────────────────────────────────────────────────────────────────────


SELECT_EMPLOYMENT_PERIODS_SQL = """
SELECT ep.person_id, ep.company_id, ep.title, ep.functional_domain, ep.seniority_score,
       ep.start_year, ep.end_year, ep.is_current, ep.inferred_team,
       p.source_prospect_id
FROM employment_periods ep
JOIN persons p ON p.id = ep.person_id
WHERE ep.start_year IS NOT NULL
  AND ep.account_id = $1
  AND p.source_prospect_id IS NOT NULL
ORDER BY ep.company_id, ep.person_id
"""

SELECT_EMPLOYMENT_PERIODS_LIMIT_SQL = SELECT_EMPLOYMENT_PERIODS_SQL + "LIMIT $2\n"

SELECT_ALL_ACCOUNTS_SQL = """
SELECT DISTINCT account_id
FROM employment_periods
WHERE start_year IS NOT NULL AND account_id IS NOT NULL
ORDER BY account_id
"""

# Per-account guard: count persons whose ids appear in BOTH persons and
# prospects. We compare to the count of distinct person_ids in
# employment_periods for this account.
COUNT_PERSON_PROSPECT_OVERLAP_SQL = """
SELECT
    (SELECT COUNT(DISTINCT person_id) FROM employment_periods
        WHERE account_id = $1 AND start_year IS NOT NULL) AS persons_in_scope,
    (SELECT COUNT(*) FROM persons p
        WHERE p.source_prospect_id IS NOT NULL
        AND p.id IN (
            SELECT DISTINCT person_id FROM employment_periods
            WHERE account_id = $1 AND start_year IS NOT NULL
        )
    ) AS matched
"""

SELECT_COMPANY_NAMES_SQL = """
SELECT id, canonical_name
FROM companies
WHERE id = ANY($1::uuid[])
"""

SIGNAL_EXISTS_SQL = (
    "SELECT 1 FROM signals "
    "WHERE prospect_id = $1 AND signal_type = $2 "
    "AND value->>'company_id' = $3 "
    "AND value->>'connected_to' = $4 "
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
class EmploymentRow:
    """One row from ``employment_periods`` scoped to a single account.

    `source_prospect_id` is populated by the persons↔prospects link migration
    (20260501_v3_persons_prospect_link.sql). Always non-null in the rows
    returned by SELECT_EMPLOYMENT_PERIODS_SQL — the JOIN + WHERE filter
    excludes unresolved persons before they reach this dataclass.
    """

    person_id: UUID
    company_id: UUID
    title: str | None
    functional_domain: str | None
    seniority_score: int | None
    start_year: int
    end_year: int | None
    is_current: bool
    inferred_team: str | None
    source_prospect_id: UUID


@dataclass(frozen=True, slots=True)
class OverlapPair:
    """One ordered overlap pair within a company (a.person_id < b.person_id)."""

    a: EmploymentRow
    b: EmploymentRow
    company_id: UUID
    overlap_start_year: int
    overlap_end_year: int
    overlap_years: int
    signal_type: str


@dataclass(frozen=True, slots=True)
class CareerOverlapRollup:
    """Aggregate counters for one ``bulk_career_overlap_signals_account`` call."""

    account_id: UUID
    employment_periods_read: int = 0
    company_groups: int = 0
    pairs_emitted: int = 0
    signals_inserted: int = 0
    signals_skipped_dedup: int = 0
    pairs_skipped_id_assumption_fail: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


# ── Pure planning helpers ───────────────────────────────────────────────────


def _classify_overlap_kind(a: EmploymentRow, b: EmploymentRow) -> str:
    """Classify an overlap pair into one of the three signal_types.

    Order matters: same_team beats same_domain beats general. A pair always
    classifies (no None return) — minimum overlap is checked separately by
    ``_overlap_years``.
    """
    if (
        a.inferred_team is not None
        and b.inferred_team is not None
        and a.inferred_team == b.inferred_team
    ):
        return SIGNAL_TYPE_SAME_TEAM
    if (
        a.functional_domain is not None
        and a.functional_domain == b.functional_domain
        and a.seniority_score is not None
        and b.seniority_score is not None
        and abs(a.seniority_score - b.seniority_score) < SAME_DOMAIN_GAP_THRESHOLD
    ):
        return SIGNAL_TYPE_SAME_DOMAIN
    return SIGNAL_TYPE_GENERAL


def _overlap_years(a: EmploymentRow, b: EmploymentRow, *, now_year: int = NOW_YEAR) -> int:
    """Compute year-range overlap between two tenures.

    Open-ended end_year (None) is treated as ``now_year``. Returns 0 when
    the tenures do not overlap (no negatives).
    """
    a_end = a.end_year if a.end_year is not None else now_year
    b_end = b.end_year if b.end_year is not None else now_year
    if a.start_year > b_end or b.start_year > a_end:
        return 0
    overlap = min(a_end, b_end) - max(a.start_year, b.start_year)
    return max(0, overlap)


def _overlap_window(
    a: EmploymentRow, b: EmploymentRow, *, now_year: int = NOW_YEAR
) -> tuple[int, int]:
    """Return (overlap_start_year, overlap_end_year). Caller filters by years."""
    a_end = a.end_year if a.end_year is not None else now_year
    b_end = b.end_year if b.end_year is not None else now_year
    return max(a.start_year, b.start_year), min(a_end, b_end)


def _pairs_within_company(
    rows: list[EmploymentRow], *, now_year: int = NOW_YEAR
) -> Iterator[OverlapPair]:
    """Yield every qualifying ordered overlap pair within one company.

    Invariant: ``a.person_id < b.person_id`` (UUID lexical compare). Skips
    self-pairs and same-person duplicate tenures. Skips pairs whose
    overlap is < ``MIN_OVERLAP_YEARS``.
    """
    n = len(rows)
    for i in range(n):
        a = rows[i]
        for j in range(i + 1, n):
            b = rows[j]
            if a.person_id == b.person_id:
                continue
            # Enforce ordering — caller may not have sorted by person_id.
            if a.person_id < b.person_id:
                left, right = a, b
            else:
                left, right = b, a
            overlap = _overlap_years(left, right, now_year=now_year)
            if overlap < MIN_OVERLAP_YEARS:
                continue
            start, end = _overlap_window(left, right, now_year=now_year)
            yield OverlapPair(
                a=left,
                b=right,
                company_id=left.company_id,
                overlap_start_year=start,
                overlap_end_year=end,
                overlap_years=overlap,
                signal_type=_classify_overlap_kind(left, right),
            )


def _pairs_from_index(
    rows_by_company: dict[UUID, list[EmploymentRow]],
    *,
    now_year: int = NOW_YEAR,
) -> Iterator[OverlapPair]:
    """Yield pairs for every company group, deduping across multiple
    tenures of the same person at the same company.

    If a person has two stints at the same company, we keep the longest
    (or first encountered) for pairing — we're emitting one signal per
    pair, not per tenure.
    """
    for _company_id, rows in rows_by_company.items():
        if len(rows) < 2:
            continue
        # Dedupe: keep one row per person_id (the one with earliest start
        # year — represents the broadest possible window).
        by_person: dict[UUID, EmploymentRow] = {}
        for r in rows:
            existing = by_person.get(r.person_id)
            if existing is None or r.start_year < existing.start_year:
                by_person[r.person_id] = r
        deduped = sorted(by_person.values(), key=lambda r: r.person_id)
        if len(deduped) < 2:
            continue
        yield from _pairs_within_company(deduped, now_year=now_year)


def _group_by_company(rows: list[EmploymentRow]) -> dict[UUID, list[EmploymentRow]]:
    out: dict[UUID, list[EmploymentRow]] = {}
    for r in rows:
        out.setdefault(r.company_id, []).append(r)
    return out


def _build_structured_value(
    pair: OverlapPair, *, company_name: str | None
) -> dict[str, Any]:
    """Structured JSONB payload for one emitted signal row.

    Mirrors career_overlap_clustering's evidence layout but stripped to the
    v2 ``signals.value`` shape (≤4KB, no raw blobs).
    """
    seniority_gap: int | None = None
    if pair.a.seniority_score is not None and pair.b.seniority_score is not None:
        seniority_gap = abs(pair.a.seniority_score - pair.b.seniority_score)
    # connected_to is the partner's PROSPECT_ID, not their person_id — the
    # frontend's fifth pass keys nodes on `person:${prospect_id}`. Translation
    # happens here at write time using the source_prospect_id link populated
    # by 20260501_v3_persons_prospect_link.sql.
    return {
        "connected_to": str(pair.b.source_prospect_id),
        "company_id": str(pair.company_id),
        "company_name": company_name,
        "overlap_start_year": pair.overlap_start_year,
        "overlap_end_year": pair.overlap_end_year,
        "overlap_years": pair.overlap_years,
        "team_a": pair.a.inferred_team,
        "team_b": pair.b.inferred_team,
        "domain_a": pair.a.functional_domain,
        "domain_b": pair.b.functional_domain,
        "seniority_gap": seniority_gap,
    }


# ── DB helpers ───────────────────────────────────────────────────────────────


async def _fetch_employment_periods(
    conn: asyncpg.Connection,
    account_id: UUID,
    limit: int | None,
) -> list[EmploymentRow]:
    if limit is None:
        records = await conn.fetch(SELECT_EMPLOYMENT_PERIODS_SQL, account_id)
    else:
        records = await conn.fetch(
            SELECT_EMPLOYMENT_PERIODS_LIMIT_SQL, account_id, int(limit)
        )
    out: list[EmploymentRow] = []
    for r in records:
        out.append(
            EmploymentRow(
                person_id=r["person_id"],
                company_id=r["company_id"],
                title=r["title"],
                functional_domain=r["functional_domain"],
                seniority_score=(
                    int(r["seniority_score"])
                    if r["seniority_score"] is not None
                    else None
                ),
                start_year=int(r["start_year"]),
                end_year=int(r["end_year"]) if r["end_year"] is not None else None,
                is_current=bool(r["is_current"]) if r["is_current"] is not None else False,
                inferred_team=r["inferred_team"],
                source_prospect_id=r["source_prospect_id"],
            )
        )
    return out


async def _fetch_all_account_ids(conn: asyncpg.Connection) -> list[UUID]:
    rows = await conn.fetch(SELECT_ALL_ACCOUNTS_SQL)
    return [r["account_id"] for r in rows]


async def _fetch_company_names(
    conn: asyncpg.Connection, company_ids: list[UUID]
) -> dict[UUID, str]:
    if not company_ids:
        return {}
    rows = await conn.fetch(SELECT_COMPANY_NAMES_SQL, company_ids)
    return {r["id"]: r["canonical_name"] for r in rows}


async def _verify_id_assumption(
    conn: asyncpg.Connection, account_id: UUID
) -> tuple[bool, int, int]:
    """Sanity-check the persons↔prospects link is populated.

    Returns ``(ok, persons_in_scope, matched)``. ``ok`` is True when AT LEAST
    ONE persons row in scope has source_prospect_id populated. Most past-
    employer-derived persons (created from career_history role entries) are
    NOT linked to a prospect — that's expected, not a failure mode. The
    runner's main SELECT filters to ``WHERE p.source_prospect_id IS NOT NULL``
    so only resolvable rows participate. The guard now exists purely to
    detect "linkage migration not applied" — a harder failure that would
    return matched=0 across the board.
    """
    row = await conn.fetchrow(COUNT_PERSON_PROSPECT_OVERLAP_SQL, account_id)
    if row is None:
        return True, 0, 0
    persons = int(row["persons_in_scope"] or 0)
    matched = int(row["matched"] or 0)
    # Empty scope → ok. Some matches → ok (we work with what we can resolve).
    # Zero matches across non-empty scope → migration probably hasn't run.
    return (persons == 0 or matched > 0), persons, matched


async def _signal_exists(
    conn: asyncpg.Connection,
    prospect_id: UUID,
    signal_type: str,
    company_id: str,
    connected_to: str,
) -> bool:
    row = await conn.fetchval(
        SIGNAL_EXISTS_SQL,
        prospect_id,
        signal_type,
        company_id,
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
    # natively. Using ``json.dumps(dict)`` instead would produce a Python str,
    # which asyncpg JSON-encodes a SECOND time before the ``::jsonb`` cast —
    # Postgres parses the result as a jsonb-typed *string* (jsonb_typeof
    # returns 'string'), opaque to ``value->>'key'`` subscripts and unreadable
    # by the frontend's fifth pass. See bulk_education_signals._insert_signal
    # for the original incident note.
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


async def bulk_career_overlap_signals_account(
    account_id: UUID,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    now_year: int = NOW_YEAR,
) -> CareerOverlapRollup:
    """Build the per-account career-overlap pair set and emit signals."""

    employment_periods_read = 0
    company_groups = 0
    pairs_emitted = 0
    signals_inserted = 0
    signals_skipped_dedup = 0
    pairs_skipped_id_assumption_fail = 0
    errors: list[str] = []

    # Step 1 — verify ID assumption + load rows.
    async with acquire() as conn:
        ok, persons_in_scope, matched = await _verify_id_assumption(conn, account_id)
        if not ok:
            msg = (
                f"persons↔prospects ID assumption FAILED for account {account_id}: "
                f"persons_in_scope={persons_in_scope} matched={matched}. "
                "Aborting to avoid emitting signals with mismatched prospect_ids."
            )
            log.error(msg)
            return CareerOverlapRollup(
                account_id=account_id,
                employment_periods_read=0,
                pairs_skipped_id_assumption_fail=persons_in_scope - matched,
                errors=[msg],
                dry_run=dry_run,
            )
        rows = await _fetch_employment_periods(conn, account_id, limit)
    employment_periods_read = len(rows)
    log.info(
        "career_overlap_signals start account=%s rows=%d dry_run=%s",
        account_id, employment_periods_read, dry_run,
    )

    # Step 2 — pure-function planning.
    rows_by_company = _group_by_company(rows)
    company_groups = sum(1 for v in rows_by_company.values() if len(v) >= 2)
    pair_list = list(_pairs_from_index(rows_by_company, now_year=now_year))
    pairs_emitted = len(pair_list)

    log.info(
        "career_overlap_signals planned account=%s companies=%d pairs=%d",
        account_id, company_groups, pairs_emitted,
    )

    # Step 3 — fetch company names for evidence labelling.
    company_ids = list({p.company_id for p in pair_list})
    if company_ids:
        async with acquire() as conn:
            company_names = await _fetch_company_names(conn, company_ids)
    else:
        company_names = {}

    if dry_run:
        for pair in pair_list:
            log.info(
                "[dry-run] would emit %s↔%s %s company=%s years=%d-%d (%dy)",
                pair.a.person_id, pair.b.person_id, pair.signal_type,
                company_names.get(pair.company_id, str(pair.company_id)),
                pair.overlap_start_year, pair.overlap_end_year, pair.overlap_years,
            )
        return CareerOverlapRollup(
            account_id=account_id,
            employment_periods_read=employment_periods_read,
            company_groups=company_groups,
            pairs_emitted=pairs_emitted,
            signals_inserted=0,
            signals_skipped_dedup=0,
            pairs_skipped_id_assumption_fail=0,
            errors=errors,
            dry_run=True,
        )

    # Step 4 — persist with explicit dedupe.
    async with acquire() as conn:
        for pair in pair_list:
            try:
                inserted, deduped = await _persist_pair(
                    conn,
                    account_id=account_id,
                    pair=pair,
                    company_name=company_names.get(pair.company_id),
                    now_year=now_year,
                )
                signals_inserted += inserted
                signals_skipped_dedup += deduped
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{pair.a.person_id}->{pair.b.person_id}: {exc!r}")
                log.exception(
                    "career_overlap_signals persist failed for %s↔%s",
                    pair.a.person_id, pair.b.person_id,
                )

    log.info(
        "career_overlap_signals done account=%s inserted=%d skipped_dedup=%d errors=%d",
        account_id, signals_inserted, signals_skipped_dedup, len(errors),
    )
    return CareerOverlapRollup(
        account_id=account_id,
        employment_periods_read=employment_periods_read,
        company_groups=company_groups,
        pairs_emitted=pairs_emitted,
        signals_inserted=signals_inserted,
        signals_skipped_dedup=signals_skipped_dedup,
        pairs_skipped_id_assumption_fail=0,
        errors=errors,
        dry_run=False,
    )


async def _persist_pair(
    conn: asyncpg.Connection,
    *,
    account_id: UUID,
    pair: OverlapPair,
    company_name: str | None,
    now_year: int,
) -> tuple[int, int]:
    """Persist one signal row keyed on pair.a's source_prospect_id.

    The signal is owned by the prospect that backs pair.a's person record;
    `connected_to` (in structured_value) is the prospect id that backs
    pair.b. Both translations rely on the persons.source_prospect_id link
    populated by 20260501_v3_persons_prospect_link.sql.
    """
    structured = _build_structured_value(pair, company_name=company_name)
    if await _signal_exists(
        conn,
        pair.a.source_prospect_id,
        pair.signal_type,
        str(pair.company_id),
        str(pair.b.source_prospect_id),
    ):
        return (0, 1)
    years_since_active = max(0, now_year - pair.overlap_end_year)
    confidence = compute_strength_for_type(
        pair.signal_type,
        years_since_active=float(years_since_active),
        corroboration_count=DEFAULT_CORROBORATION_COUNT,
    )
    await _insert_signal(
        conn,
        pair.a.source_prospect_id,
        account_id,
        pair.signal_type,
        structured,
        confidence,
    )
    return (1, 0)


async def bulk_career_overlap_signals_all_accounts(
    *,
    limit: int | None = None,
    dry_run: bool = False,
    now_year: int = NOW_YEAR,
) -> list[CareerOverlapRollup]:
    """Iterate every account with employment_periods and emit overlap signals."""
    async with acquire() as conn:
        account_ids = await _fetch_all_account_ids(conn)
    log.info("career_overlap_signals all-accounts: %d accounts", len(account_ids))
    rollups: list[CareerOverlapRollup] = []
    for account_id in account_ids:
        rollup = await bulk_career_overlap_signals_account(
            account_id, limit=limit, dry_run=dry_run, now_year=now_year,
        )
        rollups.append(rollup)
    return rollups


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.bulk_career_overlap_signals",
        description=(
            "Bulk per-account career-overlap runner → emits "
            "career_overlap_same_team / career_overlap_same_domain / "
            "career_overlap_general signal rows from employment_periods."
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
        help="Iterate every account with employment_periods rows.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap employment_periods read per account (default: no cap).",
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


def _print_rollup(rollup: CareerOverlapRollup) -> None:
    msg = (
        f"career_overlap_signals account={rollup.account_id} "
        f"employment_periods_read={rollup.employment_periods_read} "
        f"company_groups={rollup.company_groups} "
        f"pairs_emitted={rollup.pairs_emitted} "
        f"signals_inserted={rollup.signals_inserted} "
        f"signals_skipped_dedup={rollup.signals_skipped_dedup} "
        f"pairs_skipped_id_assumption_fail={rollup.pairs_skipped_id_assumption_fail} "
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

    async def _go() -> list[CareerOverlapRollup]:
        try:
            if args.all_accounts:
                return await bulk_career_overlap_signals_all_accounts(
                    limit=args.limit, dry_run=args.dry_run,
                )
            return [
                await bulk_career_overlap_signals_account(
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
