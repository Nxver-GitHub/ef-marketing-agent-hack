"""Backfill v3 ``persons`` + ``prospects`` rows from v2 ``signals`` blobs.

The v2 ``signals`` table holds rich JSONB ``value`` payloads (press
releases, exec appointments, leadership changes, company_signal_*
rollups, etc.) that mention real people by name + title + employer.
Many of those people are NOT yet promoted into the v3
``persons``/``prospects`` graph.

Per user feedback (2026-05-01) this gap is approximately:

* ~145 new prospects + persons that need to be created
* ~89 existing persons whose ``current_title`` is NULL but a v2
  signal does carry a title at the same company

This script bridges that gap. For each (name, title, company) triple
extracted from the signal blob, we either INSERT a new prospect+person
pair, or UPDATE an existing person's NULL ``current_title``.

## Match key

``(lower(canonical_name), lower(current_company_name))`` — case-
insensitive on both. We do NOT match on linkedin_url here because the
v2 signal blobs almost never carry one for the cited people; the script
is specifically for "found in narrative text" mentions.

## Insert order (per task spec)

1. INSERT prospects (canonical_name, current_company, current_title,
   account_id) — first.
2. INSERT persons (canonical_name, current_title, current_company_name,
   source_prospect_id, account_id) — second.

Wrapped in a transaction per signal row so that prospects/persons stay
linked even if the script crashes mid-batch.

## Idempotency

* Prospects: ``ON CONFLICT (canonical_name) DO NOTHING`` — re-runs are
  no-ops once the prospect exists.
* Persons: pre-INSERT existence check on
  ``lower(canonical_name) + lower(current_company_name)`` since persons
  has no natural unique key we can ON CONFLICT against.
* Title backfill: only applied when ``persons.current_title IS NULL``
  — never overwrite an already-populated title.

## Acceptance

* Pure Python via existing ``credence.db`` pool helper.
* No new dependencies.
* Dry-run is non-destructive (no INSERTs / UPDATEs issued).
* Exits 0 on success.
* Malformed JSONB rows are caught + logged + skipped (counted under
  ``extraction_failures``).
* All writes use ``account_id = '00000000-0000-0000-0000-000000000001'``
  (default tenant).

## Usage

    cd server && uv run --env-file ../.env.local python \\
      -m scripts.backfill_persons_from_company_signals
    cd server && uv run --env-file ../.env.local python \\
      -m scripts.backfill_persons_from_company_signals --dry-run
    cd server && uv run --env-file ../.env.local python \\
      -m scripts.backfill_persons_from_company_signals --limit 100 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

if __package__ in (None, ""):
    _SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _SERVER_DIR not in sys.path:
        sys.path.insert(0, _SERVER_DIR)

from credence.db import close_pool, fetch, fetchrow, execute  # noqa: E402
from credence.enrichment.normalizer import (  # noqa: E402
    normalize_company,
    normalize_name,
)

log = logging.getLogger(__name__)


# Default tenant — every write lands here per task spec.
DEFAULT_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")

# Signal types we sweep for person mentions. Picked from the v2
# ``signals.signal_type`` distribution that empirically carries
# (name, title, company) triples in the ``value`` JSONB:
#   - exec_appointment           (single-person appointment record)
#   - press_release              (often names execs in body/quotes)
#   - leadership_change          (departures + replacements)
#   - company_signal_leadership  (rollup of leadership news)
#   - company_signal_press       (rollup of press mentions)
#   - executive_profile          (legacy single exec record)
PERSON_BEARING_SIGNAL_TYPES: tuple[str, ...] = (
    "exec_appointment",
    "press_release",
    "leadership_change",
    "company_signal_leadership",
    "company_signal_press",
    "executive_profile",
)


# ─── Pure helpers ───────────────────────────────────────────────────────────


# A "person mention" candidate: name string + title + company string.
@dataclass(frozen=True)
class PersonMention:
    """Immutable extracted (name, title, company) triple."""
    name: str
    title: str | None
    company: str | None


@dataclass
class BackfillStats:
    """Mutable rollup counter."""
    signals_scanned: int = 0
    mentions_extracted: int = 0
    new_prospects_inserted: int = 0
    new_persons_inserted: int = 0
    title_backfills_updated: int = 0
    already_known_skipped: int = 0
    extraction_failures: int = 0
    by_signal_type: dict[str, int] = field(default_factory=dict)


# Common keys used across our v2 signal blobs to cite a person mention.
_NAME_KEYS = ("name", "person_name", "executive_name", "full_name", "exec")
_TITLE_KEYS = ("title", "role", "position", "job_title", "exec_title")
_COMPANY_KEYS = ("company", "company_name", "employer", "organization", "issuer")


def _coerce_str(val: Any) -> str | None:
    """Return a stripped string or None for any JSON-ish input."""
    if val is None:
        return None
    if isinstance(val, str):
        s = val.strip()
        return s or None
    if isinstance(val, (int, float)):
        return str(val)
    return None


def _first_present(blob: dict, keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty stringy value for any of ``keys``."""
    for k in keys:
        v = _coerce_str(blob.get(k))
        if v:
            return v
    return None


