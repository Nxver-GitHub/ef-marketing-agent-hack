"""Bulk per-account career-history signal runner — Wave 5 Job A.

Reads existing v2 ``signal_type='career_history'`` rows (already populated
from PDL/LinkedIn ingest), groups prospects by canonical past-employer,
and emits ``past_employer`` signals — one row per ordered prospect pair
(``person_a < person_b`` lexically) per shared past employer.

This is a **write-only** data pipeline using only data we already have:
zero external API calls, zero PDL spend. The frontend's existing graph
builder reads ``past_employer`` signal_types and renders the edges directly.

Year data is NOT required (that's the strict ``career_overlap_*`` flow via
``career_overlap_clustering``; this is the looser shared-employer edge,
applied even when temporal evidence is missing).

## Idempotency

Re-runs do not pile up duplicates. Before INSERTing a signal we run an
explicit ``SELECT 1 ... LIMIT 1`` keyed on
``(prospect_id, signal_type, value->>'company_canonical',
value->>'connected_to')``. Same pattern as ``bulk_education_signals``.

## Current-employer exclusion

A role whose normalized company matches the prospect's
``prospects.company`` (current employer) is skipped — it would emit a
``works_at`` edge under a different name and isn't a *past* employer
relationship. The exclusion is exact-after-normalize: same alias table is
applied to both sides.

## CLI

::

    cd server && uv run python -m credence.jobs.bulk_career_history_signals \\
        --account-id <uuid> --limit 100 --dry-run

    cd server && uv run python -m credence.jobs.bulk_career_history_signals \\
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


SIGNAL_SOURCE = "v2_career_history_extraction"
SOURCE_SIGNAL_TYPE = "career_history"
EMITTED_SIGNAL_TYPE = "past_employer"

# A past-employer relationship without explicit overlap years rates as the
# loosest career_overlap variant per CLAUDE.md STRENGTH_TABLE — base 0.60.
# At 10y since-active (a midpoint guess) the resulting confidence ≈ 0.36.
DEFAULT_YEARS_SINCE_ACTIVE = 10.0
DEFAULT_CORROBORATION_COUNT = 1


# Hand-curated alias table — keep small (~20 entries) and deterministic.
# Keys are LOWERCASED post-normalize forms; values are canonical lowercase
# group keys. Mirrors `bulk_education_signals.SCHOOL_ALIASES` shape.
COMPANY_ALIASES: dict[str, str] = {
    "intel corp": "intel",
    "intel corporation": "intel",
    "google": "alphabet",
    "google llc": "alphabet",
    "google inc": "alphabet",
    "alphabet inc": "alphabet",
    "microsoft corp": "microsoft",
    "microsoft corporation": "microsoft",
    "apple inc": "apple",
    "apple computer": "apple",
    "amazon.com": "amazon",
    "amazon web services": "amazon",
    "aws": "amazon",
    "facebook": "meta",
    "facebook inc": "meta",
    "meta platforms": "meta",
    "ibm corp": "ibm",
    "international business machines": "ibm",
    "amd": "advanced micro devices",
    "tsmc": "taiwan semiconductor manufacturing company",
    "asml": "asml holding",
}


# ── SQL ──────────────────────────────────────────────────────────────────────


SELECT_CAREER_HISTORY_SIGNALS_SQL = """
SELECT s.id, s.prospect_id, s.value, p.company AS current_company
FROM signals s
JOIN prospects p ON p.id = s.prospect_id::uuid
WHERE s.signal_type = 'career_history'
  AND s.account_id = $1
  AND s.value ? 'roles'
  AND jsonb_typeof(s.value->'roles') = 'array'
ORDER BY s.prospect_id
"""

SELECT_CAREER_HISTORY_SIGNALS_LIMIT_SQL = (
    SELECT_CAREER_HISTORY_SIGNALS_SQL + "LIMIT $2\n"
)

SELECT_ALL_ACCOUNTS_SQL = """
SELECT DISTINCT account_id
FROM signals
WHERE signal_type = 'career_history'
  AND account_id IS NOT NULL
