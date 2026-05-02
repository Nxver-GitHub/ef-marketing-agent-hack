"""Stage 2 — team scraping for the customer onboarding pipeline.

Wraps the Apify ``apimaestro/linkedin-company-employees-scraper-no-cookies``
actor and adds the size-aware strategy switch defined in
``CUSTOMER_ONBOARDING_PLAN.md`` §"Stage 2 step 1":

* Probe LinkedIn company size first via
  ``apimaestro/linkedin-company-detail-by-username``. If the company has
  fewer than ``GTM_STRATEGY_THRESHOLD`` employees (or the probe fails to
  return a count), use the ``all_employees`` strategy and pull the full
  roster.
* If the company has ``>= GTM_STRATEGY_THRESHOLD`` employees, or the
  caller forces ``strategy='gtm_only'``, filter for go-to-market job
  functions (Sales / BD / Marketing / Customer Success / Alliances /
  Partnerships) by matching the employee headline + scraped title.

Progress writes
---------------
Every ``PROGRESS_WRITE_INTERVAL`` employees that pass the strategy filter
the ``onboarding_jobs.progress`` JSONB column is updated to::

    { "total": <expected>, "scraped": <so-far>, "matched": 0, "new_persons": 0 }

``matched`` and ``new_persons`` are filled later by the entity_resolver
in Wave A4 — we leave them at ``0`` here.

Why a separate module from ``enrichment/apify_apimaestro.py``
------------------------------------------------------------
That module is the *raw* Apify wrapper used by bulk enrichment scripts —
it returns lists of ``CompanyEmployee`` and is shared with the existing
``run_apimaestro_enrichment.py`` job. This module is the *onboarding*
wrapper: it owns the strategy switch, the progress accounting against
``onboarding_jobs``, and the ``ScrapedEmployee`` shape that the entity
resolver in Wave A4 will consume. Two distinct concerns, two files (per
the "many small files" rule in the global Python style guide).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, Field

from .. import db

log = logging.getLogger(__name__)

# ── Apify wiring ──────────────────────────────────────────────────────────

APIFY_API_BASE = "https://api.apify.com/v2"
EMPLOYEES_ACTOR = "apimaestro~linkedin-company-employees-scraper-no-cookies"
COMPANY_DETAIL_ACTOR = "apimaestro~linkedin-company-detail-by-username"

# Per-event prices ($USD). Mirrors apify_apimaestro.COST_EMPLOYEES_PER_ITEM_USD.
COST_EMPLOYEES_PER_ITEM_USD = 0.01
COST_COMPANY_DETAIL_USD = 0.005

# ── Strategy switch ───────────────────────────────────────────────────────

#: Threshold from CUSTOMER_ONBOARDING_PLAN.md — companies with at least
#: this many employees switch from full-roster to GTM-only scraping.
GTM_STRATEGY_THRESHOLD = 500

#: Keywords that flag an employee headline / title as belonging to a
#: go-to-market function. Substring match is intentional — LinkedIn
#: titles are noisy ("Senior Account Executive", "Head of Strategic
#: Alliances", "BD Lead, EMEA") and a function-level taxonomy is the
#: cleanest filter we can apply on a free-text headline.
#:
#: Order matters only for readability — the match is unordered.
GTM_KEYWORDS: tuple[str, ...] = (
    # Sales family
    "sales",
    "account executive",
    "account manager",
    "sales engineer",
    "solutions engineer",
    # Business Development / GTM
    "business development",
    "biz dev",
    "bd ",          # trailing space avoids matching "bdr" alone false-positively
    "bdr",
    "sdr",
    "go-to-market",
    "go to market",
    "gtm",
    "growth",
    "revenue",
    # Marketing
    "marketing",
    "demand generation",
    "field marketing",
    "product marketing",
    # Customer Success
    "customer success",
    "customer experience",
    "customer engagement",
    # Alliances / Partnerships
    "alliances",
    "partnerships",
    "partner",  # covers "Partner Manager", "Partner Engineer"
    "channel",
)

# ── Progress writes ───────────────────────────────────────────────────────

#: How often (in scraped-employee count) to flush a progress row to the
#: ``onboarding_jobs.progress`` JSONB column.
#:
#: Why 25? Apify streams employees back at roughly 1-3/sec, so a write
#: every 25 = ~10-25s between updates. That's frequent enough that the
#: frontend's 3s polling loop sees movement on every other poll, but
#: rare enough that we don't hammer Postgres with single-row UPDATEs
#: when scraping 500-person rosters. Always flushed at completion too,
#: regardless of whether the count is a multiple of 25.
PROGRESS_WRITE_INTERVAL = 25

# ── Bounded concurrency ───────────────────────────────────────────────────

#: Max parallel Apify HTTP calls. Plan section "How" requires <= 3.
MAX_CONCURRENT_API_CALLS = 3


# ── Public types ──────────────────────────────────────────────────────────


class ScrapedEmployee(BaseModel):
    """One employee returned by the team scrape.

    The ``entity_resolver`` in Wave A4 consumes this shape; do not change
    field names without coordinating there.
    """

    linkedin_url: str
    canonical_name: str
    current_title: str | None = None
    current_company: str | None = None
    profile_photo_url: str | None = None
    headline: str | None = None
    location: str | None = None


class TeamScrapeResult(BaseModel):
    """Final result of a single ``scrape_team_for_account`` call."""

    total_returned: int
    employees: list[ScrapedEmployee] = Field(default_factory=list)
    strategy_used: str
    cost_usd: float | None = None
    error: str | None = None


StrategyLiteral = Literal["all_employees", "gtm_only"]


# ── Helpers ───────────────────────────────────────────────────────────────


def _resolve_token(api_token: str | None = None) -> str:
    tok = api_token or os.environ.get("APIFY_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("APIFY_TOKEN not set")
    return tok


def _is_gtm_employee(headline: str | None, title: str | None) -> bool:
    """True if the employee's headline or current title contains a GTM keyword."""
    haystack = " ".join(filter(None, [headline, title])).lower()
    if not haystack:
        return False
    return any(kw in haystack for kw in GTM_KEYWORDS)


