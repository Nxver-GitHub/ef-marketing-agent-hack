"""POST /enrich/{prospect_id} — Contract 8 implementation (Wave 5).

Per-prospect multi-vendor enrichment. Apollo is the v1 vendor; PDL,
Parallel, Firecrawl land in subsequent phases as keys arrive.

References:
- CONTRACTS.md Contract 8 (request/response shape, fields-by-vendor,
  persistence rules, cost-cap invariant)
- CLAUDE.md L770-834 (analogous to /signals/discover-connections)
- server/credence/enrichment/apollo.py (vendor module)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .db import execute, fetch, fetchrow
from .enrichment.apollo import (
    ProspectRef,
)
from .orgchart import hierarchy as orgchart_hierarchy
from .enrichment.apollo import (
    enrich as apollo_enrich,
)
from .enrichment.budget import BudgetExceeded, assert_budget
from .enrichment.firecrawl import (
    ScrapeRequest,
)
from .enrichment.firecrawl import (
    scrape as firecrawl_scrape,
)
from .enrichment.pdl import (
    enrich as pdl_enrich,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enrich", tags=["enrich"])


# ── Contract 8 request / response models ────────────────────────────────────


VendorName = Literal["apollo", "pdl", "parallel", "firecrawl"]


class EnrichRequest(BaseModel):
    vendors: list[VendorName] | None = None
    max_cost_cents: int = Field(default=100, ge=0, le=10000)
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    refresh: bool = False


class EnrichmentRecord(BaseModel):
    vendor: VendorName
    fields: dict[str, Any]
    confidence: float = Field(ge=0.0, le=1.0)
    cost_cents: int = Field(ge=0)
    fetched_at: datetime
    cached: bool = False


class EnrichResponse(BaseModel):
    prospect_id: UUID
    records: list[EnrichmentRecord]
    vendors_attempted: list[str]
    vendors_failed: list[str]
    vendors_skipped_for_cost: list[str]
    total_cost_cents: int
    elapsed_ms: int


# ── Vendor registry ─────────────────────────────────────────────────────────
# Each enrichment vendor's `enrich()` is shimmed into a uniform shape so the
# route can fan out without per-vendor branching. PDL/Parallel/Firecrawl
# land here as they ship.


async def _apollo_runner(
    prospect: ProspectRef, max_cost_cents: int, client: httpx.AsyncClient | None = None
) -> tuple[dict[str, Any], int, float] | None:
    result = await apollo_enrich(
        prospect, client=client, max_cost_cents=max_cost_cents
    )
    if result is None:
        return None
    return dict(result.fields), result.cost_cents, result.confidence


async def _firecrawl_runner(
    prospect: ProspectRef, max_cost_cents: int, client: httpx.AsyncClient | None = None
) -> tuple[dict[str, Any], int, float] | None:
    """Firecrawl URL-scraper. v1: target the prospect's LinkedIn URL.

    Firecrawl is URL-driven, so we need a target page. v1 uses the prospect's
    `linkedin_url` if present and skips otherwise. A future iteration can
    add smarter URL resolution (company About page, GitHub profile, etc.)
    once we have a way to discover those reliably from the prospect record.

    Note: LinkedIn often serves login walls to non-authenticated scrapers;
    expect a non-trivial null-rate from this runner until the URL-resolution
    upgrade lands. Returning None is the right behavior — partial-results
    semantics per Contract 8.
    """
    if not prospect.linkedin_url:
        return None
    request = ScrapeRequest(
        url=prospect.linkedin_url,
        person_id=prospect.person_id,
    )
    result = await firecrawl_scrape(
        request, client=client, max_cost_cents=max_cost_cents
    )
    if result is None:
        return None
    return dict(result.fields), result.cost_cents, result.confidence


async def _pdl_runner(
    prospect: ProspectRef, max_cost_cents: int, client: httpx.AsyncClient | None = None
) -> tuple[dict[str, Any], int, float] | None:
    """People Data Labs `/v5/person/enrich` runner.

    Returns structured employment_periods, skills, linkedin_url, pdl_person_id.
    Skips silently when no `PDL_API_KEY` is configured (env-driven by default).
    """
    result = await pdl_enrich(
        prospect, client=client, max_cost_cents=max_cost_cents
    )
    if result is None:
        return None
    return dict(result.fields), result.cost_cents, result.confidence


_VENDOR_RUNNERS: dict[VendorName, Any] = {
    "apollo": _apollo_runner,
    "pdl": _pdl_runner,
    # "parallel": _parallel_runner, — Phase 3 (SunnyRidge — wired in signals.py per Contract 1)
    "firecrawl": _firecrawl_runner,
}


# ── Cache freshness ─────────────────────────────────────────────────────────


_CACHE_FRESH_HOURS = 24


async def _is_cache_fresh(prospect_id: UUID) -> bool:
    """Return True iff the prospect's last_enriched_at < 24h ago."""
    row = await fetchrow(
        "SELECT last_enriched_at FROM prospects WHERE id = $1", prospect_id
    )
    if row is None or row["last_enriched_at"] is None:
        return False
    last = row["last_enriched_at"]
    if not isinstance(last, datetime):
        return False
    age_hours = (datetime.now(tz=last.tzinfo) - last).total_seconds() / 3600
    return age_hours < _CACHE_FRESH_HOURS


