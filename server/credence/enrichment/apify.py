"""Apify LinkedIn scraper — primary deep-enrichment source for v3.1.

Actor: ``harvestapi/linkedin-company-employees``
Docs:  https://apify.com/harvestapi/linkedin-company-employees

## Why this actor

Verified live 2026-04-30 against Marvell:
- 10 full-profile pulls in 19.6s, $0.08 charged ($8/1k)
- 8,839 Marvell employees discoverable on LinkedIn (vast upper bound)
- Returns the deep-data shape PDL was supposed to give but didn't (PDL's
  matcher 404s on name+company; harvestapi takes a company URL + returns
  the entire roster with full profile data)
- 4.0/5 rating, 9.9k users, **last modified 14 hours ago** — actively
  maintained against LinkedIn's anti-bot churn (LP msg 162-equivalent
  research)

The decision to skip PDL fallback for the bulk pass is deliberate (user
direction): per-prospect PDL lookup adds ~$1,100 in cost duplication on
data harvestapi already returns. PDL stays in `pdl.py` for the
`/enrich/{prospect_id}` route's manager/reports_to extraction (Phase A.6)
but does not run in the bulk pipeline.

## Pricing modes (verified live)

| Mode string                              | $/1k  | What it adds                       |
|------------------------------------------|-------|------------------------------------|
| ``Short ($4 per 1k)``                    | $4    | name, title, location, URL, headline |
| ``Full ($8 per 1k)`` (default)           | $8    | + work history, education, skills, certifications, languages |
| ``Full + email search ($12 per 1k)``     | $12   | + verified email                   |

## Two run patterns

- **Sync** (``run-sync-get-dataset-items``) — only for small (≤25)
  smoke runs. Apify's run-sync endpoint times out at ~5min; 500-profile
  pulls take 12-15min and will fail.
- **Async + poll** (``runs`` + ``actor-runs/{id}``) — for everything
  ≥50 profiles. Returns immediately with a run_id; caller polls until
  status=SUCCEEDED, then fetches the dataset.

This module exposes both patterns. Pipeline (``pipeline.py``) uses
async-poll. Per-prospect ``/enrich`` may use sync for one-off lookups.

## Cost accounting

Apify charges per ``chargedEventCounts`` event:
- ``actor-start`` — flat $0 on harvestapi (verified)
- ``short-profile`` / ``full-profile`` / ``full-profile-with-email`` —
  N items × per-mode rate

Caller passes the run_id back through ``compute_run_cost_cents()`` to
get the cost in cents for the budget tracker.

## Manager / reports_to (Phase A.6 hook)

Apify's profile shape does **not** include a top-level ``manager`` field
(unlike PDL). Reporting-line signals come from the org-chart pipeline's
job-postings extractor (separate ``apify.py`` actor — ``linkedin-jobs-
scraper``) parsing REPORTING_PATTERN regex matches. This module ships
employee profiles only.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

import httpx

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────────

APIFY_API_BASE = "https://api.apify.com/v2"
ACTOR_ID = "harvestapi~linkedin-company-employees"  # ~ replaces / in URL slug
# Profile-by-URL variant — same harvestapi vendor, same data shape per item.
# Verified live (per CLAUDE.md): both actors return identical profile JSON
# (the parser is reused as-is). The difference is the *input*: this actor
# takes a list of LinkedIn profile URLs ("queries") instead of a company URL.
PROFILE_ACTOR_ID = "harvestapi~linkedin-profile-scraper"

# Per-mode pricing in cents (verified live). Mode strings are exact enum
# values from the actor's input schema — passing wrong strings silently
# defaults to "Full".
#
# COMPANY-EMPLOYEES actor (`harvestapi/linkedin-company-employees`) modes:
MODE_SHORT = "Short ($4 per 1k)"
MODE_FULL = "Full ($8 per 1k)"
MODE_FULL_EMAIL = "Full + email search ($12 per 1k)"
# PROFILE-SCRAPER actor (`harvestapi/linkedin-profile-scraper`) uses
# different mode strings — verified live 2026-05-01 against actor's
# inputSchema enum:
PROFILE_MODE_NO_EMAIL = "Profile details no email ($4 per 1k)"
PROFILE_MODE_WITH_EMAIL = "Profile details + email search ($10 per 1k)"

ScrapeMode = Literal[
    "Short ($4 per 1k)",
    "Full ($8 per 1k)",
    "Full + email search ($12 per 1k)",
]

# Per-profile cost in cents, indexed by chargedEventCounts key. The
# `actor-start` event has zero cost on harvestapi (verified live).
_PROFILE_COST_CENTS: dict[str, float] = {
    "short-profile": 0.4,             # $4 / 1000 = 0.4¢
    "full-profile": 0.8,              # $8 / 1000 = 0.8¢
    "full-profile-with-email": 1.2,   # $12 / 1000 = 1.2¢
}

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
# Apify run-sync hard-fails after ~5min. Cap our use of sync to small batches.
SYNC_MAX_ITEMS = 25
# Apify async runs have no hard cap but we still want a sensible upper bound
# so a misbehaving actor doesn't burn credit. 8000 is plausible (Marvell has
# 8,839 employees; nobody else in the target list has more).
ASYNC_MAX_ITEMS = 8000


# ─── Public types ───────────────────────────────────────────────────────────


class ApifyEmploymentPeriod(TypedDict, total=False):
    """One row from the profile's ``experience[]`` (or ``currentPosition[]``)."""

    company_name: str
    company_linkedin_url: str | None
    company_universal_name: str | None  # LinkedIn slug, useful for entity-resolve
    title: str
    employment_type: str | None         # Full-time / Contract / etc.
    workplace_type: str | None          # On-site / Remote / Hybrid
    location: str | None
    description: str | None
    start_year: int | None              # parsed from startDate.year
    start_month: int | None             # parsed from startDate.month name
    end_year: int | None
    end_month: int | None
    is_current: bool
    duration_text: str | None           # human-readable "5 mos"


