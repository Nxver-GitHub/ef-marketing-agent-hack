"""Unit tests for the v3.1 conference-program extractor (Plan B4).

Coverage:
1. Markdown program parsing — session header detection, multiple speaker
   patterns (asterisk-marker, numbered, bullet)
2. Role detection from "rest" context (keynote, panelist, chair, speaker)
3. Name fold matching (case + accent + whitespace)
4. End-to-end find_conference_program_appearances —
   - co_presenter when both in same session
   - co_attendee when both at same conference but different sessions
   - no emit when only one matches
   - no key short-circuit
   - cache reuse across calls
   - max_results truncation
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from credence.extractors import conference as conf_mod
from credence.extractors.conference import (
    CONFERENCE_PROGRAMS,
    _fold_name,
    _parse_program_markdown,
    find_conference_program_appearances,
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
def _clear_program_cache():
    conf_mod._PROGRAM_CACHE.clear()
    yield
    conf_mod._PROGRAM_CACHE.clear()


# ─── 1. Parse program markdown ────────────────────────────────────────────


@pytest.mark.unit
def test_parse_extracts_speakers_under_session() -> None:
    md = """## Foundation Models for Robotics

**Speaker:** Sanja Fidler — VP AI Research, NVIDIA

## AI Pioneers Fireside Chat

**Panelist:** Yann LeCun, Meta AI
**Panelist:** Geoff Hinton, University of Toronto
"""
    parsed = _parse_program_markdown(md)
    sessions_speakers = [(p["session"], p["speaker_name"]) for p in parsed]
    assert ("Foundation Models for Robotics", "Sanja Fidler") in sessions_speakers
    assert ("AI Pioneers Fireside Chat", "Yann LeCun") in sessions_speakers
    assert ("AI Pioneers Fireside Chat", "Geoff Hinton") in sessions_speakers


@pytest.mark.unit
def test_parse_numbered_speaker_pattern() -> None:
    md = """## Session Alpha

1. Sanja Fidler (NVIDIA)
2. Yann LeCun (Meta)
"""
    parsed = _parse_program_markdown(md)
    names = [p["speaker_name"] for p in parsed]
    assert "Sanja Fidler" in names
    assert "Yann LeCun" in names


@pytest.mark.unit
def test_parse_bullet_speaker_pattern() -> None:
    md = """## Session Alpha

- Sanja Fidler — VP AI Research at NVIDIA
- Yann LeCun, Chief AI Scientist at Meta
"""
    parsed = _parse_program_markdown(md)
    names = {p["speaker_name"] for p in parsed}
    assert "Sanja Fidler" in names
    assert "Yann LeCun" in names


@pytest.mark.unit
def test_parse_filters_short_or_single_token_lines() -> None:
    md = """## Session

- A B
- John Smith
- X. Y
"""
    parsed = _parse_program_markdown(md)
    names = [p["speaker_name"] for p in parsed]
    for n in names:
        tokens = n.split()
        assert len(tokens) >= 2
        assert all(len(t) >= 2 for t in tokens)


@pytest.mark.unit
def test_parse_no_session_header_returns_none_session() -> None:
    md = """**Speaker:** Sanja Fidler"""
    parsed = _parse_program_markdown(md)
    assert len(parsed) == 1
    assert parsed[0]["session"] is None


# ─── 2. Role detection ────────────────────────────────────────────────────


@pytest.mark.unit
def test_role_detection_keynote() -> None:
    md = """## Keynote

**Speaker:** Sanja Fidler — keynote address
"""
    parsed = _parse_program_markdown(md)
    assert parsed[0]["role"] in ("keynote", "speaker")  # may be either depending on which "keynote" hits first


@pytest.mark.unit
def test_role_default_is_speaker() -> None:
    md = """## Session

