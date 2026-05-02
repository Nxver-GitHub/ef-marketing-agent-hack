"""Persistence layer for enrichment pipeline output."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..db import execute, fetchrow
from .normalizer import CanonicalPerson, normalize_company

logger = logging.getLogger(__name__)


def _parse_iso_datetime(s: Any) -> datetime | None:
    """Apify emits ``registered_at`` as ISO strings like ``'2022-11-14T22:38:06.416Z'``.
    asyncpg's timestamptz parameter binding rejects strings — convert here.
    Returns None on any parse failure (caller treats as null).
    """
    if isinstance(s, datetime):
        return s
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        # Python 3.11+: fromisoformat handles 'Z' suffix
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


@dataclass
class WriteResult:
    """Counts of rows written, by table. Idempotent re-runs report
    inserts + updates separately so callers can see what changed."""

    persons_inserted: int = 0
    persons_updated: int = 0
    companies_inserted: int = 0
    companies_updated: int = 0
    employment_periods_inserted: int = 0
    employment_periods_updated: int = 0
    education_periods_inserted: int = 0
    education_periods_updated: int = 0
    person_signals_written: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_persons(self) -> int:
        return self.persons_inserted + self.persons_updated


# ─── Companies ──────────────────────────────────────────────────────────────


async def _upsert_company(
    canonical_name: str,
    *,
    account_id: UUID,
    industry: str | None = None,
    name_variants: list[str] | None = None,
    employee_count_estimate: int | None = None,
) -> tuple[UUID | None, bool]:
    """Insert or update a ``companies`` row. Returns ``(id, was_insert)``.

    Conflict key: ``canonical_name`` UNIQUE. On conflict, only fields that
    add information get updated (don't overwrite a populated value with
    None).
    """
    if not canonical_name:
        return None, False

    row = await fetchrow(
        """
        INSERT INTO public.companies
            (canonical_name, name_variants, industry, employee_count_estimate, account_id)
        VALUES ($1, $2::text[], $3, $4, $5)
        ON CONFLICT (canonical_name) DO UPDATE
        SET name_variants = (
                SELECT ARRAY(SELECT DISTINCT unnest(public.companies.name_variants || EXCLUDED.name_variants))
            ),
            industry = COALESCE(public.companies.industry, EXCLUDED.industry),
            employee_count_estimate = COALESCE(EXCLUDED.employee_count_estimate, public.companies.employee_count_estimate),
            updated_at = NOW()
        RETURNING id, (xmax = 0) AS inserted
        """,
        canonical_name,
        list(name_variants or []),
        industry,
        employee_count_estimate,
        account_id,
    )
    if row is None:
        return None, False
    return UUID(str(row["id"])), bool(row["inserted"])


# ─── Persons ────────────────────────────────────────────────────────────────


async def _upsert_person(
    person: CanonicalPerson,
    *,
    account_id: UUID,
    current_company_id: UUID | None,
) -> tuple[UUID | None, bool]:
    """Insert or update a ``persons`` row.

    Conflict resolution:
      - If ``linkedin_url`` present → ON CONFLICT (linkedin_url) — partial
        unique index handles the WHERE NOT NULL case automatically.
      - If no LinkedIn URL → SELECT by canonical_name + current_company_id.
        Insert when not found.

    Returns ``(person_id, was_insert)``.
    """
    if person.linkedin_url:
        # `persons_linkedin_url_key` is a PARTIAL unique index
        # `WHERE linkedin_url IS NOT NULL`. PostgreSQL doesn't infer
        # partial indexes from `ON CONFLICT (col)` alone — must repeat
        # the predicate. See `\d persons` for the index definition.
        row = await fetchrow(
            """
            INSERT INTO public.persons
                (canonical_name, name_variants, linkedin_url, orcid, uspto_inventor_id,
                 current_company_id, current_title, current_seniority_score,
                 current_functional_domain, enrichment_tier, account_id,
                 email, email_status, headline, location_text, country_code,
                 connections_count, followers_count, premium, verified,
                 open_to_work, hiring, registered_at)
            VALUES ($1, $2::text[], $3, $4, $5, $6, $7, $8, $9, $10, $11,
                    $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23::timestamptz)
            ON CONFLICT (linkedin_url) WHERE linkedin_url IS NOT NULL DO UPDATE
            SET canonical_name = COALESCE(public.persons.canonical_name, EXCLUDED.canonical_name),
                name_variants = (
                    SELECT ARRAY(SELECT DISTINCT unnest(public.persons.name_variants || EXCLUDED.name_variants))
                ),
                orcid = COALESCE(EXCLUDED.orcid, public.persons.orcid),
                uspto_inventor_id = COALESCE(EXCLUDED.uspto_inventor_id, public.persons.uspto_inventor_id),
                current_company_id = COALESCE(EXCLUDED.current_company_id, public.persons.current_company_id),
                current_title = COALESCE(EXCLUDED.current_title, public.persons.current_title),
                current_seniority_score = COALESCE(EXCLUDED.current_seniority_score, public.persons.current_seniority_score),
                current_functional_domain = COALESCE(EXCLUDED.current_functional_domain, public.persons.current_functional_domain),
                enrichment_tier = GREATEST(EXCLUDED.enrichment_tier, public.persons.enrichment_tier),
                email = COALESCE(EXCLUDED.email, public.persons.email),
                email_status = COALESCE(EXCLUDED.email_status, public.persons.email_status),
                headline = COALESCE(EXCLUDED.headline, public.persons.headline),
                location_text = COALESCE(EXCLUDED.location_text, public.persons.location_text),
                country_code = COALESCE(EXCLUDED.country_code, public.persons.country_code),
                connections_count = COALESCE(EXCLUDED.connections_count, public.persons.connections_count),
                followers_count = COALESCE(EXCLUDED.followers_count, public.persons.followers_count),
                premium = EXCLUDED.premium OR public.persons.premium,
                verified = EXCLUDED.verified OR public.persons.verified,
                open_to_work = EXCLUDED.open_to_work,
                hiring = EXCLUDED.hiring,
                registered_at = COALESCE(public.persons.registered_at, EXCLUDED.registered_at),
                updated_at = NOW()
            RETURNING id, (xmax = 0) AS inserted
            """,
            person.canonical_name,
            list(person.name_variants),
            person.linkedin_url,
            person.orcid,
            person.uspto_inventor_id,
            current_company_id,
            person.current_title,
            person.current_seniority_score,
            person.current_functional_domain,
            3 if person.linkedin_url else 2,  # tier 3 if we have LinkedIn
            account_id,
            person.email,
            person.email_status,
            getattr(person, "headline", None),
            person.location_text,
            person.country_code,
            getattr(person, "connections_count", None),
            getattr(person, "followers_count", None),
            getattr(person, "premium", False),
            getattr(person, "verified", False),
            getattr(person, "open_to_work", False),
            getattr(person, "hiring", False),
            _parse_iso_datetime(getattr(person, "registered_at", None)),
        )
        if row is None:
            return None, False
        return UUID(str(row["id"])), bool(row["inserted"])

    # No LinkedIn URL — SELECT by canonical_name + current_company_id, fall
    # back to INSERT
    existing = await fetchrow(
        """
        SELECT id FROM public.persons
        WHERE canonical_name = $1
          AND COALESCE(current_company_id, '00000000-0000-0000-0000-000000000000'::uuid)
              = COALESCE($2::uuid, '00000000-0000-0000-0000-000000000000'::uuid)
          AND account_id = $3
        LIMIT 1
        """,
        person.canonical_name,
        current_company_id,
        account_id,
    )
    if existing is not None:
        # Update title + seniority if we have new values; preserve existing
        await execute(
            """
            UPDATE public.persons
            SET current_title = COALESCE($2, current_title),
                current_seniority_score = COALESCE($3, current_seniority_score),
                current_functional_domain = COALESCE($4, current_functional_domain),
                updated_at = NOW()
            WHERE id = $1
            """,
            existing["id"],
            person.current_title,
            person.current_seniority_score,
            person.current_functional_domain,
        )
        return UUID(str(existing["id"])), False

    row = await fetchrow(
        """
        INSERT INTO public.persons
            (canonical_name, name_variants, current_company_id, current_title,
             current_seniority_score, current_functional_domain, enrichment_tier,
             account_id)
        VALUES ($1, $2::text[], $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        person.canonical_name,
        list(person.name_variants),
        current_company_id,
        person.current_title,
        person.current_seniority_score,
        person.current_functional_domain,
        1,  # tier 1 — no LinkedIn URL
        account_id,
    )
    if row is None:
        return None, False
    return UUID(str(row["id"])), True