class ApifyEducation(TypedDict, total=False):
    """One row from the profile's ``education[]``."""

    school_name: str
    school_linkedin_url: str | None
    school_id: str | None               # LinkedIn school ID
    degree: str | None                  # "Bachelor of Science - BS"
    field_of_study: str | None
    start_year: int | None
    end_year: int | None
    insights: str | None                # honors, GPA, activities — when present


@dataclass(frozen=True, slots=True)
class ApifyProfile:
    """One profile from harvestapi/linkedin-company-employees.

    All fields defensively typed — LinkedIn shapes vary across profiles
    (premium, retired, etc.) and the parser must never crash.
    """

    linkedin_id: str               # "ACoAAA..." — internal LinkedIn ID
    public_identifier: str         # URL slug — "rhonda-whitney-28183b28"
    linkedin_url: str              # full canonical profile URL
    first_name: str
    last_name: str
    headline: str | None
    location_text: str | None      # "Hayward, California, United States"
    country_code: str | None       # ISO 3166-1 alpha-2 — "US"
    email: str | None              # only populated in Full+email mode

    employment_periods: list[ApifyEmploymentPeriod]
    education_periods: list[ApifyEducation]
    skills: list[str]
    certifications: list[dict[str, Any]]
    languages: list[str]

    # Engagement signals — fed into Authority / Warmth scoring
    connections_count: int | None
    followers_count: int | None
    open_to_work: bool
    hiring: bool
    premium: bool
    verified: bool
    registered_at: str | None      # ISO datetime when joined LinkedIn

    # Formal evidence — fed into Authenticity scoring
    publications: list[dict[str, Any]] = field(default_factory=list)
    patents: list[dict[str, Any]] = field(default_factory=list)
    honors_and_awards: list[dict[str, Any]] = field(default_factory=list)
    organizations: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """Per-run result handed back to the pipeline. Mirrors apollo/pdl shape."""

    profiles: list[ApifyProfile]
    cost_cents: int
    run_id: str | None
    cache_hit: bool = False


# ─── Field-extraction helpers (defensive, none of these may raise) ─────────


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _int_or_none(v: Any) -> int | None:
    if isinstance(v, bool):
        return None  # bool is a subclass of int in Python; reject explicitly
    return int(v) if isinstance(v, int) else None


def _bool_or_false(v: Any) -> bool:
    return bool(v) if isinstance(v, bool) else False


def _list_of_str(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [s for s in v if isinstance(s, str) and s.strip()]


_MONTH_TO_NUM = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    # Full names — sometimes LinkedIn returns these instead
    "January": 1, "February": 2, "March": 3, "April": 4, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10,
    "November": 11, "December": 12,
}


