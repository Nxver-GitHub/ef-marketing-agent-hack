"""Unit tests for `credence.onboarding.cost`.

Covers ledger arithmetic, the JSONB round-trip used to embed costs in
``onboarding_jobs.progress``, and the Apify ``chargedEventCounts``
accumulator. No paid APIs touched — Apify responses are inline dicts
that mirror the contract documented in
``credence/enrichment/apify.py`` (see ``compute_run_cost_cents``).
"""
from __future__ import annotations

import pytest

from credence.onboarding.cost import (
    STAGE_COMPANY_ENRICHMENT,
    STAGE_REP_LOOKUP,
    STAGE_TEAM_SCRAPING,
    OnboardingCostLedger,
    track_apify_cost,
)


# ── total_dollars ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_total_dollars_rounds_correctly() -> None:
    ledger = OnboardingCostLedger(
        rep_lookup_cents=100,
        company_enrichment_cents=12000,
        team_scraping_cents=245,
    )
    # 100 + 12000 + 245 = 12345 cents = $123.45
    assert ledger.total_dollars() == 123.45


@pytest.mark.unit
def test_total_dollars_zero_ledger() -> None:
    assert OnboardingCostLedger().total_dollars() == 0.0


@pytest.mark.unit
def test_total_dollars_two_decimal_rounding() -> None:
    # 1¢ in two of three slots rounds cleanly to two dp.
    ledger = OnboardingCostLedger(rep_lookup_cents=1, team_scraping_cents=2)
    assert ledger.total_dollars() == 0.03


# ── to_progress_dict ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_to_progress_dict_shape_matches_spec() -> None:
    ledger = OnboardingCostLedger(
        rep_lookup_cents=10,
        company_enrichment_cents=600,
        team_scraping_cents=300,
    )
    out = ledger.to_progress_dict()
    assert set(out.keys()) == {"cost"}
    cost = out["cost"]
    assert set(cost.keys()) == {
        "rep_lookup_cents",
        "company_enrichment_cents",
        "team_scraping_cents",
        "total_usd",
    }
    assert cost["rep_lookup_cents"] == 10
    assert cost["company_enrichment_cents"] == 600
    assert cost["team_scraping_cents"] == 300
    assert cost["total_usd"] == 9.10
    # Field types must JSON-serialize cleanly into JSONB.
    assert isinstance(cost["rep_lookup_cents"], int)
    assert isinstance(cost["total_usd"], float)


# ── from_progress_dict ────────────────────────────────────────────────────


@pytest.mark.unit
def test_from_progress_dict_round_trips() -> None:
    original = OnboardingCostLedger(
        rep_lookup_cents=42,
        company_enrichment_cents=1234,
        team_scraping_cents=5678,
    )
    rebuilt = OnboardingCostLedger.from_progress_dict(original.to_progress_dict())
    assert rebuilt == original


@pytest.mark.unit
def test_from_progress_dict_defaults_missing_fields_to_zero() -> None:
    # Missing 'cost' key entirely (fresh onboarding_jobs.progress = '{}').
    assert OnboardingCostLedger.from_progress_dict({}) == OnboardingCostLedger()

    # Partial 'cost' dict — only one stage populated.
    partial = OnboardingCostLedger.from_progress_dict({"cost": {"rep_lookup_cents": 7}})
    assert partial.rep_lookup_cents == 7
    assert partial.company_enrichment_cents == 0
    assert partial.team_scraping_cents == 0


@pytest.mark.unit
def test_from_progress_dict_tolerates_other_progress_keys() -> None:
    # onboarding_jobs.progress also carries scrape progress
    # (CUSTOMER_ONBOARDING_PLAN.md Stage 2). Those keys must be ignored.
    payload = {
        "total": 150,
        "scraped": 89,
        "matched": 50,
        "new_persons": 39,
        "cost": {
            "rep_lookup_cents": 1,
            "company_enrichment_cents": 6,
            "team_scraping_cents": 150,
            "total_usd": 1.57,
        },
    }
    ledger = OnboardingCostLedger.from_progress_dict(payload)
    assert ledger.rep_lookup_cents == 1
    assert ledger.company_enrichment_cents == 6
    assert ledger.team_scraping_cents == 150


# ── track_apify_cost ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_track_apify_cost_rep_lookup_increments_rep_lookup_cents() -> None:
    """Rep lookup is one Apify ``full-profile`` event = 0.8¢ → 1¢ rounded up."""
    ledger = OnboardingCostLedger()
    run = {"chargedEventCounts": {"full-profile": 1}}
    track_apify_cost(ledger, run, STAGE_REP_LOOKUP)
    assert ledger.rep_lookup_cents == 1
    assert ledger.company_enrichment_cents == 0
    assert ledger.team_scraping_cents == 0


