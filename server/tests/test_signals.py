"""Tests for `credence.signals.discover_connections` — Contract 1 orchestration.

Exercises the FastAPI route end-to-end with monkeypatched extractors + DB
shims. No real PatentsView, Semantic Scholar, or Supabase calls.

Coverage:
1. Happy path with one source returning data
2. Per-source filtering via `sources=[...]`
3. Same prospect IDs → Pydantic 422
4. Prospect not found → 400
5. Partial results — one extractor raises → sources_failed includes it
6. Timeout — extractor hangs → truncated=true, all sources failed
7. Empty result — extractors return [] → 200 with connections_found=0
8. Persistence-failure cascade — every persist raises → 502
9. Truncation — extractor returns >= max_results items → truncated=true
10. Career sub-types — extractor's per-payload signal_type honored
11. Confidence policy — uspto=0.95, scholar tiers by author_count, career by sub-type

References:
- CONTRACTS.md Contract 1
- CLAUDE.md L770-834 (the spec the route implements)
- Mirrors signals.py:215 (the route under test)
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from credence import signals as signals_module
from credence.api import app
from credence.extractors.patents import PersonRef

# ── Fixtures ────────────────────────────────────────────────────────────────


PROSPECT_A_ID = UUID("00000000-0000-0000-0000-00000000a001")
PROSPECT_B_ID = UUID("00000000-0000-0000-0000-00000000b002")


def _person_ref(uuid_: UUID, name: str = "Test Person") -> PersonRef:
    return PersonRef(person_id=str(uuid_), canonical_name=name)


@pytest.fixture
async def client():
    """Async test client that bypasses lifespan (no real DB pool).

    Sends `X-Credence-Demo: true` on every request so the M2 SessionMiddleware
    (added in Wave 6) resolves to the demo pseudo-tenant without needing a
    Supabase JWT. Tests for the route's own logic don't care which tenant
    they run under, only that the request reaches the handler.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Credence-Demo": "true"},
    ) as c:
        yield c


@pytest.fixture
def stub_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub `_load_person_ref` and `_persist_signal` so tests don't hit the DB.

    Returns a state dict the test can inspect (`persisted` rows count,
    `persist_should_fail` toggle).
    """
    state: dict[str, Any] = {
        "persisted": [],
        "persist_should_fail": False,
        "missing_prospects": set(),
    }

    async def fake_load(prospect_id: UUID) -> PersonRef:
        if prospect_id in state["missing_prospects"]:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=400,
                detail={
                    "error": "prospect_not_found",
                    "field": "prospect_id",
                    "value": str(prospect_id),
                },
            )
        return _person_ref(prospect_id)

    async def fake_persist(
        prospect_id: UUID,
        account_id: UUID,
        source: str,
        signal_type: str,
        structured_value: dict[str, Any],
        confidence: float,
    ) -> UUID:
        if state["persist_should_fail"]:
            raise RuntimeError("simulated persist failure")
        sig_id = uuid4()
        state["persisted"].append(
            {
                "id": sig_id,
                "prospect_id": prospect_id,
                "account_id": account_id,
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
def stub_extractors(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the 3 extractors with controllable async stubs."""
    state: dict[str, Any] = {
        "uspto": {"return": [], "raise": None, "delay": 0.0},
        "scholar": {"return": [], "raise": None, "delay": 0.0},
        "career": {"return": [], "raise": None, "delay": 0.0},
    }

    def make_stub(source: str):
        async def stub(person_a, person_b, *, max_results: int):
            cfg = state[source]
            if cfg["delay"]:
                await asyncio.sleep(cfg["delay"])
            if cfg["raise"] is not None:
                raise cfg["raise"]
            return list(cfg["return"])

        return stub

    new_extractors = {
        "uspto": make_stub("uspto"),
        "scholar": make_stub("scholar"),
        "career": make_stub("career"),
    }
    monkeypatch.setattr(signals_module, "_EXTRACTORS", new_extractors)
    return state


def _post_payload(
    *,
    a: UUID = PROSPECT_A_ID,
    b: UUID = PROSPECT_B_ID,
    sources: list[str] | None = None,
    max_results: int = 25,
    timeout: float = 5.0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "prospect_a_id": str(a),
        "prospect_b_id": str(b),
        "max_results_per_source": max_results,
        "timeout_seconds": timeout,
    }
    if sources is not None:
        payload["sources"] = sources
    return payload


