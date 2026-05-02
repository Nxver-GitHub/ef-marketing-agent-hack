"""Bulk linkedin_profile headline → synthetic education signal extractor — Wave 6 Job W6.2.

Reads existing v2 ``signal_type='linkedin_profile'`` rows, scans each row's
``headline`` field for institution mentions using LavenderPrairie's
``institutions`` alias table (msg 162: 30 institutions × ~145 aliases),
and emits synthetic ``signal_type='education'`` rows for every prospect-school
hit. Those synthetic rows are picked up automatically on the next
``bulk_education_signals`` run, cascading into cohort signals.

Same pattern as ``bulk_bio_extraction`` (Wave 5 Job B), just sourced from
LinkedIn headlines instead of bio text. Bigger source pool (12,256 vs 2,804)
but each headline is shorter and denser (typically <120 chars).

Phase 1 / school-only — no patent / conference / standards regex (precision
minefield).

Zero external API calls.

## Degree inference

Same as ``bulk_bio_extraction``: piggyback on the
``institutions.institution_type`` flag and synthesize the canonical degree
token. Downstream ``bulk_education_signals._classify_degree`` turns each into
the right cohort kind.

## Idempotency

A re-run is a no-op. Before INSERTing a synthetic education row we run a
lookup keyed on ``(prospect_id, signal_type='education',
source='v2_linkedin_profile_extraction',
value->'degrees'->0->>'school' = canonical_name)``.

## CLI

::

    cd server && uv run python -m credence.jobs.bulk_linkedin_profile_extraction \\
        --account-id <uuid> --limit 100 --dry-run

    cd server && uv run python -m credence.jobs.bulk_linkedin_profile_extraction \\
        --all-accounts
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from ..db import acquire, close_pool

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


SIGNAL_SOURCE = "v2_linkedin_profile_extraction"
SOURCE_SIGNAL_TYPE = "linkedin_profile"
EMITTED_SIGNAL_TYPE = "education"

# Default degree token per institution_type. Mirrors bulk_bio_extraction.
ITYPE_TO_DEGREE: dict[str, str | None] = {
    "mba": "MBA",
    "phd": "PhD",
    "undergrad": "BS",
    "exec_ed": "Executive Education",
    "other": None,
}

EMIT_CONFIDENCE = 0.50

# Min alias length to consider — short tokens like "MIT" or "USC" are
# legitimate but anything <3 chars is too noisy to substring-match safely.
MIN_ALIAS_LEN = 3


# ── SQL ──────────────────────────────────────────────────────────────────────


SELECT_LINKEDIN_PROFILE_SIGNALS_SQL = """
SELECT id, prospect_id, value
FROM signals
WHERE signal_type = 'linkedin_profile'
  AND account_id = $1
  AND value ? 'headline'
ORDER BY prospect_id
"""

SELECT_LINKEDIN_PROFILE_SIGNALS_LIMIT_SQL = (
    SELECT_LINKEDIN_PROFILE_SIGNALS_SQL + "LIMIT $2\n"
)

SELECT_INSTITUTIONS_SQL = (
    "SELECT canonical_name, short_name, aliases, institution_type "
    "FROM institutions"
)

SELECT_ALL_ACCOUNTS_SQL = """
SELECT DISTINCT account_id
FROM signals
WHERE signal_type = 'linkedin_profile'
  AND account_id IS NOT NULL
