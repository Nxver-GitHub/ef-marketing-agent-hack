"""Tests for `credence.enrichment.apify` — harvestapi/linkedin-company-employees.

Uses ``httpx.MockTransport`` for the unit tests so we don't burn Apify
credit. The fixture data mirrors the real shape captured during the
2026-04-30 Marvell smoke (10 profiles, 19.6s, $0.08).
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from credence.enrichment.apify import (
    ACTOR_ID,
    MODE_FULL,
    MODE_FULL_EMAIL,
    MODE_SHORT,
    ApifyProfile,
    compute_run_cost_cents,
    fetch_run_dataset,
    find_company_employees_async,
    find_company_employees_sync,
    parse_profile,
    start_company_employees_run,
    wait_for_run,
)


# ── Fixtures: real Apify shape, captured live ──────────────────────────────


def _mk_profile(
    *,
    linkedin_id: str = "ACoAAAXRnsEBxgqpJ_8lvNxiymHSEsG8zHZMp2U",
    public_identifier: str = "rhonda-whitney-28183b28",
    first: str = "Rhonda",
    last: str = "Whitney",
    company: str = "Marvell Technology",
    title: str = "Global Protection Services, GSOC Manager",
    school: str = "University of Maryland Global Campus",
    skills: list[dict[str, Any]] | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    return {
        "id": linkedin_id,
        "publicIdentifier": public_identifier,
        "linkedinUrl": f"https://www.linkedin.com/in/{public_identifier}",
        "firstName": first,
        "lastName": last,
        "headline": "Next 4 month Laser focus",
        "location": {
            "linkedinText": "Hayward, California, United States",
            "countryCode": "US",
        },
        "emails": [email] if email else [],
        "currentPosition": [
            {
                "position": title,
                "companyName": company,
                "companyLinkedinUrl": "https://www.linkedin.com/company/marvell/",
                "companyUniversalName": "marvell",
                "employmentType": "Full-time",
                "startDate": {"month": "Jan", "year": 2026, "text": "Jan 2026"},
                "endDate": {"text": "Present"},
                "duration": "5 mos",
            }
        ],
        "experience": [
            {
                "position": title,
                "companyName": company,
                "companyLinkedinUrl": "https://www.linkedin.com/company/marvell/",
                "companyUniversalName": "marvell",
                "employmentType": "Full-time",
                "startDate": {"month": "Jan", "year": 2026, "text": "Jan 2026"},
                "endDate": {"text": "Present"},
                "duration": "5 mos",
            }
        ],
        "education": [
            {
                "schoolName": school,
                "degree": "Bachelor of Science - BS",
                "fieldOfStudy": "Cybersecurity Management",
                "startDate": {"month": "Apr", "year": 2023, "text": "Apr 2023"},
                "endDate": {"month": "May", "year": 2025, "text": "May 2025"},
                "insights": "Grade: 3.85",
            }
        ],
        "skills": skills if skills is not None else [
            {"name": "Safety Management", "positions": ["3 experiences at Apple"]},
            {"name": "Leadership"},
        ],
        "certifications": [{"title": "CISSP"}],
        "languages": [{"name": "English"}],
        "connectionsCount": 422,
        "followerCount": 443,
        "openToWork": False,
        "hiring": False,
        "premium": True,
        "verified": True,
        "registeredAt": "2010-12-18T12:06:08.662Z",
        "publications": [],
        "patents": [],
        "honorsAndAwards": [],
        "organizations": [],
    }


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _apify_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a fake APIFY_TOKEN so calls don't short-circuit on env-missing."""
    monkeypatch.setenv("APIFY_TOKEN", "fake-test-token")