async def _load_prospect_ref(prospect_id: UUID) -> ProspectRef | None:
    """Resolve a prospect id to the minimal identifier set the vendor needs."""
    row = await fetchrow(
        """
        SELECT id, name, company, linkedin_url
        FROM prospects
        WHERE id = $1
        """,
        prospect_id,
    )
    if row is None:
        return None
    return ProspectRef(
        person_id=str(row["id"]),
        canonical_name=str(row["name"] or ""),
        organization_name=str(row["company"]) if row.get("company") else None,
        linkedin_url=str(row["linkedin_url"]) if row.get("linkedin_url") else None,
    )


# ── Persistence ─────────────────────────────────────────────────────────────


async def _write_cost_log(
    prospect_id: UUID,
    account_id: UUID,
    vendor: str,
    *,
    cost_cents: int,
    cache_hit: bool,
    success: bool,
    error_message: str | None = None,
    endpoint: str | None = None,
) -> None:
    """Insert one row into enrichment_cost_log per Contract 8 invariant —
    every vendor invocation logs, even cache hits."""
    err = error_message
    if err and len(err) > 1024:
        err = err[:1021] + "..."
    await execute(
        """
        INSERT INTO enrichment_cost_log
            (prospect_id, account_id, vendor, endpoint, cost_cents,
             cache_hit, success, error_message, called_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
        """,
        prospect_id,
        account_id,
        vendor,
        endpoint,
        cost_cents,
        cache_hit,
        success,
        err,
    )


async def _resolve_prospect_person_id(prospect_id: UUID) -> UUID | None:
    """Resolve a prospect to its canonical `persons.id` via employment_periods.

    Phase A.6: Track F's `persons` backfill is sparse — many prospects
    don't yet have a persons row. Returns None when no match (route layer
    silently skips the explicit-edge write in that case).
    """
    row = await fetchrow(
        """
        SELECT p.id AS person_id
        FROM persons p
        JOIN employment_periods ep ON ep.person_id = p.id
        WHERE ep.prospect_id = $1
        LIMIT 1
        """,
        prospect_id,
    )
    if row is None:
        return None
    pid = row.get("person_id") if hasattr(row, "get") else row["person_id"]
    if pid is None:
        return None
    return UUID(str(pid))


async def _resolve_manager_person_id(
    name: str | None, linkedin_url: str | None
) -> UUID | None:
    """Best-effort lookup of a manager name/url → `persons.id`.

    Phase A.6: matches on canonical_name (case-insensitive) OR linkedin_url.
    Returns None when no match. Caller handles the no-match case silently
    per Contract 8 partial-results semantics (a manager we can't resolve
    isn't a hard error — we just don't write the explicit edge yet).
    """
    if not name and not linkedin_url:
        return None
    rows = await fetch(
        """
        SELECT id
        FROM persons
        WHERE ($1::text IS NOT NULL AND canonical_name ILIKE $1)
           OR ($2::text IS NOT NULL AND linkedin_url = $2)
        LIMIT 1
        """,
        name,
        linkedin_url,
    )
    if not rows:
        return None
    pid = rows[0].get("id") if hasattr(rows[0], "get") else rows[0]["id"]
    return UUID(str(pid)) if pid is not None else None


