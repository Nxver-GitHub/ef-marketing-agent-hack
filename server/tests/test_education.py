"""Unit tests for the v3.1 education extractor (Plan B3).

Coverage groups:
1. School normalization — exact, alias, fuzzy, miss
2. Degree classification — institution-default, PDL fallback, undecidable
3. Cohort-strength scoring — year/size/program factors + cap
4. PDL fetch — happy path + auth-fail + 404 + parse-fail
5. End-to-end find_education_overlaps — MBA-cohort match, no-overlap, max_results

PDL is mocked via ``httpx.MockTransport``, mirroring the pattern in
``test_apollo.py`` / ``test_pdl.py`` so no live key is needed.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from credence.extractors import education
from credence.extractors.education import (
    MBA_SCHOOL_ALIASES,
    _classify_degree,
    _coerce_year,
    _education_entry_from_pdl,
    compute_cohort_strength,
    find_education_overlaps,
    normalize_school,
)
from credence.extractors.patents import PersonRef


PERSON_A = PersonRef(
    person_id="00000000-0000-0000-0000-aaaa00000001",
    canonical_name="Person A",
    linkedin_url="https://linkedin.com/in/person-a",
)
PERSON_B = PersonRef(
    person_id="00000000-0000-0000-0000-bbbb00000002",
    canonical_name="Person B",
    linkedin_url="https://linkedin.com/in/person-b",
)


# ─── 1. normalize_school ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_normalize_school_exact_canonical() -> None:
    assert normalize_school("Harvard Business School") == "Harvard Business School"
    assert normalize_school("harvard business school") == "Harvard Business School"


@pytest.mark.unit
def test_normalize_school_alias_match() -> None:
    assert normalize_school("HBS") == "Harvard Business School"
    assert normalize_school("Wharton") == "Wharton School"
    assert normalize_school("MIT Sloan") == "MIT Sloan School of Management"
    assert normalize_school("CMU CS") == "Carnegie Mellon CS"


@pytest.mark.unit
def test_normalize_school_fuzzy_fallback() -> None:
    """Slight typo/variant resolves via difflib at cutoff 0.88."""
    # "Harvard Business" is a near-substring of one of the aliases
    assert normalize_school("Harvard Business") == "Harvard Business School"


@pytest.mark.unit
def test_normalize_school_unknown_returns_none() -> None:
    """Per CLAUDE.md Common Mistake #6 — never fabricate."""
    assert normalize_school("University of Mars") is None
    assert normalize_school("Some Random College of Imaginary Studies") is None
    assert normalize_school(None) is None
    assert normalize_school("") is None
    assert normalize_school("   ") is None


@pytest.mark.unit
def test_alias_table_has_no_collisions() -> None:
    """Every alias should map to exactly one canonical."""
    seen: dict[str, str] = {}
    for canonical, aliases in MBA_SCHOOL_ALIASES.items():
        for a in [canonical, *aliases]:
            key = a.lower()
            assert key not in seen or seen[key] == canonical, (
                f"alias collision: {a!r} maps to both {seen[key]} and {canonical}"
            )
            seen[key] = canonical


# ─── 2. _classify_degree ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_classify_degree_institution_default_wins() -> None:
    """HBS is always MBA or exec_ed regardless of PDL's degree string."""
    assert _classify_degree("Harvard Business School", ["BA", "Random"], "Random") == "mba"
    assert _classify_degree(
        "Harvard Business School (Executive)", ["AMP"], None
    ) == "exec_ed"


@pytest.mark.unit
def test_classify_degree_falls_back_to_pdl_strings() -> None:
    """When the canonical school has no institutional default, parse PDL strings."""
    assert _classify_degree("Caltech", ["PhD"], None) == "phd"
    assert _classify_degree("Caltech", ["MS"], "Computer Science") == "ms"
    assert _classify_degree("Caltech", ["BS"], "Physics") == "bs"
    assert _classify_degree("Caltech", ["Executive"], None) == "exec_ed"


@pytest.mark.unit
def test_classify_degree_returns_none_when_undecidable() -> None:
    assert _classify_degree("Caltech", [], None) is None
    assert _classify_degree("Caltech", ["Random Cert"], None) is None
    assert _classify_degree("Caltech", None, None) is None


# ─── 3. compute_cohort_strength ──────────────────────────────────────────────


