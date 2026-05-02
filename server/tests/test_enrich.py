"""Tests for `credence.enrich.enrich_prospect` — Contract 8 orchestration.

Mirrors `test_signals.py` patterns: monkeypatched DB shims, monkeypatched
vendor runners, FastAPI ASGITransport client.

Coverage:
1. Happy path — Apollo returns email → 200 + 1 record + cost log written
2. Prospect not found → 400
3. Cache fresh → cached=true, no vendor call, cost_cents=0
4. Refresh=true → bypass cache, hit vendor anyway
5. Apollo declines (returns None) → vendors_failed includes apollo
6. Apollo raises → vendors_failed, log success=false
7. Endpoint timeout → all vendors marked failed, truncated-style response
8. Empty vendors list → 200, no records, no calls
9. cost_cents propagated into response
10. Apollo fields persisted back to prospects.email
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from credence import enrich as enrich_module
from credence.api import app
from credence.enrichment.apollo import APOLLO_EMAIL_CREDIT_CENTS

PROSPECT_ID = UUID("00000000-0000-0000-0000-aaaa00000001")
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture(autouse=True)
def _stub_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """SwiftElk's M4 `assert_budget` queries `enrichment_cost_log` via the
    real DB at pre-flight. Tests don't have a live Postgres — short-circuit
    to a no-op so the route's budget gate is exercised structurally without
    hitting the network. The dedicated budget tests live in
    `tests/test_budget.py` (SwiftElk).
    """
    async def fake_assert_budget(*args, **kwargs):
        return None

    # Patch the import-bound name in the enrich module so the route picks
    # up the stub regardless of how it dereferences the helper.
    monkeypatch.setattr(enrich_module, "assert_budget", fake_assert_budget)


@pytest.fixture
async def client():
    """AsyncClient with a default demo header so the M2 SessionMiddleware
    short-circuits to the demo pseudo-tenant for these route tests."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Credence-Demo": "true"},
    ) as c:
        yield c