# ── parse_profile ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_profile_happy_path() -> None:
    p = parse_profile(_mk_profile())
    assert p is not None
    assert isinstance(p, ApifyProfile)
    assert p.linkedin_url == "https://www.linkedin.com/in/rhonda-whitney-28183b28"
    assert p.first_name == "Rhonda"
    assert p.last_name == "Whitney"
    assert p.country_code == "US"
    assert p.location_text == "Hayward, California, United States"
    assert p.connections_count == 422
    assert p.premium is True
    # Employment shape
    assert len(p.employment_periods) == 1
    emp = p.employment_periods[0]
    assert emp["company_name"] == "Marvell Technology"
    assert emp["title"] == "Global Protection Services, GSOC Manager"
    assert emp["company_universal_name"] == "marvell"
    assert emp["start_year"] == 2026
    assert emp["start_month"] == 1
    assert emp["end_year"] is None
    assert emp["is_current"] is True
    # Education
    assert len(p.education_periods) == 1
    edu = p.education_periods[0]
    assert edu["school_name"] == "University of Maryland Global Campus"
    assert edu["start_year"] == 2023
    assert edu["end_year"] == 2025
    # Skills as flat list of strings
    assert "Safety Management" in p.skills
    assert "Leadership" in p.skills


@pytest.mark.unit
def test_parse_profile_no_linkedin_url_returns_none() -> None:
    """Without linkedinUrl the profile is useless for entity resolution."""
    raw = _mk_profile()
    raw["linkedinUrl"] = ""
    assert parse_profile(raw) is None


@pytest.mark.unit
def test_parse_profile_handles_email_present() -> None:
    raw = _mk_profile(email="rhonda.whitney@example.com")
    p = parse_profile(raw)
    assert p is not None
    assert p.email == "rhonda.whitney@example.com"


@pytest.mark.unit
def test_parse_profile_email_empty_in_full_mode() -> None:
    """Full mode (no +email) leaves emails empty — confirmed live."""
    p = parse_profile(_mk_profile())
    assert p is not None
    assert p.email is None


@pytest.mark.unit
def test_parse_profile_drops_malformed_experience() -> None:
    raw = _mk_profile()
    raw["experience"] = [
        {"position": "Valid", "companyName": "Real Co"},
        {},                                           # empty → drop
        {"location": "Nowhere"},                       # no company/title → drop
        "stringy noise",                               # non-dict → drop
        {"position": "", "companyName": ""},           # empty strings → drop
    ]
    p = parse_profile(raw)
    assert p is not None
    assert len(p.employment_periods) == 1


@pytest.mark.unit
def test_parse_profile_present_endDate_marks_current() -> None:
    raw = _mk_profile()
    raw["experience"][0]["endDate"] = {"text": "Present"}
    p = parse_profile(raw)
    assert p is not None
    assert p.employment_periods[0]["is_current"] is True


@pytest.mark.unit
def test_parse_profile_falls_back_to_currentPosition_when_experience_empty() -> None:
    """Some profiles only return currentPosition (no experience array)."""
    raw = _mk_profile()
    raw["experience"] = []
    p = parse_profile(raw)
    assert p is not None
    assert len(p.employment_periods) == 1
    assert p.employment_periods[0]["company_name"] == "Marvell Technology"


@pytest.mark.unit
def test_parse_profile_skills_handles_string_list() -> None:
    """Some actor versions return skills as plain string list, not dicts."""
    raw = _mk_profile(skills=["Foo", "Bar", "Baz"])
    # The fake list is dicts in our fixture; replace with strings:
    raw["skills"] = ["Foo", "Bar", "Baz"]
    p = parse_profile(raw)
    assert p is not None
    assert p.skills == ["Foo", "Bar", "Baz"]


@pytest.mark.unit
def test_parse_profile_education_drops_no_school() -> None:
    raw = _mk_profile()
    raw["education"] = [
        {"schoolName": "MIT", "degree": "BS"},
        {"degree": "MS"},   # no school name → drop
    ]
    p = parse_profile(raw)
    assert p is not None
    assert len(p.education_periods) == 1
    assert p.education_periods[0]["school_name"] == "MIT"


