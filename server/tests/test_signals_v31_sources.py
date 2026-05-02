"""Tests for the v3.1 source-name expansion in `signals.py` (Plan B6).

Locks the dispatch + confidence + signal_type behavior of the three new
sources (`education`, `conference`, `standards`) without touching the
existing `test_signals.py` fixtures. The new sources are stub-backed
until DarkBeaver's B3/B4/B5 ship — these tests pin the route's wiring,
not the extractor implementations.

Coverage:
1. Each new source name is accepted by the request schema
2. Each new source dispatches to its own extractor function in `_EXTRACTORS`
3. Education multi-type signal_type read from payload
4. Education confidence scaling per cohort kind
5. Conference + standards confidence + default signal_type
6. Default `sources` list now includes all 7 source names
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from credence.api import app
from credence import signals as signals_module
from credence.extractors.patents import PersonRef


PROSPECT_A_ID = UUID("00000000-0000-0000-0000-00000000a001")
PROSPECT_B_ID = UUID("00000000-0000-0000-0000-00000000b002")


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Credence-Demo": "true"},
    ) as c:
        yield c


@pytest.fixture
def stub_db_v31(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Minimal DB shim that captures persisted signals.

    Mirrors `test_signals.py::stub_db` but locally so the two suites stay
    independent. Same persist signature: takes `account_id` per Wave 6.
    """
    state: dict[str, Any] = {"persisted": []}

    async def fake_load(prospect_id: UUID) -> PersonRef:
        return PersonRef(person_id=str(prospect_id), canonical_name="Test")

    async def fake_persist(
        prospect_id: UUID,
        account_id: UUID,
        source: str,
        signal_type: str,
        structured_value: dict[str, Any],
        confidence: float,
    ) -> UUID:
        sig_id = uuid4()
        state["persisted"].append(
            {
                "id": sig_id,
                "source": source,
                "signal_type": signal_type,
                "structured_value": structured_value,
                "confidence": confidence,
            }
        )
        return sig_id

    monkeypatch.setattr(signals_module, "_load_person_ref", fake_load)
    monkeypatch.setattr(signals_module, "_persist_signal", fake_persist)
    return state