@pytest.fixture
def stub_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub fetchrow + execute (and the prospect lookup helpers) so tests
    don't hit Supabase. State dict tracks what got written."""
    state: dict[str, Any] = {
        "prospect": {
            "id": PROSPECT_ID,
            "name": "Lin Wei",
            "company": "TSMC",
            "linkedin_url": "https://linkedin.com/in/lin-wei",
            "email": None,
            "email_status": None,
            "current_title": None,
            "last_enriched_at": None,
            "account_id": ACCOUNT_ID,
        },
        "missing_prospect": False,
        "cost_log_rows": [],
        "prospect_updates": [],
    }

    async def fake_fetchrow(sql: str, *args: Any) -> Any:
        sql_upper = sql.upper().strip()
        if "FROM PROSPECTS WHERE ID" in sql_upper or "FROM PROSPECTS\n" in sql_upper:
            if state["missing_prospect"]:
                return None
            return dict(state["prospect"])
        return None

    async def fake_execute(sql: str, *args: Any) -> Any:
        sql_upper = sql.upper()
        if "INSERT INTO ENRICHMENT_COST_LOG" in sql_upper:
            state["cost_log_rows"].append(
                {
                    "prospect_id": args[0],
                    "account_id": args[1],
                    "vendor": args[2],
                    "endpoint": args[3],
                    "cost_cents": args[4],
                    "cache_hit": args[5],
                    "success": args[6],
                    "error_message": args[7],
                }
            )
        elif "UPDATE PROSPECTS" in sql_upper:
            update = {"prospect_id": args[0]}
            if len(args) > 1:
                update["email"] = args[1]
            if len(args) > 2:
                update["email_status"] = args[2]
            if len(args) > 3:
                update["current_title"] = args[3]
            state["prospect_updates"].append(update)
        return "ok"

    monkeypatch.setattr(enrich_module, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(enrich_module, "execute", fake_execute)
    return state


@pytest.fixture
def stub_apollo(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "return": (
            {
                "email": "lin.wei@tsmc.com",
                "email_status": "verified",
                "current_title": "VP Process Engineering",
                "current_company_name": "TSMC",
                "apollo_person_id": "apollo-12345",
            },
            APOLLO_EMAIL_CREDIT_CENTS,
            0.95,
        ),
        "raise": None,
        "delay": 0.0,
    }

    async def fake_apollo(prospect: Any, max_cost_cents: int, client: Any = None) -> Any:
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        if state["raise"] is not None:
            raise state["raise"]
        return state["return"]

    monkeypatch.setitem(enrich_module._VENDOR_RUNNERS, "apollo", fake_apollo)
    return state


@pytest.fixture
def stub_firecrawl(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the firecrawl runner. Defaults to returning None (no linkedin_url
    on the test prospect) so it doesn't pollute happy-path apollo tests with
    extra records. Tests that exercise firecrawl explicitly can flip the
    state["return"] field."""
    state: dict[str, Any] = {
        "return": None,  # no linkedin_url → runner returns None by default
        "raise": None,
        "delay": 0.0,
    }

    async def fake_firecrawl(prospect: Any, max_cost_cents: int, client: Any = None) -> Any:
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        if state["raise"] is not None:
            raise state["raise"]
        return state["return"]

    monkeypatch.setitem(enrich_module._VENDOR_RUNNERS, "firecrawl", fake_firecrawl)
    return state


@pytest.fixture
def stub_pdl(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the PDL runner — defaults to None (no key configured)."""
    state: dict[str, Any] = {
        "return": None,
        "raise": None,
        "delay": 0.0,
    }

    async def fake_pdl(prospect: Any, max_cost_cents: int, client: Any = None) -> Any:
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        if state["raise"] is not None:
            raise state["raise"]
        return state["return"]

    monkeypatch.setitem(enrich_module._VENDOR_RUNNERS, "pdl", fake_pdl)
    return state


@pytest.mark.unit
async def test_happy_path_apollo(client, stub_db, stub_apollo) -> None:
    resp = await client.post(
        f"/enrich/{PROSPECT_ID}", json={"vendors": ["apollo"], "max_cost_cents": 100}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["records"]) == 1
    rec = body["records"][0]
    assert rec["vendor"] == "apollo"
    assert rec["fields"]["email"] == "lin.wei@tsmc.com"
    assert rec["cost_cents"] == APOLLO_EMAIL_CREDIT_CENTS
    assert body["total_cost_cents"] == APOLLO_EMAIL_CREDIT_CENTS
    assert body["vendors_attempted"] == ["apollo"]
    assert body["vendors_failed"] == []
    # Persistence side effects
    assert len(stub_db["cost_log_rows"]) == 1
    assert stub_db["cost_log_rows"][0]["success"] is True
    assert stub_db["cost_log_rows"][0]["cost_cents"] == APOLLO_EMAIL_CREDIT_CENTS
    assert any(u.get("email") == "lin.wei@tsmc.com" for u in stub_db["prospect_updates"])


@pytest.mark.unit
async def test_prospect_not_found(client, stub_db, stub_apollo) -> None:
    stub_db["missing_prospect"] = True
    resp = await client.post(f"/enrich/{PROSPECT_ID}", json={"vendors": ["apollo"]})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "prospect_not_found"


@pytest.mark.unit
async def test_apollo_declines_returns_failed(client, stub_db, stub_apollo) -> None:
    """Apollo returns None (no match / no key / over cap) — apollo in vendors_failed."""
    stub_apollo["return"] = None
    resp = await client.post(f"/enrich/{PROSPECT_ID}", json={"vendors": ["apollo"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["vendors_failed"] == ["apollo"]
    assert body["records"] == []
    assert len(stub_db["cost_log_rows"]) == 1
    assert stub_db["cost_log_rows"][0]["success"] is False


@pytest.mark.unit
async def test_apollo_raises_returns_failed(client, stub_db, stub_apollo) -> None:
    stub_apollo["raise"] = RuntimeError("apollo blew up")
    resp = await client.post(f"/enrich/{PROSPECT_ID}", json={"vendors": ["apollo"]})
    assert resp.status_code == 200
    assert "apollo" in resp.json()["vendors_failed"]
    assert stub_db["cost_log_rows"][0]["success"] is False


@pytest.mark.unit
async def test_endpoint_timeout(client, stub_db, stub_apollo) -> None:
    stub_apollo["delay"] = 1.0
    resp = await client.post(
        f"/enrich/{PROSPECT_ID}",
        json={"vendors": ["apollo"], "timeout_seconds": 0.05},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["records"] == []
    assert body["vendors_failed"] == ["apollo"]


@pytest.mark.unit
async def test_unknown_vendors_skipped(client, stub_db, stub_apollo) -> None:
    """Vendors not yet wired (parallel — lives in signals.py per Contract 1) skipped."""
    resp = await client.post(
        f"/enrich/{PROSPECT_ID}",
        json={"vendors": ["parallel"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["vendors_attempted"] == []
    assert body["records"] == []


@pytest.mark.unit
async def test_default_vendors_uses_registry(
    client, stub_db, stub_apollo, stub_firecrawl, stub_pdl
) -> None:
    """Omitting `vendors` uses every wired runner.

    Currently registered: apollo (Phase 1) + firecrawl (Phase 4) + pdl (Phase 2).
    Parallel lives in signals.py per Contract 1 (per-pair, not per-prospect).
    Order assertion uses a set comparison so tests don't break on iteration-
    order rearrangements in the registry.
    """
    resp = await client.post(f"/enrich/{PROSPECT_ID}", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["vendors_attempted"]) == {"apollo", "firecrawl", "pdl"}


@pytest.mark.unit
async def test_pdl_persistence_writes_employment_periods(
    client, stub_db, stub_apollo, stub_pdl, stub_firecrawl
) -> None:
    """When PDL returns structured fields, they land on the prospect row."""
    stub_pdl["return"] = (
        {
            "linkedin_url": "https://linkedin.com/in/lin-wei",
            "skills": ["3nm yield", "GAA transistors"],
            "employment_periods": [
                {
                    "company_name": "TSMC",
                    "title": "VP Process Engineering",
                    "functional_domain": "engineering",
                    "start_date": "2018-04",
                    "end_date": None,
                    "is_current": True,
                }
            ],
            "pdl_person_id": "qEnOZ5Oh0poWnQ1luFBfVw_0000",
        },
        28,
        0.9,
    )
    resp = await client.post(
        f"/enrich/{PROSPECT_ID}", json={"vendors": ["pdl"], "max_cost_cents": 100}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["records"]) == 1
    rec = body["records"][0]
    assert rec["vendor"] == "pdl"
    assert rec["fields"]["pdl_person_id"] == "qEnOZ5Oh0poWnQ1luFBfVw_0000"
    assert rec["fields"]["employment_periods"][0]["company_name"] == "TSMC"
    assert rec["cost_cents"] == 28
    # Persistence: the UPDATE prospects … pdl branch fires
    assert any(u["prospect_id"] == PROSPECT_ID for u in stub_db["prospect_updates"])


@pytest.mark.unit
async def test_concurrent_enrich_no_lost_cost_logs(
    client, stub_db, stub_apollo
) -> None:
    """Two simultaneous /enrich calls on the same prospect — neither response
    is dropped, both cost-log rows are written. Locks current race semantics
    (each call IS billable, no false dedup)."""
    resp_a, resp_b = await asyncio.gather(
        client.post(f"/enrich/{PROSPECT_ID}", json={"vendors": ["apollo"]}),
        client.post(f"/enrich/{PROSPECT_ID}", json={"vendors": ["apollo"]}),
    )
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    # Each call must produce its own cost-log row — total = 2 (one per call).
    apollo_rows = [
        r for r in stub_db["cost_log_rows"]
        if r["vendor"] == "apollo"
    ]
    assert len(apollo_rows) == 2, (
        f"expected 2 cost-log rows for 2 concurrent calls, got {len(apollo_rows)}"
    )
    # Both calls must record success (or both fail — but never one silently dropped)
    assert all(r["success"] for r in apollo_rows)


@pytest.mark.unit
async def test_apollo_reports_to_triggers_ingest_explicit_edge(
    client, stub_db, stub_apollo, monkeypatch
) -> None:
    """Phase A.6: when Apollo returns reports_to fields and the prospect +
    manager both resolve to persons rows, `ingest_explicit_edge` is called
    once with signal_type='linkedin_reports_to' and confidence=0.92.
    """
    from uuid import UUID as _UUID

    from credence import enrich as enrich_mod

    PROSPECT_PERSON_ID = _UUID("00000000-0000-0000-0000-bbbb00000001")
    MANAGER_PERSON_ID = _UUID("00000000-0000-0000-0000-bbbb00000002")

    stub_apollo["return"] = (
        {
            "email": "lin.wei@tsmc.com",
            "email_status": "verified",
            "current_title": "VP Process Engineering",
            "apollo_person_id": "apollo-12345",
            "reports_to_name": "Wei Chen",
            "reports_to_apollo_id": "apollo-manager-99",
        },
        3,
        0.95,
    )

    # Monkeypatch the helper resolvers so they return canned person UUIDs
    async def fake_resolve_prospect(prospect_id):
        return PROSPECT_PERSON_ID

    async def fake_resolve_manager(name, linkedin_url):
        assert name == "Wei Chen"
        return MANAGER_PERSON_ID

    captured: dict = {"calls": []}

    async def fake_ingest(**kwargs):
        captured["calls"].append(kwargs)

    monkeypatch.setattr(
        enrich_mod, "_resolve_prospect_person_id", fake_resolve_prospect
    )
    monkeypatch.setattr(
        enrich_mod, "_resolve_manager_person_id", fake_resolve_manager
    )
    monkeypatch.setattr(
        enrich_mod.orgchart_hierarchy, "ingest_explicit_edge", fake_ingest
    )

    resp = await client.post(
        f"/enrich/{PROSPECT_ID}", json={"vendors": ["apollo"]}
    )
    assert resp.status_code == 200, resp.text
    assert len(captured["calls"]) == 1
    call = captured["calls"][0]
    assert call["manager_id"] == MANAGER_PERSON_ID
    assert call["report_id"] == PROSPECT_PERSON_ID
    assert call["account_id"] == ACCOUNT_ID
    assert call["signal_type"] == "linkedin_reports_to"
    assert call["confidence"] == 0.92


@pytest.mark.unit
async def test_reports_to_failure_does_not_break_enrichment(
    client, stub_db, stub_apollo, monkeypatch
) -> None:
    """Phase A.6 — Contract 8 partial-results: if the explicit-edge write
    raises, the rest of the enrichment response must still succeed.
    """
    from credence import enrich as enrich_mod

    stub_apollo["return"] = (
        {
            "email": "lin.wei@tsmc.com",
            "email_status": "verified",
            "current_title": "VP Process Engineering",
            "apollo_person_id": "apollo-12345",
            "reports_to_name": "Wei Chen",
        },
        3,
        0.95,
    )

    async def boom(*a, **kw):
        raise RuntimeError("DB unavailable")

    # Force the resolver to blow up — simulates a Postgres glitch.
    monkeypatch.setattr(enrich_mod, "_resolve_prospect_person_id", boom)

    resp = await client.post(
        f"/enrich/{PROSPECT_ID}", json={"vendors": ["apollo"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    # Apollo record still came through despite the org-chart failure.
    assert len(body["records"]) == 1
    assert body["records"][0]["fields"]["email"] == "lin.wei@tsmc.com"


@pytest.mark.unit
async def test_response_includes_elapsed_ms(client, stub_db, stub_apollo) -> None:
    resp = await client.post(f"/enrich/{PROSPECT_ID}", json={"vendors": ["apollo"]})
    assert isinstance(resp.json()["elapsed_ms"], int)
    assert resp.json()["elapsed_ms"] >= 0
