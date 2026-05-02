"""Three-tier entity resolution for scraped LinkedIn employees.

Stage 2 of the customer onboarding pipeline (CUSTOMER_ONBOARDING_PLAN.md
§"Stage 2 — Team Scraping") feeds scraped employee dicts through this
module. For each scraped person we either find an existing
``persons`` row or insert a fresh one, then link it into
``account_team_members`` for the rep's account.

## Three-tier match (strict order)

    1. linkedin_url exact (case-insensitive, /-stripped)
    2. canonical_name + current_company_id
    3. INSERT a new persons row (enrichment_tier = 0)

After the persons row is resolved the account_team_members link is
upserted with ``ON CONFLICT (account_id, person_id) DO UPDATE`` so the
function is fully idempotent — calling twice with identical inputs is a
no-op the second time.

## Reuses

* ``credence.enrichment.normalizer.normalize_company`` and
  ``normalize_name`` — single source of truth for company alias /
  suffix stripping and person name canonicalization.
* ``credence.taxonomy.seniority_from_title`` and
  ``domain_from_title`` — derive seniority_score and functional_domain
  from a raw title at INSERT time.

## Constraints

* Pure async, asyncpg-native. Caller passes the connection so the work
  participates in any outer transaction the orchestrator opens.
* The whole resolve+link sequence runs inside ``conn.transaction()`` —
  if the team_member upsert fails we don't leave a dangling persons row
  for this scrape attempt.
* Type-annotated, immutable inputs.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict

from ..enrichment.normalizer import normalize_company, normalize_name
from ..taxonomy import domain_from_title, seniority_from_title

if TYPE_CHECKING:
    # The companion Wave-A3 module — imported lazily at call-time below so
    # this file still imports cleanly while team_scraper.py is in flight.
    from .team_scraper import ScrapedEmployee  # noqa: F401


logger = logging.getLogger(__name__)


# ─── Public types ───────────────────────────────────────────────────────────


class ResolvedTeamMember(BaseModel):
    """Outcome of resolving a single scraped employee.

    Returned to the orchestrator so it can update progress counters
    (``matched`` vs ``new_persons``) on the onboarding job.
    """

    model_config = ConfigDict(frozen=True)

    person_id: UUID
    was_new: bool
    account_team_member_id: UUID


# ─── URL + name normalization helpers ──────────────────────────────────────


def _normalize_linkedin_url(raw: str | None) -> str | None:
    """Strip trailing slash, lowercase, drop trailing whitespace.

    Mirrors ``normalizer._normalize_url`` but is duplicated here so we
    don't depend on a private helper. Returns None for empty inputs so
    downstream `WHERE linkedin_url = $1` queries don't accidentally
    match the wide partial-NULL bucket.
    """
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().rstrip("/")
    if not cleaned:
        return None
    return cleaned.lower()


def _normalize_company_url(raw: str | None) -> str | None:
    """Pull the slug out of a LinkedIn company URL.

    ``https://www.linkedin.com/company/foo/`` → ``foo``. Used only for
    logging / future dedupe — entity matching itself goes via
    ``current_company_id`` which is supplied by the caller.
    """
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().rstrip("/").lower()
    if not cleaned:
        return None
    marker = "/company/"
    if marker in cleaned:
        cleaned = cleaned.split(marker, 1)[1]
    # Drop any remaining path segments after the slug
    return cleaned.split("/", 1)[0] or None


def _canonical_name_from_raw(raw: Any) -> str | None:
    """Derive the canonical "First Last" form from a scraped name.

    Reuses ``normalizer.normalize_name`` (strips prefixes, suffixes,
    middle initials). Falls back to a whitespace-collapsed version of
    the raw name when normalize_name rejects it (single token, etc.) so
    we never silently drop a scraped employee just because their name
    parses oddly.
    """
    if not isinstance(raw, str):
        return None
    raw_trimmed = " ".join(raw.split())
    if not raw_trimmed:
        return None
    first, last = normalize_name(raw_trimmed)
    if first and last:
        return f"{first} {last}"
    return raw_trimmed


def _scraped_attr(employee: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from either a dataclass/BaseModel or a plain dict.

    The ScrapedEmployee shape is owned by Wave A3; we don't want to
    couple to either form prematurely. Use this everywhere we touch the
    input.
    """
    if isinstance(employee, dict):
        return employee.get(name, default)
    return getattr(employee, name, default)


# ─── Tier 1 + 2 lookups ────────────────────────────────────────────────────