@pytest.mark.unit
def test_parse_profile_non_dict_returns_none() -> None:
    assert parse_profile(None) is None
    assert parse_profile("string") is None  # type: ignore[arg-type]
    assert parse_profile([1, 2, 3]) is None  # type: ignore[arg-type]


# ── compute_run_cost_cents ──────────────────────────────────────────────────


@pytest.mark.unit
def test_compute_run_cost_cents_full_mode() -> None:
    """10 full-profile @ 0.8¢ each = 8¢, ceiling rounded → 8¢."""
    cost = compute_run_cost_cents(
        {"actor-start": 1, "full-profile": 10, "full-profile-with-email": 0, "short-profile": 0}
    )
    # 0.8 * 10 = 8.0 → ceil = 8
    assert cost == 8


@pytest.mark.unit
def test_compute_run_cost_cents_email_mode() -> None:
    """500 full-profile-with-email @ 1.2¢ = 600¢ ($6 for 500 profiles)."""
    cost = compute_run_cost_cents({"full-profile-with-email": 500, "actor-start": 1})
    assert cost == 600


@pytest.mark.unit
def test_compute_run_cost_cents_short_mode_rounds_up() -> None:
    """1 short-profile = 0.4¢ → ceiling = 1¢."""
    cost = compute_run_cost_cents({"short-profile": 1})
    assert cost == 1


@pytest.mark.unit
def test_compute_run_cost_cents_handles_none() -> None:
    assert compute_run_cost_cents(None) == 0
    assert compute_run_cost_cents({}) == 0
    assert compute_run_cost_cents({"unknown-event": 100}) == 0


# ── find_company_employees_sync ─────────────────────────────────────────────


@pytest.mark.unit
async def test_find_company_employees_sync_happy_path() -> None:
    """Mock transport returns 3 profiles; we get back EnrichmentResult."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert ACTOR_ID in str(request.url)
        assert "run-sync-get-dataset-items" in str(request.url)
        body = request.read()
        # Confirm key fields landed in the request body
        body_str = body.decode("utf-8")
        assert "marvell" in body_str
        assert "Full ($8 per 1k)" in body_str
        return httpx.Response(
            201,
            json=[_mk_profile(public_identifier=f"p{i}") for i in range(3)],
        )

    async with _client_with(handler) as client:
        result = await find_company_employees_sync(
            "https://www.linkedin.com/company/marvell/",
            max_items=3,
            client=client,
        )

    assert result is not None
    assert len(result.profiles) == 3
    # 3 × 0.8 = 2.4 → ceil = 3
    assert result.cost_cents == 3


@pytest.mark.unit
async def test_find_company_employees_sync_no_token_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without APIFY_TOKEN we short-circuit (no HTTP, no charge)."""
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    async with _client_with(handler) as client:
        result = await find_company_employees_sync(
            "https://www.linkedin.com/company/marvell/",
            max_items=3,
            api_token=None,
            client=client,
        )

    assert result is None
    assert called is False


@pytest.mark.unit
async def test_find_company_employees_sync_http_500_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Apify down")

    async with _client_with(handler) as client:
        result = await find_company_employees_sync(
            "https://www.linkedin.com/company/marvell/",
            max_items=3,
            client=client,
        )

    assert result is None


@pytest.mark.unit
async def test_find_company_employees_sync_non_json_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>fail</html>")

    async with _client_with(handler) as client:
        result = await find_company_employees_sync(
            "https://www.linkedin.com/company/marvell/",
            max_items=3,
            client=client,
        )

    assert result is None


@pytest.mark.unit
async def test_find_company_employees_sync_network_error_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failed", request=request)

    async with _client_with(handler) as client:
        result = await find_company_employees_sync(
            "https://www.linkedin.com/company/marvell/",
            max_items=3,
            client=client,
        )

    assert result is None