ORDER BY account_id
"""

SIGNAL_EXISTS_SQL = (
    "SELECT 1 FROM signals "
    "WHERE prospect_id = $1 AND signal_type = $2 "
    "AND source = $3 "
    "AND value->'degrees'->0->>'school' = $4 "
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
class LinkedInProfileSignalRow:
    """One v2 row from ``signals WHERE signal_type='linkedin_profile'``."""

    id: UUID
    prospect_id: UUID
    headline: str


@dataclass(frozen=True, slots=True)
class InstitutionAlias:
    """One row from ``institutions`` plus its derived alias-keyword set."""

    canonical_name: str
    institution_type: str
    aliases_lower: frozenset[str]


@dataclass(frozen=True, slots=True)
class LinkedInHit:
    """One detected (prospect, institution) tuple ready to emit."""

    prospect_id: UUID
    institution_canonical: str
    institution_type: str
    degree_token: str


@dataclass(frozen=True, slots=True)
class LinkedInExtractionRollup:
    """Aggregate counters for one ``bulk_linkedin_profile_extraction_account`` call."""

    account_id: UUID
    profile_signals_read: int = 0
    institutions_loaded: int = 0
    profile_hits_total: int = 0
    profile_hits_emittable: int = 0
    signals_inserted: int = 0
    signals_skipped_dedup: int = 0
    signals_skipped_other_type: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


# ── Pure planning helpers ───────────────────────────────────────────────────


def _coerce_headline(value: Any) -> str:
    """value['headline'] may arrive via a JSON-encoded string from asyncpg."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ""
    if not isinstance(value, dict):
        return ""
    headline = value.get("headline")
    if not isinstance(headline, str):
        return ""
    return headline


def _build_alias_table(
    rows: Iterable[asyncpg.Record],
) -> list[InstitutionAlias]:
    """Build the canonical → alias-set list from ``institutions`` rows.

    Mirrors ``bulk_bio_extraction._build_alias_table`` shape so future
    deduplication/sharing of the loader is mechanical.
    """
    out: list[InstitutionAlias] = []
    for row in rows:
        canonical = row["canonical_name"]
        if not canonical:
            continue
        itype = row["institution_type"] or "other"
        names = {canonical}
        if row["short_name"]:
            names.add(row["short_name"])
        for alias in row["aliases"] or ():
            if alias:
                names.add(alias)
        cleaned: set[str] = set()
        for name in names:
            n = name.strip().lower()
            if len(n) < MIN_ALIAS_LEN:
                continue
            cleaned.add(n)
        if not cleaned:
            continue
        out.append(InstitutionAlias(
            canonical_name=canonical,
            institution_type=itype,
            aliases_lower=frozenset(cleaned),
        ))
    return out


def _scan_text_for_aliases(
    text_lower: str,
    institutions: list[InstitutionAlias],
) -> list[InstitutionAlias]:
    """Return institutions whose alias appears in ``text_lower`` with word boundaries.

    Identical to ``bulk_bio_extraction._scan_text_for_aliases``.
    """
    hits: list[InstitutionAlias] = []
    n = len(text_lower)
    for inst in institutions:
        for alias in inst.aliases_lower:
            pos = 0
            matched = False
            while pos < n:
                idx = text_lower.find(alias, pos)
                if idx == -1:
                    break
                left_ok = idx == 0 or not text_lower[idx - 1].isalnum()
                right_ok = (
                    idx + len(alias) == n
                    or not text_lower[idx + len(alias)].isalnum()
                )
                if left_ok and right_ok:
                    matched = True
                    break
                pos = idx + 1
            if matched:
                hits.append(inst)
                break
    return hits


def _hits_for_profile(
    row: LinkedInProfileSignalRow,
    institutions: list[InstitutionAlias],
) -> tuple[list[LinkedInHit], int]:
    """Return (emittable hits, dropped-other-type count) for one profile row."""
    text_lower = row.headline.lower()
    if not text_lower:
        return [], 0
    matches = _scan_text_for_aliases(text_lower, institutions)
    emittable: list[LinkedInHit] = []
    skipped = 0
    for inst in matches:
        degree = ITYPE_TO_DEGREE.get(inst.institution_type)
        if degree is None:
            skipped += 1
            continue
        emittable.append(LinkedInHit(
            prospect_id=row.prospect_id,
            institution_canonical=inst.canonical_name,
            institution_type=inst.institution_type,
            degree_token=degree,
        ))
    return emittable, skipped


def _build_synth_value(hit: LinkedInHit) -> dict[str, Any]:
    """Synthetic education-signal value matching bulk_education_signals' shape."""
    return {
        "degrees": [
            {
                "school": hit.institution_canonical,
                "degree": hit.degree_token,
                "field": None,
            }
        ],
        "extraction_method": "linkedin_headline_alias_match",
    }