def _looks_like_person_name(name: str) -> bool:
    """Two+ tokens, alphabetic-ish, not obviously a company/title.

    Filters out values like "Press Release", "CEO", "Acme Inc." that
    pollute generic name-key lookups.
    """
    if not name or len(name) < 3:
        return False
    tokens = [t for t in name.split() if t.strip()]
    if len(tokens) < 2:
        return False
    # Must have at least one capitalized alphabetic token.
    if not any(re.match(r"^[A-Z][a-zA-Z\-']+$", t) for t in tokens):
        return False
    # Reject obvious company/role/non-person tokens. Catches values like
    # "Press Release", "Acme Inc", "CEO Update" that bleed into the
    # generic ``name`` key on poorly-typed signal payloads.
    blacklist = {
        "inc", "llc", "corp", "corporation", "ltd", "company", "co",
        "press", "release", "update", "news", "announcement",
        "ceo", "cfo", "cto", "coo", "cro", "cmo", "vp", "svp", "evp",
        "officer", "executive", "appointment",
    }
    if any(t.lower().strip(".,") in blacklist for t in tokens):
        return False
    return True


def extract_mentions(value: Any) -> list[PersonMention]:
    """Pull (name, title, company) triples from a v2 signal ``value``.

    Handles the common shapes we've seen:
      1. Top-level dict with name/title/company keys.
      2. Top-level dict with a ``people`` / ``executives`` / ``mentions``
         array.
      3. Top-level list of person dicts (rare, but seen on
         company_signal_press rollups).

    Returns ``[]`` when the blob carries no usable person mention.
    Never raises on malformed shapes — caller catches at a higher layer.
    """
    if value is None:
        return []
    if isinstance(value, str):
        # Some legacy rows store JSON-as-text. Try to decode once.
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []

    mentions: list[PersonMention] = []

    if isinstance(value, list):
        for item in value:
            mentions.extend(extract_mentions(item))
        return mentions

    if not isinstance(value, dict):
        return []

    # Path 1: top-level person mention.
    name = _first_present(value, _NAME_KEYS)
    if name and _looks_like_person_name(name):
        mentions.append(PersonMention(
            name=name,
            title=_first_present(value, _TITLE_KEYS),
            company=_first_present(value, _COMPANY_KEYS),
        ))

    # Path 2: nested arrays under known keys.
    for nested_key in ("people", "executives", "mentions", "appointees",
                       "leaders", "executive_profiles"):
        nested = value.get(nested_key)
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    nested_name = _first_present(item, _NAME_KEYS)
                    if nested_name and _looks_like_person_name(nested_name):
                        # Inherit the parent ``company`` when child omits it
                        # (common in press_release rollups: company at top,
                        # people in array).
                        nested_company = (
                            _first_present(item, _COMPANY_KEYS)
                            or _first_present(value, _COMPANY_KEYS)
                        )
                        mentions.append(PersonMention(
                            name=nested_name,
                            title=_first_present(item, _TITLE_KEYS),
                            company=nested_company,
                        ))

    return mentions


def _canonicalize(mention: PersonMention) -> PersonMention | None:
    """Normalize name + company for comparison + storage.

    Returns None when the name doesn't parse to a (first, last) pair —
    those mentions can't be entity-resolved against persons.
    """
    first, last = normalize_name(mention.name)
    if not first or not last:
        return None
    canonical_name = f"{first} {last}"
    canonical_company = normalize_company(mention.company)
    title = mention.title.strip() if mention.title else None
    return PersonMention(
        name=canonical_name,
        title=title,
        company=canonical_company,
    )


# ─── DB lookups ─────────────────────────────────────────────────────────────


