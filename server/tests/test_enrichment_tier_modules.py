"""Tests for the Tier-1 + Tier-2 enrichment modules.

Coverage:
- ``github.py``: profile + orgs + repos parsing, rate-limit handling
- ``recognition.py``: parser + cost
- ``company_site.py``: page-kind dispatch, executive + press parsing
- ``news.py``: query construction, mention parsing, no-key short-circuit
- ``prioritize.py``: smoke (DB-touching, monkey-patched fetch/execute)

All tests use ``httpx.MockTransport`` so no real API spend.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from credence.enrichment.company_site import (
    CompanyExecutive,
    CompanySiteSignals,
    PressRelease,
    scrape_company_page,
    scrape_company_site,
)
from credence.enrichment.github import (
    GitHubProfile,
    enrich_github_profile,
    parse_org,
    parse_repo,
)
from credence.enrichment.news import find_news_mentions
from credence.enrichment.recognition import (
    DEFAULT_SOURCES,
    RecognitionSource,
    scrape_source,
)


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ═════════════════════════════════════════════════════════════════════════════
# github.py
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
def test_github_parse_repo_happy_path() -> None:
    repo = parse_repo({
        "name": "credence", "full_name": "anthropics/credence",
        "description": "B2B GTM platform", "stargazers_count": 1234,
        "forks_count": 56, "language": "TypeScript", "fork": False,
        "archived": False, "html_url": "https://github.com/anthropics/credence",
    })
    assert repo is not None
    assert repo.name == "credence"
    assert repo.stars == 1234
    assert repo.is_fork is False


@pytest.mark.unit
def test_github_parse_repo_drops_no_name() -> None:
    assert parse_repo({"description": "no name"}) is None
    assert parse_repo({"name": "x"}) is None  # missing full_name
    assert parse_repo(None) is None


@pytest.mark.unit
def test_github_parse_org_happy_path() -> None:
    org = parse_org({"login": "anthropics", "name": "Anthropic",
                      "html_url": "https://github.com/anthropics",
                      "description": "AI safety"})
    assert org is not None
    assert org.login == "anthropics"
    assert org.name == "Anthropic"


@pytest.mark.unit
def test_github_parse_org_drops_no_login() -> None:
    assert parse_org({"name": "Anthropic"}) is None


@pytest.mark.unit
async def test_github_enrich_profile_happy_path() -> None:
    """Full-stack: user + orgs + repos succeed, return populated profile."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/users/satyanadella"):
            return httpx.Response(200, json={
                "login": "satyanadella", "name": "Satya Nadella",
                "company": "Microsoft", "location": "Redmond, WA",
                "bio": "CEO at Microsoft", "blog": "satyanadella.com",
                "twitter_username": "satyanadella", "public_repos": 5,
                "public_gists": 0, "followers": 50000, "following": 100,
                "created_at": "2010-01-01T00:00:00Z",
                "html_url": "https://github.com/satyanadella",
            })
        if url.endswith("/users/satyanadella/orgs"):
            return httpx.Response(200, json=[
                {"login": "microsoft", "name": "Microsoft",
                 "html_url": "https://github.com/microsoft",
                 "description": "Tech company"}
            ])
        if "/users/satyanadella/repos" in url:
            return httpx.Response(200, json=[
                {"name": "demo", "full_name": "satyanadella/demo",
                 "stargazers_count": 100, "forks_count": 10,
                 "language": "Python", "fork": False, "archived": False,
                 "html_url": "https://github.com/satyanadella/demo"},
                {"name": "old", "full_name": "satyanadella/old",
                 "stargazers_count": 5000, "forks_count": 200,
                 "language": "C++", "fork": False, "archived": True,
                 "html_url": "https://github.com/satyanadella/old"},
            ])
        return httpx.Response(404)

    async with _client_with(handler) as client:
        profile = await enrich_github_profile(
            "satyanadella", client=client, top_repos=10
        )

    assert profile is not None
    assert profile.username == "satyanadella"
    assert profile.followers == 50000
    assert len(profile.orgs) == 1
    assert profile.orgs[0].login == "microsoft"
    # Repos come back sorted by stars desc
    assert profile.top_repos[0].stars == 5000
    assert profile.top_repos[1].stars == 100


@pytest.mark.unit
async def test_github_user_404_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    async with _client_with(handler) as client:
        profile = await enrich_github_profile("nonexistent-user", client=client)

    assert profile is None


@pytest.mark.unit
async def test_github_403_rate_limit_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="API rate limit exceeded")

    async with _client_with(handler) as client:
        profile = await enrich_github_profile("any-user", client=client)

    assert profile is None