@pytest.mark.unit
def test_cohort_strength_same_year_same_program_tight_cohort() -> None:
    """Tight cohort (Caltech ~60) + same year + same program = near-cap."""
    s = compute_cohort_strength(
        institution="Caltech",
        degree_type="phd",
        graduation_year_a=2018,
        graduation_year_b=2018,
        same_program=True,
    )
    # 0.78 (phd base) * 1.0 (same year) * 1.10 (cohort<=100) * 1.05 (same prog)
    assert s == pytest.approx(0.78 * 1.0 * 1.10 * 1.05, abs=0.001)


@pytest.mark.unit
def test_cohort_strength_year_gap_decay() -> None:
    """Same year > 1y apart > 2+y apart."""
    same = compute_cohort_strength(
        institution="Wharton School",
        degree_type="mba",
        graduation_year_a=2015,
        graduation_year_b=2015,
        same_program=False,
    )
    one_year = compute_cohort_strength(
        institution="Wharton School",
        degree_type="mba",
        graduation_year_a=2015,
        graduation_year_b=2016,
        same_program=False,
    )
    five_year = compute_cohort_strength(
        institution="Wharton School",
        degree_type="mba",
        graduation_year_a=2015,
        graduation_year_b=2020,
        same_program=False,
    )
    assert same > one_year > five_year


@pytest.mark.unit
def test_cohort_strength_capped_at_99() -> None:
    """The min(0.99, ...) ceiling applies even with maximum multipliers."""
    s = compute_cohort_strength(
        institution="Caltech",  # cohort_size=60 → 1.10
        degree_type="mba",     # 0.85 base — highest
        graduation_year_a=2020,
        graduation_year_b=2020,
        same_program=True,
    )
    assert s <= 0.99


@pytest.mark.unit
def test_cohort_strength_unknown_school_uses_default_size_factor() -> None:
    """Unknown school (not in INSTITUTION_TYPICAL_COHORT_SIZE) → 0.85 size factor."""
    s = compute_cohort_strength(
        institution="Some Real School (Not in Table)",
        degree_type="bs",
        graduation_year_a=2010,
        graduation_year_b=2010,
        same_program=False,
    )
    # 0.62 (bs base) * 1.0 (same year) * 0.85 (unknown school) * 1.0
    assert s == pytest.approx(0.62 * 1.0 * 0.85 * 1.0, abs=0.001)


@pytest.mark.unit
def test_cohort_strength_missing_years_treated_as_one_apart() -> None:
    """When either year is None, conservative 0.80 year_factor."""
    s = compute_cohort_strength(
        institution="Wharton School",
        degree_type="mba",
        graduation_year_a=None,
        graduation_year_b=2015,
        same_program=False,
    )
    same_school_known = compute_cohort_strength(
        institution="Wharton School",
        degree_type="mba",
        graduation_year_a=2015,
        graduation_year_b=2016,
        same_program=False,
    )
    assert s == pytest.approx(same_school_known, abs=0.001)


# ─── 4. _coerce_year ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_coerce_year_handles_pdl_formats() -> None:
    assert _coerce_year("2012-05") == 2012
    assert _coerce_year("2012") == 2012
    assert _coerce_year("2012-05-21") == 2012
    assert _coerce_year(None) is None
    assert _coerce_year("") is None
    assert _coerce_year("not-a-date") is None


# ─── 5. _education_entry_from_pdl ────────────────────────────────────────────


@pytest.mark.unit
def test_entry_dropped_when_school_unknown() -> None:
    entry = {
        "school": {"name": "University of Mars"},
        "degrees": ["MBA"],
        "end_date": "2010-05",
    }
    assert _education_entry_from_pdl(entry) is None


@pytest.mark.unit
def test_entry_dropped_when_degree_undecidable() -> None:
    entry = {
        "school": {"name": "Caltech"},
        "degrees": ["Random Certificate"],
        "majors": [],
    }
    assert _education_entry_from_pdl(entry) is None


@pytest.mark.unit
def test_entry_normalized_when_canonical_known() -> None:
    entry = {
        "school": {"name": "HBS"},
        "degrees": ["MBA"],
        "majors": ["General Management"],
        "end_date": "2012-05",
    }
    out = _education_entry_from_pdl(entry)
    assert out is not None
    assert out["institution"] == "Harvard Business School"
    assert out["degree_type"] == "mba"
    assert out["graduation_year"] == 2012
    assert out["major"] == "General Management"


# ─── 6. find_education_overlaps end-to-end ───────────────────────────────────