def _to_scraped_employee(raw: dict[str, Any]) -> ScrapedEmployee | None:
    """Map one Apify employee record → ``ScrapedEmployee``.

    Returns ``None`` if the record is missing the load-bearing fields
    (linkedin URL or fullname). The Apify payload shape mirrors
    ``apify_apimaestro._parse_employee``; this helper exists so the
    onboarding scraper can return its own typed model without leaking
    the bulk-enrichment ``CompanyEmployee`` dataclass into the
    onboarding API surface.
    """
    if not isinstance(raw, dict):
        return None

    profile_url = str(raw.get("profile_url") or "").strip()
    fullname = str(raw.get("fullname") or "").strip()
    if not profile_url or not fullname:
        return None

    loc = raw.get("location") or {}
    location_text: str | None = None
    if isinstance(loc, dict):
        location_text = loc.get("full") or loc.get("city") or loc.get("country")

    # Apimaestro's Stage A doesn't always return a current_title field —
    # it returns a headline. We carry both through so downstream code
    # can pick whichever is more specific.
    headline = raw.get("headline")
    current_title = raw.get("current_title") or raw.get("title")
    current_company = raw.get("current_company") or raw.get("company")
    photo = raw.get("profile_picture_url") or raw.get("profile_photo_url")

    return ScrapedEmployee(
        linkedin_url=profile_url,
        canonical_name=fullname,
        current_title=current_title,
        current_company=current_company,
        profile_photo_url=photo,
        headline=headline,
        location=location_text,
    )


def _extract_employee_count(raw: dict[str, Any]) -> int | None:
    """Pull an integer headcount out of an Apify company-detail payload.

    Apimaestro's company-detail actor exposes the headcount under one of
    a few keys depending on the LinkedIn page format
    (``employee_count``, ``employees_count``, ``company_size`` as an int,
    or as a range string like ``"1001-5000 employees"``). We try each in
    that order; if it's a range string we use the lower bound (it's the
    one that determines whether we're under the GTM threshold).
    """
    if not isinstance(raw, dict):
        return None

    # Direct integer keys — most common shape.
    for key in ("employee_count", "employees_count", "employee_count_estimate"):
        v = raw.get(key)
        if isinstance(v, int) and v > 0:
            return v

    # company_size can be either an int or a range string.
    cs = raw.get("company_size")
    if isinstance(cs, int) and cs > 0:
        return cs
    if isinstance(cs, str):
        # "1001-5000 employees" → lower bound 1001
        digits = "".join(ch if ch.isdigit() else " " for ch in cs).split()
        if digits:
            try:
                return int(digits[0])
            except ValueError:
                return None
    return None


# ── Apify HTTP wrappers ───────────────────────────────────────────────────


