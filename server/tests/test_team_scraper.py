"""Tests for ``credence.onboarding.team_scraper``.

Covers the size-aware strategy switch, GTM keyword filter, progress
write cadence, error handling, idempotency, and cost accounting.

Mock-only — no live HTTP, no live Postgres. ``httpx.MockTransport``
intercepts the Apify calls; ``credence.db.acquire`` is monkeypatched to
yield a mock connection that records every UPDATE.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from credence import db
from credence.onboarding import team_scraper
from credence.onboarding.team_scraper import (
    COMPANY_DETAIL_ACTOR,
    EMPLOYEES_ACTOR,
    GTM_STRATEGY_THRESHOLD,
    PROGRESS_WRITE_INTERVAL,
    ScrapedEmployee,
    TeamScrapeResult,
    _is_gtm_employee,
    scrape_team_for_account,
)

ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")
JOB_ID = UUID("00000000-0000-0000-0000-0000000000aa")
COMPANY_URL = "https://www.linkedin.com/company/nvidia/"


# ── Fixtures ─────────────────────────────────────────────────────────────


def _employee(
    slug: str,
    name: str,
    *,
    headline: str | None = "Engineer at NVIDIA",
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "profile_url": f"https://linkedin.com/in/{slug}",
        "public_identifier": slug,
        "fullname": name,
        "first_name": name.split()[0],
        "last_name": " ".join(name.split()[1:]) or "X",
        "headline": headline,
        "current_title": title,
        "current_company": "NVIDIA",
        "location": {"full": "Santa Clara, CA, US", "country_code": "US"},
        "profile_picture_url": f"https://media.licdn.com/{slug}.jpg",
    }


def _gtm_employees(n: int) -> list[dict[str, Any]]:
    titles = [
        "Senior Sales Engineer",
        "VP of Marketing",
        "Customer Success Manager",
        "Strategic Alliances Lead",
        "Business Development Director",
        "Head of Partnerships",
        "Account Executive",
        "Demand Generation Manager",
    ]
    out: list[dict[str, Any]] = []
    for i in range(n):
        out.append(_employee(
            f"gtm-{i}", f"GTM Person{i}",
            headline=titles[i % len(titles)],
        ))
    return out


def _non_gtm_employees(n: int) -> list[dict[str, Any]]:
    titles = [
        "Senior Software Engineer",
        "Principal Hardware Engineer",
        "Staff Researcher",
        "Manufacturing Operations Director",
        "Compiler Engineer",
    ]
    out: list[dict[str, Any]] = []
    for i in range(n):
        out.append(_employee(
            f"eng-{i}", f"Engineer Person{i}",
            headline=titles[i % len(titles)],
        ))
    return out


# ── Mock asyncpg connection / pool ───────────────────────────────────────


class _MockConn:
    """Records every execute() call so tests can assert on UPDATE cadence."""

    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executes.append((sql, args))
        return "UPDATE 1"


@pytest.fixture
def mock_db(monkeypatch: pytest.MonkeyPatch) -> _MockConn:
    """Replace ``credence.db.acquire`` with one that yields a single shared mock conn."""
    conn = _MockConn()

    @asynccontextmanager
    async def _fake_acquire():
        yield conn

    monkeypatch.setattr(db, "acquire", _fake_acquire)
    monkeypatch.setattr(team_scraper.db, "acquire", _fake_acquire)
    return conn


# ── Mock HTTP transport ──────────────────────────────────────────────────


def _make_handler(
    *,
    employees: list[dict[str, Any]] | None = None,
    company_size: int | None = None,
    employees_status: int = 201,
    company_status: int = 201,
) -> tuple[Any, dict[str, Any]]:
    """Return (handler, captures). captures['employees_calls'] etc."""
    captures: dict[str, Any] = {
        "employees_calls": 0,
        "company_calls": 0,
        "last_employees_body": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content) if request.content else {}
        if COMPANY_DETAIL_ACTOR in url:
            captures["company_calls"] += 1
            if company_status not in (200, 201):
                return httpx.Response(company_status, text="upstream error")
            payload: list[dict[str, Any]] = []
            if company_size is not None:
                payload = [{"employee_count": company_size,
                            "name": "NVIDIA"}]
            return httpx.Response(company_status, json=payload)
        if EMPLOYEES_ACTOR in url:
            captures["employees_calls"] += 1
            captures["last_employees_body"] = body
            if employees_status not in (200, 201):
                return httpx.Response(employees_status, text="upstream error")
            return httpx.Response(employees_status, json=employees or [])
        return httpx.Response(404, text=f"unmocked {url}")

    return handler, captures


def _client_for(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── _is_gtm_employee unit tests ──────────────────────────────────────────


@pytest.mark.unit
def test_is_gtm_employee_matches_known_titles():
    assert _is_gtm_employee("Senior Account Executive", None) is True
    assert _is_gtm_employee(None, "VP of Marketing") is True
    assert _is_gtm_employee("Customer Success Manager", None) is True
    assert _is_gtm_employee("Head of Strategic Alliances", None) is True
    assert _is_gtm_employee("Business Development Director", None) is True
    assert _is_gtm_employee("Director of Channel Sales", None) is True


@pytest.mark.unit
def test_is_gtm_employee_rejects_non_gtm_titles():
    assert _is_gtm_employee("Principal Hardware Engineer", None) is False
    assert _is_gtm_employee("Staff Software Engineer", None) is False
    assert _is_gtm_employee(None, None) is False
    assert _is_gtm_employee("", "") is False


# ── 1. all_employees strategy returns full roster ────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_employees_strategy_returns_full_roster(mock_db: _MockConn):
    roster = _gtm_employees(3) + _non_gtm_employees(4)
    handler, _ = _make_handler(employees=roster, company_size=120)
    async with _client_for(handler) as client:
        result = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="all_employees",
            onboarding_job_id=JOB_ID,
            max_employees=500,
            api_token="test-token",
            client=client,
        )
    assert isinstance(result, TeamScrapeResult)
    assert result.strategy_used == "all_employees"
    assert result.total_returned == 7
    assert {e.canonical_name for e in result.employees} == {
        f"GTM Person{i}" for i in range(3)
    } | {f"Engineer Person{i}" for i in range(4)}


# ── 2. gtm_only strategy filters out non-GTM titles ──────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gtm_only_strategy_filters_non_gtm(mock_db: _MockConn):
    roster = _gtm_employees(2) + _non_gtm_employees(5)
    handler, _ = _make_handler(employees=roster, company_size=50)
    async with _client_for(handler) as client:
        result = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="gtm_only",
            onboarding_job_id=JOB_ID,
            max_employees=500,
            api_token="test-token",
            client=client,
        )
    assert result.strategy_used == "gtm_only"
    assert result.total_returned == 2
    assert all("GTM Person" in e.canonical_name for e in result.employees)


# ── 3. Strategy auto-switches based on probed company size ───────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strategy_auto_switches_when_company_large(mock_db: _MockConn):
    # User asked for 'all_employees' but company has 5000 employees.
    roster = _gtm_employees(3) + _non_gtm_employees(3)
    handler, _ = _make_handler(employees=roster, company_size=5000)
    async with _client_for(handler) as client:
        result = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="all_employees",
            onboarding_job_id=JOB_ID,
            max_employees=500,
            api_token="test-token",
            client=client,
        )
    # Auto-switched because 5000 >= GTM_STRATEGY_THRESHOLD (500).
    assert GTM_STRATEGY_THRESHOLD == 500
    assert result.strategy_used == "gtm_only"
    # Only the 3 GTM employees survived.
    assert result.total_returned == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strategy_does_not_switch_when_company_small(mock_db: _MockConn):
    roster = _gtm_employees(2) + _non_gtm_employees(4)
    handler, _ = _make_handler(employees=roster, company_size=200)
    async with _client_for(handler) as client:
        result = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="all_employees",
            onboarding_job_id=JOB_ID,
            max_employees=500,
            api_token="test-token",
            client=client,
        )
    assert result.strategy_used == "all_employees"
    assert result.total_returned == 6


# ── 4. max_employees cap respected ───────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_employees_cap_respected(mock_db: _MockConn):
    roster = _gtm_employees(50) + _non_gtm_employees(50)
    handler, _ = _make_handler(employees=roster, company_size=100)
    async with _client_for(handler) as client:
        result = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="all_employees",
            onboarding_job_id=JOB_ID,
            max_employees=10,
            api_token="test-token",
            client=client,
        )
    assert result.total_returned == 10


# ── 5. Progress writes happen at expected intervals ──────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_progress_writes_at_expected_intervals(mock_db: _MockConn):
    # 60 GTM employees → expect interval writes at 25, 50 + final flush = 3 UPDATEs.
    roster = _gtm_employees(60)
    handler, _ = _make_handler(employees=roster, company_size=100)
    async with _client_for(handler) as client:
        result = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="all_employees",
            onboarding_job_id=JOB_ID,
            max_employees=500,
            api_token="test-token",
            client=client,
        )
    assert result.total_returned == 60
    # Intervals at 25 and 50, then final flush at 60. (60 % 25 != 0 so
    # the intermediate write at 60 doesn't fire — only the final flush.)
    assert PROGRESS_WRITE_INTERVAL == 25
    assert len(mock_db.executes) == 3
    # Last UPDATE shows the terminal scraped value.
    last_sql, last_args = mock_db.executes[-1]
    assert "UPDATE public.onboarding_jobs" in last_sql
    last_progress = last_args[1]
    assert last_progress["scraped"] == 60
    assert last_progress["matched"] == 0
    assert last_progress["new_persons"] == 0


# ── 6. Progress writes use the right onboarding_job_id ───────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_progress_writes_use_correct_job_id(mock_db: _MockConn):
    roster = _gtm_employees(5)
    custom_job_id = uuid4()
    handler, _ = _make_handler(employees=roster, company_size=50)
    async with _client_for(handler) as client:
        await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="all_employees",
            onboarding_job_id=custom_job_id,
            max_employees=500,
            api_token="test-token",
            client=client,
        )
    assert mock_db.executes, "expected at least one progress UPDATE"
    for _sql, args in mock_db.executes:
        assert args[0] == custom_job_id


# ── 7. Apify actor failure → empty result with error, cost_usd=None ─────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apify_failure_returns_empty_with_no_cost(mock_db: _MockConn):
    handler, _ = _make_handler(
        employees=[],
        company_size=None,           # probe also fails to return data
        employees_status=500,
        company_status=500,
    )
    async with _client_for(handler) as client:
        result = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="all_employees",
            onboarding_job_id=JOB_ID,
            max_employees=500,
            api_token="test-token",
            client=client,
        )
    assert result.total_returned == 0
    assert result.employees == []
    assert result.error is not None
    assert "apify_status_500" in result.error
    assert result.cost_usd is None


# ── 8. Cost is logged (positive number for successful scrape) ────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cost_is_positive_on_successful_scrape(mock_db: _MockConn):
    roster = _gtm_employees(5) + _non_gtm_employees(5)
    handler, _ = _make_handler(employees=roster, company_size=100)
    async with _client_for(handler) as client:
        result = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="all_employees",
            onboarding_job_id=JOB_ID,
            max_employees=500,
            api_token="test-token",
            client=client,
        )
    assert result.cost_usd is not None
    # 10 employees @ $0.01 + $0.005 probe = $0.105
    assert result.cost_usd > 0
    assert result.cost_usd == pytest.approx(0.105, rel=0.01)


# ── 9. Empty company (zero employees) returns valid empty result ─────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_company_returns_valid_empty_result(mock_db: _MockConn):
    handler, _ = _make_handler(employees=[], company_size=0)
    async with _client_for(handler) as client:
        result = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="all_employees",
            onboarding_job_id=JOB_ID,
            max_employees=500,
            api_token="test-token",
            client=client,
        )
    assert result.total_returned == 0
    assert result.employees == []
    assert result.strategy_used == "all_employees"
    assert result.error is None
    # Final progress flush still happened.
    assert len(mock_db.executes) >= 1
    final_progress = mock_db.executes[-1][1][1]
    assert final_progress["scraped"] == 0


# ── 10. Idempotency — same inputs twice produce equal employee lists ────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_idempotency_same_inputs_same_output(mock_db: _MockConn):
    roster = _gtm_employees(4) + _non_gtm_employees(4)
    handler, _ = _make_handler(employees=roster, company_size=100)

    async with _client_for(handler) as client:
        first = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="all_employees",
            onboarding_job_id=JOB_ID,
            max_employees=500,
            api_token="test-token",
            client=client,
        )
    handler2, _ = _make_handler(employees=roster, company_size=100)
    async with _client_for(handler2) as client2:
        second = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="all_employees",
            onboarding_job_id=JOB_ID,
            max_employees=500,
            api_token="test-token",
            client=client2,
        )
    a = [(e.linkedin_url, e.canonical_name) for e in first.employees]
    b = [(e.linkedin_url, e.canonical_name) for e in second.employees]
    assert a == b
    assert first.total_returned == second.total_returned
    assert first.strategy_used == second.strategy_used


# ── Bonus: Apify returns duplicate URLs → deduped ────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_employee_urls_are_deduped(mock_db: _MockConn):
    dup = _employee("alice", "Alice Smith", headline="VP Sales")
    roster = [dup, dup, _employee("bob", "Bob Jones", headline="Sales Director")]
    handler, _ = _make_handler(employees=roster, company_size=100)
    async with _client_for(handler) as client:
        result = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="gtm_only",
            onboarding_job_id=JOB_ID,
            max_employees=500,
            api_token="test-token",
            client=client,
        )
    assert result.total_returned == 2
    urls = [e.linkedin_url for e in result.employees]
    assert len(urls) == len(set(urls))


# ── Bonus: probe failure does not break scrape (fall through to all) ────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_failure_falls_back_to_caller_strategy(mock_db: _MockConn):
    roster = _gtm_employees(3) + _non_gtm_employees(3)
    # Probe fails (500), employees succeed.
    handler, _ = _make_handler(
        employees=roster,
        company_size=None,
        company_status=500,
    )
    async with _client_for(handler) as client:
        result = await scrape_team_for_account(
            ACCOUNT_ID, COMPANY_URL,
            strategy="all_employees",
            onboarding_job_id=JOB_ID,
            max_employees=500,
            api_token="test-token",
            client=client,
        )
    # Probe failed → keep caller's strategy → full roster returned.
    assert result.strategy_used == "all_employees"
    assert result.total_returned == 6


# ── Bonus: ScrapedEmployee parses essential fields ──────────────────────


@pytest.mark.unit
def test_scraped_employee_carries_expected_fields():
    e = ScrapedEmployee(
        linkedin_url="https://linkedin.com/in/jane",
        canonical_name="Jane Doe",
        current_title="VP of Marketing",
        current_company="Acme",
        profile_photo_url="https://media.licdn.com/jane.jpg",
        headline="Marketing leader",
        location="NYC",
    )
    assert e.linkedin_url == "https://linkedin.com/in/jane"
    assert e.canonical_name == "Jane Doe"
    assert e.headline == "Marketing leader"
