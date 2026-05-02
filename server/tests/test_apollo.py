"""Tests for `credence.enrichment.apollo` — Contract 8 vendor implementation.

Uses `httpx.MockTransport` to return canned Apollo /people/match responses
so the parsing logic is exercised without network access. Live API
integration is **deferred to `tests/test_apollo_live.py`** (TBD when
APOLLO_API_KEY lands).

Phone numbers are intentionally not requested per user direction — tests
assert phone is absent from ApolloFields and that cost reflects email-only.

Coverage:
1. Happy path — verified email + title → full ApolloFields, phone absent
2. No-match response (empty `person` field) → None
3. Missing API key → None, no API call
4. Cost cap below email-credit cost → None, no API call
5. Network error / 5xx / non-JSON → None
6. Authentication failure (401) → None
7. Rate limit (429) → None
8. Email status `guessed` → confidence 0.7
9. Email status missing → no_match
10. ProspectRef.linkedin_url propagated into payload
11. Cost calculation: email match
12. Field extraction defends against missing nested `organization`
13. ApolloFields never carries a `phone` key
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from credence.enrichment.apollo import (
    APOLLO_BASE_URL,
    APOLLO_EMAIL_CREDIT_CENTS,
    ProspectRef,
    enrich,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


PERSON_LIN = ProspectRef(
    person_id="p:lin",
    canonical_name="Lin Wei",
    organization_name="TSMC",
    linkedin_url="https://linkedin.com/in/lin-wei",
)


def _full_apollo_person() -> dict[str, Any]:
    """Canned response shape — note Apollo *does* return phone_number when
    available, but our extractor intentionally ignores it. Including it in
    the canned response is a deliberate test of the "phone-is-discarded"
    invariant."""
    return {
        "id": "apollo-12345",
        "first_name": "Lin",
        "last_name": "Wei",
        "title": "VP Process Engineering",
        "email": "lin.wei@tsmc.com",
        "email_status": "verified",
        "phone_number": "+886-2-1234-5678",  # intentionally present; should be discarded
        "linkedin_url": "https://linkedin.com/in/lin-wei",
        "city": "Hsinchu",
        "country": "Taiwan",
        "organization": {
            "id": "org-tsmc",
            "name": "TSMC",
            "primary_domain": "tsmc.com",
            "website_url": "tsmc.com",
        },
    }


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_happy_path_full_match() -> None:
    """Verified email + title → ApolloFields populated; phone discarded."""
    canned = {"person": _full_apollo_person()}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _client_with(handler) as client:
        result = await enrich(
            PERSON_LIN, client=client, api_key="fake-key", max_cost_cents=100
        )

    assert result is not None
    assert result.fields["email"] == "lin.wei@tsmc.com"
    assert result.fields["email_status"] == "verified"
    # Phone explicitly NOT in fields — user opted out of phone enrichment
    assert "phone" not in result.fields
    assert result.fields["current_title"] == "VP Process Engineering"
    assert result.fields["current_company_name"] == "TSMC"
    assert result.fields["current_company_domain"] == "tsmc.com"
    assert result.fields["apollo_person_id"] == "apollo-12345"
    assert result.confidence == 0.95
    # Cost is email-only — phone was in the canned response but discarded
    assert result.cost_cents == APOLLO_EMAIL_CREDIT_CENTS
    assert result.cache_hit is False


@pytest.mark.unit
async def test_phone_in_response_is_discarded() -> None:
    """Apollo may return phone_number; the extractor must drop it.

    Locks the user direction "no phone" against future drift. If a developer
    re-enables phone extraction in `_extract_apollo_person`, this test fails
    and forces an explicit decision.
    """
    canned = {"person": _full_apollo_person()}  # contains phone_number

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert "phone" not in result.fields, (
        "phone leaked into ApolloFields — user direction is no-phone-enrichment"
    )


@pytest.mark.unit
async def test_no_match_returns_none() -> None:
    """Apollo returns 200 with no `person` field → enrich returns None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"person": None})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is None


@pytest.mark.unit
async def test_missing_api_key_returns_none() -> None:
    """No api_key + no APOLLO_API_KEY env var → None, no HTTP call."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={})

    # Ensure env doesn't accidentally have the key
    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("APOLLO_API_KEY", None)
        async with _client_with(handler) as client:
            result = await enrich(PERSON_LIN, client=client, api_key=None)

    assert result is None
    assert call_count["n"] == 0


@pytest.mark.unit
async def test_cost_cap_below_email_credit_skips_call() -> None:
    """max_cost_cents below email-credit cost (3¢) → None, no HTTP call."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={})

    async with _client_with(handler) as client:
        result = await enrich(
            PERSON_LIN,
            client=client,
            api_key="fake-key",
            max_cost_cents=1,  # below email-credit cost (3¢)
        )

    assert result is None
    assert call_count["n"] == 0