_PERSON_BY_LINKEDIN_SQL = """
SELECT id, current_title, headline, current_company_id, current_seniority_score,
       current_functional_domain
FROM public.persons
WHERE LOWER(linkedin_url) = $1
LIMIT 1
"""


_PERSON_BY_NAME_AND_COMPANY_SQL = """
SELECT id, linkedin_url, current_title, headline, current_seniority_score,
       current_functional_domain
FROM public.persons
WHERE canonical_name = $1
  AND current_company_id = $2
LIMIT 1
"""


_PERSON_UPDATE_SQL = """
UPDATE public.persons
SET current_title = COALESCE($2, current_title),
    headline = COALESCE($3, headline),
    linkedin_url = COALESCE(linkedin_url, $4),
    current_company_id = COALESCE(current_company_id, $5),
    current_seniority_score = COALESCE(current_seniority_score, $6),
    current_functional_domain = COALESCE(current_functional_domain, $7),
    updated_at = NOW()
WHERE id = $1
"""


_PERSON_INSERT_SQL = """
INSERT INTO public.persons
    (canonical_name, name_variants, linkedin_url, headline, current_title,
     current_company_id, current_seniority_score, current_functional_domain,
     enrichment_tier, account_id)
VALUES ($1, $2::text[], $3, $4, $5, $6, $7, $8, 0, $9)
RETURNING id
"""


_TEAM_MEMBER_UPSERT_SQL = """
INSERT INTO public.account_team_members
    (account_id, person_id, linkedin_url, role, scrape_status, scraped_at)
VALUES ($1, $2, $3, 'member', 'done', NOW())
ON CONFLICT (account_id, person_id) DO UPDATE
SET scrape_status = 'done',
    scraped_at = NOW(),
    linkedin_url = COALESCE(EXCLUDED.linkedin_url, public.account_team_members.linkedin_url)
RETURNING id
"""


# ─── Public entrypoint ─────────────────────────────────────────────────────


async def resolve_or_insert_team_member(
    raw_employee: Any,
    account_id: UUID,
    company_id: UUID,
    conn: asyncpg.Connection,
) -> ResolvedTeamMember:
    """Resolve a scraped employee → existing or new persons row.

    Args:
        raw_employee: A ScrapedEmployee dataclass / BaseModel (Wave A3)
            or compatible dict. Read via ``_scraped_attr`` so either
            shape works.
        account_id: The customer's account UUID.
        company_id: The canonical companies row for this employee. The
            orchestrator resolves this once per scrape batch from the
            company URL → canonical company alias.
        conn: An asyncpg connection. The whole sequence runs inside
            ``conn.transaction()`` so a failure in the link-table
            upsert rolls back any persons mutation.

    Returns:
        A ``ResolvedTeamMember`` describing what happened — used by the
        orchestrator to advance ``onboarding_jobs.progress``.
    """
    raw_name = _scraped_attr(raw_employee, "name") or _scraped_attr(
        raw_employee, "full_name"
    )
    canonical_name = _canonical_name_from_raw(raw_name)
    raw_linkedin = _scraped_attr(raw_employee, "linkedin_url")
    linkedin_url_norm = _normalize_linkedin_url(raw_linkedin)
    title = _scraped_attr(raw_employee, "title") or _scraped_attr(
        raw_employee, "current_title"
    )
    headline = _scraped_attr(raw_employee, "headline")

    seniority_score = seniority_from_title(title) if isinstance(title, str) else None
    functional_domain = (
        domain_from_title(title) if isinstance(title, str) else None
    )

    # Optional logging hook — useful when the company slug embedded in
    # the scrape doesn't match the canonical_name we already resolved.
    raw_company_url = _scraped_attr(raw_employee, "company_url")
    company_slug = _normalize_company_url(raw_company_url)
    if company_slug:
        logger.debug(
            "entity_resolver: scraped employee %s from company slug %s",
            canonical_name,
            company_slug,
        )

    # Make the company normalization side-effect visible in logs even
    # when we don't act on it — the canonical company name is owned by
    # the orchestrator, but we may want to confirm a mismatch.
    raw_company_name = _scraped_attr(raw_employee, "company_name")
    if isinstance(raw_company_name, str):
        canon = normalize_company(raw_company_name)
        if canon:
            logger.debug(
                "entity_resolver: %s reports company %s (canonical %s)",
                canonical_name,
                raw_company_name,
                canon,
            )

    if canonical_name is None:
        # We allow this to surface as a value error so the orchestrator
        # can record it in onboarding_jobs.progress["errors"] without
        # crashing the entire batch.
        raise ValueError("scraped employee is missing a usable name")

    async with conn.transaction():
        person_id, was_new = await _resolve_person(
            conn,
            canonical_name=canonical_name,
            linkedin_url=linkedin_url_norm,
            title=title if isinstance(title, str) else None,
            headline=headline if isinstance(headline, str) else None,
            company_id=company_id,
            account_id=account_id,
            seniority_score=seniority_score,
            functional_domain=functional_domain,
        )
        atm_id = await _upsert_team_member(
            conn,
            account_id=account_id,
            person_id=person_id,
            linkedin_url=raw_linkedin if isinstance(raw_linkedin, str) else None,
        )

    return ResolvedTeamMember(
        person_id=person_id,
        was_new=was_new,
        account_team_member_id=atm_id,
    )