@pytest.mark.unit
async def test_find_company_employees_sync_zero_items_returns_empty_result() -> None:
    """Slug-not-found returns [] from the actor — we surface as 0 profiles, 0 cost."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=[])

    async with _client_with(handler) as client:
        result = await find_company_employees_sync(
            "https://www.linkedin.com/company/wrong-slug/",
            max_items=3,
            client=client,
        )

    assert result is not None
    assert result.profiles == []
    assert result.cost_cents == 0


# ── async pattern (start → wait → fetch) ────────────────────────────────────


@pytest.mark.unit
async def test_start_run_returns_run_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"data": {"id": "run-abc-123", "status": "READY"}})

    async with _client_with(handler) as client:
        run_id = await start_company_employees_run(
            "https://www.linkedin.com/company/marvell/",
            max_items=500,
            client=client,
        )

    assert run_id == "run-abc-123"


@pytest.mark.unit
async def test_wait_for_run_returns_succeeded_immediately() -> None:
    """When the first poll returns SUCCEEDED, we don't wait."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {
                "id": "run-1",
                "status": "SUCCEEDED",
                "defaultDatasetId": "ds-1",
                "chargedEventCounts": {"full-profile": 5, "actor-start": 1},
            }},
        )

    async with _client_with(handler) as client:
        status, data = await wait_for_run("run-1", poll_interval=0.01, client=client)

    assert status == "SUCCEEDED"
    assert data is not None
    assert data["defaultDatasetId"] == "ds-1"


@pytest.mark.unit
async def test_wait_for_run_fails_returns_failed_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"id": "run-2", "status": "FAILED"}},
        )

    async with _client_with(handler) as client:
        status, data = await wait_for_run("run-2", poll_interval=0.01, client=client)

    assert status == "FAILED"
    assert data is not None
    assert data["status"] == "FAILED"


@pytest.mark.unit
async def test_fetch_run_dataset_happy_path() -> None:
    """Run finished, dataset fetch returns 2 items, cost computed from event counts."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "datasets/ds-1/items" in url:
            return httpx.Response(200, json=[
                _mk_profile(public_identifier="p1"),
                _mk_profile(public_identifier="p2"),
            ])
        return httpx.Response(404)

    run_data = {
        "id": "run-1",
        "defaultDatasetId": "ds-1",
        "chargedEventCounts": {"full-profile": 2, "actor-start": 1},
    }

    async with _client_with(handler) as client:
        result = await fetch_run_dataset(run_data, client=client)

    assert result is not None
    assert len(result.profiles) == 2
    # 2 × 0.8 = 1.6 → ceil = 2
    assert result.cost_cents == 2
    assert result.run_id == "run-1"


@pytest.mark.unit
async def test_fetch_run_dataset_no_dataset_id_returns_none() -> None:
    result = await fetch_run_dataset({"id": "x", "chargedEventCounts": {}})
    assert result is None


@pytest.mark.unit
async def test_find_company_employees_async_end_to_end() -> None:
    """Full async flow: start → poll → fetch dataset → 1 profile back."""
    state = {"phase": "start"}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/runs?token=" in url and request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run-async-1", "status": "READY"}})
        if "actor-runs/run-async-1" in url:
            return httpx.Response(200, json={"data": {
                "id": "run-async-1",
                "status": "SUCCEEDED",
                "defaultDatasetId": "ds-async-1",
                "chargedEventCounts": {"full-profile": 1, "actor-start": 1},
            }})
        if "datasets/ds-async-1/items" in url:
            return httpx.Response(200, json=[_mk_profile()])
        return httpx.Response(404, text=f"unexpected: {url}")

    async with _client_with(handler) as client:
        result = await find_company_employees_async(
            "https://www.linkedin.com/company/marvell/",
            max_items=500,
            client=client,
            poll_interval=0.01,
        )

    assert result is not None
    assert len(result.profiles) == 1
    assert result.cost_cents == 1
    assert result.run_id == "run-async-1"