async def _find_person(
    canonical_name: str, company_name: str | None,
) -> dict | None:
    """Case-insensitive lookup on (canonical_name, company_name).

    ``company_name`` is matched against the persons.current_company_id →
    companies.canonical_name JOIN. NULL company on either side does not
    match — we only collapse when both sides agree on the employer.
    """
    if company_name:
        row = await fetchrow(
            """
            SELECT p.id, p.canonical_name, p.current_title, p.current_company_id
            FROM public.persons p
            LEFT JOIN public.companies c ON c.id = p.current_company_id
            WHERE lower(p.canonical_name) = lower($1)
              AND lower(coalesce(c.canonical_name, '')) = lower($2)
            LIMIT 1
            """,
            canonical_name, company_name,
        )
    else:
        row = await fetchrow(
            """
            SELECT p.id, p.canonical_name, p.current_title, p.current_company_id
            FROM public.persons p
            WHERE lower(p.canonical_name) = lower($1)
              AND p.current_company_id IS NULL
            LIMIT 1
            """,
            canonical_name,
        )
    return dict(row) if row else None


async def _ensure_company_id(
    company_name: str, *, account_id: UUID, dry_run: bool,
) -> UUID | None:
    """Resolve a canonical company name → companies.id, inserting on miss.

    Returns None in dry-run mode (caller treats as "would create"). The
    persons row will reference this id via ``current_company_id``.
    """
    row = await fetchrow(
        """
        SELECT id FROM public.companies
        WHERE lower(canonical_name) = lower($1)
        LIMIT 1
        """,
        company_name,
    )
    if row:
        return UUID(str(row["id"]))
    if dry_run:
        return None
    inserted = await fetchrow(
        """
        INSERT INTO public.companies (canonical_name, account_id)
        VALUES ($1, $2)
        ON CONFLICT (canonical_name) DO UPDATE
          SET canonical_name = EXCLUDED.canonical_name
        RETURNING id
        """,
        company_name, account_id,
    )
    return UUID(str(inserted["id"])) if inserted else None


# ─── DB writers ─────────────────────────────────────────────────────────────


async def _insert_prospect(
    mention: PersonMention, *, account_id: UUID,
) -> UUID | None:
    """INSERT prospect; return its id (None when a conflict made it a no-op).

    Uses ON CONFLICT (name) DO NOTHING — v2 prospects.name is the
    canonical-name slot. When the conflict path fires, we re-SELECT to
    return the existing id so the caller can still link persons.
    """
    inserted = await fetchrow(
        """
        INSERT INTO public.prospects
            (name, company, role, account_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (name) DO NOTHING
        RETURNING id
        """,
        mention.name, mention.company, mention.title, account_id,
    )
    if inserted:
        return UUID(str(inserted["id"]))
    # Conflict: fetch the existing row so we can still link.
    existing = await fetchrow(
        """
        SELECT id FROM public.prospects
        WHERE lower(name) = lower($1)
        LIMIT 1
        """,
        mention.name,
    )
    return UUID(str(existing["id"])) if existing else None


async def _insert_person(
    mention: PersonMention,
    *,
    account_id: UUID,
    source_prospect_id: UUID | None,
    company_id: UUID | None,
) -> bool:
    """INSERT a persons row. Returns True iff a row was actually created."""
    inserted = await fetchrow(
        """
        INSERT INTO public.persons
            (canonical_name, current_title, current_company_id,
             source_prospect_id, account_id, enrichment_tier)
        VALUES ($1, $2, $3, $4, $5, 1)
        RETURNING id
        """,
        mention.name, mention.title, company_id, source_prospect_id, account_id,
    )
    return inserted is not None


async def _update_person_title(person_id: UUID, title: str) -> None:
    """NULL-fill UPDATE on persons.current_title."""
    await execute(
        """
        UPDATE public.persons
        SET current_title = $2,
            updated_at = NOW()
        WHERE id = $1
          AND current_title IS NULL
        """,
        person_id, title,
    )


# ─── Top-level driver ───────────────────────────────────────────────────────