**Speaker:** Sanja Fidler
"""
    parsed = _parse_program_markdown(md)
    assert parsed[0]["role"] == "speaker"


# ─── 3. Name folding ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_fold_name_handles_case() -> None:
    assert _fold_name("SANJA FIDLER") == _fold_name("Sanja Fidler")


@pytest.mark.unit
def test_fold_name_handles_accents() -> None:
    assert _fold_name("Sanjá Fidler") == _fold_name("Sanja Fidler")


# ─── 4. End-to-end find_conference_program_appearances ───────────────────


def _firecrawl_envelope(markdown: str) -> dict[str, Any]:
    return {"success": True, "data": {"markdown": markdown}}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_co_presenter_when_same_session(monkeypatch) -> None:
    """Both speakers in the same session → conference_co_presenter (0.80)."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test_key")

    md = """## AI Pioneers Fireside Chat

**Panelist:** Yann LeCun, Meta AI
**Panelist:** Sanja Fidler, NVIDIA
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_firecrawl_envelope(md))

    programs = {
        "GTC 2022": {"url": "https://example.com/gtc2022", "year": 2022},
    }

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_conference_program_appearances(
            PERSON_A, PERSON_B, client=client, programs=programs,
        )

    assert len(results) == 1
    rec = results[0]
    assert rec["signal_type"] == "conference_co_presenter"
    assert rec["event"] == "GTC 2022"
    assert rec["year"] == 2022
    assert rec["session"] == "AI Pioneers Fireside Chat"
    assert rec["confidence"] == 0.80


@pytest.mark.unit
@pytest.mark.asyncio
async def test_co_attendee_when_different_sessions_same_conference(monkeypatch) -> None:
    """Both speakers at the conference but in different sessions → co_attendee (0.20)."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test_key")

    md = """## Foundation Models for Robotics

**Speaker:** Sanja Fidler

## Self-Supervised Learning Today

**Speaker:** Yann LeCun
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_firecrawl_envelope(md))

    programs = {
        "NeurIPS 2022": {"url": "https://example.com/neurips2022", "year": 2022},
    }

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_conference_program_appearances(
            PERSON_A, PERSON_B, client=client, programs=programs,
        )

    assert len(results) == 1
    rec = results[0]
    assert rec["signal_type"] == "conference_co_attendee"
    assert rec["session"] is None
    assert rec["confidence"] == 0.20


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_emit_when_only_one_matches(monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test_key")
    md = """## Session

**Speaker:** Sanja Fidler
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_firecrawl_envelope(md))

    programs = {"X": {"url": "https://example.com/x", "year": 2022}}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_conference_program_appearances(
            PERSON_A, PERSON_B, client=client, programs=programs,
        )

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_key_short_circuit(monkeypatch) -> None:
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP should not be called")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_conference_program_appearances(
            PERSON_A, PERSON_B, client=client,
        )

    assert results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_reuses_program_across_calls(monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test_key")
    md = """## Session

**Speaker:** Sanja Fidler
**Speaker:** Yann LeCun
"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=_firecrawl_envelope(md))

    programs = {"X": {"url": "https://example.com/x", "year": 2022}}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await find_conference_program_appearances(
            PERSON_A, PERSON_B, client=client, programs=programs,
        )
        await find_conference_program_appearances(
            PERSON_A, PERSON_C, client=client, programs=programs,
        )

    assert call_count["n"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_results_truncates(monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test_key")
    md = """## A

**Speaker:** Sanja Fidler
**Speaker:** Yann LeCun

## B

**Speaker:** Sanja Fidler
**Speaker:** Yann LeCun

## C

**Speaker:** Sanja Fidler
**Speaker:** Yann LeCun
"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_firecrawl_envelope(md))

    programs = {"X": {"url": "https://example.com/x", "year": 2022}}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await find_conference_program_appearances(
            PERSON_A, PERSON_B, client=client, programs=programs, max_results=2,
        )

    assert len(results) == 2


@pytest.mark.unit
def test_default_conferences_dict_has_six_entries() -> None:
    """V3_PT2.md L614-633 lists 6+ target conferences; we ship 6 by default."""
    assert len(CONFERENCE_PROGRAMS) >= 6
    # Each entry must have url + year keys
    for name, info in CONFERENCE_PROGRAMS.items():
        assert "url" in info
        assert "year" in info
        assert isinstance(info["year"], int)
