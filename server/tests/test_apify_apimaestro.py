"""Tests for apimaestro Apify pipeline (apify_apimaestro.py).

Mock-only — no live HTTP calls. Covers the parser layer
(_parse_employee, to_canonical_person), HTTP wiring
(list_company_employees, fetch_profile_detail) via httpx.MockTransport,
and edge-case handling (empty payload, malformed entries, missing
identifiers, FREE-tier silent-fail).
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from credence.enrichment.apify_apimaestro import (
    CompanyEmployee,
    EMPLOYEES_ACTOR,
    FullProfile,
    PROFILE_ACTOR,
    _parse_employee,
    fetch_profile_detail,
    list_company_employees,
    to_canonical_person,
)


# ── Test fixtures ──────────────────────────────────────────────────────


def _employee_record(**overrides: Any) -> dict[str, Any]:
    """Minimal valid employee listing record from apimaestro."""
    base: dict[str, Any] = {
        "company_url": "https://www.linkedin.com/company/nvidia/",
        "profile_url": "https://linkedin.com/in/jenhsunhuang",
        "fullname": "Jensen Huang",
        "first_name": "Jensen",
        "last_name": "Huang",
        "headline": "Founder and CEO, NVIDIA",
        "public_identifier": "jenhsunhuang",
        "location": {
            "country": "United States",
            "city": "Los Altos, California",
            "full": "Los Altos, California, United States",
            "country_code": "US",
        },
        "is_premium": True,
        "is_creator": False,
        "is_influencer": True,
        "open_to_work": False,
        "urn": "ACoAABMznFkB_XPgkYHnUl33kIHTWt1DtMAV6Pg",
    }
    base.update(overrides)
    return base


def _profile_detail_record(**overrides: Any) -> dict[str, Any]:
    """Minimal valid profile-detail record from apimaestro."""
    base: dict[str, Any] = {
        "basic_info": {
            "fullname": "Jensen Huang",
            "first_name": "Jensen",
            "last_name": "Huang",
            "headline": "Founder and CEO, NVIDIA",
            "public_identifier": "jenhsunhuang",
            "profile_url": "https://linkedin.com/in/jenhsunhuang",
            "location": {
                "country": "United States",
                "city": "Los Altos, California",
                "full": "Los Altos, California, United States",
                "country_code": "US",
            },
            "is_premium": True,
            "is_influencer": True,
            "open_to_work": False,
            "urn": "ACoAABMznFkB_XPgkYHnUl33kIHTWt1DtMAV6Pg",
            "follower_count": 705_311,
            "connection_count": 1_582,
            "current_company": "NVIDIA",
            "top_skills": ["GPU", "AI", "Deep Learning"],
            "email": None,
        },
        "experience": [
            {
                "title": "Founder and CEO",
                "company": "NVIDIA",
                "duration": "1993 - Present · 33 yrs",
                "start_date": {"year": 1993, "month": 4},
                "is_current": True,
                "company_linkedin_url": "https://www.linkedin.com/company/nvidia/",
            },
            {
                "title": "Dishwasher, Busboy, Waiter",
                "company": "Denny's",
                "duration": "1978 - 1983 · 5 yrs",
                "start_date": {"year": 1978},
                "end_date": {"year": 1983},
                "is_current": False,
            },
        ],
        "education": [
            {
                "school": "Stanford University",
                "degree": "Master's",
                "degree_name": "MS, Electrical Engineering",
                "start_date": {"year": 1990},
                "end_date": {"year": 1992},
            },
        ],
    }
    base.update(overrides)
    return base


# ── _parse_employee ────────────────────────────────────────────────────


def test_parse_employee_happy_path():
    emp = _parse_employee(_employee_record())
    assert isinstance(emp, CompanyEmployee)
    assert emp.fullname == "Jensen Huang"
    assert emp.public_identifier == "jenhsunhuang"
    assert emp.first_name == "Jensen"
    assert emp.last_name == "Huang"
    assert emp.headline == "Founder and CEO, NVIDIA"
    assert emp.location_text == "Los Altos, California, United States"
    assert emp.country_code == "US"
    assert emp.is_premium is True
    assert emp.is_influencer is True
    assert emp.open_to_work is False


def test_parse_employee_rejects_non_dict():
    assert _parse_employee("not a dict") is None  # type: ignore[arg-type]
    assert _parse_employee(None) is None  # type: ignore[arg-type]
    assert _parse_employee([]) is None  # type: ignore[arg-type]


def test_parse_employee_rejects_missing_profile_url():
    rec = _employee_record(profile_url="")
    assert _parse_employee(rec) is None


def test_parse_employee_rejects_missing_public_identifier():
    rec = _employee_record(public_identifier="")
    assert _parse_employee(rec) is None


def test_parse_employee_rejects_missing_fullname():
    rec = _employee_record(fullname="")
    assert _parse_employee(rec) is None


def test_parse_employee_falls_back_to_split_when_first_last_missing():
    rec = _employee_record(first_name="", last_name="", fullname="Wei Chen")
    emp = _parse_employee(rec)
    assert emp is not None
    assert emp.first_name == "Wei"
    assert emp.last_name == "Chen"


def test_parse_employee_returns_none_when_fullname_unparseable():
    rec = _employee_record(first_name="", last_name="", fullname="Cher")
    assert _parse_employee(rec) is None


def test_parse_employee_handles_missing_location_dict():
    rec = _employee_record(location=None)
    emp = _parse_employee(rec)
    assert emp is not None
    assert emp.location_text is None
    assert emp.country_code is None


# ── to_canonical_person ────────────────────────────────────────────────


def test_to_canonical_person_happy_path():
    canon = to_canonical_person(FullProfile(raw=_profile_detail_record()))
    assert canon is not None
    assert canon.canonical_name == "Jensen Huang"
    assert canon.first_name == "Jensen"
    assert canon.last_name == "Huang"
    # URL normalized to non-www form for stable UPSERT keying
    assert canon.linkedin_url == "https://linkedin.com/in/jenhsunhuang"
    assert canon.headline == "Founder and CEO, NVIDIA"
    assert canon.location_text == "Los Altos, California, United States"
    assert canon.country_code == "US"
    assert canon.followers_count == 705_311
    assert canon.connections_count == 1_582
    assert canon.premium is True
    assert canon.open_to_work is False
    # Skills land as list
    assert canon.skills == ["GPU", "AI", "Deep Learning"]
    # Employment periods preserve current/past + dates
    assert len(canon.employment_periods) == 2
    current = canon.employment_periods[0]
    assert current["title"] == "Founder and CEO"
    assert current["is_current"] is True
    assert current["start_year"] == 1993
    past = canon.employment_periods[1]
    assert past["is_current"] is False
    assert past["start_year"] == 1978
    assert past["end_year"] == 1983
    # Education
    assert len(canon.education_periods) == 1
    edu = canon.education_periods[0]
    assert edu["school_name"] == "Stanford University"
    assert edu["start_year"] == 1990
    assert edu["end_year"] == 1992


def test_to_canonical_person_returns_none_when_no_profile_url():
    raw = _profile_detail_record()
    raw["basic_info"]["profile_url"] = ""
    assert to_canonical_person(FullProfile(raw=raw)) is None


def test_to_canonical_person_returns_none_when_no_basic_info():
    assert to_canonical_person(FullProfile(raw={})) is None


def test_to_canonical_person_email_status_unverified_when_email_present():
    raw = _profile_detail_record()
    raw["basic_info"]["email"] = "jensen@nvidia.com"
    canon = to_canonical_person(FullProfile(raw=raw))
    assert canon is not None
    assert canon.email == "jensen@nvidia.com"
    assert canon.email_status == "unverified"
    assert canon.sources.get("email") == "apify_apimaestro"


def test_to_canonical_person_email_status_none_when_email_missing():
    canon = to_canonical_person(FullProfile(raw=_profile_detail_record()))
    assert canon is not None
    assert canon.email is None
    assert canon.email_status is None
    assert "email" not in canon.sources


def test_to_canonical_person_handles_no_experience():
    raw = _profile_detail_record()
    raw["experience"] = []
    canon = to_canonical_person(FullProfile(raw=raw))
    assert canon is not None
    assert canon.employment_periods == []
    assert canon.current_title is None
    assert canon.current_company_name is None


def test_to_canonical_person_picks_first_when_no_is_current():
    raw = _profile_detail_record()
    for exp in raw["experience"]:
        exp["is_current"] = False
    canon = to_canonical_person(FullProfile(raw=raw))
    assert canon is not None
    # Falls back to first entry as "current"
    assert canon.current_title == "Founder and CEO"


def test_to_canonical_person_skips_invalid_experience_entries():
    raw = _profile_detail_record()
    raw["experience"] = [
        {"title": "Valid", "company": "ValidCo", "is_current": True},
        {"title": "", "company": "MissingTitle"},  # rejected — empty title
        {"title": "MissingCompany", "company": ""},  # rejected — empty company
        "not a dict",  # rejected
    ]
    canon = to_canonical_person(FullProfile(raw=raw))
    assert canon is not None
    assert len(canon.employment_periods) == 1
    assert canon.employment_periods[0]["title"] == "Valid"


# ── HTTP wiring (httpx.MockTransport) ──────────────────────────────────


def _make_mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_list_company_employees_happy_path():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json=[_employee_record(), _employee_record(
            profile_url="https://linkedin.com/in/lee-d",
            public_identifier="lee-d",
            fullname="Lee D",
            first_name="Lee",
            last_name="D",
        )])

    async with _make_mock_client(handler) as client:
        emps, cost = await list_company_employees(
            "https://www.linkedin.com/company/nvidia/",
            max_items=2,
            api_token="test-token",
            client=client,
        )

    assert len(emps) == 2
    assert cost == 2  # 2 items × $0.01 = 2¢
    assert EMPLOYEES_ACTOR in captured["url"]
    assert captured["body"]["identifier"] == "https://www.linkedin.com/company/nvidia/"
    assert captured["body"]["max_employees"] == 2


@pytest.mark.asyncio
async def test_list_company_employees_returns_empty_on_non_201():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream error")

    async with _make_mock_client(handler) as client:
        emps, cost = await list_company_employees(
            "https://www.linkedin.com/company/nvidia/",
            max_items=10,
            api_token="test-token",
            client=client,
        )

    assert emps == []
    assert cost == 0


@pytest.mark.asyncio
async def test_list_company_employees_returns_empty_on_non_list_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"error": "not a list"})

    async with _make_mock_client(handler) as client:
        emps, cost = await list_company_employees(
            "https://www.linkedin.com/company/nvidia/",
            max_items=10,
            api_token="test-token",
            client=client,
        )

    assert emps == []
    assert cost == 0


@pytest.mark.asyncio
async def test_list_company_employees_silently_filters_unparseable_records():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=[
            _employee_record(),
            {"error": "no profile_url here"},  # rejected by _parse_employee
            None,  # rejected
        ])

    async with _make_mock_client(handler) as client:
        emps, cost = await list_company_employees(
            "https://www.linkedin.com/company/nvidia/",
            max_items=10,
            api_token="test-token",
            client=client,
        )

    assert len(emps) == 1
    # Cost reflects ALL items returned by the actor (we paid for them)
    # even though some were unparseable.
    assert cost == 3


@pytest.mark.asyncio
async def test_fetch_profile_detail_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=[_profile_detail_record()])

    async with _make_mock_client(handler) as client:
        prof, cost = await fetch_profile_detail(
            "jenhsunhuang", api_token="test-token", client=client,
        )

    assert prof is not None
    assert isinstance(prof, FullProfile)
    assert prof.raw["basic_info"]["fullname"] == "Jensen Huang"


@pytest.mark.asyncio
async def test_fetch_profile_detail_returns_none_on_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=[])

    async with _make_mock_client(handler) as client:
        prof, cost = await fetch_profile_detail(
            "ghost-username", api_token="test-token", client=client,
        )

    assert prof is None
    assert cost == 0


@pytest.mark.asyncio
async def test_fetch_profile_detail_returns_none_on_non_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=["not a dict"])

    async with _make_mock_client(handler) as client:
        prof, _ = await fetch_profile_detail(
            "bad-payload", api_token="test-token", client=client,
        )

    assert prof is None


@pytest.mark.asyncio
async def test_fetch_profile_detail_returns_none_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream")

    async with _make_mock_client(handler) as client:
        prof, cost = await fetch_profile_detail(
            "anyone", api_token="test-token", client=client,
        )

    assert prof is None
    assert cost == 0


@pytest.mark.asyncio
async def test_fetch_profile_detail_uses_correct_actor():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json=[_profile_detail_record()])

    async with _make_mock_client(handler) as client:
        await fetch_profile_detail(
            "jenhsunhuang", api_token="test-token", client=client,
        )

    assert PROFILE_ACTOR in captured["url"]
    assert captured["body"] == {"username": "jenhsunhuang"}