# ─── Employment + education periods ────────────────────────────────────────


async def _upsert_employment_period(
    person_id: UUID,
    company_id: UUID,
    *,
    account_id: UUID,
    title: str,
    functional_domain: str | None = None,
    seniority_score: int | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    is_current: bool = False,
    inferred_team: str | None = None,
) -> bool:
    """SELECT-then-INSERT/UPDATE on ``(person_id, company_id, start_year)``.

    Returns True when a new row was inserted, False when an existing row was
    updated (or no-op).
    """
    existing = await fetchrow(
        """
        SELECT id FROM public.employment_periods
        WHERE person_id = $1 AND company_id = $2
          AND COALESCE(start_year, 0) = COALESCE($3, 0)
        LIMIT 1
        """,
        person_id, company_id, start_year,
    )
    if existing is not None:
        await execute(
            """
            UPDATE public.employment_periods
            SET title = COALESCE($2, title),
                functional_domain = COALESCE($3, functional_domain),
                seniority_score = COALESCE($4, seniority_score),
                end_year = COALESCE($5, end_year),
                is_current = $6,
                inferred_team = COALESCE($7, inferred_team)
            WHERE id = $1
            """,
            existing["id"], title, functional_domain, seniority_score,
            end_year, is_current, inferred_team,
        )
        return False

    await execute(
        """
        INSERT INTO public.employment_periods
            (person_id, company_id, title, functional_domain, seniority_score,
             start_year, end_year, is_current, inferred_team, account_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """,
        person_id, company_id, title, functional_domain, seniority_score,
        start_year, end_year, is_current, inferred_team, account_id,
    )
    return True


