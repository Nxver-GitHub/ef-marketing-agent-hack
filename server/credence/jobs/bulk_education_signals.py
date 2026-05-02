"""Bulk per-account education-cohort signal runner.

Reads existing v2 ``signal_type='education'`` rows (already populated from
PDL/LinkedIn ingest), groups prospects by ``(school_normalized, degree_kind)``,
and emits cohort-edge signals (``same_mba_cohort``, ``same_phd_program``,
``executive_education``, ``same_undergrad_cohort``) — one row per ordered
prospect pair (``person_a < person_b`` lexically) per group.

This is a **write-only** data pipeline using only data we already have:
zero external API calls, zero PDL spend. The frontend's existing fifth pass
(``src/lib/graph.ts:1023``) already reads these signal_types and renders the
cohort edges.

## Idempotency

Re-runs do not pile up duplicates. Before INSERTing a signal we run an
explicit ``SELECT 1 ... LIMIT 1`` keyed on
``(prospect_id, signal_type, value->>'institution_normalized',
value->>'connected_to')``. Same pattern as ``bulk_scholar_ingest``.

## CLI

::

    cd server && uv run python -m credence.jobs.bulk_education_signals \\
        --account-id <uuid> --limit 100 --dry-run

    cd server && uv run python -m credence.jobs.bulk_education_signals \\
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


SIGNAL_SOURCE = "v2_education_extraction"
SOURCE_SIGNAL_TYPE = "education"

# We don't have graduation years in the v2 education payload — pick a rough
# midpoint that matches "established alum" decay (10y is the median career age
# of a typical buyer cohort and lines up with the existing strength table's
# DECAY_RATES tuning for cohort edges).
DEFAULT_YEARS_SINCE_ACTIVE = 10.0
DEFAULT_CORROBORATION_COUNT = 1

# Map degree_kind → cohort signal_type. Masters has no edge variant per
# CLAUDE.md (V3_PT2.md L376-381); see _cohort_signal_type below.
COHORT_SIGNAL_TYPES: dict[str, str] = {
    "mba": "same_mba_cohort",
    "phd": "same_phd_program",
    "exec_ed": "executive_education",
    "undergrad": "same_undergrad_cohort",
}


# Hand-curated alias table for top US schools that appear in many forms in
# the v2 data. Keys match LOWERCASED-normalized inputs (post-strip, post-the,
# post-collapse-whitespace, post-trailing-punctuation). Values are the
# canonical lowercase name we group by.
SCHOOL_ALIASES: dict[str, str] = {
    "mit": "massachusetts institute of technology",
    "ucla": "university of california, los angeles",
    "usc": "university of southern california",
    "harvard business school": "harvard university",
    "harvard college": "harvard university",
    "harvard law school": "harvard university",
    "stanford gsb": "stanford university",
    "wharton": "university of pennsylvania",
    "kellogg": "northwestern university",
    "booth": "university of chicago",
    "haas": "university of california, berkeley",
    "uc berkeley": "university of california, berkeley",
    "berkeley": "university of california, berkeley",
}


# ── SQL ──────────────────────────────────────────────────────────────────────


SELECT_EDUCATION_SIGNALS_SQL = """
SELECT id, prospect_id, value
FROM signals
WHERE signal_type = 'education'
  AND account_id = $1
  AND value ? 'degrees'
  AND jsonb_typeof(value->'degrees') = 'array'
ORDER BY prospect_id
"""

SELECT_EDUCATION_SIGNALS_LIMIT_SQL = SELECT_EDUCATION_SIGNALS_SQL + "LIMIT $2\n"

SELECT_ALL_ACCOUNTS_SQL = """
SELECT DISTINCT account_id
FROM signals
WHERE signal_type = 'education'
  AND account_id IS NOT NULL
