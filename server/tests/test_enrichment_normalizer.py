"""Tests for `credence.enrichment.normalizer` — entity resolution.

Pure functional, no DB / HTTP.
"""
from __future__ import annotations

import pytest

from credence.enrichment.apify import ApifyProfile
from credence.enrichment.normalizer import (
    CanonicalPerson,
    from_apify,
    from_apollo,
    merge_records,
    normalize_company,
    normalize_name,
    same_person,
)


# ── normalize_name ──────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ("James R. Clarke", ("James", "Clarke")),
    ("Phebe N. Novakovic", ("Phebe", "Novakovic")),
    ("Dr. Sarah Kim", ("Sarah", "Kim")),
    ("Wei Chen", ("Wei", "Chen")),
    ("Dr. James R. Clarke, Jr.", ("James", "Clarke")),
    ("Marcus Hale Sr.", ("Marcus", "Hale")),
    ("Lisa O'Connor, Ph.D.", ("Lisa", "O'Connor")),
    ("Madonna", ("", "")),
    ("", ("", "")),
    ("   ", ("", "")),
])
def test_normalize_name(raw: str, expected: tuple[str, str]) -> None:
    assert normalize_name(raw) == expected


# ── normalize_company ───────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ("Lockheed Martin Corporation", "Lockheed Martin"),
    ("Lockheed Martin Corp", "Lockheed Martin"),
    ("LMT", "Lockheed Martin"),
    ("Marvell Semiconductor", "Marvell Technology"),
    ("Raytheon Technologies", "RTX"),
    ("Raytheon", "RTX"),
    ("AMD Inc", "AMD"),
    ("Advanced Micro Devices, Inc.", "AMD"),
    ("Intel Corporation", "Intel"),
    ("KLA-Tencor", "KLA Corporation"),
    ("NVIDIA Corp", "NVIDIA"),
    ("ASML Holding NV", "ASML"),
    # No alias hit but generic suffix gets stripped
    ("Some Random Co", "Some Random"),
    ("Acme Inc.", "Acme"),
    # Lowercase-canonical alias still resolves
    ("amd inc", "AMD"),
    # Edge cases
    ("", None),
    (None, None),
    ("   ", None),
])
def test_normalize_company(raw: str | None, expected: str | None) -> None:
    assert normalize_company(raw) == expected


# ── same_person ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_same_person_linkedin_url_match() -> None:
    a = CanonicalPerson(canonical_name="John Doe", first_name="John", last_name="Doe",
                        linkedin_url="https://linkedin.com/in/johndoe/")
    b = CanonicalPerson(canonical_name="J. Doe", first_name="J", last_name="Doe",
                        linkedin_url="https://LinkedIn.com/in/johndoe")  # case + slash
    assert same_person(a, b) is True


@pytest.mark.unit
def test_same_person_email_match() -> None:
    a = CanonicalPerson(canonical_name="John Doe", first_name="John", last_name="Doe",
                        email="john.doe@example.com")
    b = CanonicalPerson(canonical_name="Jonathan Doe", first_name="Jonathan", last_name="Doe",
                        email="JOHN.DOE@example.com")
    assert same_person(a, b) is True


@pytest.mark.unit
def test_same_person_name_company_exact() -> None:
    a = CanonicalPerson(canonical_name="Jane Smith", first_name="Jane", last_name="Smith",
                        current_company_name="Intel")
    b = CanonicalPerson(canonical_name="Jane Smith", first_name="Jane", last_name="Smith",
                        current_company_name="Intel")
    assert same_person(a, b) is True


@pytest.mark.unit
def test_same_person_different_companies_returns_false() -> None:
    a = CanonicalPerson(canonical_name="Jane Smith", first_name="Jane", last_name="Smith",
                        current_company_name="Intel")
    b = CanonicalPerson(canonical_name="Jane Smith", first_name="Jane", last_name="Smith",
                        current_company_name="NVIDIA")
    # Same name at different companies — likely different humans, return False
    assert same_person(a, b) is False