def _parse_date_obj(d: Any) -> tuple[int | None, int | None]:
    """Map ``{"month": "Jan", "year": 2026, "text": "Jan 2026"}`` → (year, month).

    Both fields independently optional. ``text: "Present"`` returns (None, None)
    — caller treats that as the current/end-not-set sentinel.
    """
    if not isinstance(d, dict):
        return None, None
    year = _int_or_none(d.get("year"))
    month_raw = d.get("month")
    month = _MONTH_TO_NUM.get(month_raw) if isinstance(month_raw, str) else None
    return year, month


def _experience_to_employment(exp: Any) -> ApifyEmploymentPeriod | None:
    """Map one ``experience[]`` or ``currentPosition[]`` entry to our shape.

    Returns None when neither company nor position is present — nothing
    to render.
    """
    if not isinstance(exp, dict):
        return None
    company_name = _str_or_none(exp.get("companyName"))
    title = _str_or_none(exp.get("position"))
    if not company_name and not title:
        return None

    start_year, start_month = _parse_date_obj(exp.get("startDate"))
    end_year, end_month = _parse_date_obj(exp.get("endDate"))
    end_text = ""
    end_obj = exp.get("endDate")
    if isinstance(end_obj, dict):
        end_text = (end_obj.get("text") or "").strip()
    is_current = end_text.lower() == "present" or (end_year is None and end_month is None)

    rec: ApifyEmploymentPeriod = {
        "company_name": company_name or "",
        "company_linkedin_url": _str_or_none(exp.get("companyLinkedinUrl")),
        "company_universal_name": _str_or_none(exp.get("companyUniversalName")),
        "title": title or "",
        "employment_type": _str_or_none(exp.get("employmentType")),
        "workplace_type": _str_or_none(exp.get("workplaceType")),
        "location": _str_or_none(exp.get("location")),
        "description": _str_or_none(exp.get("description")),
        "start_year": start_year,
        "start_month": start_month,
        "end_year": end_year,
        "end_month": end_month,
        "is_current": is_current,
        "duration_text": _str_or_none(exp.get("duration")),
    }
    return rec


def _education_to_period(edu: Any) -> ApifyEducation | None:
    """Map one ``education[]`` entry. Drops rows lacking school name."""
    if not isinstance(edu, dict):
        return None
    school = _str_or_none(edu.get("schoolName"))
    if not school:
        return None

    start_year, _ = _parse_date_obj(edu.get("startDate"))
    end_year, _ = _parse_date_obj(edu.get("endDate"))

    rec: ApifyEducation = {
        "school_name": school,
        "school_linkedin_url": _str_or_none(edu.get("schoolLinkedinUrl")),
        "school_id": _str_or_none(edu.get("schoolId")),
        "degree": _str_or_none(edu.get("degree")),
        "field_of_study": _str_or_none(edu.get("fieldOfStudy")),
        "start_year": start_year,
        "end_year": end_year,
        "insights": _str_or_none(edu.get("insights")),
    }
    return rec


def _extract_skills(raw: Any) -> list[str]:
    """``skills[]`` is ``[{"name": "Safety Management", "positions": [...]}, ...]``."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for s in raw:
        if isinstance(s, dict):
            name = _str_or_none(s.get("name"))
            if name:
                out.append(name)
        elif isinstance(s, str) and s.strip():
            out.append(s.strip())
    return out


def _extract_languages(raw: Any) -> list[str]:
    """``languages[]`` is similar to skills — name + proficiency dict."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for lng in raw:
        if isinstance(lng, dict):
            name = _str_or_none(lng.get("name") or lng.get("language"))
            if name:
                out.append(name)
        elif isinstance(lng, str) and lng.strip():
            out.append(lng.strip())
    return out


def _extract_email(raw: Any) -> str | None:
    """``emails[]`` is a list. Take the first non-empty entry."""
    if isinstance(raw, list):
        for e in raw:
            if isinstance(e, str) and "@" in e:
                return e.strip()
            if isinstance(e, dict):
                addr = _str_or_none(e.get("email") or e.get("address") or e.get("value"))
                if addr and "@" in addr:
                    return addr
    elif isinstance(raw, str) and "@" in raw:
        return raw.strip()
    return None