async def _maybe_write_reports_to_edge(
    prospect_id: UUID,
    account_id: UUID,
    fields: dict[str, Any],
) -> None:
    """Phase A.6: if a vendor surfaced reports_to, write an explicit
    org_reporting_edges row.

    Looks at `reports_to_name` + (optional) `reports_to_linkedin_url` from
    the vendor `fields` dict, resolves both the prospect and the manager
    to `persons.id`, and calls `hierarchy.ingest_explicit_edge`.

    Failures (no persons row, no manager match, DB error) are logged at
    warning and swallowed — Contract 8 partial-results semantics. The
    rest of the enrichment must not be derailed by an explicit-edge miss.

    Confidence: 0.92. LinkedIn `reports_to` is a strong signal but titles
    drift without LinkedIn updates, so we don't claim 1.0.
    """
    name = fields.get("reports_to_name")
    linkedin_url = fields.get("reports_to_linkedin_url")
    if not name and not linkedin_url:
        return
    try:
        report_person_id = await _resolve_prospect_person_id(prospect_id)
        if report_person_id is None:
            # Sparse persons coverage from Track F backfill — skip silently.
            return
        manager_person_id = await _resolve_manager_person_id(name, linkedin_url)
        if manager_person_id is None:
            return
        if manager_person_id == report_person_id:
            return  # self-edge — guard against bad data
        await orgchart_hierarchy.ingest_explicit_edge(
            manager_id=manager_person_id,
            report_id=report_person_id,
            account_id=account_id,
            signal_type="linkedin_reports_to",
            confidence=0.92,
        )
    except Exception as exc:  # noqa: BLE001 — Contract 8 partial-results
        logger.warning(
            "enrich: ingest_explicit_edge failed for prospect %s: %s",
            prospect_id,
            exc,
        )


async def _persist_apollo_fields(
    prospect_id: UUID, fields: dict[str, Any]
) -> None:
    """Write Apollo-derived fields back to prospects."""
    await execute(
        """
        UPDATE prospects
        SET email             = COALESCE($2, email),
            email_status      = COALESCE($3, email_status),
            current_title     = COALESCE($4, current_title),
            last_enriched_at  = NOW(),
            updated_at        = NOW()
        WHERE id = $1
        """,
        prospect_id,
        fields.get("email"),
        fields.get("email_status"),
        fields.get("current_title"),
    )


async def _persist_pdl_fields(
    prospect_id: UUID, fields: dict[str, Any]
) -> None:
    """Write PDL-derived fields back to prospects.

    Lands the structured `employment_periods` array, `skills`, and
    `pdl_person_id` on the prospect row per the JSONB-on-prospects
    target chosen for v1 (see `20260430_v3_pdl_persistence.sql`).
    `linkedin_url` is filled only when the prospect didn't have one
    — PDL's match can supply a URL we didn't have before.
    """
    employment_periods = fields.get("employment_periods") or []
    skills = fields.get("skills") or []
    pdl_person_id = fields.get("pdl_person_id") or None
    linkedin_url = fields.get("linkedin_url") or None
    await execute(
        """
        UPDATE prospects
        SET employment_periods = CASE
                WHEN jsonb_array_length($2::jsonb) > 0 THEN $2::jsonb
                ELSE employment_periods
            END,
            skills             = CASE
                WHEN array_length($3::text[], 1) IS NOT NULL THEN $3::text[]
                ELSE skills
            END,
            pdl_person_id      = COALESCE($4, pdl_person_id),
            linkedin_url       = COALESCE(linkedin_url, $5),
            last_enriched_at   = NOW(),
            updated_at         = NOW()
        WHERE id = $1
        """,
        prospect_id,
        json.dumps(employment_periods),
        list(skills),
        pdl_person_id,
        linkedin_url,
    )


