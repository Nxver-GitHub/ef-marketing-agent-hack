"""Tests for `credence.enrichment.pdl` — People Data Labs vendor.

Uses `httpx.MockTransport` to return canned PDL `/v5/person/enrich`
responses. Live integration is in `tests/test_pdl_live.py` (skipped
when no PDL_API_KEY).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from credence.enrichment.pdl import (
    PDL_BASE_URL,
    PDL_ENRICH_CREDIT_CENTS,
    ProspectRef,
    enrich,
)

PERSON_LIN = ProspectRef(
    person_id="p:lin",
    canonical_name="Lin Wei",
    organization_name="TSMC",
    linkedin_url="https://linkedin.com/in/lin-wei",
)


def _full_pdl_payload() -> dict[str, Any]:
    """A canned PDL match response shaped per their published v5 docs."""
    return {
        "status": 200,
        "likelihood": 9,
        "data": {
            "id": "qEnOZ5Oh0poWnQ1luFBfVw_0000",
            "linkedin_url": "https://linkedin.com/in/lin-wei",
            "skills": ["3nm yield", "GAA transistors", "process engineering"],
            "experience": [
                {
                    "is_primary": True,
                    "start_date": "2018-04",
                    "end_date": None,
                    "company": {"name": "TSMC", "size": "10001+"},
                    "title": {
                        "name": "VP Process Engineering",
                        "role": "engineering",
                    },
                },
                {
                    "is_primary": False,
                    "start_date": "2010-08",
                    "end_date": "2018-03",
                    "company": {"name": "Intel", "size": "10001+"},
                    "title": {
                        "name": "Principal Engineer",
                        "role": "engineering",
                    },
                },
            ],
        },
    }


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_happy_path_full_match() -> None:
    """200 + likelihood=9 + 2-job experience → full PDLFields populated."""
    canned = _full_pdl_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _client_with(handler) as client:
        result = await enrich(
            PERSON_LIN, client=client, api_key="fake-key", max_cost_cents=100
        )

    assert result is not None
    assert result.fields["pdl_person_id"] == "qEnOZ5Oh0poWnQ1luFBfVw_0000"
    assert result.fields["linkedin_url"] == "https://linkedin.com/in/lin-wei"
    assert "3nm yield" in result.fields["skills"]
    assert len(result.fields["employment_periods"]) == 2
    current_job = result.fields["employment_periods"][0]
    assert current_job["company_name"] == "TSMC"
    assert current_job["title"] == "VP Process Engineering"
    assert current_job["start_date"] == "2018-04"
    assert current_job["end_date"] is None
    assert current_job["is_current"] is True
    assert result.confidence == pytest.approx(0.9)
    assert result.cost_cents == PDL_ENRICH_CREDIT_CENTS


@pytest.mark.unit
async def test_no_match_404_returns_none() -> None:
    """PDL returns 404 + status=404 in body → no result."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status": 404, "error": {"message": "no person found"}})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is None


@pytest.mark.unit
async def test_no_match_inner_status() -> None:
    """200 outer but inner status 404 → no result."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 404, "data": None})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is None


@pytest.mark.unit
async def test_missing_api_key_returns_none() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={})

    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("PDL_API_KEY", None)
        async with _client_with(handler) as client:
            result = await enrich(PERSON_LIN, client=client, api_key=None)

    assert result is None
    assert call_count["n"] == 0


@pytest.mark.unit
async def test_cost_cap_below_credit_skips_call() -> None:
    """max_cost_cents below per-call credit cost → None, no HTTP call."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={})

    async with _client_with(handler) as client:
        result = await enrich(
            PERSON_LIN,
            client=client,
            api_key="fake-key",
            max_cost_cents=PDL_ENRICH_CREDIT_CENTS - 1,
        )

    assert result is None
    assert call_count["n"] == 0


@pytest.mark.unit
async def test_network_error_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failed", request=request)

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is None


@pytest.mark.unit
async def test_auth_401_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": 401, "error": "unauthorized"})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is None


@pytest.mark.unit
async def test_billing_402_returns_none() -> None:
    """Out-of-credits is sticky — operator action needed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"status": 402, "error": "out of credits"})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is None


@pytest.mark.unit
async def test_rate_limit_429_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="too many requests")

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
async def test_empty_data_returns_none() -> None:
    """200 + empty data dict → no usable info, return None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 200, "data": {}})

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is None