ORDER BY account_id
"""

SIGNAL_EXISTS_SQL = (
    "SELECT 1 FROM signals "
    "WHERE prospect_id = $1 AND signal_type = $2 "
    "AND value->>'institution_normalized' = $3 "
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
class EducationSignalRow:
    """One v2 row from ``signals WHERE signal_type='education'``."""

    id: UUID
    prospect_id: UUID
    degrees_json: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ProspectEducationEntry:
    """One classified (prospect, school, degree-kind) tuple, indexable."""

    prospect_id: UUID
    school_canonical: str
    degree_kind: str
    field: str | None
    degree_raw: str
    school_raw: str


@dataclass(frozen=True, slots=True)
class EducationCohortRollup:
    """Aggregate counters for one ``bulk_education_signals_account`` call."""

    account_id: UUID
    education_signals_read: int = 0
    schools_indexed: int = 0
    cohort_groups: int = 0
    pairs_emitted: int = 0
    signals_inserted: int = 0
    signals_skipped_dedup: int = 0
    signals_skipped_unclassifiable: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


# ── Pure planning helpers ───────────────────────────────────────────────────


_PUNCT_TRAIL = re.compile(r"[\s,.;:!\?\-_/\\]+$")
_WHITESPACE = re.compile(r"\s+")


def _normalize_school(name: str) -> str:
    """Canonicalize a school name for grouping.

    Returns empty string when the input is empty/whitespace-only — caller
    filters those out.
    """
    if not name:
        return ""
    s = name.strip().lower()
    if not s:
        return ""
    if s.startswith("the "):
        s = s[4:]
    s = _PUNCT_TRAIL.sub("", s)
    s = _WHITESPACE.sub(" ", s).strip()
    if not s:
        return ""
    if s in SCHOOL_ALIASES:
        s = SCHOOL_ALIASES[s]
    return s


def _classify_degree(degree_string: str) -> str | None:
    """Map a free-form degree string to one of mba/phd/undergrad/masters/exec_ed.

    Returns None for unclassifiable input.
    """
    if not degree_string:
        return None
    d = degree_string.strip().lower()
    if not d:
        return None

    # MBA — check before generic "master".
    if "mba" in d or "master of business administration" in d:
        return "mba"

    # PhD — check before generic "doctor".
    if "phd" in d or "ph.d" in d or "ph. d" in d:
        return "phd"
    if "doctor of philosophy" in d or "doctorate" in d:
        return "phd"

    # Executive education — check before generic "master/bachelor".
    if "executive" in d:  # covers "executive program", "executive management", etc.
        return "exec_ed"

    # Undergrad.
    if (
        "bachelor" in d
        or "undergraduate" in d
        or "b.s." in d
        or "b.a." in d
        or re.search(r"\bb\.?s\b", d)
        or re.search(r"\bb\.?a\b", d)
    ):
        return "undergrad"

    # Masters (last — broadest "master" check).
    if (
        "master" in d
        or "m.s." in d
        or "m.a." in d
        or "ms." in d
        or re.search(r"\bm\.?s\b", d)
        or re.search(r"\bm\.?a\b", d)
    ):
        return "masters"

    return None


def _cohort_signal_type(degree_kind: str) -> str | None:
    """Map degree_kind → cohort signal_type. Masters has no edge variant."""
    return COHORT_SIGNAL_TYPES.get(degree_kind)


def _index_key(school_canonical: str, degree_kind: str) -> tuple[str, str]:
    return (school_canonical, degree_kind)


def _entry_from_degree(
    prospect_id: UUID,
    degree_obj: dict[str, Any],
) -> ProspectEducationEntry | None:
    """Pure: turn one degree dict into an entry, or None if unclassifiable."""
    school_raw = str(degree_obj.get("school") or "").strip()
    degree_raw = str(degree_obj.get("degree") or "").strip()
    field_raw = str(degree_obj.get("field") or "").strip()

    school_canonical = _normalize_school(school_raw)
    if not school_canonical:
        return None
    degree_kind = _classify_degree(degree_raw)
    if degree_kind is None:
        return None
    return ProspectEducationEntry(
        prospect_id=prospect_id,
        school_canonical=school_canonical,
        degree_kind=degree_kind,
        field=field_raw or None,
        degree_raw=degree_raw,
        school_raw=school_raw,
    )


def _build_index(
    rows: list[EducationSignalRow],
) -> tuple[dict[tuple[str, str], list[ProspectEducationEntry]], int]:
    """Build the (school, degree_kind) → [entries] index.

    Returns (index, unclassifiable_count). One prospect may contribute
    multiple entries (e.g., a triple-MIT alum). Within a group we dedupe
    by prospect_id + degree_raw so the same (prospect, MIT, MBA) doesn't
    pair against itself.
    """
    index: dict[tuple[str, str], list[ProspectEducationEntry]] = {}
    seen_in_group: dict[tuple[str, str], set[tuple[UUID, str]]] = {}
    unclassifiable = 0
    for row in rows:
        for degree in row.degrees_json:
            if not isinstance(degree, dict):
                unclassifiable += 1
                continue
            entry = _entry_from_degree(row.prospect_id, degree)
            if entry is None:
                unclassifiable += 1
                continue
            key = _index_key(entry.school_canonical, entry.degree_kind)
            seen_set = seen_in_group.setdefault(key, set())
            dedupe_key = (entry.prospect_id, entry.degree_raw.lower())
            if dedupe_key in seen_set:
                continue
            seen_set.add(dedupe_key)
            index.setdefault(key, []).append(entry)
    return index, unclassifiable


def _pairs_from_index(
    index: dict[tuple[str, str], list[ProspectEducationEntry]],
) -> Iterator[tuple[ProspectEducationEntry, ProspectEducationEntry, str]]:
    """Yield (a, b, signal_type) for every ordered pair in every group ≥2.

    Skips groups whose degree_kind has no cohort signal_type (masters).
    Order is enforced via UUID lexical compare so emissions match the
    Postgres ``person_a_id < person_b_id`` invariant.
    """
    for (_school, degree_kind), entries in index.items():
        signal_type = _cohort_signal_type(degree_kind)
        if signal_type is None:
            continue
        # One prospect may appear multiple times (e.g., BS + MS + PhD all
        # MIT) — but for a single (school, degree_kind) group _build_index
        # already deduped to ≤1 entry per prospect_id. Still defensive:
        by_prospect: dict[UUID, ProspectEducationEntry] = {}
        for e in entries:
            by_prospect.setdefault(e.prospect_id, e)
        if len(by_prospect) < 2:
            continue
        ordered = sorted(by_prospect.keys())
        for i, pid_a in enumerate(ordered):
            for pid_b in ordered[i + 1:]:
                yield by_prospect[pid_a], by_prospect[pid_b], signal_type


def _build_structured_value(
    entry_a: ProspectEducationEntry,
    entry_b: ProspectEducationEntry,
    *,
    other_prospect_id: UUID,
) -> dict[str, Any]:
    """structured_value for one direction of a cohort emission."""
    return {
        "connected_to": str(other_prospect_id),
        "institution": entry_a.school_raw or entry_b.school_raw,
        "institution_normalized": entry_a.school_canonical,
        "program": entry_a.field or entry_b.field or None,
        "degree_a": entry_a.degree_raw,
        "degree_b": entry_b.degree_raw,
        "degree_kind": entry_a.degree_kind,
    }


# ── DB helpers ───────────────────────────────────────────────────────────────


def _coerce_degrees(value: Any) -> list[dict[str, Any]]:
    """value['degrees'] may arrive as a JSON-encoded string from asyncpg."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, dict):
        return []
    degrees = value.get("degrees")
    if isinstance(degrees, str):
        try:
            degrees = json.loads(degrees)
        except json.JSONDecodeError:
            return []
    if not isinstance(degrees, list):
        return []
    return [d for d in degrees if isinstance(d, dict)]