@pytest.mark.unit
async def test_cost_cap_at_email_credit_allows_call() -> None:
    """max_cost_cents == email-credit cost → call proceeds."""
    canned = {"person": _full_apollo_person()}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _client_with(handler) as client:
        result = await enrich(
            PERSON_LIN,
            client=client,
            api_key="fake-key",
            max_cost_cents=APOLLO_EMAIL_CREDIT_CENTS,  # exactly at the floor
        )

    assert result is not None
    assert result.cost_cents == APOLLO_EMAIL_CREDIT_CENTS


@pytest.mark.unit
async def test_network_error_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failed", request=request)

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is None


@pytest.mark.unit
async def test_http_5xx_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is None


@pytest.mark.unit
async def test_non_json_body_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>error</html>")

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is None


@pytest.mark.unit
async def test_auth_failure_401_returns_none() -> None:
    """Stale API key → 401 → None (and a loud log)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is None


@pytest.mark.unit
async def test_rate_limit_429_returns_none() -> None:
    """Rate-limit response → None; route's timeout handles backoff."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="too many requests")

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is None


@pytest.mark.unit
async def test_guessed_email_status_drops_confidence() -> None:
    """email_status='guessed' → confidence = 0.7."""
    person = _full_apollo_person()
    person["email_status"] = "guessed"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"person": person})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert result.fields["email_status"] == "guessed"
    assert result.confidence == 0.7


@pytest.mark.unit
async def test_no_email_status_or_email_resolves_to_no_match() -> None:
    """Apollo returns the person but no email at all → email_status='no_match'."""
    person = _full_apollo_person()
    del person["email"]
    del person["email_status"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"person": person})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert result.fields["email"] is None
    assert result.fields["email_status"] == "no_match"
    assert result.confidence == 0.5


@pytest.mark.unit
async def test_request_payload_contains_linkedin_url() -> None:
    """When ProspectRef.linkedin_url is set, it lands in the POST body."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"person": _full_apollo_person()})

    async with _client_with(handler) as client:
        await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert "lin-wei" in captured["payload"]
    assert "TSMC" in captured["payload"]


@pytest.mark.unit
async def test_request_targets_apollo_endpoint() -> None:
    """Request URL is the documented `/people/match` endpoint."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"person": _full_apollo_person()})

    async with _client_with(handler) as client:
        await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert APOLLO_BASE_URL in captured["url"]
    assert "/people/match" in captured["url"]


@pytest.mark.unit
async def test_missing_organization_field_renders_nulls() -> None:
    """A person record without nested `organization` doesn't crash."""
    person = _full_apollo_person()
    del person["organization"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"person": person})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert result.fields["current_company_name"] is None
    assert result.fields["current_company_domain"] is None


@pytest.mark.unit
async def test_reports_to_nested_manager_populates_fields() -> None:
    """Phase A.6: nested `manager` object → reports_to_name + reports_to_apollo_id."""
    person = _full_apollo_person()
    person["manager"] = {
        "id": "apollo-manager-99",
        "first_name": "Wei",
        "last_name": "Chen",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"person": person})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert result.fields["reports_to_name"] == "Wei Chen"
    assert result.fields["reports_to_apollo_id"] == "apollo-manager-99"


@pytest.mark.unit
async def test_reports_to_flat_manager_populates_fields() -> None:
    """Phase A.6: flat `manager_first_name`/`manager_last_name` → reports_to_name."""
    person = _full_apollo_person()
    person["manager_first_name"] = "Sarah"
    person["manager_last_name"] = "Kim"
    person["manager_id"] = "apollo-manager-77"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"person": person})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert result.fields["reports_to_name"] == "Sarah Kim"
    assert result.fields["reports_to_apollo_id"] == "apollo-manager-77"


@pytest.mark.unit
async def test_reports_to_absent_is_none() -> None:
    """Phase A.6: no manager fields in response → reports_to_name/id == None."""
    person = _full_apollo_person()  # no manager-related fields

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"person": person})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert result.fields["reports_to_name"] is None
    assert result.fields["reports_to_apollo_id"] is None


@pytest.mark.unit
async def test_reports_to_partial_name_drops_to_none() -> None:
    """Phase A.6: only first_name (no last_name) is too ambiguous → None.

    A manager record with `first_name="Wei"` but no last name can't be
    resolved against `persons.canonical_name` reliably. Better to drop
    than emit "Wei " and create an invalid match.
    """
    person = _full_apollo_person()
    person["manager"] = {"first_name": "Wei", "id": "apollo-manager-55"}
    # Last name omitted entirely

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"person": person})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert result.fields["reports_to_name"] is None
    # The id can still survive even without a usable name
    assert result.fields["reports_to_apollo_id"] == "apollo-manager-55"


@pytest.mark.unit
async def test_no_email_match_costs_zero() -> None:
    """Person record with no email → cost = 0¢ (no billable resource consumed)."""
    person = _full_apollo_person()
    del person["email"]
    person["email_status"] = "no_match"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"person": person})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert result.fields["email"] is None
    assert "phone" not in result.fields
    assert result.cost_cents == 0