async def _upsert_person_signal_rollup(
    person_id: UUID,
    *,
    account_id: UUID,
    signal_type: str,
    structured_value: Any,
) -> bool:
    """UPSERT a rollup signal (one-row-per-person types) into person_signals.

    The migration's partial unique index ``person_signals_one_per_rollup_signal``
    handles ``(person_id, signal_type)`` for the rollup signal types. Re-runs
    UPDATE the structured_value with the latest scrape rather than appending.

    Returns True if the row was newly inserted; False on conflict UPDATE.
    """
    if not structured_value:
        return False
    row = await fetchrow(
        """
        INSERT INTO public.person_signals
            (person_id, account_id, signal_type, structured_value, source)
        VALUES ($1, $2, $3, $4::jsonb, 'apify')
        ON CONFLICT (person_id, signal_type)
            WHERE signal_type IN (
                'linkedin_skill_set', 'linkedin_certifications',
                'linkedin_languages', 'linkedin_publications',
                'linkedin_patents', 'linkedin_honors_and_awards',
                'linkedin_organizations', 'github_profile'
            )
        DO UPDATE
        SET structured_value = EXCLUDED.structured_value,
            collected_at = NOW()
        RETURNING (xmax = 0) AS inserted
        """,
        person_id, account_id, signal_type,
        json.dumps(structured_value),
    )
    if row is None:
        return False
    return bool(row["inserted"])


async def _write_person_rollup_signals(
    person_id: UUID,
    person: CanonicalPerson,
    *,
    account_id: UUID,
) -> int:
    """Persist all the bag-of-signals data from a CanonicalPerson into
    person_signals. Returns the number of rows touched (insert or update)."""
    n = 0
    rollups: tuple[tuple[str, Any], ...] = (
        ("linkedin_skill_set", {"skills": list(person.skills or [])} if person.skills else None),
        ("linkedin_certifications", {"certifications": list(person.certifications or [])} if person.certifications else None),
        ("linkedin_languages", {"languages": list(person.languages or [])} if person.languages else None),
        ("linkedin_publications", {"publications": list(person.publications or [])} if person.publications else None),
        ("linkedin_patents", {"patents": list(person.patents or [])} if person.patents else None),
        ("linkedin_honors_and_awards", {"honors_and_awards": list(person.honors_and_awards or [])} if person.honors_and_awards else None),
        ("linkedin_organizations", {"organizations": list(person.organizations or [])} if person.organizations else None),
    )
    for signal_type, value in rollups:
        if value is None:
            continue
        try:
            await _upsert_person_signal_rollup(
                person_id, account_id=account_id,
                signal_type=signal_type, structured_value=value,
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "writer: person_signal %s for %s failed: %s",
                signal_type, person.canonical_name, exc,
            )
    return n