ORDER BY account_id
"""

SIGNAL_EXISTS_SQL = (
    "SELECT 1 FROM signals "
    "WHERE prospect_id = $1 AND signal_type = $2 "
    "AND value->>'company_canonical' = $3 "
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
class CareerHistorySignalRow:
    """One v2 row from ``signals WHERE signal_type='career_history'``."""

    id: UUID
    prospect_id: UUID
    current_company: str | None
    roles_json: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ProspectRoleEntry:
    """One classified (prospect, company, role) tuple, indexable."""

    prospect_id: UUID
    company_canonical: str
    company_raw: str
    title: str | None


@dataclass(frozen=True, slots=True)
class CareerHistoryRollup:
    """Aggregate counters for one ``bulk_career_history_signals_account`` call."""

    account_id: UUID
    career_history_signals_read: int = 0
    companies_indexed: int = 0
    employer_groups: int = 0
    pairs_emitted: int = 0
    signals_inserted: int = 0
    signals_skipped_dedup: int = 0
    signals_skipped_current_employer: int = 0
    signals_skipped_no_company: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


# ── Pure planning helpers ───────────────────────────────────────────────────


_PUNCT_TRAIL = re.compile(r"[\s,.;:!\?\-_/\\]+$")
_WHITESPACE = re.compile(r"\s+")
_LEGAL_SUFFIX = re.compile(
    r"[\s,]+(?:inc|llc|ltd|limited|corp|corporation|co|company|"
    r"plc|gmbh|ag|sa|s\.a\.|nv|n\.v\.|bv|b\.v\.|kk|kgaa)\.?$",
    re.IGNORECASE,
)


def _normalize_company(name: str) -> str:
    """Canonicalize a company name for grouping.

    Applies — in order:
    1. lowercase + strip
    2. drop a leading "the "
    3. strip trailing legal suffixes (Inc / LLC / Corp / GmbH / etc.)
    4. drop trailing punctuation
    5. collapse internal whitespace
    6. consult ``COMPANY_ALIASES`` for canonical form

    Returns "" for empty input — caller filters those out.
    """
    if not name:
        return ""
    s = name.strip().lower()
    if not s:
        return ""
    if s.startswith("the "):
        s = s[4:]
    s = _LEGAL_SUFFIX.sub("", s)
    s = _PUNCT_TRAIL.sub("", s)
    s = _WHITESPACE.sub(" ", s).strip()
    if not s:
        return ""
    if s in COMPANY_ALIASES:
        s = COMPANY_ALIASES[s]
    return s


def _entry_from_role(
    prospect_id: UUID,
    role_obj: dict[str, Any],
    *,
    current_employer_canonical: str,
) -> ProspectRoleEntry | None:
    """Pure: turn one role dict into an entry, or None if unusable.

    Skips:
    - empty / whitespace-only company
    - current employer (matches ``current_employer_canonical`` after normalize)
    """
    company_raw = str(role_obj.get("company") or "").strip()
    if not company_raw:
        return None
    company_canonical = _normalize_company(company_raw)
    if not company_canonical:
        return None
    if (
        current_employer_canonical
        and company_canonical == current_employer_canonical
    ):
        # filtered out at a higher level via counters
        return None
    title_raw = str(role_obj.get("role") or role_obj.get("title") or "").strip()
    title = title_raw or None
    return ProspectRoleEntry(
        prospect_id=prospect_id,
        company_canonical=company_canonical,
        company_raw=company_raw,
        title=title,
    )


def _build_index(
    rows: list[CareerHistorySignalRow],
) -> tuple[
    dict[str, list[ProspectRoleEntry]],
    int,  # skipped_no_company
    int,  # skipped_current_employer
]:
    """Build the company_canonical → [entries] index.

    A single prospect is indexed once per (company_canonical) — multiple
    roles at the same employer fold to one entry (the first parse-clean
    role). Same prospect can appear in many different company groups.
    """
    index: dict[str, list[ProspectRoleEntry]] = {}
    seen_in_group: dict[str, set[UUID]] = {}
    skipped_no_company = 0
    skipped_current_employer = 0

    for row in rows:
        current_canonical = _normalize_company(row.current_company or "")
        for role in row.roles_json:
            if not isinstance(role, dict):
                skipped_no_company += 1
                continue
            company_raw = str(role.get("company") or "").strip()
            if not company_raw:
                skipped_no_company += 1
                continue
            company_canonical = _normalize_company(company_raw)
            if not company_canonical:
                skipped_no_company += 1
                continue
            if (
                current_canonical
                and company_canonical == current_canonical
            ):
                skipped_current_employer += 1
                continue
            entry = _entry_from_role(
                row.prospect_id, role,
                current_employer_canonical=current_canonical,
            )
            if entry is None:
                # _entry_from_role returns None for the same reasons as
                # above; double-counted as no_company.
                skipped_no_company += 1
                continue
            seen = seen_in_group.setdefault(company_canonical, set())
            if entry.prospect_id in seen:
                continue  # one entry per prospect per company
            seen.add(entry.prospect_id)
            index.setdefault(company_canonical, []).append(entry)
    return index, skipped_no_company, skipped_current_employer


def _pairs_from_index(
    index: dict[str, list[ProspectRoleEntry]],
) -> Iterator[tuple[ProspectRoleEntry, ProspectRoleEntry]]:
    """Yield (a, b) for every ordered pair in every group ≥2.

    Order is enforced via UUID lexical compare so emissions match the
    Postgres ``person_a_id < person_b_id`` invariant.
    """
    for entries in index.values():
        if len(entries) < 2:
            continue
        by_prospect: dict[UUID, ProspectRoleEntry] = {}
        for e in entries:
            by_prospect.setdefault(e.prospect_id, e)
        if len(by_prospect) < 2:
            continue
        ordered = sorted(by_prospect.keys())
        for i, pid_a in enumerate(ordered):
            for pid_b in ordered[i + 1:]:
                yield by_prospect[pid_a], by_prospect[pid_b]


def _build_structured_value(
    entry_a: ProspectRoleEntry,
    entry_b: ProspectRoleEntry,
    *,
    other_prospect_id: UUID,
) -> dict[str, Any]:
    """structured_value for one direction of a past_employer emission.

    The reading agent (``GraphChat.expand_node``, ``buildGraph`` 5th pass)
    keys evidence rendering off ``company_canonical`` + ``connected_to`` — keep
    those names stable across this and ``bulk_education_signals``.
    """
    return {
        "connected_to": str(other_prospect_id),
        "company_name": entry_a.company_raw or entry_b.company_raw,
        "company_canonical": entry_a.company_canonical,
        "role_a": entry_a.title,
        "role_b": entry_b.title,
    }


# ── DB helpers ───────────────────────────────────────────────────────────────


def _coerce_roles(value: Any) -> list[dict[str, Any]]:
    """value['roles'] may arrive as a JSON-encoded string from asyncpg."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, dict):
        return []
    roles = value.get("roles")
    if isinstance(roles, str):
        try:
            roles = json.loads(roles)
        except json.JSONDecodeError:
            return []
    if not isinstance(roles, list):
        return []
    return [r for r in roles if isinstance(r, dict)]