# ── DB helpers ───────────────────────────────────────────────────────────────


async def _fetch_profile_signals(
    conn: asyncpg.Connection,
    account_id: UUID,
    limit: int | None,
) -> list[LinkedInProfileSignalRow]:
    if limit is None:
        rows = await conn.fetch(SELECT_LINKEDIN_PROFILE_SIGNALS_SQL, account_id)
    else:
        rows = await conn.fetch(
            SELECT_LINKEDIN_PROFILE_SIGNALS_LIMIT_SQL, account_id, int(limit)
        )
    out: list[LinkedInProfileSignalRow] = []
    for r in rows:
        headline = _coerce_headline(r["value"])
        if not headline:
            continue
        out.append(LinkedInProfileSignalRow(
            id=r["id"],
            prospect_id=r["prospect_id"],
            headline=headline,
        ))
    return out


async def _fetch_institutions(conn: asyncpg.Connection) -> list[InstitutionAlias]:
    rows = await conn.fetch(SELECT_INSTITUTIONS_SQL)
    return _build_alias_table(rows)


async def _fetch_all_account_ids(conn: asyncpg.Connection) -> list[UUID]:
    rows = await conn.fetch(SELECT_ALL_ACCOUNTS_SQL)
    return [r["account_id"] for r in rows]


async def _signal_exists(
    conn: asyncpg.Connection,
    prospect_id: UUID,
    school_canonical: str,
) -> bool:
    row = await conn.fetchval(
        SIGNAL_EXISTS_SQL,
        prospect_id,
        EMITTED_SIGNAL_TYPE,
        SIGNAL_SOURCE,
        school_canonical,
    )
    return row is not None


async def _insert_synth_education_signal(
    conn: asyncpg.Connection,
    hit: LinkedInHit,
    account_id: UUID,
) -> None:
    structured = _build_synth_value(hit)
    # Pass dict directly — the asyncpg pool's JSONB codec handles
    # encoding. See bulk_bio_extraction comment for the double-encode
    # pitfall this avoids.
    await conn.execute(
        INSERT_SIGNAL_SQL,
        hit.prospect_id,
        account_id,
        SIGNAL_SOURCE,
        EMITTED_SIGNAL_TYPE,
        structured,
        EMIT_CONFIDENCE,
    )


# ── Public orchestrator ──────────────────────────────────────────────────────