@pytest.mark.unit
async def test_experience_without_company_or_title_dropped() -> None:
    """Experience entry missing both company.name AND title.name → dropped."""
    canned = _full_pdl_payload()
    canned["data"]["experience"].append(
        {
            "company": {},
            "title": {},
            "start_date": "2000-01",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    # Bad entry dropped; only the 2 good ones remain
    assert len(result.fields["employment_periods"]) == 2


@pytest.mark.unit
async def test_request_payload_uses_linkedin_when_present() -> None:
    """profile= LinkedIn URL is the highest-precision identifier."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_full_pdl_payload())

    async with _client_with(handler) as client:
        await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert "profile=" in captured["url"]
    assert "linkedin.com" in captured["url"]


@pytest.mark.unit
async def test_request_payload_falls_back_to_name_company() -> None:
    """No linkedin_url on prospect → name+company params."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_full_pdl_payload())

    person = ProspectRef(
        person_id="p:m",
        canonical_name="Marcus Hale",
        organization_name="Intel",
    )
    async with _client_with(handler) as client:
        await enrich(person, client=client, api_key="fake-key")

    assert "name=" in captured["url"]
    assert "Marcus" in captured["url"] or "Marcus+Hale" in captured["url"] or "Marcus%20Hale" in captured["url"]
    assert "company=" in captured["url"]


@pytest.mark.unit
async def test_no_identifier_skips_call() -> None:
    """Prospect with no linkedin / no canonical_name → no call."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=_full_pdl_payload())

    person = ProspectRef(person_id="p:ghost", canonical_name="")
    async with _client_with(handler) as client:
        result = await enrich(person, client=client, api_key="fake-key")

    assert result is None
    assert call_count["n"] == 0


@pytest.mark.unit
async def test_likelihood_drives_confidence() -> None:
    """likelihood=5 → confidence=0.5; missing → fallback 0.7."""
    canned = _full_pdl_payload()
    canned["likelihood"] = 5

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert result.confidence == pytest.approx(0.5)


@pytest.mark.unit
async def test_reports_to_manager_populates_fields() -> None:
    """Phase A.6: top-level `manager` object → reports_to_* fields populated."""
    canned = _full_pdl_payload()
    canned["data"]["manager"] = {
        "name": "Wei Chen",
        "linkedin_url": "https://linkedin.com/in/wei-chen-tsmc",
        "id": "pdl-manager-abc",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert result.fields["reports_to_name"] == "Wei Chen"
    assert (
        result.fields["reports_to_linkedin_url"]
        == "https://linkedin.com/in/wei-chen-tsmc"
    )
    assert result.fields["reports_to_pdl_id"] == "pdl-manager-abc"


@pytest.mark.unit
async def test_reports_to_absent_is_none() -> None:
    """Phase A.6: no manager in response → reports_to_* fields are None."""
    canned = _full_pdl_payload()  # no manager

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert result.fields["reports_to_name"] is None
    assert result.fields["reports_to_linkedin_url"] is None
    assert result.fields["reports_to_pdl_id"] is None


@pytest.mark.unit
async def test_reports_to_partial_manager_handled_defensively() -> None:
    """Phase A.6: malformed manager (empty name, no first/last) → None.

    A manager dict that lacks a usable `name` AND lacks a usable
    `first_name`+`last_name` pair must collapse to None for the name
    field. Other identifier fields (linkedin_url, id) survive when
    present so the route layer can still attempt resolution by URL.
    """
    canned = _full_pdl_payload()
    canned["data"]["manager"] = {
        "name": "",  # empty string
        "first_name": "Wei",  # only first half — too ambiguous
        "linkedin_url": "https://linkedin.com/in/wei-c",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned)

    async with _client_with(handler) as client:
        result = await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert result is not None
    assert result.fields["reports_to_name"] is None
    # linkedin_url survives — useful for URL-based persons lookup downstream
    assert (
        result.fields["reports_to_linkedin_url"]
        == "https://linkedin.com/in/wei-c"
    )
    assert result.fields["reports_to_pdl_id"] is None


@pytest.mark.unit
async def test_request_targets_pdl_endpoint() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_full_pdl_payload())

    async with _client_with(handler) as client:
        await enrich(PERSON_LIN, client=client, api_key="fake-key")

    assert PDL_BASE_URL in captured["url"]
    assert "/person/enrich" in captured["url"]