# ── Cases ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_happy_path_one_source(client, stub_db, stub_extractors) -> None:
    """USPTO returns one patent → response includes one ConnectionRecord."""
    stub_extractors["uspto"]["return"] = [
        {
            "patent_number": "10,234,567",
            "patent_title": "Yield optimization method",
            "filing_date": "2018-04-21",
            "grant_date": "2020-01-14",
            "assignee": "Intel Corporation",
            "uspto_url": "https://patents.uspto.gov/10234567",
        }
    ]
    resp = await client.post("/signals/discover-connections", json=_post_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connections_found"] == 1
    assert body["connections"][0]["signal_type"] == "patent_co_inventor"
    assert body["connections"][0]["source"] == "uspto"
    assert body["connections"][0]["confidence"] == 0.95
    assert body["sources_failed"] == []
    assert "uspto" in body["sources_attempted"]
    assert body["truncated"] is False
    assert len(stub_db["persisted"]) == 1


@pytest.mark.unit
async def test_filters_to_requested_sources(client, stub_db, stub_extractors) -> None:
    """When `sources=['uspto']`, scholar/career are not invoked."""
    stub_extractors["uspto"]["return"] = [
        {
            "patent_number": "P1",
            "patent_title": "T",
            "filing_date": "2020-01-01",
            "assignee": "X",
        }
    ]
    stub_extractors["scholar"]["return"] = [{"paper_title": "should not appear"}]

    resp = await client.post(
        "/signals/discover-connections", json=_post_payload(sources=["uspto"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sources_attempted"] == ["uspto"]
    assert all(c["source"] == "uspto" for c in body["connections"])


@pytest.mark.unit
async def test_same_prospect_ids_rejected(client, stub_db, stub_extractors) -> None:
    """Pydantic validator catches A == B."""
    resp = await client.post(
        "/signals/discover-connections",
        json=_post_payload(a=PROSPECT_A_ID, b=PROSPECT_A_ID),
    )
    assert resp.status_code == 422  # Pydantic ValueError → FastAPI 422


@pytest.mark.unit
async def test_prospect_not_found(client, stub_db, stub_extractors) -> None:
    """When `_load_person_ref` raises, route returns 400."""
    stub_db["missing_prospects"].add(PROSPECT_A_ID)
    resp = await client.post("/signals/discover-connections", json=_post_payload())
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "prospect_not_found"


@pytest.mark.unit
async def test_partial_results_one_extractor_raises(
    client, stub_db, stub_extractors
) -> None:
    """USPTO raises, scholar succeeds — partial-results contract."""
    stub_extractors["uspto"]["raise"] = RuntimeError("USPTO blew up")
    stub_extractors["scholar"]["return"] = [
        {
            "paper_title": "Some paper",
            "venue": "NeurIPS",
            "year": 2023,
            "citation_count": 5,
            "semantic_scholar_id": "ssX",
        }
    ]
    resp = await client.post("/signals/discover-connections", json=_post_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert "uspto" in body["sources_failed"]
    assert "scholar" not in body["sources_failed"]
    assert any(c["source"] == "scholar" for c in body["connections"])


@pytest.mark.unit
async def test_timeout_returns_truncated(
    client, stub_db, stub_extractors
) -> None:
    """Extractor hangs > timeout → response has truncated=true and all sources_failed."""
    stub_extractors["uspto"]["delay"] = 2.0  # exceeds timeout below
    resp = await client.post(
        "/signals/discover-connections", json=_post_payload(timeout=0.1)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    assert body["connections_found"] == 0
    assert set(body["sources_failed"]) == set(body["sources_attempted"])


@pytest.mark.unit
async def test_no_connections_found(client, stub_db, stub_extractors) -> None:
    """All extractors return [] → 200 with connections_found=0 (success, not error)."""
    resp = await client.post("/signals/discover-connections", json=_post_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["connections_found"] == 0
    assert body["connections"] == []
    assert body["sources_failed"] == []
    assert body["truncated"] is False


@pytest.mark.unit
async def test_persistence_failure_returns_502_when_all_fail(
    client, stub_db, stub_extractors
) -> None:
    """Every signal write fails AND no successes → 502 per Contract 1."""
    stub_db["persist_should_fail"] = True
    stub_extractors["uspto"]["return"] = [
        {
            "patent_number": "P",
            "patent_title": "T",
            "filing_date": "2020-01-01",
            "assignee": "X",
        }
    ]
    resp = await client.post("/signals/discover-connections", json=_post_payload())
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "signal_persist_failed"


@pytest.mark.unit
async def test_truncation_at_max_results(client, stub_db, stub_extractors) -> None:
    """Extractor returns >= max_results_per_source → truncated=true."""
    cap = 3
    stub_extractors["uspto"]["return"] = [
        {
            "patent_number": f"P{i}",
            "patent_title": "T",
            "filing_date": "2020-01-01",
            "assignee": "X",
        }
        for i in range(cap)
    ]
    resp = await client.post(
        "/signals/discover-connections", json=_post_payload(max_results=cap)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True


@pytest.mark.unit
async def test_career_signal_types_honored(
    client, stub_db, stub_extractors
) -> None:
    """Career extractor sets signal_type per-payload (3 sub-types)."""
    stub_extractors["career"]["return"] = [
        {
            "signal_type": "career_overlap_same_team",
            "company_name": "Intel",
            "overlap_years": 4,
        },
        {
            "signal_type": "career_overlap_general",
            "company_name": "Apple",
            "overlap_years": 1,
        },
    ]
    resp = await client.post(
        "/signals/discover-connections", json=_post_payload(sources=["career"])
    )
    assert resp.status_code == 200
    body = resp.json()
    sig_types = sorted(c["signal_type"] for c in body["connections"])
    assert sig_types == ["career_overlap_general", "career_overlap_same_team"]
    # Confidence per Contract 1 / CLAUDE.md L924-933
    by_type = {c["signal_type"]: c["confidence"] for c in body["connections"]}
    assert by_type["career_overlap_same_team"] == 0.88
    assert by_type["career_overlap_general"] == 0.60


@pytest.mark.unit
async def test_scholar_confidence_tiers_by_author_count(
    client, stub_db, stub_extractors
) -> None:
    """Per Contract 1: scholar confidence is 0.90 for ≤5 authors, 0.75 otherwise."""
    stub_extractors["scholar"]["return"] = [
        {
            "paper_title": "Few authors",
            "venue": "V",
            "year": 2023,
            "citation_count": 1,
            "author_count": 3,
        },
        {
            "paper_title": "Many authors",
            "venue": "V",
            "year": 2023,
            "citation_count": 1,
            "author_count": 12,
        },
    ]
    resp = await client.post(
        "/signals/discover-connections", json=_post_payload(sources=["scholar"])
    )
    assert resp.status_code == 200
    confidences = sorted(c["confidence"] for c in resp.json()["connections"])
    assert confidences == [0.75, 0.90]


@pytest.mark.unit
async def test_connected_to_propagated_into_structured_value(
    client, stub_db, stub_extractors
) -> None:
    """Every persisted structured_value gets `connected_to` field per Contract 1."""
    stub_extractors["uspto"]["return"] = [
        {
            "patent_number": "P",
            "patent_title": "T",
            "filing_date": "2020-01-01",
            "assignee": "X",
        }
    ]
    resp = await client.post("/signals/discover-connections", json=_post_payload())
    assert resp.status_code == 200
    sv = resp.json()["connections"][0]["structured_value"]
    assert sv["connected_to"] == str(PROSPECT_B_ID)
    assert sv["patent_number"] == "P"


@pytest.mark.unit
async def test_response_includes_elapsed_ms(client, stub_db, stub_extractors) -> None:
    resp = await client.post("/signals/discover-connections", json=_post_payload())
    assert resp.status_code == 200
    assert isinstance(resp.json()["elapsed_ms"], int)
    assert resp.json()["elapsed_ms"] >= 0


@pytest.mark.unit
async def test_extractors_called_with_max_results_arg(
    client, stub_db, stub_extractors, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route passes max_results_per_source through to each extractor."""
    captured: dict[str, int] = {}

    async def make_capturing_stub(name: str):
        async def stub(person_a, person_b, *, max_results: int):
            captured[name] = max_results
            return []

        return stub

    new_extractors = {
        name: await make_capturing_stub(name) for name in ("uspto", "scholar", "career")
    }
    monkeypatch.setattr(signals_module, "_EXTRACTORS", new_extractors)
    await client.post(
        "/signals/discover-connections", json=_post_payload(max_results=7)
    )
    assert captured["uspto"] == 7
    assert captured["scholar"] == 7
    assert captured["career"] == 7