async def bulk_linkedin_profile_extraction_account(
    account_id: UUID,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> LinkedInExtractionRollup:
    """Build the per-account headline-extraction index and emit synthetic education signals."""
    profile_signals_read = 0
    institutions_loaded = 0
    profile_hits_total = 0
    profile_hits_emittable = 0
    signals_inserted = 0
    signals_skipped_dedup = 0
    signals_skipped_other_type = 0
    errors: list[str] = []

    # Step 1 — load institutions.
    async with acquire() as conn:
        institutions = await _fetch_institutions(conn)
    institutions_loaded = len(institutions)
    if not institutions:
        log.warning(
            "linkedin_profile account=%s: institutions table empty; nothing to scan.",
            account_id,
        )
        return LinkedInExtractionRollup(
            account_id=account_id,
            institutions_loaded=0,
            dry_run=dry_run,
        )

    # Step 2 — load linkedin_profile signals.
    async with acquire() as conn:
        rows = await _fetch_profile_signals(conn, account_id, limit)
    profile_signals_read = len(rows)
    log.info(
        "linkedin_profile start account=%s rows=%d institutions=%d dry_run=%s",
        account_id, profile_signals_read, institutions_loaded, dry_run,
    )

    # Step 3 — scan each headline.
    all_hits: list[LinkedInHit] = []
    for row in rows:
        hits, dropped = _hits_for_profile(row, institutions)
        profile_hits_total += len(hits) + dropped
        profile_hits_emittable += len(hits)
        signals_skipped_other_type += dropped
        all_hits.extend(hits)

    log.info(
        "linkedin_profile scan done account=%s hits_total=%d emittable=%d skipped_other=%d",
        account_id, profile_hits_total, profile_hits_emittable, signals_skipped_other_type,
    )

    if dry_run:
        for hit in all_hits[:50]:
            log.info(
                "[dry-run] would emit %s school=%s type=%s degree=%s",
                hit.prospect_id, hit.institution_canonical,
                hit.institution_type, hit.degree_token,
            )
        return LinkedInExtractionRollup(
            account_id=account_id,
            profile_signals_read=profile_signals_read,
            institutions_loaded=institutions_loaded,
            profile_hits_total=profile_hits_total,
            profile_hits_emittable=profile_hits_emittable,
            signals_inserted=0,
            signals_skipped_dedup=0,
            signals_skipped_other_type=signals_skipped_other_type,
            errors=errors,
            dry_run=True,
        )

    # Step 4 — persist with explicit dedupe.
    async with acquire() as conn:
        for hit in all_hits:
            try:
                if await _signal_exists(
                    conn, hit.prospect_id, hit.institution_canonical
                ):
                    signals_skipped_dedup += 1
                    continue
                await _insert_synth_education_signal(conn, hit, account_id)
                signals_inserted += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{hit.prospect_id}->{hit.institution_canonical}: {exc!r}")
                log.exception(
                    "linkedin_profile persist failed for %s @ %s",
                    hit.prospect_id, hit.institution_canonical,
                )

    log.info(
        "linkedin_profile done account=%s inserted=%d skipped_dedup=%d errors=%d",
        account_id, signals_inserted, signals_skipped_dedup, len(errors),
    )
    return LinkedInExtractionRollup(
        account_id=account_id,
        profile_signals_read=profile_signals_read,
        institutions_loaded=institutions_loaded,
        profile_hits_total=profile_hits_total,
        profile_hits_emittable=profile_hits_emittable,
        signals_inserted=signals_inserted,
        signals_skipped_dedup=signals_skipped_dedup,
        signals_skipped_other_type=signals_skipped_other_type,
        errors=errors,
        dry_run=False,
    )


async def bulk_linkedin_profile_extraction_all_accounts(
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> list[LinkedInExtractionRollup]:
    """Iterate every account with linkedin_profile signals and emit synthetic education rows."""
    async with acquire() as conn:
        account_ids = await _fetch_all_account_ids(conn)
    log.info("linkedin_profile all-accounts: %d accounts", len(account_ids))
    rollups: list[LinkedInExtractionRollup] = []
    for account_id in account_ids:
        rollup = await bulk_linkedin_profile_extraction_account(
            account_id, limit=limit, dry_run=dry_run,
        )
        rollups.append(rollup)
    return rollups


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="credence.jobs.bulk_linkedin_profile_extraction",
        description=(
            "Phase 1 LinkedIn-headline extractor → emits synthetic education "
            "signal rows by alias-matching institution names. School-only."
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
        help="Iterate every account with v2 linkedin_profile signals.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap profile signals read per account (default: no cap).",
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


def _print_rollup(rollup: LinkedInExtractionRollup) -> None:
    msg = (
        f"linkedin_profile account={rollup.account_id} "
        f"profile_signals_read={rollup.profile_signals_read} "
        f"institutions_loaded={rollup.institutions_loaded} "
        f"profile_hits_total={rollup.profile_hits_total} "
        f"profile_hits_emittable={rollup.profile_hits_emittable} "
        f"signals_inserted={rollup.signals_inserted} "
        f"signals_skipped_dedup={rollup.signals_skipped_dedup} "
        f"signals_skipped_other_type={rollup.signals_skipped_other_type} "
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

    async def _go() -> list[LinkedInExtractionRollup]:
        try:
            if args.all_accounts:
                return await bulk_linkedin_profile_extraction_all_accounts(
                    limit=args.limit, dry_run=args.dry_run,
                )
            return [
                await bulk_linkedin_profile_extraction_account(
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