async def _fetch_career_history_signals(
    conn: asyncpg.Connection,
    account_id: UUID,
    limit: int | None,
) -> list[CareerHistorySignalRow]:
    if limit is None:
        rows = await conn.fetch(SELECT_CAREER_HISTORY_SIGNALS_SQL, account_id)
    else:
        rows = await conn.fetch(
            SELECT_CAREER_HISTORY_SIGNALS_LIMIT_SQL, account_id, int(limit)
        )
    out: list[CareerHistorySignalRow] = []
    for r in rows:
        roles = _coerce_roles(r["value"])
        out.append(
            CareerHistorySignalRow(
                id=r["id"],
                prospect_id=r["prospect_id"],
                current_company=r["current_company"],
                roles_json=roles,
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
    company_canonical: str,
    connected_to: str,
) -> bool:
    row = await conn.fetchval(
        SIGNAL_EXISTS_SQL,
        prospect_id,
        signal_type,
        company_canonical,
        connected_to,
    )
    return row is not None


async def _insert_signal(
    conn: asyncpg.Connection,
    prospect_id: UUID,
    account_id: UUID,
    structured_value: dict[str, Any],
    confidence: float,
) -> None:
    # The asyncpg pool registers a JSONB codec that runs ``json.dumps`` on
    # the encoder side — pass the dict directly so we don't double-encode.
    await conn.execute(
        INSERT_SIGNAL_SQL,
        prospect_id,
        account_id,
        SIGNAL_SOURCE,
        EMITTED_SIGNAL_TYPE,
        structured_value,
        confidence,
    )


# ── Public orchestrator ──────────────────────────────────────────────────────


async def bulk_career_history_signals_account(
    account_id: UUID,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> CareerHistoryRollup:
    """Build the per-account past-employer index and emit signals."""
    career_history_signals_read = 0
    companies_indexed = 0
    employer_groups = 0
    pairs_emitted = 0
    signals_inserted = 0
    signals_skipped_dedup = 0
    signals_skipped_current_employer = 0
    signals_skipped_no_company = 0
    errors: list[str] = []

    # Step 1 — load career_history signals.
    async with acquire() as conn:
        rows = await _fetch_career_history_signals(conn, account_id, limit)
    career_history_signals_read = len(rows)
    log.info(
        "career_history start account=%s rows=%d dry_run=%s",
        account_id, career_history_signals_read, dry_run,
    )

    # Step 2 — pure-function index build.
    index, skipped_no_company, skipped_current_employer = _build_index(rows)
    companies_indexed = len(index)
    signals_skipped_no_company = skipped_no_company
    signals_skipped_current_employer = skipped_current_employer

    # Step 3 — emit pair tuples.
    pair_tuples = list(_pairs_from_index(index))
    pairs_emitted = len(pair_tuples)
    employer_groups = sum(
        1
        for entries in index.values()
        if len({e.prospect_id for e in entries}) >= 2
    )

    log.info(
        "career_history indexed account=%s companies=%d groups=%d pairs=%d "
        "skipped_current_employer=%d skipped_no_company=%d",
        account_id, companies_indexed, employer_groups, pairs_emitted,
        signals_skipped_current_employer, signals_skipped_no_company,
    )

    if dry_run:
        for entry_a, entry_b in pair_tuples:
            log.info(
                "[dry-run] would emit %s↔%s %s company=%s",
                entry_a.prospect_id, entry_b.prospect_id,
                EMITTED_SIGNAL_TYPE, entry_a.company_canonical,
            )
        return CareerHistoryRollup(
            account_id=account_id,
            career_history_signals_read=career_history_signals_read,
            companies_indexed=companies_indexed,
            employer_groups=employer_groups,
            pairs_emitted=pairs_emitted,
            signals_inserted=0,
            signals_skipped_dedup=0,
            signals_skipped_current_employer=signals_skipped_current_employer,
            signals_skipped_no_company=signals_skipped_no_company,
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
                errors.append(f"{entry_a.prospect_id}->{entry_b.prospect_id}: {exc!r}")
                log.exception(
                    "career_history persist failed for %s↔%s",
                    entry_a.prospect_id, entry_b.prospect_id,
                )

    log.info(
        "career_history done account=%s inserted=%d skipped_dedup=%d errors=%d",
        account_id, signals_inserted, signals_skipped_dedup, len(errors),
    )
    return CareerHistoryRollup(
        account_id=account_id,
        career_history_signals_read=career_history_signals_read,
        companies_indexed=companies_indexed,
        employer_groups=employer_groups,
        pairs_emitted=pairs_emitted,
        signals_inserted=signals_inserted,
        signals_skipped_dedup=signals_skipped_dedup,
        signals_skipped_current_employer=signals_skipped_current_employer,
        signals_skipped_no_company=signals_skipped_no_company,
        errors=errors,
        dry_run=False,
    )


async def _persist_pair(
    conn: asyncpg.Connection,
    *,
    account_id: UUID,
    entry_a: ProspectRoleEntry,
    entry_b: ProspectRoleEntry,
) -> tuple[int, int]:
    """Persist one signal row pointing entry_a → entry_b. Returns (inserted, deduped)."""
    structured = _build_structured_value(
        entry_a, entry_b, other_prospect_id=entry_b.prospect_id
    )
    if await _signal_exists(
        conn,
        entry_a.prospect_id,
        EMITTED_SIGNAL_TYPE,
        entry_a.company_canonical,
        str(entry_b.prospect_id),
    ):
        return (0, 1)
    confidence = compute_strength_for_type(
        "career_overlap_general",
        years_since_active=DEFAULT_YEARS_SINCE_ACTIVE,
        corroboration_count=DEFAULT_CORROBORATION_COUNT,
    )
    await _insert_signal(
        conn,
        entry_a.prospect_id,
        account_id,
        structured,
        confidence,
    )
    return (1, 0)


async def bulk_career_history_signals_all_accounts(
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> list[CareerHistoryRollup]:
    """Iterate every account with career_history signals and emit past_employer rows."""
    async with acquire() as conn:
        account_ids = await _fetch_all_account_ids(conn)
    log.info("career_history all-accounts: %d accounts", len(account_ids))
    rollups: list[CareerHistoryRollup] = []
    for account_id in account_ids:
        rollup = await bulk_career_history_signals_account(
            account_id, limit=limit, dry_run=dry_run,
        )
        rollups.append(rollup)
    return rollups


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.bulk_career_history_signals",
        description=(
            "Bulk per-account career-history runner → emits past_employer "
            "signal rows from existing v2 career_history data."
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
        help="Iterate every account with v2 career_history signals.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap career_history signals read per account (default: no cap).",
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


def _print_rollup(rollup: CareerHistoryRollup) -> None:
    msg = (
        f"career_history account={rollup.account_id} "
        f"career_history_signals_read={rollup.career_history_signals_read} "
        f"companies_indexed={rollup.companies_indexed} "
        f"employer_groups={rollup.employer_groups} "
        f"pairs_emitted={rollup.pairs_emitted} "
        f"signals_inserted={rollup.signals_inserted} "
        f"signals_skipped_dedup={rollup.signals_skipped_dedup} "
        f"signals_skipped_current_employer={rollup.signals_skipped_current_employer} "
        f"signals_skipped_no_company={rollup.signals_skipped_no_company} "
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

    async def _go() -> list[CareerHistoryRollup]:
        try:
            if args.all_accounts:
                return await bulk_career_history_signals_all_accounts(
                    limit=args.limit, dry_run=args.dry_run,
                )
            return [
                await bulk_career_history_signals_account(
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
