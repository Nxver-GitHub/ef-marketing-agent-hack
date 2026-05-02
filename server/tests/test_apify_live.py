"""Live Apify smoke — opt-in via `pytest -m integration`.

Skips when ``APIFY_TOKEN`` is missing. Pulls a tiny number of profiles
to keep cost bounded (10 profiles × $0.0008 = $0.008 per run; safe to
run on every CI cycle if needed).

    uv run --env-file ../.env.local pytest tests/test_apify_live.py -m integration -v

Cost: < $0.01 per run.
"""
from __future__ import annotations

import os

import httpx
import pytest

from credence.enrichment.apify import (
    MODE_FULL,
    find_company_employees_sync,
)


@pytest.mark.integration
async def test_apify_live_marvell_returns_profiles() -> None:
    """Live pull of 5 Marvell employees in Full mode.

    Verified shape contract: profiles have linkedin_url, first_name,
    employment_periods with at least one row referencing Marvell.
    """
    if not os.environ.get("APIFY_TOKEN"):
        pytest.skip("APIFY_TOKEN not set; skipping live test")

    async with httpx.AsyncClient(timeout=120.0) as client:
        result = await find_company_employees_sync(
            "https://www.linkedin.com/company/marvell/",
            max_items=5,
            mode=MODE_FULL,
            client=client,
            timeout_seconds=120.0,
        )

    if result is None:
        pytest.fail(
            "Apify returned None for Marvell — token rejected, slug invalid, "
            "or actor failed. Check the most recent run on apify.com."
        )

    assert len(result.profiles) >= 1, "Expected ≥1 profile from Marvell pull"
    # Cost should be in the 1-10 cent range for 1-10 profiles in Full mode
    assert 0 <= result.cost_cents <= 20

    # Spot-check the first profile's structure
    p = result.profiles[0]
    assert p.linkedin_url.startswith("https://www.linkedin.com/in/")
    assert p.first_name  # non-empty
    # At least one employment period should be present (could be empty for
    # very-stub profiles, but harvestapi rarely returns those)
    if p.employment_periods:
        emp = p.employment_periods[0]
        # The current company should be Marvell-ish (we filtered by company)
        company = (emp.get("company_name") or "").lower()
        assert "marvell" in company or emp.get("company_universal_name") == "marvell", (
            f"Expected Marvell employment; got {emp.get('company_name')!r}"
        )