def _extract_location(raw: Any) -> tuple[str | None, str | None]:
    """``location`` is ``{linkedinText, countryCode, parent}``."""
    if not isinstance(raw, dict):
        return None, None
    text = _str_or_none(raw.get("linkedinText") or raw.get("text"))
    cc = _str_or_none(raw.get("countryCode"))
    return text, cc


def parse_profile(raw: dict[str, Any]) -> ApifyProfile | None:
    """Map one harvestapi item → ``ApifyProfile``.

    Returns None when the item lacks a LinkedIn URL — useless for
    downstream entity resolution.
    """
    if not isinstance(raw, dict):
        return None
    linkedin_url = _str_or_none(raw.get("linkedinUrl"))
    if not linkedin_url:
        return None

    employment_raw = raw.get("experience") or raw.get("currentPosition") or []
    employment_periods: list[ApifyEmploymentPeriod] = []
    if isinstance(employment_raw, list):
        for exp in employment_raw:
            mapped = _experience_to_employment(exp)
            if mapped is not None:
                employment_periods.append(mapped)

    education_raw = raw.get("education") or []
    education_periods: list[ApifyEducation] = []
    if isinstance(education_raw, list):
        for edu in education_raw:
            mapped = _education_to_period(edu)
            if mapped is not None:
                education_periods.append(mapped)

    location_text, country_code = _extract_location(raw.get("location"))

    return ApifyProfile(
        linkedin_id=_str_or_none(raw.get("id")) or "",
        public_identifier=_str_or_none(raw.get("publicIdentifier")) or "",
        linkedin_url=linkedin_url,
        first_name=_str_or_none(raw.get("firstName")) or "",
        last_name=_str_or_none(raw.get("lastName")) or "",
        headline=_str_or_none(raw.get("headline")),
        location_text=location_text,
        country_code=country_code,
        email=_extract_email(raw.get("emails") or raw.get("email")),
        employment_periods=employment_periods,
        education_periods=education_periods,
        skills=_extract_skills(raw.get("skills")),
        certifications=raw.get("certifications") if isinstance(raw.get("certifications"), list) else [],
        languages=_extract_languages(raw.get("languages")),
        connections_count=_int_or_none(raw.get("connectionsCount")),
        followers_count=_int_or_none(raw.get("followerCount")),
        open_to_work=_bool_or_false(raw.get("openToWork")),
        hiring=_bool_or_false(raw.get("hiring")),
        premium=_bool_or_false(raw.get("premium")),
        verified=_bool_or_false(raw.get("verified")),
        registered_at=_str_or_none(raw.get("registeredAt")),
        publications=raw.get("publications") if isinstance(raw.get("publications"), list) else [],
        patents=raw.get("patents") if isinstance(raw.get("patents"), list) else [],
        honors_and_awards=raw.get("honorsAndAwards") if isinstance(raw.get("honorsAndAwards"), list) else [],
        organizations=raw.get("organizations") if isinstance(raw.get("organizations"), list) else [],
    )


# ─── Cost computation ───────────────────────────────────────────────────────


def compute_run_cost_cents(charged_event_counts: dict[str, Any] | None) -> int:
    """Convert Apify ``chargedEventCounts`` into integer cents.

    Rounded up to the nearest cent (Apify charges by 0.4 / 0.8 / 1.2 cent
    units per profile; rounding up means the budget tracker never
    under-counts spend).
    """
    if not isinstance(charged_event_counts, dict):
        return 0
    cents = 0.0
    for event, count in charged_event_counts.items():
        if not isinstance(count, int) or count <= 0:
            continue
        rate = _PROFILE_COST_CENTS.get(event, 0.0)
        cents += rate * count
    # Round up — budget should never under-count
    return int(cents + 0.999) if cents > 0 else 0


# ─── HTTP I/O ───────────────────────────────────────────────────────────────


def _resolve_token(api_token: str | None) -> str | None:
    """Returns the token to use, or None when the operator hasn't configured one.

    Caller must short-circuit on None — Apify rejects bad tokens with 401
    rather than empty results, so this guards against silent quota burn.
    """
    return api_token or os.environ.get("APIFY_TOKEN")


