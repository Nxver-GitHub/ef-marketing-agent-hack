"""Unit tests for the v3.1 standards-roster extractor (Plan B5).

Coverage:
1. Roster parsing — header detection, member-line extraction, year parsing
2. Name normalization — case + accent folding
3. End-to-end find_standards_roster_memberships — match, mismatch, no-key,
   roster-fetch-fail, multi-body
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from credence.extractors import standards as standards_mod
from credence.extractors.standards import (
    STANDARDS_BODIES,
    _fold_name,
    _parse_roster_markdown,
    find_standards_roster_memberships,
)
from credence.extractors.patents import PersonRef


PERSON_A = PersonRef(
    person_id="00000000-0000-0000-0000-aaaa00000001",
    canonical_name="Sanja Fidler",
)
PERSON_B = PersonRef(
    person_id="00000000-0000-0000-0000-bbbb00000002",
    canonical_name="Yann LeCun",
)
PERSON_C = PersonRef(
    person_id="00000000-0000-0000-0000-cccc00000003",
    canonical_name="Geoff Hinton",
)


@pytest.fixture(autouse=True)
def _clear_roster_cache() -> None:
    """Reset module-level cache between tests."""
    standards_mod._ROSTER_CACHE.clear()
    yield
    standards_mod._ROSTER_CACHE.clear()


# ─── 1. _fold_name ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_fold_name_case_insensitive() -> None:
    assert _fold_name("Sanja Fidler") == _fold_name("sanja fidler")
    assert _fold_name("SANJA FIDLER") == _fold_name("Sanja Fidler")


@pytest.mark.unit
def test_fold_name_accent_insensitive() -> None:
    assert _fold_name("Sanjá Fidler") == _fold_name("Sanja Fidler")
    assert _fold_name("Müller") == _fold_name("Muller")


@pytest.mark.unit
def test_fold_name_collapses_whitespace() -> None:
    assert _fold_name("Sanja  Fidler") == _fold_name("Sanja Fidler")
    assert _fold_name("  Sanja Fidler  ") == _fold_name("Sanja Fidler")


# ─── 2. _parse_roster_markdown ──────────────────────────────────────────────


@pytest.mark.unit
def test_parse_roster_extracts_members_under_committee() -> None:
    md = """## JC-42.4 / Memory Module Subcommittee

Active members (2018-2022):

- John Smith — Micron Technology
- Jane Doe — Samsung Electronics
- Sanja Fidler — NVIDIA Research
"""
    parsed = _parse_roster_markdown(md, body="JEDEC")
    members = [(p["committee"], p["member_name"]) for p in parsed]
    assert ("JC-42.4 / Memory Module Subcommittee", "John Smith") in members
    assert ("JC-42.4 / Memory Module Subcommittee", "Jane Doe") in members
    assert ("JC-42.4 / Memory Module Subcommittee", "Sanja Fidler") in members


@pytest.mark.unit
def test_parse_roster_propagates_years() -> None:
    md = """## Working Group Alpha

Active members (2018-2022):

- John Smith — Acme Corp
"""
    parsed = _parse_roster_markdown(md, body="IEEE SA")
    assert parsed[0]["years"] == "2018-2022"


@pytest.mark.unit
def test_parse_roster_falls_back_to_body_name_when_no_header() -> None:
    """A flat list with no committee header → committee = body name."""
    md = """- John Smith — Acme
- Jane Doe — Foo Inc
"""
    parsed = _parse_roster_markdown(md, body="MLCommons")
    assert all(p["committee"] == "MLCommons" for p in parsed)


@pytest.mark.unit
def test_parse_roster_filters_short_names() -> None:
    """Single-token or very short matches don't pass the 2-token min."""
    md = """- A B
- John Smith
- X. Y
"""
    parsed = _parse_roster_markdown(md, body="JEDEC")
    names = [p["member_name"] for p in parsed]
    assert "John Smith" in names
    # Short single-token entries shouldn't appear
    for n in names:
        tokens = n.split()
        assert len(tokens) >= 2
        assert all(len(t) >= 2 for t in tokens)


# ─── 3. find_standards_roster_memberships end-to-end ────────────────────────