@pytest.mark.unit
def test_same_person_fuzzy_name_same_company() -> None:
    """rapidfuzz token-sort should accept word-order variations."""
    a = CanonicalPerson(canonical_name="Sarah Kim", first_name="Sarah", last_name="Kim",
                        current_company_name="Intel")
    b = CanonicalPerson(canonical_name="Sarah J Kim", first_name="Sarah", last_name="Kim",
                        current_company_name="Intel")
    assert same_person(a, b) is True


@pytest.mark.unit
def test_same_person_obviously_different_returns_false() -> None:
    a = CanonicalPerson(canonical_name="Alice Anderson", first_name="Alice", last_name="Anderson",
                        current_company_name="Intel")
    b = CanonicalPerson(canonical_name="Bob Brown", first_name="Bob", last_name="Brown",
                        current_company_name="Intel")
    assert same_person(a, b) is False


# ── from_apify ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_from_apify_happy_path() -> None:
    profile = ApifyProfile(
        linkedin_id="ACoAA123",
        public_identifier="lin-wei",
        linkedin_url="https://www.linkedin.com/in/lin-wei",
        first_name="Lin", last_name="Wei",
        headline="VP Process Engineering",
        location_text="Hsinchu, Taiwan", country_code="TW",
        email="lin.wei@tsmc.com",
        employment_periods=[{
            "company_name": "TSMC",
            "title": "VP Process Engineering",
            "is_current": True,
            "start_year": 2018,
        }],
        education_periods=[{
            "school_name": "MIT EECS",
            "degree": "PhD",
            "start_year": 2008, "end_year": 2014,
        }],
        skills=["3nm yield", "GAA transistors"],
        certifications=[],
        languages=["English", "Mandarin"],
        connections_count=500, followers_count=1200,
        open_to_work=False, hiring=True, premium=True, verified=True,
        registered_at="2010-01-01T00:00:00Z",
    )
    cp = from_apify(profile)
    assert cp is not None
    assert cp.canonical_name == "Lin Wei"
    assert cp.linkedin_url == "https://www.linkedin.com/in/lin-wei"
    assert cp.email == "lin.wei@tsmc.com"
    assert cp.current_title == "VP Process Engineering"
    # Company normalization should kick in
    assert cp.current_company_name == "TSMC"
    assert cp.sources["linkedin_url"] == "apify"


@pytest.mark.unit
def test_from_apify_no_linkedin_url_returns_none() -> None:
    profile = ApifyProfile(
        linkedin_id="ACoAA456",
        public_identifier="",
        linkedin_url="",
        first_name="Lin", last_name="Wei",
        headline=None, location_text=None, country_code=None, email=None,
        employment_periods=[], education_periods=[], skills=[], certifications=[],
        languages=[], connections_count=None, followers_count=None,
        open_to_work=False, hiring=False, premium=False, verified=False,
        registered_at=None,
    )
    assert from_apify(profile) is None


# ── from_apollo ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_from_apollo_happy_path() -> None:
    cp = from_apollo({
        "name": "Phebe N. Novakovic",
        "organization_name": "General Dynamics",
        "title": "CEO",
        "email": "phebe@gd.com",
        "email_status": "verified",
    })
    assert cp is not None
    assert cp.first_name == "Phebe"
    assert cp.last_name == "Novakovic"
    assert cp.current_company_name == "General Dynamics"
    assert cp.email == "phebe@gd.com"
    assert cp.email_status == "verified"
    assert cp.sources["email"] == "apollo"


@pytest.mark.unit
def test_from_apollo_missing_name_returns_none() -> None:
    assert from_apollo({"title": "CEO"}) is None
    assert from_apollo({}) is None
    assert from_apollo("string") is None  # type: ignore[arg-type]