async def find_company_employees_sync(
    company_url: str,
    *,
    max_items: int = 10,
    mode: ScrapeMode = MODE_FULL,  # type: ignore[assignment]
    locations: list[str] | None = None,
    job_titles: list[str] | None = None,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = 300.0,
) -> EnrichmentResult | None:
    """Sync run — only safe for ``max_items <= SYNC_MAX_ITEMS`` (~25).

    Apify's run-sync hard-fails at ~5min. Larger pulls must use
    ``find_company_employees_async`` + polling. Returns None when the
    actor returns zero items (slug not found, rate-limited, etc.).
    """
    if max_items > SYNC_MAX_ITEMS:
        logger.warning(
            "apify.find_company_employees_sync: max_items=%d > %d safe limit; "
            "use find_company_employees_async for larger pulls",
            max_items, SYNC_MAX_ITEMS,
        )

    token = _resolve_token(api_token)
    if not token:
        logger.info("apify: no APIFY_TOKEN set — skipping")
        return None

    payload: dict[str, Any] = {
        "companies": [company_url],
        "maxItems": max_items,
        "profileScraperMode": mode,
    }
    if locations:
        payload["locations"] = locations
    if job_titles:
        payload["jobTitles"] = job_titles

    url = (
        f"{APIFY_API_BASE}/acts/{ACTOR_ID}/run-sync-get-dataset-items"
        f"?token={token}"
    )
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        try:
            r = await http.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning("apify sync request failed for %s: %s", company_url, exc)
            return None
        if r.status_code not in (200, 201):
            logger.warning(
                "apify sync HTTP %d for %s: %s",
                r.status_code, company_url, r.text[:200],
            )
            return None
        try:
            items = r.json()
        except ValueError:
            logger.warning("apify sync returned non-JSON for %s", company_url)
            return None
        if not isinstance(items, list):
            logger.warning("apify sync returned non-list for %s", company_url)
            return None
        # run-sync-get-dataset-items doesn't return chargedEventCounts in the
        # response body — we'd have to re-fetch the run for cost accounting.
        # Compute cost from item count + mode as a deterministic estimate
        # (matches the live behavior verified 2026-04-30: 10 items × $8/1k = $0.08).
        rate = _PROFILE_COST_CENTS.get(_mode_to_event_key(mode), 0.0)
        cost_cents = int(rate * len(items) + 0.999) if items else 0
    finally:
        if own_client:
            await http.aclose()

    profiles: list[ApifyProfile] = []
    for raw in items:
        p = parse_profile(raw)
        if p is not None:
            profiles.append(p)
    return EnrichmentResult(
        profiles=profiles,
        cost_cents=cost_cents,
        run_id=None,
        cache_hit=False,
    )


def _mode_to_event_key(mode: str) -> str:
    if mode == MODE_SHORT:
        return "short-profile"
    if mode == MODE_FULL_EMAIL:
        return "full-profile-with-email"
    return "full-profile"


# ─── Async pattern (for bulk runs that exceed sync's 5min window) ──────────