def _firecrawl_envelope(markdown: str, success: bool = True) -> dict[str, Any]:
    return {
        "success": success,
        "data": {"markdown": markdown},
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_emits_when_both_match_same_committee(monkeypatch) -> None:
    """Both persons appear in the same committee → one emit dict."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test_key")

    md = """## JC-42.4 / Memory Module Subcommittee

Active members (2018-2022):

- Sanja Fidler — NVIDIA
- Yann LeCun — Meta AI
- John Smith — Acme
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_firecrawl_envelope(md))

    bodies = {"JEDEC": "https://example.com/jedec"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_standards_roster_memberships(
            PERSON_A, PERSON_B, client=client, bodies=bodies
        )

    assert len(results) == 1
    rec = results[0]
    assert rec["signal_type"] == "standards_committee_peer"
    assert rec["committee"] == "JC-42.4 / Memory Module Subcommittee"
    assert rec["body"] == "JEDEC"
    assert rec["years"] == "2018-2022"
    assert rec["url"] == "https://example.com/jedec"
    assert rec["confidence"] == 0.82


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_no_emit_when_only_one_matches(monkeypatch) -> None:
    """One match in roster, the other absent → []."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test_key")
    md = """## Committee

- Sanja Fidler — NVIDIA
- John Smith — Acme
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_firecrawl_envelope(md))

    bodies = {"JEDEC": "https://example.com/jedec"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_standards_roster_memberships(
            PERSON_A, PERSON_B, client=client, bodies=bodies
        )

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_returns_empty_without_api_key(monkeypatch) -> None:
    """No FIRECRAWL_API_KEY → [] without HTTP attempt."""
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP should not be called")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_standards_roster_memberships(
            PERSON_A, PERSON_B, client=client
        )

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_skips_failed_body_continues_others(monkeypatch) -> None:
    """One body 500s → skip it, other bodies still contribute."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test_key")

    good_md = """## CommitteeName

- Sanja Fidler — NVIDIA
- Yann LeCun — Meta
"""

    def handler(request: httpx.Request) -> httpx.Response:
        # firecrawl-scrape POST endpoint is the same for every body; the
        # target URL lives in the JSON body. Branch on that.
        body_text = request.content.decode("utf-8")
        if "good" in body_text:
            return httpx.Response(200, json=_firecrawl_envelope(good_md))
        return httpx.Response(500, json={"error": "upstream"})

    bodies = {
        "GOOD_BODY": "https://example.com/good",
        "BAD_BODY": "https://example.com/bad",
    }

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_standards_roster_memberships(
            PERSON_A, PERSON_B, client=client, bodies=bodies
        )

    # Only good body should produce a result
    assert len(results) == 1
    assert results[0]["body"] == "GOOD_BODY"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_handles_multiple_committees_per_body(monkeypatch) -> None:
    """Both persons on TWO committees in same body → 2 emit dicts."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test_key")
    md = """## Committee Alpha

- Sanja Fidler — NVIDIA
- Yann LeCun — Meta

## Committee Beta

- Sanja Fidler — NVIDIA
- Yann LeCun — Meta
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_firecrawl_envelope(md))

    bodies = {"BODY": "https://example.com/x"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_standards_roster_memberships(
            PERSON_A, PERSON_B, client=client, bodies=bodies
        )

    assert len(results) == 2
    committees = {r["committee"] for r in results}
    assert "Committee Alpha" in committees
    assert "Committee Beta" in committees


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_max_results_truncates(monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test_key")
    md = """## Committee Alpha

- Sanja Fidler — NVIDIA
- Yann LeCun — Meta

## Committee Beta

- Sanja Fidler — NVIDIA
- Yann LeCun — Meta

## Committee Gamma

- Sanja Fidler — NVIDIA
- Yann LeCun — Meta
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_firecrawl_envelope(md))

    bodies = {"BODY": "https://example.com/x"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_standards_roster_memberships(
            PERSON_A, PERSON_B, client=client, bodies=bodies, max_results=2,
        )

    assert len(results) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_caches_roster_per_body_within_module(monkeypatch) -> None:
    """Second call against same body uses cache, no second HTTP."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test_key")
    md = """## Committee

- Sanja Fidler — NVIDIA
- Yann LeCun — Meta
"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=_firecrawl_envelope(md))

    bodies = {"BODY": "https://example.com/x"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await find_standards_roster_memberships(
            PERSON_A, PERSON_B, client=client, bodies=bodies
        )
        await find_standards_roster_memberships(
            PERSON_A, PERSON_C, client=client, bodies=bodies
        )

    # Only one HTTP fetch — second call hit the module cache
    assert call_count["n"] == 1


@pytest.mark.unit
def test_standards_bodies_include_six_per_spec() -> None:
    """V3_PT2.md L695-702 specifies 6 bodies."""
    expected = {
        "JEDEC", "IEEE SA", "SEMI",
        "Wi-Fi Alliance", "RISC-V International", "MLCommons",
    }
    assert set(STANDARDS_BODIES.keys()) == expected
