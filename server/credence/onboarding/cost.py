"""Per-stage cost ledger for the customer onboarding pipeline.

The onboarding pipeline (CUSTOMER_ONBOARDING_PLAN.md, "The Four Stages")
spends paid-API credit across three distinct surfaces:

  rep_lookup          — Stage 0: Apify LinkedIn People Search (~$0.01/lookup)
  company_enrichment  — Stage 1: Apify profile detail / Firecrawl pulls
  team_scraping       — Stage 2: Apify LinkedIn Company Employee Scraper

This module owns the ledger that aggregates those charges per onboarding
job so the cost surfaces in `onboarding_jobs.progress` (a JSONB column)
and so callers can trip a budget check before dispatching the next stage.

The Apify cost contract (per ``chargedEventCounts``) is shared with
``credence.enrichment.apify`` (harvestapi event keys: short-profile /
full-profile / full-profile-with-email) and
``credence.enrichment.apify_apimaestro`` (per-item rates for the
employees-listing + profile-detail actors). We reuse those constants
as the single source of truth — no duplicated pricing tables here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping

from ..enrichment.apify import _PROFILE_COST_CENTS as _HARVESTAPI_EVENT_COST_CENTS
from ..enrichment.apify_apimaestro import (
    COST_EMPLOYEES_PER_ITEM_USD,
    COST_PROFILE_PER_ITEM_USD,
)

# ── Stage identifiers ─────────────────────────────────────────────────────

STAGE_REP_LOOKUP: Final[str] = "rep_lookup"
STAGE_COMPANY_ENRICHMENT: Final[str] = "company_enrichment"
STAGE_TEAM_SCRAPING: Final[str] = "team_scraping"

_VALID_STAGES: Final[frozenset[str]] = frozenset(
    {STAGE_REP_LOOKUP, STAGE_COMPANY_ENRICHMENT, STAGE_TEAM_SCRAPING}
)

# ── Per-event cost table (cents, indexed by Apify chargedEventCounts key) ──
#
# Apify v2 returns ``chargedEventCounts`` on each run-detail response — a
# dict mapping event-name → integer count. We translate that to cents
# using the per-event rates published by the actor. Where an actor doesn't
# emit per-event keys (apimaestro charges flatly per dataset item), we
# expose synthetic keys ("apimaestro-employee-item",
# "apimaestro-profile-item") that callers can pre-populate from item
# counts before calling ``track_apify_cost``.
_EVENT_COST_CENTS: Final[dict[str, float]] = {
    # harvestapi/linkedin-company-employees + linkedin-profile-scraper
    # (verified live 2026-04-30; see apify.py:_PROFILE_COST_CENTS)
    **_HARVESTAPI_EVENT_COST_CENTS,
    # apimaestro flat per-item pricing (see apify_apimaestro.py)
    "apimaestro-employee-item": COST_EMPLOYEES_PER_ITEM_USD * 100,  # 1.0¢
    "apimaestro-profile-item": COST_PROFILE_PER_ITEM_USD * 100,     # 0.5¢
}


# ── Ledger ────────────────────────────────────────────────────────────────


@dataclass
class OnboardingCostLedger:
    """Mutable cost accumulator for a single onboarding job.

    One ledger per ``onboarding_jobs`` row. Stages mutate their own
    field as paid-API responses come back (see ``track_apify_cost``).
    The ledger is serialized into the job's ``progress`` JSONB at
    each stage boundary so the frontend status poller can show
    cumulative spend.

    Not thread-safe: this lives inside a single asyncio task.
    Asyncio is single-threaded so no lock is required, but callers
    must NOT share a ledger reference across concurrent tasks.
    """

    rep_lookup_cents: int = 0
    company_enrichment_cents: int = 0
    team_scraping_cents: int = 0

    def total_dollars(self) -> float:
        """Return total spend across all stages in USD, rounded to 2dp."""
        total_cents = (
            self.rep_lookup_cents
            + self.company_enrichment_cents
            + self.team_scraping_cents
        )
        return round(total_cents / 100, 2)

    def to_progress_dict(self) -> dict[str, Any]:
        """Serialize to the shape embedded in ``onboarding_jobs.progress``.

        The wire format is intentionally nested under ``cost`` so the
        same JSONB blob can also carry stage-specific progress keys
        (``total``, ``scraped``, ``matched``, ``new_persons`` —
        see CUSTOMER_ONBOARDING_PLAN.md Stage 2 §"Update progress").
        """
        return {
            "cost": {
                "rep_lookup_cents": self.rep_lookup_cents,
                "company_enrichment_cents": self.company_enrichment_cents,
                "team_scraping_cents": self.team_scraping_cents,
                "total_usd": self.total_dollars(),
            }
        }

    @classmethod
    def from_progress_dict(cls, d: Mapping[str, Any]) -> "OnboardingCostLedger":
        """Inverse of ``to_progress_dict``. Defaults missing fields to 0.

        Tolerant of missing ``cost`` key and of partial fields — a
        freshly-created onboarding job has ``progress = '{}'`` (see
        the schema in CUSTOMER_ONBOARDING_PLAN.md §"Schema Additions"),
        so the no-key path must succeed cleanly.
        """
        cost = d.get("cost") if isinstance(d, Mapping) else None
        if not isinstance(cost, Mapping):
            cost = {}
        return cls(
            rep_lookup_cents=_coerce_int(cost.get("rep_lookup_cents")),
            company_enrichment_cents=_coerce_int(cost.get("company_enrichment_cents")),
            team_scraping_cents=_coerce_int(cost.get("team_scraping_cents")),
        )


# ── Apify accumulator ─────────────────────────────────────────────────────


def track_apify_cost(
    ledger: OnboardingCostLedger,
    run_response: Mapping[str, Any],
    stage: str,
) -> None:
    """Increment the ledger field for ``stage`` from an Apify run response.

    Reads ``run_response['chargedEventCounts']`` (Apify v2 contract) and
    multiplies each event count by its per-event rate from the actor's
    pricing schedule (``_EVENT_COST_CENTS``). The total is rounded UP to
    the nearest cent (parity with ``apify.compute_run_cost_cents`` — the
    budget tracker must never under-count spend). If
    ``chargedEventCounts`` is missing, malformed, or empty, this is a
    no-op and the ledger is unchanged.

    Mutates ``ledger`` in place. NOT safe for concurrent use across
    asyncio tasks — call sites must hold the ledger reference within
    a single task. Asyncio's single-threaded scheduler makes that the
    natural pattern; we don't introduce a lock for the same reason
    that ``OnboardingCostLedger`` doesn't carry one.

    Raises:
        ValueError: if ``stage`` is not one of the three known stages.
    """
    if stage not in _VALID_STAGES:
        raise ValueError(
            f"Unknown onboarding stage {stage!r}; "
            f"expected one of {sorted(_VALID_STAGES)}"
        )

    if not isinstance(run_response, Mapping):
        return

    charged = run_response.get("chargedEventCounts")
    if not isinstance(charged, Mapping):
        return

    cents = 0.0
    for event, count in charged.items():
        if not isinstance(event, str):
            continue
        # Reject bool explicitly: bool is a subclass of int in Python,
        # so True would otherwise be treated as count=1.
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            continue
        rate = _EVENT_COST_CENTS.get(event, 0.0)
        cents += rate * count

    if cents <= 0:
        return

    # Round up to the nearest cent — same convention as
    # apify.compute_run_cost_cents (budget never under-counts).
    delta = int(cents + 0.999)

    if stage == STAGE_REP_LOOKUP:
        ledger.rep_lookup_cents += delta
    elif stage == STAGE_COMPANY_ENRICHMENT:
        ledger.company_enrichment_cents += delta
    else:  # STAGE_TEAM_SCRAPING — exhaustively checked above
        ledger.team_scraping_cents += delta


# ── Internals ─────────────────────────────────────────────────────────────


def _coerce_int(value: Any) -> int:
    """Best-effort int coercion for JSONB round-trip. Defaults to 0."""
    if isinstance(value, bool):
        # bool is a subclass of int — exclude it to avoid silent True→1.
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


__all__ = [
    "OnboardingCostLedger",
    "STAGE_COMPANY_ENRICHMENT",
    "STAGE_REP_LOOKUP",
    "STAGE_TEAM_SCRAPING",
    "track_apify_cost",
]