async def start_company_employees_run(
    company_url: str,
    *,
    max_items: int,
    mode: ScrapeMode = MODE_FULL,  # type: ignore[assignment]
    locations: list[str] | None = None,
    job_titles: list[str] | None = None,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Kick off an async run. Returns the run_id or None on failure.

    Caller polls via ``wait_for_run`` and then ``fetch_run_dataset``.
    Async runs cost the same as sync runs — the difference is only in
    how the caller waits for results.
    """
    if max_items > ASYNC_MAX_ITEMS:
        logger.warning(
            "apify.start_company_employees_run: max_items=%d capped to %d",
            max_items, ASYNC_MAX_ITEMS,
        )
        max_items = ASYNC_MAX_ITEMS

    token = _resolve_token(api_token)
    if not token:
        return None

    payload: dict[str, Any] = {
        "companies": [company_url],
        "maxItems": max_items,
        "profileScraperMode": mode,
    }
    if locations:
        payload["locations"] = locations
    if job_titles:
        payload["jobTitles"] = job_titles

    url = f"{APIFY_API_BASE}/acts/{ACTOR_ID}/runs?token={token}"
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        try:
            r = await http.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            logger.warning("apify start run failed for %s: %s", company_url, exc)
            return None
        if r.status_code not in (200, 201):
            logger.warning(
                "apify start run HTTP %d for %s: %s",
                r.status_code, company_url, r.text[:200],
            )
            return None
        try:
            body = r.json()
        except ValueError:
            return None
    finally:
        if own_client:
            await http.aclose()

    run_id = ((body or {}).get("data") or {}).get("id")
    return run_id if isinstance(run_id, str) and run_id else None


async def wait_for_run(
    run_id: str,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_wait_seconds: float = 1800.0,  # 30 min
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Poll until the run reaches a terminal state.

    Returns ``(status, run_data)``. Status is one of ``SUCCEEDED``,
    ``FAILED``, ``ABORTED``, ``TIMED_OUT``, or ``UNKNOWN`` if our own
    timeout fired. ``run_data`` is the full run document (with
    ``defaultDatasetId`` for the dataset fetch + ``chargedEventCounts``
    for cost accounting).
    """
    token = _resolve_token(api_token)
    if not token:
        return "UNKNOWN", None

    deadline = asyncio.get_event_loop().time() + max_wait_seconds
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    # Bug fix 2026-05-01: previously bailed on first transient error
    # (HTTP 502 / connection drop). Apify routinely returns 502s under
    # load — the run is still executing on their side and charging
    # credit. We must keep polling. Track consecutive failures so we
    # eventually give up if Apify is genuinely down, but transients
    # don't stop us.
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 10  # ~50s at 5s poll interval
    last_known_data: dict[str, Any] | None = None
    try:
        while True:
            try:
                r = await http.get(
                    f"{APIFY_API_BASE}/actor-runs/{run_id}?token={token}"
                )
                if r.status_code != 200:
                    logger.warning(
                        "apify poll HTTP %d for run %s (consec=%d) — retrying",
                        r.status_code, run_id, consecutive_failures + 1,
                    )
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.error(
                            "apify poll failed %d consecutive times for run %s — giving up",
                            consecutive_failures, run_id,
                        )
                        return "UNKNOWN", last_known_data
                    await asyncio.sleep(poll_interval)
                    continue
                try:
                    data = (r.json() or {}).get("data") or {}
                except ValueError:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        return "UNKNOWN", last_known_data
                    await asyncio.sleep(poll_interval)
                    continue
            except httpx.HTTPError as exc:
                consecutive_failures += 1
                logger.warning(
                    "apify poll exc for run %s (consec=%d): %s — retrying",
                    run_id, consecutive_failures, exc,
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        "apify poll failed %d consecutive times for run %s — giving up",
                        consecutive_failures, run_id,
                    )
                    return "UNKNOWN", last_known_data
                await asyncio.sleep(poll_interval)
                continue

            # Successful poll → reset failure counter, keep last data
            consecutive_failures = 0
            last_known_data = data
            status = data.get("status") or "UNKNOWN"
            if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT"):
                return status, data
            if asyncio.get_event_loop().time() >= deadline:
                logger.warning(
                    "apify wait_for_run: poll timeout (%ds) on run %s — last status=%s",
                    int(max_wait_seconds), run_id, status,
                )
                return "UNKNOWN", data
            await asyncio.sleep(poll_interval)
    finally:
        if own_client:
            await http.aclose()


async def fetch_run_dataset(
    run_data: dict[str, Any],
    *,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> EnrichmentResult | None:
    """Pull the items from a finished run and shape them into ``EnrichmentResult``.

    Cost is computed from ``run_data['chargedEventCounts']`` — the
    authoritative source from Apify's billing.
    """
    dataset_id = run_data.get("defaultDatasetId")
    if not isinstance(dataset_id, str) or not dataset_id:
        return None
    token = _resolve_token(api_token)
    if not token:
        return None

    own_client = client is None
    http = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        try:
            r = await http.get(
                f"{APIFY_API_BASE}/datasets/{dataset_id}/items?token={token}"
            )
        except httpx.HTTPError as exc:
            logger.warning("apify fetch_run_dataset failed: %s", exc)
            return None
        if r.status_code != 200:
            return None
        try:
            items = r.json()
        except ValueError:
            return None
        if not isinstance(items, list):
            return None
    finally:
        if own_client:
            await http.aclose()

    profiles: list[ApifyProfile] = []
    for raw in items:
        p = parse_profile(raw)
        if p is not None:
            profiles.append(p)

    cost_cents = compute_run_cost_cents(run_data.get("chargedEventCounts"))
    return EnrichmentResult(
        profiles=profiles,
        cost_cents=cost_cents,
        run_id=run_data.get("id"),
        cache_hit=False,
    )


async def abort_run(
    run_id: str,
    *,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Best-effort POST to abort a running actor run.

    Used as a last resort when our polling has decisively given up — we
    don't want the actor to keep executing + charging credit while we've
    moved on. Returns True on a 200/202 response, False otherwise.
    """
    token = _resolve_token(api_token)
    if not token:
        return False
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        try:
            r = await http.post(
                f"{APIFY_API_BASE}/actor-runs/{run_id}/abort?token={token}"
            )
        except httpx.HTTPError as exc:
            logger.warning("apify abort failed for run %s: %s", run_id, exc)
            return False
        return r.status_code in (200, 202)
    finally:
        if own_client:
            await http.aclose()


async def find_company_employees_async(
    company_url: str,
    *,
    max_items: int = 500,
    mode: ScrapeMode = MODE_FULL,  # type: ignore[assignment]
    locations: list[str] | None = None,
    job_titles: list[str] | None = None,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_wait_seconds: float = 1800.0,
) -> EnrichmentResult | None:
    """One-shot async pull: start → poll → fetch dataset.

    The bulk pipeline's per-company entry point. Returns None when the
    run failed, was aborted, or timed out.

    When polling decisively fails (transient errors past
    MAX_CONSECUTIVE_FAILURES) but the actor may still be executing, we
    aggressively try to fetch whatever's in the dataset so far — the
    failure mode of the original bulk run that abandoned $30 of paid-
    for profile data. We also issue an abort to stop further spend.
    """
    run_id = await start_company_employees_run(
        company_url,
        max_items=max_items,
        mode=mode,
        locations=locations,
        job_titles=job_titles,
        api_token=api_token,
        client=client,
    )
    if not run_id:
        return None

    status, run_data = await wait_for_run(
        run_id,
        poll_interval=poll_interval,
        max_wait_seconds=max_wait_seconds,
        api_token=api_token,
        client=client,
    )

    # Recovery path: even when we didn't get a SUCCEEDED status, attempt
    # to fetch any data the actor produced before our polling gave up.
    # Then issue an abort to stop further charging.
    if status != "SUCCEEDED" or run_data is None:
        logger.warning(
            "apify run %s did not succeed (status=%s) — attempting recovery + abort",
            run_id, status,
        )
        if status not in ("ABORTED", "FAILED") and run_id:
            await abort_run(run_id, api_token=api_token, client=client)
        if run_data is not None and run_data.get("defaultDatasetId"):
            recovery = await fetch_run_dataset(
                run_data, api_token=api_token, client=client
            )
            if recovery is not None and recovery.profiles:
                logger.info(
                    "apify run %s recovered %d profiles after non-SUCCEEDED status",
                    run_id, len(recovery.profiles),
                )
                return recovery
        return None

    return await fetch_run_dataset(run_data, api_token=api_token, client=client)


async def start_profile_by_url_run(
    linkedin_urls: list[str],
    *,
    mode: str = PROFILE_MODE_NO_EMAIL,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Kick off an async run of ``harvestapi/linkedin-profile-scraper`` with N urls.

    Mirrors :func:`start_company_employees_run` but targets the
    profile-scraper actor and accepts a list of LinkedIn profile URLs as
    ``queries``. Returns the run document (``{"id", "defaultDatasetId",
    "status", ...}``) on success — same shape Apify returns under
    ``data`` from POST ``/acts/{actor}/runs``. Caller polls via
    :func:`wait_for_run` (passing ``run_data["id"]``), then fetches via
    :func:`fetch_run_dataset`.

    URLs are capped at :data:`ASYNC_MAX_ITEMS` per run — chunk above
    that. Returns None on missing token, transport error, or non-2xx
    response.
    """
    if not isinstance(linkedin_urls, list) or not linkedin_urls:
        return None
    # Defensive: filter blank entries before counting.
    cleaned = [u for u in linkedin_urls if isinstance(u, str) and u.strip()]
    if not cleaned:
        return None
    if len(cleaned) > ASYNC_MAX_ITEMS:
        logger.warning(
            "apify.start_profile_by_url_run: %d urls capped to %d",
            len(cleaned), ASYNC_MAX_ITEMS,
        )
        cleaned = cleaned[:ASYNC_MAX_ITEMS]

    token = _resolve_token(api_token)
    if not token:
        logger.info("apify.start_profile_by_url_run: no APIFY_TOKEN — skipping")
        return None

    payload: dict[str, Any] = {
        "profileScraperMode": mode,
        "queries": cleaned,
        "maxItems": len(cleaned),
    }

    url = f"{APIFY_API_BASE}/acts/{PROFILE_ACTOR_ID}/runs?token={token}"
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        try:
            r = await http.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "apify start_profile_by_url_run failed (n=%d): %s",
                len(cleaned), exc,
            )
            return None
        if r.status_code not in (200, 201):
            logger.warning(
                "apify start_profile_by_url_run HTTP %d (n=%d): %s",
                r.status_code, len(cleaned), r.text[:200],
            )
            return None
        try:
            body = r.json()
        except ValueError:
            logger.warning(
                "apify start_profile_by_url_run non-JSON response (n=%d)",
                len(cleaned),
            )
            return None
    finally:
        if own_client:
            await http.aclose()

    data = (body or {}).get("data")
    if not isinstance(data, dict):
        return None
    run_id = data.get("id")
    if not isinstance(run_id, str) or not run_id:
        return None
    return data


async def fetch_profile_by_url(
    linkedin_url: str,
    *,
    mode: str = PROFILE_MODE_NO_EMAIL,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> EnrichmentResult | None:
    """Sync-call ``harvestapi/linkedin-profile-scraper`` for one LinkedIn URL.

    Mirrors :func:`find_company_employees_sync`'s pattern: POST to
    ``/acts/{PROFILE_ACTOR_ID}/run-sync-get-dataset-items`` with input
    ``{"profileScraperMode": mode, "queries": [linkedin_url]}``, parse the
    returned dataset items via :func:`parse_profile`.

    Returns ``EnrichmentResult`` with ``.profiles`` populated (zero or one
    profile in the typical case). Returns None when no token is configured
    or the actor errored. ``cost_cents`` is approximated from item count
    × per-mode rate — Apify's run-sync endpoint doesn't return
    ``chargedEventCounts`` in the body.
    """
    token = _resolve_token(api_token)
    if not token:
        logger.info("apify.fetch_profile_by_url: no APIFY_TOKEN — skipping")
        return None
    if not isinstance(linkedin_url, str) or not linkedin_url.strip():
        return None

    payload: dict[str, Any] = {
        "profileScraperMode": mode,
        "queries": [linkedin_url],
    }

    url = (
        f"{APIFY_API_BASE}/acts/{PROFILE_ACTOR_ID}/run-sync-get-dataset-items"
        f"?token={token}"
    )
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        try:
            r = await http.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "apify fetch_profile_by_url request failed for %s: %s",
                linkedin_url, exc,
            )
            return None
        if r.status_code not in (200, 201):
            logger.warning(
                "apify fetch_profile_by_url HTTP %d for %s: %s",
                r.status_code, linkedin_url, r.text[:200],
            )
            return None
        try:
            items = r.json()
        except ValueError:
            logger.warning(
                "apify fetch_profile_by_url returned non-JSON for %s",
                linkedin_url,
            )
            return None
        if not isinstance(items, list):
            logger.warning(
                "apify fetch_profile_by_url returned non-list for %s",
                linkedin_url,
            )
            return None
        rate = _PROFILE_COST_CENTS.get(_mode_to_event_key(mode), 0.0)
        cost_cents = int(rate * len(items) + 0.999) if items else 0
    finally:
        if own_client:
            await http.aclose()

    profiles: list[ApifyProfile] = []
    for raw in items:
        p = parse_profile(raw)
        if p is not None:
            profiles.append(p)
    return EnrichmentResult(
        profiles=profiles,
        cost_cents=cost_cents,
        run_id=None,
        cache_hit=False,
    )


__all__ = [
    "ACTOR_ID",
    "PROFILE_ACTOR_ID",
    "MODE_SHORT",
    "MODE_FULL",
    "MODE_FULL_EMAIL",
    "ApifyEmploymentPeriod",
    "ApifyEducation",
    "ApifyProfile",
    "EnrichmentResult",
    "parse_profile",
    "compute_run_cost_cents",
    "find_company_employees_sync",
    "find_company_employees_async",
    "start_company_employees_run",
    "wait_for_run",
    "fetch_run_dataset",
    "fetch_profile_by_url",
    "start_profile_by_url_run",
]
