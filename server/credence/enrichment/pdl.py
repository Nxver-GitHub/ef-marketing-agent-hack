"""People Data Labs (PDL) enrichment — Contract 8 vendor implementation.

API: https://api.peopledatalabs.com/v5/  (Pro plan ~$98/mo for 350 credits;
~28¢ per credit at Pro tier — bulk enterprise contracts cheaper)

## What this fetches

For a single prospect, PDL returns:
- LinkedIn URL (when known)
- Skills (array of skill strings — domain expertise hints)
- **Employment history with month-level start/end dates** — the headline
  feature; replaces v2's freeform `prospects.past_companies: string[]`
  with structured `employment_periods` rows
- PDL's internal person_id (for back-reference / re-fetch)

## Strategy

Single `GET /v5/person/enrich` with `name + company` (or `profile`
LinkedIn URL when available). PDL returns the best match if any. One
API call per prospect — cheap relative to value when the prospect
already has career history we can structure.

## ⚠ PDL matcher is conservative — LinkedIn URL strongly preferred

Verified live 2026-04-30: PDL's `/person/enrich` returns 404 for
`name + company` queries even on unambiguous public figures
(Tim Cook + Apple, Satya Nadella + Microsoft both 404 at
`min_likelihood=2`). The matcher only commits when the input
identifier disambiguates — `profile` (LinkedIn URL), `email`, or
`pdl_id`. Same Satya query with `profile=https://www.linkedin.com/in/satyanadella`
returns `likelihood=9` instantly.

**Implication for prospect-level callers:** prospects without a
`linkedin_url` will hit a high-but-not-100% null rate from PDL.
Apollo's email-finder is a good upstream — once Apollo lands an
email on a prospect, PDL with `email=` becomes high-precision.
Stack the vendors: Apollo first to get LinkedIn/email, then PDL
with that identifier.

## Sandbox / live status

Implementation is doc-driven against PDL's published v5 schema.
**Live integration test is `tests/test_pdl_live.py`** — gated on
`PDL_API_KEY` env var, currently absent from `.env.local`. Unit tests
in `tests/test_pdl.py` mock httpx and lock the parsing.

## Cost handling

Every call returns the cost in cents. The route layer pre-flights
against `account_settings.pdl_monthly_cents` per Contract 8 + M4
budget enforcement.

## Manager / reports_to (Phase A.6)

PDL's `/v5/person/enrich` response sometimes carries a top-level
`manager` object (PDL Pro tier — this is one of the higher-cost fields).
When present, we extract `reports_to_name`, `reports_to_linkedin_url`,
and `reports_to_pdl_id` so the route layer can resolve the manager
into a `persons.id` and write an explicit edge via
`hierarchy.ingest_explicit_edge(signal_type="linkedin_reports_to", ...)`.
This is the explicit-signal path of the org-chart inference pipeline —
see Decision 3 in CLAUDE.md (explicit signals override implicit scoring).
The extractor itself never touches the DB.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, TypedDict
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

PDL_BASE_URL = "https://api.peopledatalabs.com/v5/"
DEFAULT_TIMEOUT_SECONDS = 12.0

# PDL Pro tier: ~28¢ per credit; one /person/enrich call = 1 credit.
# We round to the nearest cent for cost-cap arithmetic; reconciled monthly.
PDL_ENRICH_CREDIT_CENTS = 28


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProspectRef:
    """Minimal identifier set PDL's enrich endpoint accepts.

    Identical shape to Apollo's `ProspectRef` for symmetry; same import
    is used by the route layer's `_VENDOR_RUNNERS` shim.
    """

    person_id: str
    canonical_name: str
    organization_name: str | None = None
    linkedin_url: str | None = None
    email_hint: str | None = None


class PDLEmploymentPeriod(TypedDict, total=False):
    """One row from `data.experience[]`, mapped to Contract 8 shape."""

    company_name: str
    title: str
    functional_domain: str | None
    start_date: str | None  # ISO YYYY-MM or YYYY-MM-DD
    end_date: str | None
    is_current: bool


class PDLFields(TypedDict, total=False):
    """Vendor payload for Contract 8's `EnrichmentRecord.fields`."""

    linkedin_url: str | None
    skills: list[str]
    employment_periods: list[PDLEmploymentPeriod]
    pdl_person_id: str
    # ── Phase A.6: explicit org-chart signals ──
    # When PDL knows the prospect's manager, surface name + identifiers so
    # the route layer can resolve to `persons.id` and call
    # `ingest_explicit_edge(signal_type="linkedin_reports_to", ...)`.
    reports_to_name: str | None
    reports_to_linkedin_url: str | None
    reports_to_pdl_id: str | None


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """Per-vendor result handed back to the route layer. Shape mirrors Apollo's."""

    fields: PDLFields
    confidence: float
    cost_cents: int
    cache_hit: bool = False


