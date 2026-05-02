"""Live-API smoke test for PDL enrichment — opt-in via `pytest -m integration`.

Skipped by default. Auto-skips when `PDL_API_KEY` is not set in env.

    uv run --env-file ../.env.local pytest tests/test_pdl_live.py -m integration -v

Cost: 1 PDL credit per run (~28¢ at Pro tier).
"""
from __future__ import annotations

import os

import httpx
import pytest

from credence.enrichment.pdl import (
    PDL_ENRICH_CREDIT_CENTS,
    ProspectRef,
    enrich,
)


@pytest.mark.integration
async def test_pdl_live_match_for_well_known_executive() -> None:
    """Live GET to PDL /v5/person/enrich for a known public figure.

    Uses a LinkedIn URL identifier rather than name+company. PDL's
    `/person/enrich` matcher is conservative — without a high-precision
    identifier (linkedin_url, email, or pdl_id), even unambiguous queries
    like "Tim Cook" + "Apple" return 404 because too many name-collisions
    exist in their corpus. Verified 2026-04-30 against the live API:
    `name + company` returns 404 even at min_likelihood=2; LinkedIn URL
    returns likelihood=9 immediately. Document this in
    `credence.enrichment.pdl` for prospect-level callers.
    """
    if not os.environ.get("PDL_API_KEY"):
        pytest.skip("PDL_API_KEY not set; skipping live test")

    # Satya Nadella's verified LinkedIn URL — confirmed live to return
    # likelihood=9, full_name="satya nadella", with employment periods.
    person = ProspectRef(
        person_id="test:satya",
        canonical_name="Satya Nadella",
        organization_name="Microsoft",
        linkedin_url="https://www.linkedin.com/in/satyanadella",
    )

    async with httpx.AsyncClient() as client:
        result = await enrich(person, client=client, max_cost_cents=100)

    if result is None:
        pytest.fail(
            "PDL /person/enrich returned no result for Satya Nadella "
            "(LinkedIn). Either PDL's matcher regressed, the API key has "
            "insufficient permissions, or the request payload schema drifted."
        )

    assert result.fields.get("pdl_person_id"), "expected a PDL person_id"
    assert result.cost_cents == PDL_ENRICH_CREDIT_CENTS
    assert 0.0 <= result.confidence <= 1.0
    # Satya should have ≥ 1 employment period (Microsoft at minimum)
    assert len(result.fields.get("employment_periods", [])) >= 1


@pytest.mark.integration
async def test_pdl_live_no_match_returns_none() -> None:
    """A clearly-impossible name should not produce a fabricated match."""
    if not os.environ.get("PDL_API_KEY"):
        pytest.skip("PDL_API_KEY not set; skipping live test")

    person = ProspectRef(
        person_id="test:ghost",
        canonical_name="Zzzzzzz Yyyyyyyy",
        organization_name="ImaginaryCorp 99999",
    )

    async with httpx.AsyncClient() as client:
        result = await enrich(person, client=client, max_cost_cents=100)

    # Either None (no match) or a result whose pdl_person_id is empty.
    if result is not None:
        assert not result.fields.get("pdl_person_id"), (
            "PDL returned an id for a clearly-impossible name — schema drift "
            "or a too-loose match query"
        )