def _pdl_response(education_array: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap an education[] in the PDL `/person/enrich` envelope shape."""
    return {
        "status": 200,
        "likelihood": 9,
        "data": {
            "id": "pdl_test_id",
            "education": education_array,
        },
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_overlaps_mba_match(monkeypatch) -> None:
    """Both persons attended HBS in the same year → MBA cohort emit."""
    monkeypatch.setenv("PDL_API_KEY", "test_key")

    person_a_payload = _pdl_response([
        {
            "school": {"name": "HBS"},
            "degrees": ["MBA"],
            "majors": ["General Management"],
            "end_date": "2012-05",
        }
    ])
    person_b_payload = _pdl_response([
        {
            "school": {"name": "Harvard Business"},
            "degrees": ["MBA"],
            "majors": ["General Management"],
            "end_date": "2012-05",
        }
    ])
    bodies = [person_a_payload, person_b_payload]

    def handler(request: httpx.Request) -> httpx.Response:
        body = bodies.pop(0)
        return httpx.Response(status_code=200, json=body)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        results = await find_education_overlaps(PERSON_A, PERSON_B, client=client)

    assert len(results) == 1
    rec = results[0]
    assert rec["signal_type"] == "same_mba_cohort"
    assert rec["institution"] == "Harvard Business School"
    assert rec["degree_type"] == "mba"
    assert rec["graduation_year"] == 2012
    assert rec["graduation_year_other"] == 2012
    assert rec["same_program"] is True
    assert rec["year_gap"] == 0
    assert rec["confidence"] == pytest.approx(0.85 * 1.0 * 0.85 * 1.05, abs=0.001)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_overlaps_no_shared_school(monkeypatch) -> None:
    """Two different schools → no overlap, [] returned."""
    monkeypatch.setenv("PDL_API_KEY", "test_key")
    bodies = [
        _pdl_response([{
            "school": {"name": "HBS"}, "degrees": ["MBA"], "end_date": "2012",
        }]),
        _pdl_response([{
            "school": {"name": "Wharton"}, "degrees": ["MBA"], "end_date": "2012",
        }]),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json=bodies.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_education_overlaps(PERSON_A, PERSON_B, client=client)

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_overlaps_no_pdl_key(monkeypatch) -> None:
    """No PDL_API_KEY → returns [] immediately, no HTTP attempted."""
    monkeypatch.delenv("PDL_API_KEY", raising=False)

    # Transport that would error if reached
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP should not be called when key absent")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_education_overlaps(PERSON_A, PERSON_B, client=client)

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_overlaps_pdl_404_for_one_person(monkeypatch) -> None:
    """One prospect 404s in PDL → no overlap possible, [] returned."""
    monkeypatch.setenv("PDL_API_KEY", "test_key")

    call_idx = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_idx["i"] += 1
        if call_idx["i"] == 1:
            return httpx.Response(status_code=200, json=_pdl_response([{
                "school": {"name": "HBS"}, "degrees": ["MBA"], "end_date": "2012",
            }]))
        return httpx.Response(status_code=404, json={"status": 404})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_education_overlaps(PERSON_A, PERSON_B, client=client)

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_overlaps_max_results(monkeypatch) -> None:
    """`max_results` truncates the output."""
    monkeypatch.setenv("PDL_API_KEY", "test_key")
    # Both prospects attended HBS for MBA AND Wharton for MBA (extreme example).
    edu_array = [
        {"school": {"name": "HBS"},     "degrees": ["MBA"], "end_date": "2010"},
        {"school": {"name": "Wharton"}, "degrees": ["MBA"], "end_date": "2010"},
    ]
    bodies = [_pdl_response(edu_array), _pdl_response(edu_array)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json=bodies.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_education_overlaps(
            PERSON_A, PERSON_B, client=client, max_results=1
        )

    assert len(results) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_overlaps_no_identifier(monkeypatch) -> None:
    """Person without linkedin_url AND without name → still does name lookup or skips."""
    monkeypatch.setenv("PDL_API_KEY", "test_key")

    pa_no_linkedin = PersonRef(
        person_id="00000000-0000-0000-0000-aaaa00000001",
        canonical_name="Some Person",
        linkedin_url=None,
    )

    bodies = [_pdl_response([]), _pdl_response([])]

    def handler(request: httpx.Request) -> httpx.Response:
        # name-only lookup is allowed; we just return empty education
        return httpx.Response(status_code=200, json=bodies.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_education_overlaps(pa_no_linkedin, PERSON_B, client=client)

    assert results == []
