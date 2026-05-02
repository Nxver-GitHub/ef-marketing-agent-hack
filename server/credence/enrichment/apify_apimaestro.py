"""Apimaestro LinkedIn scrapers — alternative to harvestapi/* family.

Why this exists
---------------
``harvestapi/linkedin-company-employees`` and friends gate non-paying
Apify-tier consumers at 10 lifetime runs (verified live 2026-05-01 via
run logs: ``"Free users are limited to 10 runs"``). After 10 runs the
actor returns SUCCEEDED with 0 items and 0 charges — silent failure.

Apimaestro's actors don't have this gate. Verified live with the new
suryuhhh FREE-tier token: 11th call returned actual nvidia data
where harvestapi returned empty.

Two stages
----------
Stage A (``list_company_employees``):
  Actor: ``apimaestro/linkedin-company-employees-scraper-no-cookies``
  Input: ``{"identifier": "<company URL>", "max_employees": <int>}``
  Cost: $0.01 per result item
  Returns: list of profile-listing dicts (URL, name, headline, location)

Stage B (``fetch_profile_detail``):
  Actor: ``apimaestro/linkedin-profile-detail``
  Input: ``{"username": "<linkedin slug>"}``
  Cost: $0.005 per result item
  Returns: full profile (basic_info + experience + education + featured)

The two-stage cost is $0.015 per fully-enriched person, vs $0.012 for
harvestapi MODE_FULL_EMAIL. Slightly more, but harvestapi is unusable
on the new account.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..taxonomy import domain_from_title, seniority_from_title
from .normalizer import CanonicalPerson, normalize_company

log = logging.getLogger(__name__)

APIFY_API_BASE = "https://api.apify.com/v2"
EMPLOYEES_ACTOR = "apimaestro~linkedin-company-employees-scraper-no-cookies"
PROFILE_ACTOR = "apimaestro~linkedin-profile-detail"

# Per-event prices ($USD). Used for cost-tracking parity with harvestapi.
COST_EMPLOYEES_PER_ITEM_USD = 0.01
COST_PROFILE_PER_ITEM_USD = 0.005


@dataclass(slots=True)
class CompanyEmployee:
    """Stage-A result — a single employee listing entry."""
    profile_url: str
    public_identifier: str
    fullname: str
    first_name: str
    last_name: str
    headline: str | None = None
    location_text: str | None = None
    country_code: str | None = None
    is_premium: bool = False
    is_creator: bool = False
    is_influencer: bool = False
    open_to_work: bool = False
    urn: str | None = None


@dataclass(slots=True)
class FullProfile:
    """Stage-B result — full profile detail dict (raw, plus parsed convenience fields)."""
    raw: dict[str, Any]


def _resolve_token(api_token: str | None = None) -> str:
    tok = api_token or os.environ.get("APIFY_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("APIFY_TOKEN not set")
    return tok


# ── Stage A — list company employees ──────────────────────────────────────


def _parse_employee(raw: dict[str, Any]) -> CompanyEmployee | None:
    if not isinstance(raw, dict):
        return None
    profile_url = str(raw.get("profile_url") or "").strip()
    public_id = str(raw.get("public_identifier") or "").strip()
    fullname = str(raw.get("fullname") or "").strip()
    first = str(raw.get("first_name") or "").strip()
    last = str(raw.get("last_name") or "").strip()
    if not (profile_url and public_id and fullname):
        return None
    if not first or not last:
        # Best-effort split on first space
        if " " in fullname:
            first, last = fullname.split(" ", 1)
        else:
            return None

    loc = raw.get("location") or {}
    return CompanyEmployee(
        profile_url=profile_url,
        public_identifier=public_id,
        fullname=fullname,
        first_name=first,
        last_name=last,
        headline=raw.get("headline"),
        location_text=(loc.get("full") if isinstance(loc, dict) else None),
        country_code=(loc.get("country_code") if isinstance(loc, dict) else None),
        is_premium=bool(raw.get("is_premium")),
        is_creator=bool(raw.get("is_creator")),
        is_influencer=bool(raw.get("is_influencer")),
        open_to_work=bool(raw.get("open_to_work")),
        urn=raw.get("urn"),
    )


async def list_company_employees(
    company_url: str,
    *,
    max_items: int = 500,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = 600.0,
) -> tuple[list[CompanyEmployee], int]:
    """Stage A. Returns (employees, charged_cents).

    Uses run-sync-get-dataset-items because Stage A is a single-shot
    call per company (~500 items max, well under sync timeout limits).
    """
    token = _resolve_token(api_token)
    payload = {
        "identifier": company_url,
        "max_employees": max_items,
        "maxItems": max_items,
    }
    owns_client = client is None
    cli = client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        url = f"{APIFY_API_BASE}/acts/{EMPLOYEES_ACTOR}/run-sync-get-dataset-items?token={token}"
        r = await cli.post(url, json=payload)
        if r.status_code != 201 and r.status_code != 200:
            log.warning("Stage A %s HTTP %d: %s", company_url, r.status_code, r.text[:200])
            return [], 0
        items = r.json() if r.text else []
        if not isinstance(items, list):
            return [], 0
        emps = [e for e in (_parse_employee(it) for it in items) if e is not None]
        cost_cents = round(len(items) * COST_EMPLOYEES_PER_ITEM_USD * 100)
        return emps, cost_cents
    finally:
        if owns_client:
            await cli.aclose()


# ── Stage B — fetch profile detail ────────────────────────────────────────


async def fetch_profile_detail(
    username: str,
    *,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = 120.0,
) -> tuple[FullProfile | None, int]:
    """Stage B. Returns (profile, charged_cents). None on empty/error."""
    token = _resolve_token(api_token)
    payload = {"username": username}
    owns_client = client is None
    cli = client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        url = f"{APIFY_API_BASE}/acts/{PROFILE_ACTOR}/run-sync-get-dataset-items?token={token}"
        r = await cli.post(url, json=payload)
        if r.status_code not in (200, 201):
            log.warning("Stage B %s HTTP %d: %s", username, r.status_code, r.text[:200])
            return None, 0
        items = r.json() if r.text else []
        if not isinstance(items, list) or not items:
            return None, 0
        first = items[0]
        if not isinstance(first, dict):
            return None, 0
        # Apimaestro's profile-detail returns 1 item, but bills per-item.
        cost_cents = round(len(items) * COST_PROFILE_PER_ITEM_USD * 100)
        return FullProfile(raw=first), cost_cents
    finally:
        if owns_client:
            await cli.aclose()


# ── Mapping to CanonicalPerson ────────────────────────────────────────────


def _experience_to_employment_period(exp: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(exp, dict):
        return None
    title = exp.get("title") or ""
    company = exp.get("company") or ""
    if not title or not company:
        return None
    sd = exp.get("start_date") or {}
    ed = exp.get("end_date") or {}
    is_current = bool(exp.get("is_current"))
    return {
        "title": title,
        "company_name": normalize_company(company),
        "company_linkedin_url": exp.get("company_linkedin_url"),
        "start_year": (sd.get("year") if isinstance(sd, dict) else None),
        "start_month": (sd.get("month") if isinstance(sd, dict) else None),
        "end_year": (ed.get("year") if isinstance(ed, dict) else None),
        "end_month": (ed.get("month") if isinstance(ed, dict) else None),
        "is_current": is_current,
    }


def _education_to_period(edu: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(edu, dict):
        return None
    school = edu.get("school") or ""
    if not school:
        return None
    sd = edu.get("start_date") or {}
    ed = edu.get("end_date") or {}
    return {
        "school_name": school,
        "school_linkedin_url": edu.get("school_linkedin_url"),
        "degree": edu.get("degree") or edu.get("degree_name"),
        "start_year": (sd.get("year") if isinstance(sd, dict) else None),
        "end_year": (ed.get("year") if isinstance(ed, dict) else None),
    }


def to_canonical_person(profile: FullProfile) -> CanonicalPerson | None:
    """Map apimaestro/profile-detail result → CanonicalPerson."""
    raw = profile.raw
    bi = raw.get("basic_info") or {}
    if not isinstance(bi, dict):
        return None

    profile_url = bi.get("profile_url") or ""
    if not profile_url:
        return None
    # Normalize URL — drop "www." prefix variant for stable UPSERT keying
    linkedin_url = profile_url.replace("https://www.linkedin.com/", "https://linkedin.com/")
    if not linkedin_url.startswith("http"):
        linkedin_url = "https://linkedin.com/" + linkedin_url.lstrip("/")

    first = (bi.get("first_name") or "").strip()
    last = (bi.get("last_name") or "").strip()
    fullname = (bi.get("fullname") or f"{first} {last}").strip()
    if not fullname:
        return None
    if not first or not last:
        if " " in fullname:
            first, last = fullname.split(" ", 1)
        else:
            first = first or fullname
            last = last or ""

    loc = bi.get("location") or {}
    location_text = loc.get("full") if isinstance(loc, dict) else None
    country_code = loc.get("country_code") if isinstance(loc, dict) else None

    employment = [
        ep for ep in (_experience_to_employment_period(e) for e in (raw.get("experience") or []))
        if ep is not None
    ]
    education = [
        ed for ed in (_education_to_period(e) for e in (raw.get("education") or []))
        if ed is not None
    ]

    current_emp = next((e for e in employment if e.get("is_current")), employment[0] if employment else None)
    current_title = (current_emp or {}).get("title")
    current_company = (current_emp or {}).get("company_name")

    email = bi.get("email") or None
    skills = list(bi.get("top_skills") or [])

    return CanonicalPerson(
        canonical_name=fullname,
        first_name=first,
        last_name=last,
        name_variants=[fullname],
        linkedin_url=linkedin_url,
        linkedin_id=bi.get("urn"),
        email=email,
        email_status=("unverified" if email else None),
        current_title=current_title,
        current_company_name=current_company,
        current_seniority_score=seniority_from_title(current_title),
        current_functional_domain=domain_from_title(current_title),
        location_text=location_text,
        country_code=country_code,
        headline=bi.get("headline"),
        connections_count=bi.get("connection_count"),
        followers_count=bi.get("follower_count"),
        premium=bool(bi.get("is_premium")),
        verified=False,  # apimaestro doesn't expose verified status
        open_to_work=bool(bi.get("open_to_work")),
        hiring=False,  # apimaestro doesn't expose hiring status
        registered_at=None,  # apimaestro returns created_timestamp, not ISO
        employment_periods=employment,
        education_periods=education,
        skills=skills,
        certifications=[],
        languages=[],
        publications=[],
        patents=[],
        honors_and_awards=[],
        organizations=[],
        sources={
            "canonical_name": "apify_apimaestro",
            "linkedin_url": "apify_apimaestro",
            "current_title": "apify_apimaestro",
            "current_company_name": "apify_apimaestro",
            "employment_periods": "apify_apimaestro",
            "education_periods": "apify_apimaestro",
            **({"email": "apify_apimaestro"} if email else {}),
        },
    )
