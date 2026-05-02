"""Live-API smoke test for Apollo enrichment — opt-in via `pytest -m integration`.

Skipped by default. Run when `APOLLO_API_KEY` is set in env (`.env.local`):

    uv run --env-file .env.local pytest tests/test_apollo_live.py -m integration -v

Validates that:
1. The Apollo /people/match endpoint responds with the documented shape.
2. `find_apollo_match → ApolloFields` mapping handles the live payload.
3. The cost calculation matches our doc-driven estimate.

If schema drifts (Apollo changes their response keys), this test fails
and points at the field that drifted — adjust `_extract_apollo_person`.

Cost: this test issues ~1 Apollo /people/match call per run = 1 credit (~3¢).
"""
from __future__ import annotations

import os

import httpx
import pytest

from credence.enrichment.apollo import (
    APOLLO_EMAIL_CREDIT_CENTS,
    ProspectRef,
    enrich,
)


@pytest.mark.integration
async def test_apollo_live_match_for_well_known_executive() -> None:
    """Live POST to Apollo /people/match for a known public figure.

    Uses Tim Cook (Apple CEO) as a test target — he's well-indexed and
    every Apollo plan includes Apple. If this test starts returning None,
    either Apollo's data drifted or our key permissions changed.
    """
    if not os.environ.get("APOLLO_API_KEY"):
        pytest.skip("APOLLO_API_KEY not set; skipping live test")

    person = ProspectRef(
        person_id="test:tim",
        canonical_name="Tim Cook",
        organization_name="Apple",
    )

    async with httpx.AsyncClient() as client:
        result = await enrich(person, client=client, max_cost_cents=10)

    if result is None:
        pytest.fail(
            "Apollo /people/match returned no result for Tim Cook @ Apple. "
            "Either Apollo's matcher regressed, the API key has insufficient "
            "permissions, or our request payload schema drifted."
        )

    assert result.fields.get("apollo_person_id"), "expected an Apollo person_id"
    assert result.cost_cents <= APOLLO_EMAIL_CREDIT_CENTS, (
        f"cost {result.cost_cents}¢ exceeds expected email-only cost"
    )
    # Phone must NOT be present per user direction (no-phone enrichment)
    assert "phone" not in result.fields, "phone leaked — phone enrichment is disabled"
    # Confidence must be in [0, 1]
    assert 0.0 <= result.confidence <= 1.0


@pytest.mark.integration
async def test_apollo_live_no_match_returns_none() -> None:
    """A clearly-impossible name should not produce a fabricated match."""
    if not os.environ.get("APOLLO_API_KEY"):
        pytest.skip("APOLLO_API_KEY not set; skipping live test")

    person = ProspectRef(
        person_id="test:ghost",
        canonical_name="Zzzzzzz Yyyyyyyy",  # nobody is named this
        organization_name="ImaginaryCorp 99999",
    )

    async with httpx.AsyncClient() as client:
        result = await enrich(person, client=client, max_cost_cents=10)

    # Either None (no match) or a result whose email is null. NEVER a
    # fabricated email for an impossible name.
    if result is not None:
        assert result.fields.get("email") is None, (
            "Apollo returned an email for a clearly-impossible name — schema "
            "drift, hallucination, or our match query is too loose"
        )