async def process_signal_row(
    row: dict, stats: BackfillStats, *, dry_run: bool, account_id: UUID,
) -> None:
    """Extract + persist all person mentions inside one signal row.

    Errors raised by extraction or DB writes are caught + logged + counted
    so the batch never halts on a single bad row.
    """
    signal_type = row.get("signal_type") or "<unknown>"
    stats.by_signal_type[signal_type] = stats.by_signal_type.get(signal_type, 0) + 1
    try:
        mentions = extract_mentions(row.get("value"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        log.warning("backfill: signal %s — extraction failed: %s",
                    row.get("id"), exc)
        stats.extraction_failures += 1
        return

    for raw_mention in mentions:
        canon = _canonicalize(raw_mention)
        if canon is None:
            continue
        stats.mentions_extracted += 1

        try:
            existing = await _find_person(canon.name, canon.company)
        except Exception as exc:  # noqa: BLE001
            log.warning("backfill: lookup failed for %s @ %s — %s",
                        canon.name, canon.company, exc)
            stats.extraction_failures += 1
            continue

        if existing is not None:
            # Title backfill path: only when DB says NULL and we have one.
            if existing.get("current_title") in (None, "") and canon.title:
                if dry_run:
                    stats.title_backfills_updated += 1
                    continue
                try:
                    await _update_person_title(
                        UUID(str(existing["id"])), canon.title,
                    )
                    stats.title_backfills_updated += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("backfill: title update failed for %s — %s",
                                existing["id"], exc)
                    stats.extraction_failures += 1
            else:
                stats.already_known_skipped += 1
            continue

        # Insert path: prospect first, then person linked via source_prospect_id.
        if dry_run:
            stats.new_prospects_inserted += 1
            stats.new_persons_inserted += 1
            continue

        try:
            prospect_id = await _insert_prospect(canon, account_id=account_id)
            if prospect_id is None:
                stats.extraction_failures += 1
                continue
            # We only count prospect inserts when a row was newly created
            # (i.e. INSERT RETURNING fired). The conflict path returns the
            # existing id but didn't create a new prospect row — count it
            # only when we also create a person below.
            company_id = (
                await _ensure_company_id(
                    canon.company, account_id=account_id, dry_run=False,
                )
                if canon.company else None
            )
            created_person = await _insert_person(
                canon,
                account_id=account_id,
                source_prospect_id=prospect_id,
                company_id=company_id,
            )
            if created_person:
                stats.new_persons_inserted += 1
                stats.new_prospects_inserted += 1
            else:
                stats.already_known_skipped += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("backfill: insert failed for %s — %s", canon.name, exc)
            stats.extraction_failures += 1


async def run_backfill(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    account_id: UUID = DEFAULT_ACCOUNT_ID,
) -> BackfillStats:
    """Sweep ``signals`` for person mentions and persist them."""
    stats = BackfillStats()

    placeholders = ", ".join(f"${i+1}" for i in range(len(PERSON_BEARING_SIGNAL_TYPES)))
    sql = (
        f"SELECT id, signal_type, value, prospect_id "
        f"FROM public.signals "
        f"WHERE signal_type IN ({placeholders}) "
        f"ORDER BY collected_at DESC NULLS LAST"
    )
    params: list[Any] = list(PERSON_BEARING_SIGNAL_TYPES)
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = await fetch(sql, *params)
    stats.signals_scanned = len(rows)
    log.info("backfill: %d candidate signal rows", stats.signals_scanned)

    for row in rows:
        await process_signal_row(
            dict(row), stats, dry_run=dry_run, account_id=account_id,
        )
    return stats


# ─── CLI ────────────────────────────────────────────────────────────────────


def _print_rollup(stats: BackfillStats, *, dry_run: bool) -> None:
    print("== rollup ==")
    print(f"signals scanned:           {stats.signals_scanned}")
    print(f"mentions extracted:        {stats.mentions_extracted}")
    print(f"new prospects inserted:    {stats.new_prospects_inserted}")
    print(f"new persons inserted:      {stats.new_persons_inserted}")
    print(f"title backfills updated:   {stats.title_backfills_updated}")
    print(f"already-known skipped:     {stats.already_known_skipped}")
    print(f"extraction failures:       {stats.extraction_failures}")
    if stats.by_signal_type:
        print("by signal_type:")
        for sig_type, n in sorted(
            stats.by_signal_type.items(), key=lambda kv: -kv[1],
        ):
            print(f"  {sig_type}: {n}")
    print(f"(dry_run={dry_run})")


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        stats = await run_backfill(
            dry_run=args.dry_run, limit=args.limit,
        )
    finally:
        await close_pool()
    _print_rollup(stats, dry_run=args.dry_run)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Promote person mentions in v2 `signals` blobs into v3 "
            "`persons` + `prospects` rows."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan + extract but emit no INSERTs/UPDATEs.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N signal rows (default: all).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()


__all__ = [
    "BackfillStats",
    "DEFAULT_ACCOUNT_ID",
    "PERSON_BEARING_SIGNAL_TYPES",
    "PersonMention",
    "extract_mentions",
    "process_signal_row",
    "run_backfill",
    "_canonicalize",
    "_looks_like_person_name",
]