async def _fetch_education_signals(
    conn: asyncpg.Connection,
    account_id: UUID,
    limit: int | None,
) -> list[EducationSignalRow]:
    if limit is None:
        rows = await conn.fetch(SELECT_EDUCATION_SIGNALS_SQL, account_id)
    else:
        rows = await conn.fetch(
            SELECT_EDUCATION_SIGNALS_LIMIT_SQL, account_id, int(limit)
        )
    out: list[EducationSignalRow] = []
    for r in rows:
        degrees = _coerce_degrees(r["value"])
        out.append(
            EducationSignalRow(
                id=r["id"],
                prospect_id=r["prospect_id"],
                degrees_json=degrees,
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
    school_canonical: str,
    connected_to: str,
) -> bool:
    row = await conn.fetchval(
        SIGNAL_EXISTS_SQL,
        prospect_id,
        signal_type,
        school_canonical,
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
    # Pass the dict directly (matches `signals.py:_persist_signal`).
    # asyncpg's jsonb codec encodes dict → jsonb-object natively. Using
    # `json.dumps(dict)` instead would produce a Python str, which asyncpg
    # then JSON-encodes a second time before the `::jsonb` cast — Postgres
    # parses the result as a jsonb-typed *string* (jsonb_typeof='string'),
    # opaque to `value->>'key'` subscripts and unreadable by the frontend's
    # fifth pass. (Confirmed live: the first 283-row run hit this and had
    # to be cleaned + re-emitted.)
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


async def bulk_education_signals_account(
    account_id: UUID,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> EducationCohortRollup:
    """Build the per-account education cohort index and emit signals."""

    education_signals_read = 0
    schools_indexed = 0
    cohort_groups = 0
    pairs_emitted = 0
    signals_inserted = 0
    signals_skipped_dedup = 0
    signals_skipped_unclassifiable = 0
    errors: list[str] = []

    # Step 1 — load education signals.
    async with acquire() as conn:
        rows = await _fetch_education_signals(conn, account_id, limit)
    education_signals_read = len(rows)
    log.info(
        "education_signals start account=%s rows=%d dry_run=%s",
        account_id, education_signals_read, dry_run,
    )

    # Step 2 — pure-function index build.
    index, unclassifiable = _build_index(rows)
    schools_indexed = len(index)
    signals_skipped_unclassifiable = unclassifiable

    # Step 3 — emit pair tuples.
    pair_tuples = list(_pairs_from_index(index))
    pairs_emitted = len(pair_tuples)
    cohort_groups = sum(
        1
        for (_s, dk), entries in index.items()
        if _cohort_signal_type(dk) is not None
        and len({e.prospect_id for e in entries}) >= 2
    )

    log.info(
        "education_signals indexed account=%s schools=%d groups=%d pairs=%d unclassifiable=%d",
        account_id, schools_indexed, cohort_groups, pairs_emitted,
        signals_skipped_unclassifiable,
    )

    if dry_run:
        for entry_a, entry_b, signal_type in pair_tuples:
            log.info(
                "[dry-run] would emit %s↔%s %s school=%s",
                entry_a.prospect_id, entry_b.prospect_id,
                signal_type, entry_a.school_canonical,
            )
        return EducationCohortRollup(
            account_id=account_id,
            education_signals_read=education_signals_read,
            schools_indexed=schools_indexed,
            cohort_groups=cohort_groups,
            pairs_emitted=pairs_emitted,
            signals_inserted=0,
            signals_skipped_dedup=0,
            signals_skipped_unclassifiable=signals_skipped_unclassifiable,
            errors=errors,
            dry_run=True,
        )

    # Step 4 — persist with explicit dedupe.
    async with acquire() as conn:
        for entry_a, entry_b, signal_type in pair_tuples:
            try:
                inserted, deduped = await _persist_pair(
                    conn,
                    account_id=account_id,
                    entry_a=entry_a,
                    entry_b=entry_b,
                    signal_type=signal_type,
                )
                signals_inserted += inserted
                signals_skipped_dedup += deduped
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{entry_a.prospect_id}->{entry_b.prospect_id}: {exc!r}")
                log.exception(
                    "education_signals persist failed for %s↔%s",
                    entry_a.prospect_id, entry_b.prospect_id,
                )

    log.info(
        "education_signals done account=%s inserted=%d skipped_dedup=%d errors=%d",
        account_id, signals_inserted, signals_skipped_dedup, len(errors),
    )
    return EducationCohortRollup(
        account_id=account_id,
        education_signals_read=education_signals_read,
        schools_indexed=schools_indexed,
        cohort_groups=cohort_groups,
        pairs_emitted=pairs_emitted,
        signals_inserted=signals_inserted,
        signals_skipped_dedup=signals_skipped_dedup,
        signals_skipped_unclassifiable=signals_skipped_unclassifiable,
        errors=errors,
        dry_run=False,
    )


async def _persist_pair(
    conn: asyncpg.Connection,
    *,
    account_id: UUID,
    entry_a: ProspectEducationEntry,
    entry_b: ProspectEducationEntry,
    signal_type: str,
) -> tuple[int, int]:
    """Persist one signal row pointing entry_a → entry_b. Returns (inserted, deduped)."""
    structured = _build_structured_value(
        entry_a, entry_b, other_prospect_id=entry_b.prospect_id
    )
    if await _signal_exists(
        conn,
        entry_a.prospect_id,
        signal_type,
        entry_a.school_canonical,
        str(entry_b.prospect_id),
    ):
        return (0, 1)
    confidence = compute_strength_for_type(
        signal_type,
        years_since_active=DEFAULT_YEARS_SINCE_ACTIVE,
        corroboration_count=DEFAULT_CORROBORATION_COUNT,
    )
    await _insert_signal(
        conn,
        entry_a.prospect_id,
        account_id,
        signal_type,
        structured,
        confidence,
    )
    return (1, 0)


async def bulk_education_signals_all_accounts(
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> list[EducationCohortRollup]:
    """Iterate every account with education signals and emit cohort signals."""
    async with acquire() as conn:
        account_ids = await _fetch_all_account_ids(conn)
    log.info("education_signals all-accounts: %d accounts", len(account_ids))
    rollups: list[EducationCohortRollup] = []
    for account_id in account_ids:
        rollup = await bulk_education_signals_account(
            account_id, limit=limit, dry_run=dry_run,
        )
        rollups.append(rollup)
    return rollups


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.bulk_education_signals",
        description=(
            "Bulk per-account education-cohort runner → emits "
            "same_mba_cohort / same_phd_program / executive_education / "
            "same_undergrad_cohort signal rows from existing v2 education data."
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
        help="Iterate every account with v2 education signals.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap education signals read per account (default: no cap).",
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


def _print_rollup(rollup: EducationCohortRollup) -> None:
    msg = (
        f"education_signals account={rollup.account_id} "
        f"education_signals_read={rollup.education_signals_read} "
        f"schools_indexed={rollup.schools_indexed} "
        f"cohort_groups={rollup.cohort_groups} "
        f"pairs_emitted={rollup.pairs_emitted} "
        f"signals_inserted={rollup.signals_inserted} "
        f"signals_skipped_dedup={rollup.signals_skipped_dedup} "
        f"signals_skipped_unclassifiable={rollup.signals_skipped_unclassifiable} "
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

    async def _go() -> list[EducationCohortRollup]:
        try:
            if args.all_accounts:
                return await bulk_education_signals_all_accounts(
                    limit=args.limit, dry_run=args.dry_run,
                )
            return [
                await bulk_education_signals_account(
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