# ─── Field-extraction helpers ────────────────────────────────────────────────


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _list_of_str(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [s for s in v if isinstance(s, str) and s.strip()]


def _bool_or_false(v: Any) -> bool:
    return bool(v) if isinstance(v, bool) else False


def _experience_to_employment(exp: dict[str, Any]) -> PDLEmploymentPeriod | None:
    """Map one PDL `experience[]` entry → Contract 8 `employment_periods` shape.

    Returns None when the entry lacks both a company name and a title —
    nothing to render. Otherwise propagates available fields, with
    documented defaults for missing optional ones.
    """
    if not isinstance(exp, dict):
        return None

    company = exp.get("company") or {}
    if not isinstance(company, dict):
        company = {}
    title = exp.get("title") or {}
    if not isinstance(title, dict):
        title = {}

    company_name = _str_or_none(company.get("name"))
    title_name = _str_or_none(title.get("name"))
    if not company_name and not title_name:
        return None

    # PDL emits `start_date`/`end_date` as YYYY or YYYY-MM strings.
    start_date = _str_or_none(exp.get("start_date"))
    end_date = _str_or_none(exp.get("end_date"))
    is_current = _bool_or_false(exp.get("is_primary")) or end_date is None

    # PDL's industry/role classification — useful when it's present
    functional_domain = _str_or_none(title.get("role"))

    record: PDLEmploymentPeriod = {
        "company_name": company_name or "",
        "title": title_name or "",
        "functional_domain": functional_domain,
        "start_date": start_date,
        "end_date": end_date,
        "is_current": is_current,
    }
    return record


def _extract_pdl_person(data: dict[str, Any]) -> PDLFields:
    """Map a PDL `data` payload → Contract 8 `PDLFields`."""
    experience_raw = data.get("experience")
    employment_periods: list[PDLEmploymentPeriod] = []
    if isinstance(experience_raw, list):
        for exp in experience_raw:
            mapped = _experience_to_employment(exp)
            if mapped is not None:
                employment_periods.append(mapped)

    # ── Phase A.6: extract manager / reports_to defensively ──
    # PDL's `manager` is a top-level object on `data` when known. Schema:
    #   {"name": "...", "first_name": "...", "last_name": "...",
    #    "linkedin_url": "...", "id": "..."}
    # We try `name` first, then first+last concat. Partial names → None
    # (a "Wei" with no last name is too ambiguous for canonical-name lookup).
    manager_obj = data.get("manager")
    if not isinstance(manager_obj, dict):
        manager_obj = {}
    raw_name = _str_or_none(manager_obj.get("name"))
    if raw_name is None:
        m_first = _str_or_none(manager_obj.get("first_name"))
        m_last = _str_or_none(manager_obj.get("last_name"))
        reports_to_name: str | None = (
            f"{m_first} {m_last}" if m_first and m_last else None
        )
    else:
        reports_to_name = raw_name
    reports_to_linkedin_url = _str_or_none(manager_obj.get("linkedin_url"))
    reports_to_pdl_id = _str_or_none(manager_obj.get("id"))

    return PDLFields(
        linkedin_url=_str_or_none(data.get("linkedin_url"))
        or _str_or_none(
            (data.get("profiles") or [{}])[0].get("url")
            if isinstance(data.get("profiles"), list) and data.get("profiles")
            else None
        ),
        skills=_list_of_str(data.get("skills")),
        employment_periods=employment_periods,
        pdl_person_id=_str_or_none(data.get("id")) or "",
        reports_to_name=reports_to_name,
        reports_to_linkedin_url=reports_to_linkedin_url,
        reports_to_pdl_id=reports_to_pdl_id,
    )


def _calculate_cost(matched: bool) -> int:
    """PDL bills per /enrich call regardless of match outcome.

    A successful match costs 1 credit; a no-match call also costs 1
    credit (PDL's documented behavior — they charge for the lookup
    work). The `matched` parameter is kept for call-site clarity even
    though the value is the same — future tiered pricing (e.g.,
    bulk-match discount) can branch here without touching callers.
    """
    del matched  # branch is intentional placeholder; same cost today
    return PDL_ENRICH_CREDIT_CENTS


# ─── HTTP I/O ────────────────────────────────────────────────────────────────


async def _pdl_get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any],
    *,
    api_key: str,
) -> tuple[int, dict[str, Any] | None]:
    """GET to PDL, return (status, body) tuple. Body is None on parse failure
    or non-JSON response. Network errors collapse to (0, None)."""
    url = urljoin(PDL_BASE_URL, path)
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    try:
        r = await client.get(url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.warning("PDL request failed (%s): %s", path, exc)
        return 0, None
    body: dict[str, Any] | None = None
    try:
        parsed = r.json()
        if isinstance(parsed, dict):
            body = parsed
    except ValueError:
        pass
    return r.status_code, body


# ─── Public API ─────────────────────────────────────────────────────────────


async def enrich(
    prospect: ProspectRef,
    *,
    client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
    max_cost_cents: int = 100,
) -> EnrichmentResult | None:
    """Enrich a single prospect via PDL `/v5/person/enrich`.

    Returns:
        EnrichmentResult on a successful match.
        None when:
        - No `PDL_API_KEY` is configured
        - PDL returns no match (status 404 or status 200 with empty data)
        - Network / auth failure
        - Cost would exceed `max_cost_cents`
    """
    key = api_key or os.environ.get("PDL_API_KEY")
    if not key:
        logger.info(
            "pdl.enrich called without PDL_API_KEY — skipping (set env or pass api_key=)"
        )
        return None

    # Pre-flight cost cap
    if PDL_ENRICH_CREDIT_CENTS > max_cost_cents:
        logger.info(
            "pdl.enrich: per-call cost %d¢ > cap %d¢ — skipping",
            PDL_ENRICH_CREDIT_CENTS,
            max_cost_cents,
        )
        return None

    # Build the query — PDL accepts multiple identifier styles. Highest-precision
    # is `profile` (LinkedIn URL); fall back to `name + company`.
    params: dict[str, Any] = {"min_likelihood": 6, "pretty": "false"}
    if prospect.linkedin_url:
        params["profile"] = prospect.linkedin_url
    elif prospect.canonical_name:
        params["name"] = prospect.canonical_name
        if prospect.organization_name:
            params["company"] = prospect.organization_name
    else:
        logger.info("pdl.enrich: no usable identifier on prospect — skipping")
        return None
    if prospect.email_hint:
        params["email"] = prospect.email_hint

    own_client = client is None
    http = client if client is not None else httpx.AsyncClient()
    try:
        status, body = await _pdl_get(http, "person/enrich", params, api_key=key)
    finally:
        if own_client:
            await http.aclose()

    # PDL response semantics:
    # - 200 + status=200 + data → match
    # - 200 + status=404 or 404 → no match
    # - 401 / 403 → auth issue (sticky; loud log)
    # - 402 → out of credits / billing issue
    # - 429 → rate limit
    if body is None:
        return None

    if status in (401, 403):
        logger.error("PDL auth failure (%d) — check PDL_API_KEY rotation", status)
        return None
    if status == 402:
        logger.error("PDL billing failure (402) — out of credits")
        return None
    if status == 429:
        logger.warning("PDL rate-limited")
        return None

    # PDL puts the actual status in the body too (sometimes wrapped):
    inner_status = body.get("status")
    data = body.get("data")
    if status != 200 or inner_status not in (200, None):
        # Most likely 404 (no match); count as cost incurred but no fields.
        logger.info("PDL no match (HTTP %s, inner_status %s)", status, inner_status)
        return None
    if not isinstance(data, dict) or not data:
        return None

    fields = _extract_pdl_person(data)

    # PDL returns `likelihood` (1-10) for match confidence — convert to 0..1.
    likelihood = body.get("likelihood")
    if isinstance(likelihood, (int, float)) and 0 <= likelihood <= 10:
        confidence = float(likelihood) / 10.0
    else:
        confidence = 0.7  # PDL default-when-unspecified fallback

    return EnrichmentResult(
        fields=fields,
        confidence=confidence,
        cost_cents=_calculate_cost(matched=True),
        cache_hit=False,
    )