# ─── Internals ─────────────────────────────────────────────────────────────


async def _resolve_person(
    conn: asyncpg.Connection,
    *,
    canonical_name: str,
    linkedin_url: str | None,
    title: str | None,
    headline: str | None,
    company_id: UUID,
    account_id: UUID,
    seniority_score: int | None,
    functional_domain: str | None,
) -> tuple[UUID, bool]:
    """Run the three-tier match. Returns ``(person_id, was_new)``."""
    # ── Tier 1: linkedin_url exact ─────────────────────────────────────
    if linkedin_url:
        row = await conn.fetchrow(_PERSON_BY_LINKEDIN_SQL, linkedin_url)
        if row is not None:
            person_id = _to_uuid(row["id"])
            await _update_person_fields(
                conn,
                person_id=person_id,
                title=title,
                headline=headline,
                linkedin_url=linkedin_url,
                company_id=company_id,
                seniority_score=seniority_score,
                functional_domain=functional_domain,
            )
            return person_id, False

    # ── Tier 2: canonical_name + current_company_id ────────────────────
    row = await conn.fetchrow(
        _PERSON_BY_NAME_AND_COMPANY_SQL, canonical_name, company_id
    )
    if row is not None:
        person_id = _to_uuid(row["id"])
        await _update_person_fields(
            conn,
            person_id=person_id,
            title=title,
            headline=headline,
            linkedin_url=linkedin_url,
            company_id=company_id,
            seniority_score=seniority_score,
            functional_domain=functional_domain,
        )
        return person_id, False

    # ── Tier 3: INSERT new ────────────────────────────────────────────
    row = await conn.fetchrow(
        _PERSON_INSERT_SQL,
        canonical_name,
        [canonical_name],
        linkedin_url,
        headline,
        title,
        company_id,
        seniority_score,
        functional_domain,
        account_id,
    )
    if row is None:
        # Should never happen — INSERT ... RETURNING always yields a row
        # on success. Defensive: fail loud so the orchestrator records it.
        raise RuntimeError(
            "entity_resolver: persons INSERT returned no row for "
            f"{canonical_name}"
        )
    return _to_uuid(row["id"]), True


async def _update_person_fields(
    conn: asyncpg.Connection,
    *,
    person_id: UUID,
    title: str | None,
    headline: str | None,
    linkedin_url: str | None,
    company_id: UUID | None,
    seniority_score: int | None,
    functional_domain: str | None,
) -> None:
    """COALESCE-style UPDATE — never overwrites existing values."""
    await conn.execute(
        _PERSON_UPDATE_SQL,
        person_id,
        title,
        headline,
        linkedin_url,
        company_id,
        seniority_score,
        functional_domain,
    )


async def _upsert_team_member(
    conn: asyncpg.Connection,
    *,
    account_id: UUID,
    person_id: UUID,
    linkedin_url: str | None,
) -> UUID:
    """ON CONFLICT (account_id, person_id) → UPDATE scrape_status='done'."""
    row = await conn.fetchrow(
        _TEAM_MEMBER_UPSERT_SQL,
        account_id,
        person_id,
        linkedin_url,
    )
    if row is None:
        raise RuntimeError(
            "entity_resolver: account_team_members upsert returned no row"
        )
    return _to_uuid(row["id"])


def _to_uuid(value: Any) -> UUID:
    """Coerce DB-returned ids (UUID or str) into UUID objects."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


__all__ = ["ResolvedTeamMember", "resolve_or_insert_team_member"]