@pytest.mark.unit
def test_track_apify_cost_team_scraping_increments_team_scraping_cents() -> None:
    """150 short-profile events = 60¢ exact (no round-up needed)."""
    ledger = OnboardingCostLedger()
    run = {"chargedEventCounts": {"short-profile": 150}}
    track_apify_cost(ledger, run, STAGE_TEAM_SCRAPING)
    # 150 * 0.4¢ = 60¢
    assert ledger.team_scraping_cents == 60
    assert ledger.rep_lookup_cents == 0
    assert ledger.company_enrichment_cents == 0


@pytest.mark.unit
def test_track_apify_cost_company_enrichment_uses_apimaestro_rates() -> None:
    """Apimaestro flat per-item key — 100 employee items = $1.00 = 100¢."""
    ledger = OnboardingCostLedger()
    run = {"chargedEventCounts": {"apimaestro-employee-item": 100}}
    track_apify_cost(ledger, run, STAGE_COMPANY_ENRICHMENT)
    assert ledger.company_enrichment_cents == 100


@pytest.mark.unit
def test_track_apify_cost_noop_when_charged_event_counts_missing() -> None:
    ledger = OnboardingCostLedger(rep_lookup_cents=5)
    track_apify_cost(ledger, {}, STAGE_REP_LOOKUP)
    track_apify_cost(ledger, {"otherKey": 1}, STAGE_REP_LOOKUP)
    track_apify_cost(ledger, {"chargedEventCounts": None}, STAGE_REP_LOOKUP)
    track_apify_cost(ledger, {"chargedEventCounts": "broken"}, STAGE_REP_LOOKUP)
    # No mutation across any of the malformed inputs.
    assert ledger.rep_lookup_cents == 5
    assert ledger.company_enrichment_cents == 0
    assert ledger.team_scraping_cents == 0


@pytest.mark.unit
def test_track_apify_cost_noop_for_unknown_event_keys() -> None:
    """Unknown event keys (rate = 0) should not mutate the ledger."""
    ledger = OnboardingCostLedger()
    run = {"chargedEventCounts": {"actor-start": 1, "totally-unknown-event": 99}}
    track_apify_cost(ledger, run, STAGE_TEAM_SCRAPING)
    assert ledger.team_scraping_cents == 0


@pytest.mark.unit
def test_track_apify_cost_cumulative_across_calls() -> None:
    """Multiple Apify runs in the same stage must accumulate."""
    ledger = OnboardingCostLedger()

    # First batch: 50 short-profile = 20¢ exact
    track_apify_cost(
        ledger,
        {"chargedEventCounts": {"short-profile": 50}},
        STAGE_TEAM_SCRAPING,
    )
    assert ledger.team_scraping_cents == 20

    # Second batch: 100 short-profile = 40¢ → cumulative 60¢
    track_apify_cost(
        ledger,
        {"chargedEventCounts": {"short-profile": 100}},
        STAGE_TEAM_SCRAPING,
    )
    assert ledger.team_scraping_cents == 60

    # Mixed-event batch: 1 full-profile-with-email (1.2¢ → 2¢ rounded up)
    # + 5 short-profile (2¢) = 4¢ → cumulative 64¢
    track_apify_cost(
        ledger,
        {
            "chargedEventCounts": {
                "full-profile-with-email": 1,
                "short-profile": 5,
            }
        },
        STAGE_TEAM_SCRAPING,
    )
    assert ledger.team_scraping_cents == 64

    # Other stages still untouched.
    assert ledger.rep_lookup_cents == 0
    assert ledger.company_enrichment_cents == 0


@pytest.mark.unit
def test_track_apify_cost_rejects_unknown_stage() -> None:
    ledger = OnboardingCostLedger()
    with pytest.raises(ValueError, match="Unknown onboarding stage"):
        track_apify_cost(
            ledger,
            {"chargedEventCounts": {"full-profile": 1}},
            "stage_does_not_exist",
        )


@pytest.mark.unit
def test_track_apify_cost_ignores_zero_and_negative_counts() -> None:
    ledger = OnboardingCostLedger()
    run = {
        "chargedEventCounts": {
            "full-profile": 0,
            "short-profile": -5,
            "full-profile-with-email": 2,  # 2 * 1.2¢ = 2.4¢ → 3¢
        }
    }
    track_apify_cost(ledger, run, STAGE_REP_LOOKUP)
    assert ledger.rep_lookup_cents == 3


@pytest.mark.unit
def test_track_apify_cost_ignores_non_int_counts() -> None:
    ledger = OnboardingCostLedger()
    run = {
        "chargedEventCounts": {
            "full-profile": "10",      # string — ignored
            "short-profile": 1.5,      # float — ignored
            "full-profile-with-email": True,  # bool — ignored (subclass of int)
        }
    }
    track_apify_cost(ledger, run, STAGE_REP_LOOKUP)
    assert ledger.rep_lookup_cents == 0