@pytest.mark.unit
async def test_github_partial_failure_orgs_empty() -> None:
    """User succeeds, orgs fails — profile still returned with empty orgs."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/users/x"):
            return httpx.Response(200, json={
                "login": "x", "name": "X", "html_url": "https://github.com/x",
                "public_repos": 0,
            })
        # Orgs + repos return 503 — partial failure
        return httpx.Response(503)

    async with _client_with(handler) as client:
        profile = await enrich_github_profile("x", client=client)

    assert profile is not None
    assert profile.username == "x"
    assert profile.orgs == []
    assert profile.top_repos == []


@pytest.mark.unit
async def test_github_empty_username_returns_none() -> None:
    profile = await enrich_github_profile("")
    assert profile is None


# ═════════════════════════════════════════════════════════════════════════════
# recognition.py
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
def test_recognition_default_sources_present() -> None:
    """Sanity: the curated source list exists with expected bodies."""
    body_ids = {s.body_id for s in DEFAULT_SOURCES}
    assert "ieee_fellows" in body_ids
    assert "acm_fellows" in body_ids
    assert "nae_members" in body_ids


@pytest.mark.unit
async def test_recognition_scrape_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fake-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "success": True,
            "data": {"json": {"members": [
                {"name": "John Doe", "year_elected": 2020,
                 "citation": "For contributions to AI", "company": "MIT"},
                {"name": "Jane Smith", "year_elected": 2022,
                 "citation": "For semiconductor research"},
                {"name": "", "year_elected": 2023},  # empty name → drop
            ]}},
        })

    async with _client_with(handler) as client:
        result = await scrape_source(DEFAULT_SOURCES[0], client=client)

    assert result is not None
    assert len(result.records) == 2
    assert result.records[0].name == "John Doe"
    assert result.records[0].year_elected == 2020
    assert result.cost_cents == 3


@pytest.mark.unit
async def test_recognition_no_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    async with _client_with(handler) as client:
        result = await scrape_source(DEFAULT_SOURCES[0], api_key=None, client=client)

    assert result is None
    assert called is False


@pytest.mark.unit
async def test_recognition_http_error_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fake-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    async with _client_with(handler) as client:
        result = await scrape_source(DEFAULT_SOURCES[0], client=client)

    assert result is None


# ═════════════════════════════════════════════════════════════════════════════
# company_site.py
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
async def test_company_site_leadership_page_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fake-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": {"json": {"executives": [
                {"name": "Phebe Novakovic", "title": "Chairman & CEO",
                 "bio": "Leads General Dynamics."},
                {"name": "Jason Aiken", "title": "EVP & CFO"},
                {"name": "", "title": "ignored"},  # drop empty name
            ]}},
        })

    async with _client_with(handler) as client:
        result = await scrape_company_page(
            company_url="https://www.gd.com",
            page_url="https://www.gd.com/about/leadership",
            page_kind="leadership",
            client=client,
        )

    assert result is not None
    assert len(result.executives) == 2
    assert result.executives[0].name == "Phebe Novakovic"
    assert result.press_releases == []


@pytest.mark.unit
async def test_company_site_press_page_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fake-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": {"json": {"press_releases": [
                {"headline": "Lockheed Names New CTO",
                 "published_at": "2026-03-15", "url": "https://lockheed.com/p1",
                 "summary": "Effective immediately, …",
                 "mentioned_executives": ["Jane Smith"],
                 "reporting_phrases": ["will report to the COO"]},
            ]}},
        })

    async with _client_with(handler) as client:
        result = await scrape_company_page(
            company_url="https://lockheedmartin.com",
            page_url="https://lockheedmartin.com/news",
            page_kind="press",
            client=client,
        )

    assert result is not None
    assert len(result.press_releases) == 1
    pr = result.press_releases[0]
    assert pr.headline == "Lockheed Names New CTO"
    assert "will report to the COO" in pr.reporting_phrases


@pytest.mark.unit
async def test_company_site_no_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    result = await scrape_company_page(
        company_url="https://x.com", page_url="https://x.com/leadership",
        page_kind="leadership",
    )
    assert result is None


@pytest.mark.unit
async def test_company_site_scrape_company_site_skips_missing_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fake-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": {"json": {"executives": [{"name": "X", "title": "CEO"}]}},
        })

    async with _client_with(handler) as client:
        # Only leadership_url provided — press/investor skipped
        results = await scrape_company_site(
            "https://x.com",
            leadership_url="https://x.com/leadership",
            press_url=None,
            investor_url=None,
            client=client,
        )

    assert len(results) == 1
    assert results[0].page_kind == "leadership"


# ═════════════════════════════════════════════════════════════════════════════
# news.py
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
async def test_news_find_mentions_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARALLEL_API_KEY", "fake-key")

    def handler(request: httpx.Request) -> httpx.Response:
        body_str = request.read().decode("utf-8")
        # Verify the query mentions both name and company
        assert "Satya Nadella" in body_str
        assert "Microsoft" in body_str
        return httpx.Response(200, json={
            "task_id": "task-abc",
            "output": {"mentions": [
                {"title": "Microsoft Q4 Earnings",
                 "source": "Reuters", "url": "https://reuters.com/x",
                 "published_at": "2026-01-30",
                 "summary": "Satya Nadella announced…",
                 "sentiment": "positive", "kind": "press_release"},
                {"title": "Build Conference 2026",
                 "kind": "event_keynote"},
                {"title": ""},  # empty title → drop
            ]},
        })

    async with _client_with(handler) as client:
        result = await find_news_mentions(
            "Satya Nadella", company_name="Microsoft", client=client
        )

    assert result is not None
    assert len(result.mentions) == 2
    assert result.mentions[0].title == "Microsoft Q4 Earnings"
    assert result.mentions[0].sentiment == "positive"
    assert result.cost_cents == 50
    assert result.task_id == "task-abc"


@pytest.mark.unit
async def test_news_no_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    async with _client_with(handler) as client:
        result = await find_news_mentions("X", api_key=None, client=client)

    assert result is None
    assert called is False


@pytest.mark.unit
async def test_news_zero_mentions_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PARALLEL_API_KEY", "fake-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "task_id": "task-empty", "output": {"mentions": []},
        })

    async with _client_with(handler) as client:
        result = await find_news_mentions("Obscure Person", client=client)

    assert result is not None
    assert result.mentions == []
    # Cost still charged for the empty task
    assert result.cost_cents == 50


@pytest.mark.unit
async def test_news_http_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARALLEL_API_KEY", "fake-key")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS down", request=request)

    async with _client_with(handler) as client:
        result = await find_news_mentions("X", client=client)

    assert result is None


@pytest.mark.unit
async def test_news_empty_name_returns_none() -> None:
    assert await find_news_mentions("") is None
    assert await find_news_mentions("   ") is None


# ═════════════════════════════════════════════════════════════════════════════
# prioritize.py — DB-touching, stub fetch/execute
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
async def test_prioritize_select_top_decile(monkeypatch: pytest.MonkeyPatch) -> None:
    """select_top_decile picks the top N% via SQL — stub fetch."""
    from credence.enrichment import prioritize as prioritize_module

    test_account = UUID("00000000-0000-0000-0000-000000000001")
    state = {"count_called": 0, "list_called": 0}

    async def fake_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if "count(*)" in sql.lower():
            state["count_called"] += 1
            return [{"n": 1000}]
        # The list query — return 100 prospects (top 10% of 1000)
        state["list_called"] += 1
        return [
            {
                "id": UUID(f"00000000-0000-0000-0000-{i:012d}"),
                "canonical_name": f"Person {i}",
                "linkedin_url": f"https://linkedin.com/in/p{i}",
                "current_title": "VP Engineering",
                "current_seniority_score": 90 - i,
                "company_name": "Acme",
            }
            for i in range(100)
        ]

    monkeypatch.setattr(prioritize_module, "fetch", fake_fetch)

    prospects = await prioritize_module.select_top_decile(
        test_account, percentile=10
    )
    assert len(prospects) == 100
    assert prospects[0].canonical_name == "Person 0"
    assert state["count_called"] == 1
    assert state["list_called"] == 1


@pytest.mark.unit
async def test_prioritize_no_eligible_persons_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from credence.enrichment import prioritize as prioritize_module

    async def fake_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if "count(*)" in sql.lower():
            return [{"n": 0}]
        return []

    monkeypatch.setattr(prioritize_module, "fetch", fake_fetch)
    prospects = await prioritize_module.select_top_decile(
        UUID("00000000-0000-0000-0000-000000000001")
    )
    assert prospects == []


@pytest.mark.unit
def test_prioritize_invalid_percentile_raises() -> None:
    """percentile must be in (0, 100]."""
    import asyncio

    from credence.enrichment.prioritize import select_top_decile

    test_account = UUID("00000000-0000-0000-0000-000000000001")

    async def run_invalid() -> None:
        await select_top_decile(test_account, percentile=0)

    with pytest.raises(ValueError, match="percentile"):
        asyncio.run(run_invalid())