async def _upsert_education_period(
    person_id: UUID,
    *,
    account_id: UUID,
    school_name: str,
    degree: str | None = None,
    field_of_study: str | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
) -> bool:
    """SELECT-then-INSERT/UPDATE on ``(person_id, school_canonical_name, start_year)``."""
    existing = await fetchrow(
        """
        SELECT id FROM public.education_periods
        WHERE person_id = $1 AND school_canonical_name = $2
          AND COALESCE(start_year, 0) = COALESCE($3, 0)
        LIMIT 1
        """,
        person_id, school_name, start_year,
    )
    if existing is not None:
        await execute(
            """
            UPDATE public.education_periods
            SET degree = COALESCE($2, degree),
                field_of_study = COALESCE($3, field_of_study),
                end_year = COALESCE($4, end_year),
                updated_at = NOW()
            WHERE id = $1
            """,
            existing["id"], degree, field_of_study, end_year,
        )
        return False

    await execute(
        """
        INSERT INTO public.education_periods
            (person_id, school_canonical_name, degree, field_of_study,
             start_year, end_year, account_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        person_id, school_name, degree, field_of_study, start_year, end_year, account_id,
    )
    return True


# ─── Public entry point ────────────────────────────────────────────────────


async def write_canonical_persons(
    persons: list[CanonicalPerson],
    *,
    account_id: UUID,
    primary_company_name: str | None = None,
    primary_company_employee_count: int | None = None,
) -> WriteResult:
    """Persist a list of ``CanonicalPerson`` records.

    For each person:
      1. Resolve ``current_company_name`` to a ``companies.id`` (UPSERT)
      2. Resolve every employment_period's ``company_name`` to a
         ``companies.id`` (UPSERT)
      3. UPSERT the person itself
      4. UPSERT each employment_period
      5. UPSERT each education_period

    The ``primary_company_*`` args optionally seed the company row that
    bulk-anchored this run (e.g., when crawled by company URL on Apify).
    """
    result = WriteResult()

    # Pre-seed the primary company (the one we crawled by URL) so its
    # employee_count_estimate gets refreshed even if no person rolls up
    # to it directly.
    if primary_company_name:
        canon = normalize_company(primary_company_name)
        if canon:
            try:
                cid, inserted = await _upsert_company(
                    canon,
                    account_id=account_id,
                    name_variants=[primary_company_name] if primary_company_name != canon else [],
                    employee_count_estimate=primary_company_employee_count,
                )
                if cid:
                    if inserted:
                        result.companies_inserted += 1
                    else:
                        result.companies_updated += 1
            except Exception as exc:  # noqa: BLE001 — partial-results
                result.errors.append(f"primary_company({canon}): {exc}")

    # Cache company_name → company_id within this batch — avoids re-upserting
    # the same company once per person on a 500-employee bulk.
    company_id_cache: dict[str, UUID] = {}

    async def _resolve_company(name: str | None) -> UUID | None:
        if not name:
            return None
        canon = normalize_company(name)
        if not canon:
            return None
        if canon in company_id_cache:
            return company_id_cache[canon]
        try:
            cid, inserted = await _upsert_company(
                canon,
                account_id=account_id,
                name_variants=[name] if name != canon else [],
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"company({canon}): {exc}")
            return None
        if cid is None:
            return None
        if inserted:
            result.companies_inserted += 1
        else:
            result.companies_updated += 1
        company_id_cache[canon] = cid
        return cid

    for person in persons:
        try:
            current_company_id = await _resolve_company(person.current_company_name)

            person_id, person_inserted = await _upsert_person(
                person,
                account_id=account_id,
                current_company_id=current_company_id,
            )
            if person_id is None:
                result.errors.append(f"person({person.canonical_name}): upsert returned None")
                continue
            if person_inserted:
                result.persons_inserted += 1
            else:
                result.persons_updated += 1

            # Employment periods
            for emp in person.employment_periods:
                emp_company_id = await _resolve_company(emp.get("company_name"))
                if emp_company_id is None:
                    continue
                inserted = await _upsert_employment_period(
                    person_id, emp_company_id,
                    account_id=account_id,
                    title=emp.get("title") or "",
                    functional_domain=emp.get("functional_domain"),
                    seniority_score=emp.get("seniority_score"),
                    start_year=emp.get("start_year"),
                    end_year=emp.get("end_year"),
                    is_current=bool(emp.get("is_current")),
                    inferred_team=emp.get("inferred_team"),
                )
                if inserted:
                    result.employment_periods_inserted += 1
                else:
                    result.employment_periods_updated += 1

            # Education periods
            for edu in person.education_periods:
                school = edu.get("school_name") or edu.get("school_canonical_name")
                if not school:
                    continue
                inserted = await _upsert_education_period(
                    person_id,
                    account_id=account_id,
                    school_name=school,
                    degree=edu.get("degree"),
                    field_of_study=edu.get("field_of_study"),
                    start_year=edu.get("start_year"),
                    end_year=edu.get("end_year"),
                )
                if inserted:
                    result.education_periods_inserted += 1
                else:
                    result.education_periods_updated += 1

            # Bag-of-signals rollup (skills, certs, langs, pubs, patents,
            # honors, organizations) → person_signals table
            try:
                signals_written = await _write_person_rollup_signals(
                    person_id, person, account_id=account_id,
                )
                result.person_signals_written += signals_written
            except Exception as exc:  # noqa: BLE001
                result.errors.append(
                    f"person_signals({person.canonical_name}): {exc}"
                )

        except Exception as exc:  # noqa: BLE001 — partial-results contract
            result.errors.append(f"person({person.canonical_name}): {exc}")
            logger.exception("writer: failed to persist %s", person.canonical_name)

    return result


__all__ = ["WriteResult", "write_canonical_persons"]
