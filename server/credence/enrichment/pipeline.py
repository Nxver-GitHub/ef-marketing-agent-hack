"""Per-company enrichment orchestrator — Tier 1 + Tier 2.

Per user direction (2026-04-30 → 05-01):

- **EDGAR removed entirely.** Apify covers C-suite + below 500-deep per
  company. The previously-shipped ``edgar.py`` module + tests deleted.
- **PDL removed from the bulk pipeline.** Apify's company-employees
  Full+email mode returns the same LinkedIn-derived data PDL would, in
  one bulk call, at ~1/35th the cost. PDL stays wired in
  ``/enrich/{prospect_id}`` for per-prospect on-demand work (Phase A.6
  manager extraction).
- **Tier 2 gated to top 10%** — LinkedIn posts and news mentions only
  run for prospects promoted to ``persons.enrichment_tier=3`` by
  ``prioritize.promote_top_decile``.

## The 8 steps

```
TIER 1 — runs on every company / prospect

  1. Apify Full+email           → discovery + deep enrichment (~$6/co at 500)
  2. Normalize                  → canonicalize across sources
  3. Apollo email gap-fill      → null-email subset only (~3¢/match)
  4. Writer (persons + emp)     → idempotent UPSERT to Supabase
  5. Company-site Firecrawl     → /leadership + /press + /investor (~$0.10/co)

TIER 2 — runs on top 10% only (after step 4 populates seniority_score)

  6. prioritize.promote_top_decile → mark top 10% as enrichment_tier=3
  7. Apify posts on Tier-3      → ~$150 across 2k prospects
  8. News mentions on Tier-3    → ~$1,000 across 2k prospects (Parallel)
```

## What the pipeline does NOT do

- **Persist Tier-2 signals** — `linkedin_post`, `news_mention`,
  `formal_recognition`, `github_profile` rows need a `person_signals`
  table that doesn't exist yet. For this iteration the pipeline collects
  them in ``CompanyEnrichmentResult`` so the caller can verify the data,
  but doesn't write them to DB. Next iteration: schema migration +
  writer extension.
- **GitHub username discovery** — github.py exists but we don't have
  usernames. Pipeline includes a heuristic (search Apify's
  ``organizations[]`` for "github.com/<handle>" patterns) but most
  prospects fall through.
- **Recognition scrape** — that's per-organization (IEEE/ACM/NAE) not
  per-company. Run as a separate periodic job, not in this pipeline.

## Cost per company (default config)

```
Apify Full+email × 500 = $6.00
Apollo gap-fill (~30%)  = ~$4.50
Company sites           = ~$0.10
GitHub                  = $0
─────────────────────
TIER 1 per company      ≈ $10.60
```

For 60 companies: ~$640 just Tier 1.

Tier 2 is gated to top 10% of the entire population (not per-company),
so it runs ONCE after all 60 bulk runs complete — not from inside
``enrich_company``. Caller invokes ``run_tier_2(account_id)`` separately.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx

from .apify import (
    MODE_FULL,
    MODE_FULL_EMAIL,
    EnrichmentResult as ApifyEnrichmentResult,
    ScrapeMode,
    find_company_employees_async,
)
from .apify_posts import (
    EnrichmentResult as ApifyPostsResult,
    scrape_profile_posts,
)
from .apollo import ProspectRef as ApolloProspectRef, enrich as apollo_enrich
from .company_site import CompanySiteSignals, scrape_company_site
from .github import GitHubProfile, enrich_github_profile
from .news import EnrichmentResult as NewsResult, find_news_mentions
from .normalizer import (
    CanonicalPerson,
    from_apify,
    merge_records,
)
from .prioritize import PriorityProspect, promote_top_decile
from .writer import WriteResult, write_canonical_persons

logger = logging.getLogger(__name__)


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass
class CompanyEnrichmentResult:
    """Roll-up of one ``enrich_company`` invocation (Tier 1 only).

    Tier-2 signals don't show up here — they materialize during a
    separate ``run_tier_2`` call after all companies have been bulk-
    enriched (so seniority_score is populated for every prospect).
    """

    company_url: str
    company_name: str

    # Tier 1 — per-prospect deep
    apify_profiles_pulled: int = 0
    apify_cost_cents: int = 0
    apollo_emails_filled: int = 0
    apollo_cost_cents: int = 0
    canonical_persons: int = 0
    write_result: WriteResult | None = None

    # Tier 1 — per-company
    company_site_pages_scraped: int = 0
    company_site_cost_cents: int = 0
    company_site_signals: list[CompanySiteSignals] = field(default_factory=list)

    # Tier 1 — per-prospect github
    github_profiles_fetched: int = 0
    github_profiles: list[GitHubProfile] = field(default_factory=list)

    errors: list[str] = field(default_factory=list)

    @property
    def total_cost_cents(self) -> int:
        return self.apify_cost_cents + self.apollo_cost_cents + self.company_site_cost_cents


@dataclass
class Tier2Result:
    """Roll-up of one ``run_tier_2`` invocation across the whole tenant."""

    promoted_count: int = 0
    posts_profiles_scraped: int = 0
    posts_total: int = 0
    posts_cost_cents: int = 0

    news_prospects_processed: int = 0
    news_mentions_total: int = 0
    news_cost_cents: int = 0

    errors: list[str] = field(default_factory=list)

    @property
    def total_cost_cents(self) -> int:
        return self.posts_cost_cents + self.news_cost_cents


# ─── Helpers ────────────────────────────────────────────────────────────────


def _slug_from_url(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    parts = [p for p in url.split("/") if p]
    if "company" in parts:
        i = parts.index("company")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def _extract_github_username(person: CanonicalPerson) -> str | None:
    """Best-effort GitHub username discovery from Apify's organizations[].

    Apify sometimes surfaces ``"organizations": [{"name": "GitHub: handle"}]``
    or includes the URL in social fields. Returns the first plausible
    handle found, or None.
    """
    for org in person.organizations or []:
        if not isinstance(org, dict):
            continue
        name = (org.get("name") or "").strip()
        url = (org.get("url") or "").strip()
        # Pattern 1: name = "GitHub" + url contains github.com/<handle>
        if "github" in name.lower() and "github.com/" in url.lower():
            handle = url.lower().split("github.com/", 1)[1].rstrip("/").split("/")[0]
            if handle:
                return handle
        # Pattern 2: name = "GitHub: handle"
        if name.lower().startswith("github:"):
            handle = name.split(":", 1)[1].strip()
            if handle:
                return handle
    return None


# ─── Tier 1: enrich_company ────────────────────────────────────────────────


async def enrich_company(
    company_url: str,
    *,
    account_id: UUID,
    company_name: str | None = None,
    max_persons: int = 500,
    mode: ScrapeMode = MODE_FULL_EMAIL,  # type: ignore[assignment]
    do_apollo_email_gapfill: bool = True,
    apollo_max_cost_cents: int = 100,
    leadership_url: str | None = None,
    press_url: str | None = None,
    investor_url: str | None = None,
    do_github: bool = True,
    client: httpx.AsyncClient | None = None,
) -> CompanyEnrichmentResult:
    """Tier-1 enrichment for one company. Idempotent.

    Steps 1-5 of the 8-step plan. Tier-2 (steps 6-8) runs separately
    via ``run_tier_2(account_id)`` after all companies are bulk-enriched.
    """
    derived_name = company_name or _slug_from_url(company_url) or company_url
    result = CompanyEnrichmentResult(company_url=company_url, company_name=derived_name)

    # ─── Step 1: Apify ──────────────────────────────────────────────────
    logger.info(
        "pipeline.enrich_company: Apify pull for %s (max=%d, mode=%s)",
        derived_name, max_persons, mode,
    )
    apify_result: ApifyEnrichmentResult | None = await find_company_employees_async(
        company_url, max_items=max_persons, mode=mode, client=client,
    )
    if apify_result is None:
        result.errors.append("apify pull returned None — run failed or token missing")
        return result

    result.apify_profiles_pulled = len(apify_result.profiles)
    result.apify_cost_cents = apify_result.cost_cents

    # ─── Step 2: Normalize ──────────────────────────────────────────────
    canonical_persons = merge_records({"apify": apify_result.profiles})
    result.canonical_persons = len(canonical_persons)

    # ─── Step 3: Apollo email gap-fill ──────────────────────────────────
    if do_apollo_email_gapfill:
        await _apollo_email_gapfill(
            canonical_persons, result=result,
            apollo_max_cost_cents=apollo_max_cost_cents, client=client,
        )

    # ─── Step 4: Writer (persons + employment + education) ──────────────
    try:
        write_result = await write_canonical_persons(
            canonical_persons,
            account_id=account_id,
            primary_company_name=derived_name,
            primary_company_employee_count=None,
        )
        result.write_result = write_result
        if write_result.errors:
            result.errors.extend(write_result.errors[:5])
    except Exception as exc:  # noqa: BLE001 — partial-results contract
        result.errors.append(f"writer raised: {exc}")
        logger.exception("pipeline: writer failed for %s", derived_name)

    # ─── Step 5a: Company-site Firecrawl ────────────────────────────────
    if leadership_url or press_url or investor_url:
        try:
            site_results = await scrape_company_site(
                company_url,
                leadership_url=leadership_url,
                press_url=press_url,
                investor_url=investor_url,
                client=client,
            )
            result.company_site_signals = site_results
            result.company_site_pages_scraped = len(site_results)
            result.company_site_cost_cents = sum(s.cost_cents for s in site_results)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"company_site raised: {exc}")
            logger.exception("pipeline: company_site failed for %s", derived_name)

    # ─── Step 5b: GitHub for engineering prospects ──────────────────────
    if do_github:
        await _github_enrich(canonical_persons, result=result, client=client)

    return result


async def _apollo_email_gapfill(
    canonical_persons: list[CanonicalPerson],
    *,
    result: CompanyEnrichmentResult,
    apollo_max_cost_cents: int,
    client: httpx.AsyncClient | None,
) -> None:
    null_email_persons = [
        p for p in canonical_persons
        if not p.email and p.canonical_name and p.first_name and p.last_name
    ]
    if not null_email_persons:
        return
    logger.info(
        "pipeline: Apollo gap-fill on %d/%d prospects with email=None",
        len(null_email_persons), len(canonical_persons),
    )
    for person in null_email_persons:
        try:
            ref = ApolloProspectRef(
                person_id=person.linkedin_id or person.canonical_name,
                canonical_name=person.canonical_name,
                organization_name=person.current_company_name,
                linkedin_url=person.linkedin_url,
            )
            apollo_result = await apollo_enrich(
                ref, client=client, max_cost_cents=apollo_max_cost_cents
            )
            if apollo_result is None:
                continue
            email = apollo_result.fields.get("email")
            email_status = apollo_result.fields.get("email_status")
            if email and "@" in email:
                person.email = email
                person.email_status = email_status
                person.sources["email"] = "apollo"
                person.sources["email_status"] = "apollo"
                result.apollo_emails_filled += 1
                result.apollo_cost_cents += apollo_result.cost_cents
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"apollo({person.canonical_name}): {exc}")
            logger.warning("pipeline: Apollo gap-fill failed for %s: %s",
                           person.canonical_name, exc)


async def _github_enrich(
    canonical_persons: list[CanonicalPerson],
    *,
    result: CompanyEnrichmentResult,
    client: httpx.AsyncClient | None,
) -> None:
    """Fire GitHub enrichment when (a) functional_domain hints engineering,
    AND (b) we can heuristically derive a username from Apify org data.

    Most prospects fall through silently — GitHub username discovery is
    unsolved at scale. Module is forward-facing.
    """
    eng_domains = {"hardware_engineering", "software_engineering", "research"}
    candidates = [
        p for p in canonical_persons
        if p.current_functional_domain in eng_domains
    ]
    if not candidates:
        return
    for person in candidates:
        username = _extract_github_username(person)
        if not username:
            continue
        try:
            profile = await enrich_github_profile(username, client=client)
            if profile is not None:
                result.github_profiles.append(profile)
                result.github_profiles_fetched += 1
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"github({username}): {exc}")
            logger.warning("pipeline: GitHub enrich failed for %s: %s", username, exc)


# ─── Tier 2: run_tier_2 ────────────────────────────────────────────────────


async def run_tier_2(
    account_id: UUID,
    *,
    percentile: int = 10,
    min_seniority_score: int = 50,
    do_posts: bool = True,
    do_news: bool = True,
    max_posts_per_profile: int = 50,
    max_news_per_prospect: int = 5,
    posts_batch_size: int = 100,
    client: httpx.AsyncClient | None = None,
) -> Tier2Result:
    """Tier-2 enrichment across the whole tenant.

    Runs ONCE after all companies have been bulk-enriched. Steps 6-8
    of the 8-step plan:

      6. ``prioritize.promote_top_decile`` — mark top 10% by seniority
      7. ``apify_posts.scrape_profile_posts`` — LinkedIn posts (batched)
      8. ``news.find_news_mentions`` — per-prospect news (Parallel)

    Tier-2 signals are returned in ``Tier2Result`` but NOT yet persisted
    to a DB table — that needs a ``person_signals`` migration. For
    this iteration callers can serialize the result for offline analysis
    or feed it to a downstream signals writer once that schema lands.
    """
    result = Tier2Result()

    # ─── Step 6: Promote top decile ──────────────────────────────────────
    prospects, n_promoted = await promote_top_decile(
        account_id, percentile=percentile, min_seniority_score=min_seniority_score,
    )
    result.promoted_count = n_promoted
    if not prospects:
        return result

    logger.info(
        "pipeline.run_tier_2: %d prospects in top %d%% (newly promoted: %d)",
        len(prospects), percentile, n_promoted,
    )

    # ─── Step 7: LinkedIn posts (batched) ────────────────────────────────
    if do_posts:
        with_linkedin = [p for p in prospects if p.linkedin_url]
        for batch_start in range(0, len(with_linkedin), posts_batch_size):
            batch = with_linkedin[batch_start : batch_start + posts_batch_size]
            urls = [str(p.linkedin_url) for p in batch if p.linkedin_url]
            if not urls:
                continue
            try:
                posts_result: ApifyPostsResult | None = await scrape_profile_posts(
                    urls,
                    max_posts_per_profile=max_posts_per_profile,
                    client=client,
                )
                if posts_result is not None:
                    result.posts_profiles_scraped += len(posts_result.by_profile)
                    result.posts_total += posts_result.total_posts
                    result.posts_cost_cents += posts_result.cost_cents
            except Exception as exc:  # noqa: BLE001
                result.errors.append(
                    f"apify_posts batch {batch_start}: {exc}"
                )
                logger.warning(
                    "pipeline.run_tier_2: posts batch %d failed: %s",
                    batch_start, exc,
                )

    # ─── Step 8: News mentions (per-prospect) ────────────────────────────
    if do_news:
        for person in prospects:
            try:
                news_result: NewsResult | None = await find_news_mentions(
                    person.canonical_name,
                    company_name=person.current_company_name,
                    max_articles=max_news_per_prospect,
                    client=client,
                )
                if news_result is not None:
                    result.news_prospects_processed += 1
                    result.news_mentions_total += len(news_result.mentions)
                    result.news_cost_cents += news_result.cost_cents
            except Exception as exc:  # noqa: BLE001
                result.errors.append(
                    f"news({person.canonical_name}): {exc}"
                )
                logger.warning(
                    "pipeline.run_tier_2: news failed for %s: %s",
                    person.canonical_name, exc,
                )

    return result


__all__ = [
    "CompanyEnrichmentResult",
    "Tier2Result",
    "enrich_company",
    "run_tier_2",
]