@pytest.fixture
def stub_v31_extractors(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Programmable stubs for the 3 new sources.

    Replaces `_EXTRACTORS` with a 7-key dict (4 v3 stubs returning [] +
    3 v3.1 stubs whose return is per-test-controllable via state).
    """
    state: dict[str, Any] = {
        "education": [],
        "conference": [],
        "standards": [],
    }

    async def empty_stub(person_a, person_b, *, max_results):
        return []

    async def make_stub_for(source: str):
        async def stub(person_a, person_b, *, max_results: int):
            return list(state[source])

        return stub

    # Build the dict with closures so each new-source key returns its own
    # state slice. The four legacy keys are no-op-empty.
    async def edu(person_a, person_b, *, max_results: int):
        return list(state["education"])

    async def conf(person_a, person_b, *, max_results: int):
        return list(state["conference"])

    async def stds(person_a, person_b, *, max_results: int):
        return list(state["standards"])

    new_extractors = {
        "uspto": empty_stub,
        "scholar": empty_stub,
        "career": empty_stub,
        "parallel": empty_stub,
        "education": edu,
        "conference": conf,
        "standards": stds,
    }
    monkeypatch.setattr(signals_module, "_EXTRACTORS", new_extractors)
    return state


def _payload(sources: list[str]) -> dict[str, Any]:
    return {
        "prospect_a_id": str(PROSPECT_A_ID),
        "prospect_b_id": str(PROSPECT_B_ID),
        "sources": sources,
        "max_results_per_source": 25,
        "timeout_seconds": 5.0,
    }


# ── Cases ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_education_source_accepted_and_dispatched(
    client, stub_db_v31, stub_v31_extractors
) -> None:
    """`sources=['education']` runs the education extractor + persists hits."""
    stub_v31_extractors["education"] = [
        {
            "signal_type": "same_mba_cohort",
            "institution": "Harvard Business School",
            "degree_type": "mba",
            "graduation_year": 2012,
            "same_program": True,
            "confidence": 0.91,
        }
    ]

    resp = await client.post(
        "/signals/discover-connections", json=_payload(["education"])
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connections_found"] == 1
    rec = body["connections"][0]
    assert rec["source"] == "education"
    assert rec["signal_type"] == "same_mba_cohort"
    # When extractor provides confidence, route honors it (not the default)
    assert rec["confidence"] == 0.91


@pytest.mark.unit
async def test_education_falls_back_to_signal_type_default_when_payload_lacks_confidence(
    client, stub_db_v31, stub_v31_extractors
) -> None:
    """No `confidence` in payload → route uses STRENGTH_TABLE fallback."""
    stub_v31_extractors["education"] = [
        {
            "signal_type": "same_phd_program",
            "institution": "MIT EECS",
            "degree_type": "phd",
            "graduation_year": 2015,
        }
    ]
    resp = await client.post(
        "/signals/discover-connections", json=_payload(["education"])
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connections_found"] == 1
    # 0.78 from V3_PT2.md L399 same_phd_program baseStrength
    assert body["connections"][0]["confidence"] == 0.78


@pytest.mark.unit
async def test_education_unknown_signal_type_yields_alumni_network_fallback(
    client, stub_db_v31, stub_v31_extractors
) -> None:
    """Missing `signal_type` → falls back to alumni_network (weakest cohort)."""
    stub_v31_extractors["education"] = [
        {"institution": "Some School", "degree_type": "bs", "graduation_year": 2010}
    ]
    resp = await client.post(
        "/signals/discover-connections", json=_payload(["education"])
    )
    body = resp.json()
    assert body["connections_found"] == 1
    rec = body["connections"][0]
    assert rec["signal_type"] == "alumni_network"
    assert rec["confidence"] == 0.25  # alumni_network table value


@pytest.mark.unit
async def test_conference_source_accepted_and_dispatched(
    client, stub_db_v31, stub_v31_extractors
) -> None:
    """`sources=['conference']` (Firecrawl-program) runs distinct from `parallel`."""
    stub_v31_extractors["conference"] = [
        {
            "signal_type": "conference_co_presenter",
            "event": "ISSCC 2023",
            "year": 2023,
            "role": "speaker",
        }
    ]
    resp = await client.post(
        "/signals/discover-connections", json=_payload(["conference"])
    )
    body = resp.json()
    assert body["connections_found"] == 1
    rec = body["connections"][0]
    assert rec["source"] == "conference"
    assert rec["signal_type"] == "conference_co_presenter"
    assert rec["confidence"] == 0.80  # STRENGTH_TABLE


@pytest.mark.unit
async def test_conference_default_signal_type_is_attendee(
    client, stub_db_v31, stub_v31_extractors
) -> None:
    """Payload without signal_type → conference_co_attendee + 0.20 conf."""
    stub_v31_extractors["conference"] = [
        {"event": "Hot Chips 2024", "year": 2024, "role": "attendee"}
    ]
    resp = await client.post(
        "/signals/discover-connections", json=_payload(["conference"])
    )
    body = resp.json()
    rec = body["connections"][0]
    assert rec["signal_type"] == "conference_co_attendee"
    assert rec["confidence"] == 0.20


@pytest.mark.unit
async def test_standards_source_accepted_and_dispatched(
    client, stub_db_v31, stub_v31_extractors
) -> None:
    """`sources=['standards']` (Firecrawl-roster) returns standards_committee_peer."""
    stub_v31_extractors["standards"] = [
        {"committee": "JEDEC JC-42.4", "years": "2018-2022"}
    ]
    resp = await client.post(
        "/signals/discover-connections", json=_payload(["standards"])
    )
    body = resp.json()
    assert body["connections_found"] == 1
    rec = body["connections"][0]
    assert rec["source"] == "standards"
    assert rec["signal_type"] == "standards_committee_peer"
    assert rec["confidence"] == 0.82


@pytest.mark.unit
async def test_default_sources_list_includes_all_seven(
    client, stub_db_v31, stub_v31_extractors
) -> None:
    """No `sources` in request → all 7 source names attempted."""
    resp = await client.post(
        "/signals/discover-connections",
        json={
            "prospect_a_id": str(PROSPECT_A_ID),
            "prospect_b_id": str(PROSPECT_B_ID),
            "max_results_per_source": 25,
            "timeout_seconds": 5.0,
        },
    )
    body = resp.json()
    expected = {"uspto", "scholar", "career", "parallel", "education", "conference", "standards"}
    assert set(body["sources_attempted"]) == expected


@pytest.mark.unit
async def test_extractor_stubs_are_importable_at_module_level() -> None:
    """The 3 new stub functions exist + are awaitable + return [] today."""
    from credence.extractors import (
        find_conference_program_appearances,
        find_education_overlaps,
        find_standards_roster_memberships,
    )

    pa = PersonRef(person_id=str(PROSPECT_A_ID), canonical_name="Test A")
    pb = PersonRef(person_id=str(PROSPECT_B_ID), canonical_name="Test B")

    edu = await find_education_overlaps(pa, pb, max_results=25)
    conf = await find_conference_program_appearances(pa, pb, max_results=25)
    stds = await find_standards_roster_memberships(pa, pb, max_results=25)

    assert edu == [] and conf == [] and stds == []