# ── merge_records ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_merge_records_apify_first_apollo_fills_email() -> None:
    """Apify came back without an email; Apollo fills it on the merge."""
    apify_p = ApifyProfile(
        linkedin_id="ACoAA1",
        public_identifier="phebe-novakovic",
        linkedin_url="https://www.linkedin.com/in/phebe-novakovic",
        first_name="Phebe", last_name="Novakovic",
        headline="CEO at GD", location_text="Fairfax, VA", country_code="US",
        email=None,  # <- Apify didn't get email
        employment_periods=[{
            "company_name": "General Dynamics",
            "title": "Chairman and Chief Executive Officer",
            "is_current": True, "start_year": 2013,
        }],
        education_periods=[],
        skills=[], certifications=[], languages=[],
        connections_count=200, followers_count=500,
        open_to_work=False, hiring=False, premium=False, verified=True,
        registered_at=None,
    )
    apollo_record = {
        "name": "Phebe Novakovic",
        "organization_name": "General Dynamics Corporation",
        "title": "Chairman & CEO",
        "email": "phebe@gd.com",
        "email_status": "verified",
    }

    merged = merge_records({"apify": [apify_p], "apollo": [apollo_record]})

    assert len(merged) == 1
    p = merged[0]
    # Apify won the title race (higher priority than Apollo)
    assert p.current_title == "Chairman and Chief Executive Officer"
    # But Apollo filled the email since Apify had None
    assert p.email == "phebe@gd.com"
    assert p.sources["email"] == "apollo"
    assert p.sources["current_title"] == "apify"


@pytest.mark.unit
def test_merge_records_dedups_same_person_across_sources() -> None:
    """Apollo + Apify return the same human; one canonical record out."""
    apify_p = ApifyProfile(
        linkedin_id="ACoAA9", public_identifier="lin-wei",
        linkedin_url="https://linkedin.com/in/lin-wei",
        first_name="Lin", last_name="Wei",
        headline=None, location_text=None, country_code=None, email=None,
        employment_periods=[{"company_name": "TSMC", "title": "VP", "is_current": True}],
        education_periods=[], skills=[], certifications=[], languages=[],
        connections_count=None, followers_count=None,
        open_to_work=False, hiring=False, premium=False, verified=False,
        registered_at=None,
    )
    merged = merge_records({
        "apify": [apify_p],
        "apollo": [{
            "name": "Lin Wei",
            "organization_name": "TSMC",
            "linkedin_url": "https://LinkedIn.com/in/lin-wei",  # same URL, diff case
            "email": "lin@tsmc.com",
        }],
    })
    assert len(merged) == 1
    assert merged[0].email == "lin@tsmc.com"


@pytest.mark.unit
def test_merge_records_distinct_people_stay_separate() -> None:
    apify_a = ApifyProfile(
        linkedin_id="A1", public_identifier="a",
        linkedin_url="https://linkedin.com/in/alice",
        first_name="Alice", last_name="Anderson",
        headline=None, location_text=None, country_code=None, email=None,
        employment_periods=[{"company_name": "Intel", "title": "VP", "is_current": True}],
        education_periods=[], skills=[], certifications=[], languages=[],
        connections_count=None, followers_count=None,
        open_to_work=False, hiring=False, premium=False, verified=False,
        registered_at=None,
    )
    apify_b = ApifyProfile(
        linkedin_id="B1", public_identifier="b",
        linkedin_url="https://linkedin.com/in/bob",
        first_name="Bob", last_name="Brown",
        headline=None, location_text=None, country_code=None, email=None,
        employment_periods=[{"company_name": "Intel", "title": "Director", "is_current": True}],
        education_periods=[], skills=[], certifications=[], languages=[],
        connections_count=None, followers_count=None,
        open_to_work=False, hiring=False, premium=False, verified=False,
        registered_at=None,
    )
    merged = merge_records({"apify": [apify_a, apify_b]})
    assert len(merged) == 2
    names = {p.canonical_name for p in merged}
    assert names == {"Alice Anderson", "Bob Brown"}


@pytest.mark.unit
def test_merge_records_empty_input() -> None:
    assert merge_records({}) == []
    assert merge_records({"apify": [], "apollo": []}) == []