async def _probe_company_size(
    company_url: str,
    *,
    api_token: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> tuple[int | None, float]:
    """Returns (employee_count_or_None, cost_usd).

    Best-effort. Falls back to ``None`` on any failure — the caller
    treats ``None`` as "below the threshold, use all_employees" because
    we'd rather over-scrape a tiny company than silently switch to GTM
    filtering for a 50k-person Fortune 500.
    """
    payload = {"identifier": company_url}
    url = f"{APIFY_API_BASE}/acts/{COMPANY_DETAIL_ACTOR}/run-sync-get-dataset-items?token={api_token}"

    async with semaphore:
        try:
            r = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.warning("company-detail probe failed for %s: %r", company_url, exc)
            return None, 0.0

    if r.status_code not in (200, 201):
        log.warning(
            "company-detail probe %s HTTP %d: %s",
            company_url, r.status_code, r.text[:200],
        )
        return None, 0.0

    try:
        items = r.json() if r.text else []
    except ValueError:
        return None, 0.0
    if not isinstance(items, list) or not items:
        return None, COST_COMPANY_DETAIL_USD
    first = items[0]
    return _extract_employee_count(first), COST_COMPANY_DETAIL_USD


async def _stream_employees(
    company_url: str,
    *,
    max_items: int,
    api_token: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> AsyncIterator[tuple[dict[str, Any] | None, float | None, str | None]]:
    """Yield (raw_employee_dict, accrued_cost_for_this_item, error_msg).

    The actor doesn't actually stream — it returns the full dataset in
    one ``run-sync-get-dataset-items`` response. We adapt that into an
    async iterator so the caller can incrementally update progress and
    apply backpressure if needed without holding the full list in a
    local variable longer than necessary.

    On HTTP failure yields a single ``(None, 0.0, "<error string>")``
    tuple and stops.
    """
    payload = {
        "identifier": company_url,
        "max_employees": max_items,
        "maxItems": max_items,
    }
    url = f"{APIFY_API_BASE}/acts/{EMPLOYEES_ACTOR}/run-sync-get-dataset-items?token={api_token}"

    async with semaphore:
        try:
            r = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            yield None, 0.0, f"apify_http_error: {exc!r}"
            return

    if r.status_code not in (200, 201):
        yield None, 0.0, f"apify_status_{r.status_code}"
        return

    try:
        items = r.json() if r.text else []
    except ValueError as exc:
        yield None, 0.0, f"apify_invalid_json: {exc!r}"
        return

    if not isinstance(items, list):
        yield None, 0.0, "apify_non_list_response"
        return

    for item in items:
        # Cost is accrued per item (we're billed even for items we
        # filter out downstream — that's why it's per raw item, not
        # per ScrapedEmployee that survives parsing).
        yield item, COST_EMPLOYEES_PER_ITEM_USD, None


# ── Progress writes ───────────────────────────────────────────────────────


async def _write_progress(
    onboarding_job_id: UUID,
    *,
    total: int,
    scraped: int,
) -> None:
    """UPDATE onboarding_jobs.progress with the latest counts.

    ``matched`` and ``new_persons`` always written as ``0`` here — they
    belong to the entity resolver in Wave A4. Using a JSONB literal keeps
    the SQL portable across asyncpg versions.
    """
    progress = {
        "total": total,
        "scraped": scraped,
        "matched": 0,
        "new_persons": 0,
    }
    sql = """
        UPDATE public.onboarding_jobs
        SET progress = $2::jsonb
        WHERE id = $1
    """
    try:
        async with db.acquire() as conn:
            await conn.execute(sql, onboarding_job_id, progress)
    except Exception as exc:  # noqa: BLE001 — progress is best-effort
        log.warning(
            "onboarding_jobs progress UPDATE failed (job=%s): %r",
            onboarding_job_id, exc,
        )


# ── Public entry point ────────────────────────────────────────────────────


async def scrape_team_for_account(
    account_id: UUID,
    company_url: str,
    strategy: StrategyLiteral,
    onboarding_job_id: UUID,
    *,
    max_employees: int = 500,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> TeamScrapeResult:
    """Scrape a rep's company team and stream progress to ``onboarding_jobs``.

    Parameters
    ----------
    account_id
        Tenant scope. Currently passed through for logging / progress
        attribution; the entity resolver in Wave A4 will use it to write
        ``account_team_members`` rows.
    company_url
        Canonical LinkedIn company URL — e.g.
        ``https://www.linkedin.com/company/nvidia/``. Used as the
        ``identifier`` to the Apimaestro actor.
    strategy
        ``"all_employees"`` to pull the full roster, ``"gtm_only"`` to
        force the GTM filter regardless of company size. The caller's
        choice is respected as the *minimum* selectivity — if it asked
        for ``all_employees`` but the probe says the company has
        ``>= GTM_STRATEGY_THRESHOLD`` employees, we auto-switch to
        ``gtm_only`` to keep cost bounded.
    onboarding_job_id
        Row in ``onboarding_jobs`` to UPDATE progress against.
    max_employees
        Hard cap on returned employees (post-filter). Default 500
        per the plan.
    api_token, client
        Test seams. Both default to live values (``APIFY_TOKEN`` env +
        a fresh ``httpx.AsyncClient``).

    Returns
    -------
    TeamScrapeResult
        Always returned — even on Apify failure. ``error`` carries the
        failure reason; ``cost_usd`` is ``None`` only when the call
        failed before any billable item was returned.
    """
    token = _resolve_token(api_token)
    owns_client = client is None
    cli = client or httpx.AsyncClient(timeout=600.0)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_API_CALLS)

    cost_usd = 0.0
    cost_charged = False  # any billable item came back?
    error: str | None = None

    try:
        # ── Step 1: probe company size and resolve effective strategy ─
        probed_count, probe_cost = await _probe_company_size(
            company_url,
            api_token=token,
            client=cli,
            semaphore=semaphore,
        )
        if probe_cost > 0:
            cost_usd += probe_cost
            cost_charged = True

        effective_strategy: StrategyLiteral = strategy
        if strategy == "all_employees" and probed_count is not None:
            if probed_count >= GTM_STRATEGY_THRESHOLD:
                log.info(
                    "auto-switching to gtm_only — probed %d employees >= threshold %d",
                    probed_count, GTM_STRATEGY_THRESHOLD,
                )
                effective_strategy = "gtm_only"

        # The expected total used in progress writes:
        # - all_employees: min(max_employees, probed) when known
        # - gtm_only:      max_employees (GTM headcount unknown until scrape)
        if effective_strategy == "all_employees" and probed_count is not None:
            expected_total = min(max_employees, probed_count)
        else:
            expected_total = max_employees

        # ── Step 2: stream employees, filter, accumulate, progress ────
        kept: list[ScrapedEmployee] = []
        seen_urls: set[str] = set()  # idempotency / dedupe within one scrape

        async for raw, item_cost, stream_err in _stream_employees(
            company_url,
            max_items=max_employees,
            api_token=token,
            client=cli,
            semaphore=semaphore,
        ):
            if stream_err is not None:
                error = stream_err
                break
            if item_cost is not None and item_cost > 0:
                cost_usd += item_cost
                cost_charged = True
            if raw is None:
                continue

            emp = _to_scraped_employee(raw)
            if emp is None:
                continue
            if emp.linkedin_url in seen_urls:
                continue

            if effective_strategy == "gtm_only":
                if not _is_gtm_employee(emp.headline, emp.current_title):
                    continue

            kept.append(emp)
            seen_urls.add(emp.linkedin_url)

            # Progress flush every PROGRESS_WRITE_INTERVAL kept items.
            if len(kept) % PROGRESS_WRITE_INTERVAL == 0:
                await _write_progress(
                    onboarding_job_id,
                    total=expected_total,
                    scraped=len(kept),
                )

            if len(kept) >= max_employees:
                break

        # Final progress flush — always, even if we stopped at a
        # multiple of PROGRESS_WRITE_INTERVAL (idempotent UPDATE).
        await _write_progress(
            onboarding_job_id,
            total=expected_total if expected_total > 0 else len(kept),
            scraped=len(kept),
        )

        return TeamScrapeResult(
            total_returned=len(kept),
            employees=kept,
            strategy_used=effective_strategy,
            cost_usd=round(cost_usd, 4) if cost_charged else None,
            error=error,
        )
    finally:
        if owns_client:
            await cli.aclose()


__all__ = [
    "GTM_KEYWORDS",
    "GTM_STRATEGY_THRESHOLD",
    "MAX_CONCURRENT_API_CALLS",
    "PROGRESS_WRITE_INTERVAL",
    "ScrapedEmployee",
    "TeamScrapeResult",
    "scrape_team_for_account",
]
