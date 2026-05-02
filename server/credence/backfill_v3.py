"""Track F — v2→v3 backfill ETL.

Reads existing v2 ``prospects`` rows and writes the equivalent v3 entity rows
into ``persons`` / ``companies`` / ``employment_periods`` (added by migration
``20260430_v3_connection_graph.sql``). Pure additive: does not touch v2 tables.

What it produces
----------------
For every prospect row P with name N, current company C, role R and N past
employer strings P_i:

  - 1 ``companies`` row per distinct canonical company name across the run
    (current company + each past company).
  - 1 ``persons`` row per prospect, with ``current_company_id`` set,
    ``current_title``/``current_seniority_score``/``current_functional_domain``
    derived from R, and ``linkedin_url`` carried through.
  - 1 ``employment_periods`` row for the current job (is_current=TRUE).
  - 1 ``employment_periods`` row per past company (title/years NULL — v2
    schema doesn't carry them; that fidelity will come from a richer
    extractor later).

What it does NOT produce
------------------------
- ``person_connections`` rows (downstream task — the career-overlap SQL from
  CLAUDE.md L893 runs after this script lands).
- ``patents`` / ``patent_inventors`` rows (USPTO extractor / Track J).
- ``connection_evidence`` rows (paired with the connection writers).
- ``education_periods`` rows (table not in the v3 migration cut yet — v2
  ``prospects.education`` data stays in the JSONB column for now).

Idempotency
-----------
Re-runnable. Each entity is matched-or-inserted within the same transaction:

- companies: matched by ``canonical_name`` (computed via ``normalize_company``
  to mirror the frontend's ``normalizeCompany``).
- persons: matched by ``linkedin_url`` when present; otherwise by the
  composite ``(canonical_name, current_company_id)``.
- employment_periods: matched by ``(person_id, company_id, is_current)`` for
  current rows, or ``(person_id, company_id, title)`` for past rows.

Existing rows are left untouched (no UPDATE) — this is a backfill, not a
sync. Re-running after data changes upstream will add new rows but won't
overwrite enrichment that other pipelines may have written downstream.

Run
---
    cd server
    python -m credence.backfill_v3                      # full backfill
    python -m credence.backfill_v3 --limit 50           # first 50 prospects
    python -m credence.backfill_v3 --dry-run            # plan, no writes

Exit codes
----------
0 on success, 1 on uncaught failure. Per-prospect failures are logged but
do not abort the run; the summary at the end reports inserted/skipped/failed
counts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from dataclasses import dataclass, field
from uuid import UUID

import asyncpg

from credence.auth import DEFAULT_ACCOUNT_ID
from credence.db import close_pool, get_pool

log = logging.getLogger("backfill_v3")


# ─────────────────────────────────────────────────────────────────────────────
# Normalization helpers — mirror src/lib/graph.ts so canonical_name values
# match between backend backfill and frontend rendering.
# ─────────────────────────────────────────────────────────────────────────────

# Frontend (graph.ts:165) strips: corp/corporation/inc/incorporated/limited/
# ltd/llc/plc/technologies/technology/semiconductor(s)/systems.
_CORP_SUFFIX_RE = re.compile(
    r"\b(corp\.?|corporation|inc\.?|incorporated|limited|ltd\.?|llc|plc|"
    r"technologies|technology|semiconductor|semiconductors?|systems?)\b",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_company(name: str | None) -> str:
    """Lowercased, suffix-stripped, whitespace-collapsed company key.

    Output is suitable for use as ``companies.canonical_name``. Mirrors
    ``normalizeCompany`` in ``src/lib/graph.ts:165`` so backend-canonical and
    frontend-canonical match.
    """
    if not name:
        return ""
    s = name.lower()
    s = _CORP_SUFFIX_RE.sub("", s)
    s = _NON_ALNUM_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s)
    return s.strip()


# Title-keyword → seniority score, ordered by specificity (most specific first
# wins). Values come from CLAUDE.md "Seniority Taxonomy".
#
# IMPORTANT — `\bpresident\b` (rank 95) is placed AFTER all *Vice President
# variants. Otherwise it greedy-matches "Senior Vice President" (which contains
# "President") and clobbers the SVP/VP rules. This was Bug #1 in
# LavenderPrairie's msg 54 + xfailed in test_backfill_v3.py before this patch.
_SENIORITY_RULES: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\bceo\b|chief executive officer", re.I), 100),
    (re.compile(r"\bc(t|o|f|p|r)o\b|chief (technology|operating|financial|product|revenue) officer", re.I), 89),
    (re.compile(r"\bevp\b|executive vice president", re.I), 82),
    (re.compile(r"\bsvp\b|senior vice president", re.I), 80),
    (re.compile(r"group vp", re.I), 72),
    (re.compile(r"\bvp\b|vice president", re.I), 70),
    # `\bpresident\b` MUST come AFTER the VP rules above so that *Vice President
    # variants don't accidentally match this. Plain "President" still works
    # because none of the above match it.
    (re.compile(r"\bpresident\b", re.I), 95),
    (re.compile(r"principal director", re.I), 63),
    (re.compile(r"senior director", re.I), 62),
    (re.compile(r"\bdirector\b", re.I), 60),
    (re.compile(r"distinguished engineer", re.I), 55),
    (re.compile(r"senior manager|group manager", re.I), 52),
    (re.compile(r"engineering manager|\bmanager\b", re.I), 50),
    (re.compile(r"principal engineer", re.I), 48),
    (re.compile(r"staff engineer", re.I), 45),
    # Bug 2 fix (DarkBeaver): match "Senior <X> Engineer" where X is any
    # discipline modifier (Software / Hardware / Platform / etc.) so the
    # whole "Senior <X> Engineer" family resolves to Senior Engineer = 40
    # per CLAUDE.md taxonomy. Bounded to 0-3 middle words to avoid greedy
    # matches across unrelated phrases. "Senior Staff Engineer" still hits
    # `staff engineer` (45) above this line because Postgres rule order is
    # specificity-first; same reasoning preserves Senior Principal Engineer = 48.
    (re.compile(r"\bsenior\s+(?:[\w-]+\s+){0,3}engineer\b", re.I), 40),
    (re.compile(r"\bengineer\b|\barchitect\b", re.I), 35),
]


def infer_seniority(title: str | None) -> int | None:
    if not title:
        return None
    for pat, score in _SENIORITY_RULES:
        if pat.search(title):
            return score
    return None


# Title-keyword → functional_domain, ordered most-specific first. Values match
# the CHECK constraint in migration 20260430_v3_connection_graph.sql L97-108.
#
# IMPORTANT — `people_ops` is placed BEFORE `manufacturing_ops`. Otherwise
# `manufacturing_ops`'s `\boperations\b` keyword greedy-matches "People
# Operations" and misclassifies it as manufacturing. This was Bug #3 in
# LavenderPrairie's msg 54 + xfailed in test_backfill_v3.py before this patch.
_FUNCTIONAL_DOMAIN_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(rtl|verification|physical design|analog|mixed signal|memory design|soc|chip|silicon|hardware|fpga|asic)\b", re.I), "hardware_engineering"),
    (re.compile(r"\b(software|firmware|embedded|sdk|driver|bsp|backend|frontend|full[- ]?stack|devops|sre|platform)\b", re.I), "software_engineering"),
    (re.compile(r"\b(product manager|program manager|tpm|product owner|roadmap)\b", re.I), "product_management"),
    # people_ops MUST come before manufacturing_ops — otherwise "People
    # Operations" hits manufacturing's `\boperations\b` first.
    (re.compile(r"\b(human resources|recruiting|people operations|culture|\bhr\b)\b", re.I), "people_ops"),
    (re.compile(r"\b(manufacturing|operations|supply chain|yield|process|fab|foundry|quality|reliability)\b", re.I), "manufacturing_ops"),
    (re.compile(r"\b(sales|marketing|gtm|account management|partnerships|business development|\bbd\b)\b", re.I), "sales_marketing"),
    (re.compile(r"\b(research|advanced development|pathfinding|exploratory|scientist)\b", re.I), "research"),
    (re.compile(r"\b(finance|legal|compliance|accounting|tax|controller)\b", re.I), "finance_legal"),
    (re.compile(r"\bgeneral manager\b|\bgm\b|business unit|p&l", re.I), "general_management"),
]


def infer_functional_domain(title: str | None) -> str | None:
    if not title:
        return None
    for pat, domain in _FUNCTIONAL_DOMAIN_RULES:
        if pat.search(title):
            return domain
    return None


# ─────────────────────────────────────────────────────────────────────────────
# v2 career_history → v3 employment_periods year extraction.
#
# v2 ``signals`` table holds ``signal_type='career_history'`` rows whose
# ``value`` JSONB has shape::
#
#     {"roles": [{"company": "NVIDIA", "role": "Director of AI",
#                 "years": "2018-2022"}, ...]}
#
# The ``years`` string format is irregular: "2018-2022", "2018 – 2022",
# "2018-present", "1993", "annual", "" or missing. ``_parse_role_years``
# extracts (start_year, end_year) by pulling all 4-digit groups; first is
# start, second (if any) is end. Out-of-range or malformed values fall back
# to None so the downstream INSERT still succeeds with NULL years (matching
# legacy behaviour for prospects without career_history signals).
# ─────────────────────────────────────────────────────────────────────────────


_YEAR_PATTERN = re.compile(r"\b(\d{4})\b")


@dataclass(frozen=True)
class CareerHistoryRole:
    """One past-employment row extracted from a v2 career_history signal."""

    company: str
    title: str | None
    start_year: int | None
    end_year: int | None


def _parse_role_years(years_str: str | None) -> tuple[int | None, int | None]:
    """Parse v2 career_history role.years string → (start_year, end_year).

    Examples
    --------
    >>> _parse_role_years("2018-2022")
    (2018, 2022)
    >>> _parse_role_years("2018 – 2022")
    (2018, 2022)
    >>> _parse_role_years("2018-present")
    (2018, None)
    >>> _parse_role_years("1993")
    (1993, None)
    >>> _parse_role_years("annual")
    (None, None)
    >>> _parse_role_years("")
    (None, None)
    >>> _parse_role_years(None)
    (None, None)

    Sanity checks:
    - Both years must fall in 1900..2100; out-of-range values return None.
    - Malformed ranges (end < start) drop the end year.
    """
    if not years_str or not isinstance(years_str, str):
        return (None, None)
    matches = _YEAR_PATTERN.findall(years_str)
    if not matches:
        return (None, None)
    start = int(matches[0])
    end: int | None = int(matches[1]) if len(matches) >= 2 else None
    if not (1900 <= start <= 2100):
        return (None, None)
    if end is not None and not (1900 <= end <= 2100):
        end = None
    if end is not None and end < start:
        end = None
    return (start, end)


async def _load_career_history(
    conn: asyncpg.Connection, prospect_id: UUID,
) -> list[CareerHistoryRole]:
    """Read v2 career_history signals; emit one record per role with parsed years.

    Roles missing a non-empty ``company`` are skipped — without a company we
    can't map to a v3 ``companies`` row. Roles with unparseable ``years`` still
    return; their start_year / end_year are NULL.
    """
    rows = await conn.fetch(
        "SELECT value FROM signals WHERE prospect_id = $1 AND signal_type = 'career_history'",
        prospect_id,
    )
    out: list[CareerHistoryRole] = []
    for row in rows:
        value = row["value"]
        if not isinstance(value, dict):
            continue
        roles = value.get("roles")
        if not isinstance(roles, list):
            continue
        for r in roles:
            if not isinstance(r, dict):
                continue
            company_raw = r.get("company")
            company = company_raw.strip() if isinstance(company_raw, str) else ""
            if not company:
                continue
            title_raw = r.get("role")
            title = (
                title_raw.strip()
                if isinstance(title_raw, str) and title_raw.strip()
                else None
            )
            start_year, end_year = _parse_role_years(r.get("years"))
            out.append(
                CareerHistoryRole(
                    company=company,
                    title=title,
                    start_year=start_year,
                    end_year=end_year,
                )
            )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# In-memory caches keyed by canonical name to dedupe within a run.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BackfillCounters:
    prospects_seen: int = 0
    persons_inserted: int = 0
    persons_matched: int = 0
    companies_inserted: int = 0
    companies_matched: int = 0
    employments_inserted: int = 0
    employments_skipped: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Caches:
    company_id_by_canonical: dict[str, UUID] = field(default_factory=dict)
    person_id_by_linkedin: dict[str, UUID] = field(default_factory=dict)
    person_id_by_composite: dict[tuple[str, UUID], UUID] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Per-entity match-or-insert helpers.
# ─────────────────────────────────────────────────────────────────────────────


async def ensure_company(
    conn: asyncpg.Connection,
    raw_name: str | None,
    cache: Caches,
    counters: BackfillCounters,
    *,
    dry_run: bool,
    account_id: UUID,
) -> UUID | None:
    """Match-or-insert a company by canonical_name. Returns the row's UUID.

    E5 polish (DarkBeaver): now that migration E.1 added
    `companies_canonical_name_key UNIQUE`, the previous SELECT-then-INSERT
    pattern (2 round-trips on cache miss) collapses to `INSERT … ON CONFLICT
    DO NOTHING RETURNING id` (1 round-trip on insert, 2 on conflict). Also
    safe under concurrent writers — Phase 3 (Parallel) and future extractors
    can hit this code path simultaneously without producing duplicate rows.
    """
    canonical = normalize_company(raw_name)
    if not canonical:
        return None

    if canonical in cache.company_id_by_canonical:
        return cache.company_id_by_canonical[canonical]

    if dry_run:
        # In dry-run we can't INSERT, so fall back to a pure SELECT to
        # report what would have happened.
        existing = await conn.fetchrow(
            "SELECT id FROM companies WHERE canonical_name = $1", canonical,
        )
        if existing:
            counters.companies_matched += 1
            cache.company_id_by_canonical[canonical] = existing["id"]
            return existing["id"]
        return None

    name_variants = [raw_name] if raw_name and raw_name != canonical else []
    row = await conn.fetchrow(
        """
        INSERT INTO companies (canonical_name, name_variants, account_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (canonical_name) DO NOTHING
        RETURNING id
        """,
        canonical, name_variants, account_id,
    )
    if row is not None:
        # Inserted (no conflict).
        company_id = row["id"]
        counters.companies_inserted += 1
    else:
        # Existed already — ON CONFLICT DO NOTHING returns 0 rows; fetch.
        existing = await conn.fetchrow(
            "SELECT id FROM companies WHERE canonical_name = $1", canonical,
        )
        company_id = existing["id"]
        counters.companies_matched += 1
    cache.company_id_by_canonical[canonical] = company_id
    return company_id


async def ensure_person(
    conn: asyncpg.Connection,
    prospect: asyncpg.Record,
    current_company_id: UUID | None,
    cache: Caches,
    counters: BackfillCounters,
    *,
    dry_run: bool,
    account_id: UUID,
) -> UUID | None:
    """Match-or-insert a person. Match key: linkedin_url, falling back to
    (canonical name + current_company_id)."""
    raw_name = prospect["name"]
    canonical_name = normalize_company(raw_name)  # same suffix-strip logic
    linkedin_url = prospect["linkedin_url"]
    title = prospect["role"]

    if linkedin_url and linkedin_url in cache.person_id_by_linkedin:
        return cache.person_id_by_linkedin[linkedin_url]

    composite_key = (canonical_name, current_company_id) if current_company_id else None
    if composite_key and composite_key in cache.person_id_by_composite:
        return cache.person_id_by_composite[composite_key]

    if linkedin_url:
        existing = await conn.fetchrow(
            "SELECT id FROM persons WHERE linkedin_url = $1",
            linkedin_url,
        )
        if existing:
            person_id = existing["id"]
            counters.persons_matched += 1
            cache.person_id_by_linkedin[linkedin_url] = person_id
            if composite_key:
                cache.person_id_by_composite[composite_key] = person_id
            return person_id

    if composite_key:
        existing = await conn.fetchrow(
            """
            SELECT id FROM persons
            WHERE canonical_name = $1 AND current_company_id = $2
            """,
            canonical_name, current_company_id,
        )
        if existing:
            person_id = existing["id"]
            counters.persons_matched += 1
            cache.person_id_by_composite[composite_key] = person_id
            if linkedin_url:
                cache.person_id_by_linkedin[linkedin_url] = person_id
            return person_id

    if dry_run:
        return None

    seniority = infer_seniority(title)
    domain = infer_functional_domain(title)
    name_variants = [raw_name] if raw_name and raw_name != canonical_name else []

    # Capture source_prospect_id at INSERT time so the linkage is established
    # eagerly — eliminates the post-hoc UPDATE pass SunnyRidge runs in the
    # bulk_career_overlap_signals loop (msg 192).
    inserted = await conn.fetchrow(
        """
        INSERT INTO persons (
          canonical_name, name_variants, linkedin_url, current_company_id,
          current_title, current_seniority_score, current_functional_domain,
          enrichment_tier, account_id, source_prospect_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id
        """,
        canonical_name, name_variants, linkedin_url, current_company_id,
        title, seniority, domain, 1, account_id,
        prospect["id"],
    )
    person_id = inserted["id"]
    counters.persons_inserted += 1
    if linkedin_url:
        cache.person_id_by_linkedin[linkedin_url] = person_id
    if composite_key:
        cache.person_id_by_composite[composite_key] = person_id
    return person_id


async def upsert_employment(
    conn: asyncpg.Connection,
    *,
    person_id: UUID,
    company_id: UUID,
    title: str | None,
    is_current: bool,
    counters: BackfillCounters,
    dry_run: bool,
    account_id: UUID,
    start_year: int | None = None,
    end_year: int | None = None,
) -> None:
    """Insert an employment_period row if no equivalent already exists.

    "Equivalent" is keyed on (person_id, company_id, is_current) for current
    rows and (person_id, company_id, title) for past rows. Past rows can
    legitimately repeat (a person might have worked at the same company in two
    different roles); current rows cannot.
    """
    if is_current:
        existing = await conn.fetchrow(
            """
            SELECT id FROM employment_periods
            WHERE person_id = $1 AND company_id = $2 AND is_current = TRUE
            """,
            person_id, company_id,
        )
    else:
        # Match either an exact title-equal row OR a legacy NULL-title row
        # (the past_companies fallback path inserts those). Prefer the
        # title-equal row when both exist so a real title doesn't fold into
        # an empty placeholder.
        existing = await conn.fetchrow(
            """
            SELECT id FROM employment_periods
            WHERE person_id = $1 AND company_id = $2 AND is_current = FALSE
              AND (title IS NULL OR COALESCE(title, '') = COALESCE($3, ''))
            ORDER BY (title IS NOT NULL) DESC, id
            LIMIT 1
            """,
            person_id, company_id, title,
        )
    if existing:
        # Retro-fill metadata onto rows that were inserted before
        # career_history year extraction (or before the title was extracted
        # from the role field) landed. ``COALESCE(<existing>, <new>)``
        # guarantees we only fill NULLs and never overwrite a real value
        # — full idempotency on repeat runs.
        if not is_current:
            await conn.execute(
                """
                UPDATE employment_periods
                SET start_year        = COALESCE(start_year, $2),
                    end_year          = COALESCE(end_year, $3),
                    title             = COALESCE(title, $4),
                    seniority_score   = COALESCE(seniority_score, $5),
                    functional_domain = COALESCE(functional_domain, $6)
                WHERE id = $1
                """,
                existing["id"], start_year, end_year, title,
                infer_seniority(title) if title else None,
                infer_functional_domain(title) if title else None,
            )
        counters.employments_skipped += 1
        return

    if dry_run:
        return

    seniority = infer_seniority(title)
    domain = infer_functional_domain(title)
    await conn.execute(
        """
        INSERT INTO employment_periods (
          person_id, company_id, title, functional_domain, seniority_score,
          is_current, account_id, start_year, end_year
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        person_id, company_id, title, domain, seniority, is_current, account_id,
        start_year, end_year,
    )
    counters.employments_inserted += 1


# ─────────────────────────────────────────────────────────────────────────────
# Main backfill loop.
# ─────────────────────────────────────────────────────────────────────────────


async def fetch_prospects(conn: asyncpg.Connection, limit: int | None) -> list[asyncpg.Record]:
    sql = "SELECT id, name, company, role, linkedin_url, past_companies FROM prospects"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return await conn.fetch(sql)


async def backfill_one(
    conn: asyncpg.Connection,
    prospect: asyncpg.Record,
    cache: Caches,
    counters: BackfillCounters,
    *,
    dry_run: bool,
    account_id: UUID,
) -> None:
    counters.prospects_seen += 1

    current_company_id = await ensure_company(
        conn, prospect["company"], cache, counters, dry_run=dry_run,
        account_id=account_id,
    )
    person_id = await ensure_person(
        conn, prospect, current_company_id, cache, counters, dry_run=dry_run,
        account_id=account_id,
    )

    if person_id and current_company_id:
        await upsert_employment(
            conn,
            person_id=person_id,
            company_id=current_company_id,
            title=prospect["role"],
            is_current=True,
            counters=counters,
            dry_run=dry_run,
            account_id=account_id,
        )

    if not person_id:
        return

    # Prefer richer career_history signals (with title + years) when available.
    # Fall back to the v2 ``past_companies`` string[] for prospects without
    # any career_history signal — those continue to land with NULL years,
    # matching pre-patch behaviour.
    career_roles = await _load_career_history(conn, prospect["id"])
    current_canonical = (
        normalize_company(prospect["company"]) if prospect["company"] else None
    )
    seen_career_company_ids: set[UUID] = set()

    for role in career_roles:
        # Skip roles that resolve to the current employer — those are already
        # covered by the is_current=TRUE row above.
        if current_canonical and normalize_company(role.company) == current_canonical:
            continue
        past_id = await ensure_company(
            conn, role.company, cache, counters, dry_run=dry_run,
            account_id=account_id,
        )
        if past_id and past_id != current_company_id:
            await upsert_employment(
                conn,
                person_id=person_id,
                company_id=past_id,
                title=role.title,
                is_current=False,
                counters=counters,
                dry_run=dry_run,
                account_id=account_id,
                start_year=role.start_year,
                end_year=role.end_year,
            )
            seen_career_company_ids.add(past_id)

    if not career_roles:
        # Legacy fallback: ``past_companies`` string[] with no year metadata.
        past_companies = prospect["past_companies"] or []
        for past in past_companies:
            if not isinstance(past, str):
                continue  # Defensive — schema says string[] but data may drift
            past_id = await ensure_company(
                conn, past, cache, counters, dry_run=dry_run,
                account_id=account_id,
            )
            if (
                past_id
                and past_id != current_company_id
                and past_id not in seen_career_company_ids
            ):
                await upsert_employment(
                    conn,
                    person_id=person_id,
                    company_id=past_id,
                    title=None,
                    is_current=False,
                    counters=counters,
                    dry_run=dry_run,
                    account_id=account_id,
                )


async def run(
    limit: int | None,
    dry_run: bool,
    account_id: UUID = DEFAULT_ACCOUNT_ID,
) -> BackfillCounters:
    cache = Caches()
    counters = BackfillCounters()

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            prospects = await fetch_prospects(conn, limit)
            log.info(
                "Loaded %d prospects from v2 schema (account_id=%s)",
                len(prospects), account_id,
            )

            for prospect in prospects:
                async with conn.transaction():
                    try:
                        await backfill_one(
                            conn, prospect, cache, counters, dry_run=dry_run,
                            account_id=account_id,
                        )
                    except Exception as exc:
                        prospect_id = str(prospect["id"])
                        log.exception("Backfill failed for prospect %s", prospect_id)
                        counters.failures.append((prospect_id, str(exc)))
                        raise  # rollback this prospect's transaction
    finally:
        await close_pool()

    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description="v2 → v3 backfill ETL")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N prospects (default: all).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Read prospects and resolve match-or-insert decisions but skip writes.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        help="Python logging level (default: INFO).",
    )
    parser.add_argument(
        "--account-id", type=UUID, default=DEFAULT_ACCOUNT_ID,
        help=(
            "Tenant to assign all backfilled rows to. Defaults to the "
            "DEFAULT tenant (00000000-…-001), which is the v2-compat home "
            "for unauthenticated/legacy data per the multitenant migration."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    counters = asyncio.run(run(args.limit, args.dry_run, args.account_id))

    log.info(
        "Backfill summary — prospects_seen=%d "
        "persons[inserted=%d, matched=%d] "
        "companies[inserted=%d, matched=%d] "
        "employments[inserted=%d, skipped=%d] "
        "failures=%d (dry_run=%s)",
        counters.prospects_seen,
        counters.persons_inserted, counters.persons_matched,
        counters.companies_inserted, counters.companies_matched,
        counters.employments_inserted, counters.employments_skipped,
        len(counters.failures), args.dry_run,
    )
    if counters.failures:
        log.warning("First failure: %s", counters.failures[0])
    return 0 if not counters.failures else 1


if __name__ == "__main__":
    sys.exit(main())