# ── Route ────────────────────────────────────────────────────────────────────


@router.post(
    "/{prospect_id}",
    response_model=EnrichResponse,
    summary="Enrich a single prospect via configured vendors (Apollo first; PDL/Parallel/Firecrawl as keys arrive).",
)
async def enrich_prospect(
    prospect_id: UUID, req: EnrichRequest = EnrichRequest()  # noqa: B008 — pydantic default
) -> EnrichResponse:
    """Per Contract 8 — fan out to vendors in parallel, persist, return."""
    started = time.monotonic()

    # 1. Prospect resolution
    person = await _load_prospect_ref(prospect_id)
    if person is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "prospect_not_found", "value": str(prospect_id)},
        )

    # account_id is required for the cost log. Look it up alongside the prospect.
    acct_row = await fetchrow(
        "SELECT account_id FROM prospects WHERE id = $1", prospect_id
    )
    account_id_val = acct_row["account_id"] if acct_row else None
    if account_id_val is None:
        # All v2 prospects backfilled to default tenant; if missing, hard fail.
        raise HTTPException(
            status_code=500,
            detail={"error": "prospect_missing_account_id", "id": str(prospect_id)},
        )
    account_id = UUID(str(account_id_val))

    # 2. Vendor selection — currently apollo-only since other keys not all present
    requested = list(req.vendors or list(_VENDOR_RUNNERS.keys()))
    vendors_attempted: list[str] = []
    for v in requested:
        if v in _VENDOR_RUNNERS:
            vendors_attempted.append(v)
        else:
            logger.info("enrich: vendor %s not yet wired — skipping", v)

    if not vendors_attempted:
        return EnrichResponse(
            prospect_id=prospect_id,
            records=[],
            vendors_attempted=[],
            vendors_failed=[],
            vendors_skipped_for_cost=[],
            total_cost_cents=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    # 3. Cache freshness — short-circuit if not refresh and prospect is fresh
    if not req.refresh and await _is_cache_fresh(prospect_id):
        # Return whatever's already on the prospect row as a "cached" record
        row = await fetchrow(
            """
            SELECT email, email_status, current_title, last_enriched_at
            FROM prospects WHERE id = $1
            """,
            prospect_id,
        )
        cached_fields: dict[str, Any] = (
            {
                "email": row["email"],
                "email_status": row["email_status"],
                "current_title": row["current_title"],
            }
            if row is not None
            else {}
        )
        for v in vendors_attempted:
            await _write_cost_log(
                prospect_id, account_id, v, cost_cents=0, cache_hit=True, success=True
            )
        return EnrichResponse(
            prospect_id=prospect_id,
            records=[
                EnrichmentRecord(
                    vendor="apollo",  # only vendor with a real cache row today
                    fields=cached_fields,
                    confidence=0.95,
                    cost_cents=0,
                    fetched_at=row["last_enriched_at"] if row is not None else datetime.now(UTC),
                    cached=True,
                )
            ]
            if "apollo" in vendors_attempted
            else [],
            vendors_attempted=vendors_attempted,
            vendors_failed=[],
            vendors_skipped_for_cost=[],
            total_cost_cents=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    # 3.5 Per-tenant budget pre-flight (Wave 6 M4) — exclude any vendors
    #     whose projected call cost would push the tenant past their
    #     monthly cap in `account_settings.<vendor>_monthly_cents`.
    #     `req.max_cost_cents` is the upper bound the caller is willing to
    #     pay per vendor; we treat it as the projected charge for the
    #     pre-flight check (worst case). Cap=0 means unlimited (default).
    #     Skipped vendors get a zero-cost cost-log row with the budget
    #     reason so the audit trail captures every attempt, even denied.
    vendors_skipped_for_cost: list[str] = []
    in_budget: list[VendorName] = []
    for v in vendors_attempted:
        try:
            await assert_budget(account_id, v, req.max_cost_cents)
        except BudgetExceeded as exc:
            logger.info("enrich: vendor %s skipped (budget): %s", v, exc)
            vendors_skipped_for_cost.append(v)
            await _write_cost_log(
                prospect_id, account_id, v,
                cost_cents=0, cache_hit=False, success=False,
                error_message=(
                    f"budget_exceeded: spent={exc.spent_cents}c "
                    f"cap={exc.cap_cents}c projected={exc.projected_cents}c"
                ),
            )
            continue
        in_budget.append(v)

    # If every vendor got skipped for cost, return early — no fanout.
    if not in_budget:
        return EnrichResponse(
            prospect_id=prospect_id,
            records=[],
            vendors_attempted=vendors_attempted,
            vendors_failed=[],
            vendors_skipped_for_cost=vendors_skipped_for_cost,
            total_cost_cents=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    vendors_attempted = in_budget

    # 4. Run vendors in parallel under timeout. Each runner is responsible
    #    for its own cost-cap pre-flight (per-vendor decision).
    async def run_one(vendor: VendorName) -> tuple[VendorName, Any]:
        runner = _VENDOR_RUNNERS[vendor]
        try:
            return vendor, await runner(person, req.max_cost_cents)
        except Exception as exc:
            logger.warning("enrich: vendor %s raised: %s", vendor, exc, exc_info=True)
            return vendor, exc

    tasks = [run_one(v) for v in vendors_attempted]

    try:
        outcomes = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=False),
            timeout=req.timeout_seconds,
        )
    except TimeoutError:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        for v in vendors_attempted:
            await _write_cost_log(
                prospect_id, account_id, v,
                cost_cents=0, cache_hit=False, success=False,
                error_message="endpoint timeout",
            )
        return EnrichResponse(
            prospect_id=prospect_id,
            records=[],
            vendors_attempted=vendors_attempted,
            vendors_failed=vendors_attempted,
            vendors_skipped_for_cost=vendors_skipped_for_cost,
            total_cost_cents=0,
            elapsed_ms=elapsed_ms,
        )

    # 5. Aggregate, persist, log
    records: list[EnrichmentRecord] = []
    vendors_failed: list[str] = []
    total_cost = 0

    for vendor, result in outcomes:
        if isinstance(result, BaseException):
            vendors_failed.append(vendor)
            await _write_cost_log(
                prospect_id, account_id, vendor,
                cost_cents=0, cache_hit=False, success=False,
                error_message=str(result),
            )
            continue
        if result is None:
            # Vendor declined (no match, cost cap, missing key) — log as a
            # zero-cost zero-cache success so the audit shows we tried.
            vendors_failed.append(vendor)
            await _write_cost_log(
                prospect_id, account_id, vendor,
                cost_cents=0, cache_hit=False, success=False,
                error_message="vendor_declined_or_no_match",
            )
            continue

        fields, cost_cents, confidence = result
        total_cost += cost_cents
        records.append(
            EnrichmentRecord(
                vendor=vendor,
                fields=fields,
                confidence=confidence,
                cost_cents=cost_cents,
                fetched_at=datetime.now(UTC),
                cached=False,
            )
        )
        await _write_cost_log(
            prospect_id, account_id, vendor,
            cost_cents=cost_cents, cache_hit=False, success=True,
        )

        # Persist vendor-specific fields to the prospect row
        if vendor == "apollo":
            await _persist_apollo_fields(prospect_id, fields)
        elif vendor == "pdl":
            await _persist_pdl_fields(prospect_id, fields)

        # Phase A.6: write explicit org-chart edge when reports_to present.
        # Wrapped helper handles its own errors per Contract 8 partial-results.
        await _maybe_write_reports_to_edge(prospect_id, account_id, fields)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return EnrichResponse(
        prospect_id=prospect_id,
        records=records,
        vendors_attempted=vendors_attempted,
        vendors_failed=vendors_failed,
        vendors_skipped_for_cost=vendors_skipped_for_cost,
        total_cost_cents=total_cost,
        elapsed_ms=elapsed_ms,
    )
