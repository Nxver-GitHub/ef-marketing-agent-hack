"""Tests for `credence.enrichment.apify_posts` — Tier-2 LinkedIn posts."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from credence.enrichment.apify_posts import (
    ACTOR_ID,
    LinkedInPost,
    compute_cost_cents,
    parse_post,
    scrape_profile_posts,
)


def _mk_post(
    *,
    url: str = "https://www.linkedin.com/posts/satyanadella_post123",
    author_url: str = "https://www.linkedin.com/in/satyanadella/",
    text: str = "Excited to announce…",
    likes: int = 1500,
    comments: int = 80,
    reposts: int = 200,
    mentioned: list[str] | None = None,
    hashtags: list[str] | None = None,
    has_image: bool = True,
) -> dict[str, Any]:
    return {
        "url": url,
        "author": {"profileUrl": author_url, "name": "Satya Nadella"},
        "text": text,
        "publishedAt": "2026-01-15T14:30:00Z",
        "likesCount": likes,
        "commentsCount": comments,
        "repostsCount": reposts,
        "mentionedProfiles": [{"url": u} for u in (mentioned or [])],
        "hashtags": hashtags or ["AI", "Microsoft"],
        "media": [{"type": "IMAGE", "url": "https://example.com/img.jpg"}] if has_image else [],
    }


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _apify_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "fake-test-token")


# ── parse_post ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_post_happy_path() -> None:
    p = parse_post(_mk_post())
    assert p is not None
    assert p.post_url == "https://www.linkedin.com/posts/satyanadella_post123"
    assert p.author_linkedin_url == "https://www.linkedin.com/in/satyanadella/"
    assert p.text == "Excited to announce…"
    assert p.likes_count == 1500
    assert p.comments_count == 80
    assert p.reposts_count == 200
    assert "AI" in p.hashtags
    assert p.has_image is True


@pytest.mark.unit
def test_parse_post_no_url_returns_none() -> None:
    raw = _mk_post()
    raw["url"] = ""
    assert parse_post(raw) is None


@pytest.mark.unit
def test_parse_post_no_author_returns_none() -> None:
    raw = _mk_post()
    raw["author"] = {}
    raw["authorProfileUrl"] = None
    assert parse_post(raw) is None


@pytest.mark.unit
def test_parse_post_uses_fallback_author() -> None:
    """When author is missing inline, fallback_author_url is used."""
    raw = _mk_post()
    raw["author"] = {}
    p = parse_post(raw, fallback_author_url="https://www.linkedin.com/in/fallback/")
    assert p is not None
    assert p.author_linkedin_url == "https://www.linkedin.com/in/fallback/"


@pytest.mark.unit
def test_parse_post_extracts_mentions() -> None:
    raw = _mk_post(mentioned=[
        "https://www.linkedin.com/in/jeffweiner/",
        "https://www.linkedin.com/in/billgates/",
    ])
    p = parse_post(raw)
    assert p is not None
    assert len(p.mentioned_profile_urls) == 2
    assert "https://www.linkedin.com/in/jeffweiner/" in p.mentioned_profile_urls


@pytest.mark.unit
def test_parse_post_handles_video_media() -> None:
    raw = _mk_post(has_image=False)
    raw["media"] = [{"type": "VIDEO", "url": "https://example.com/v.mp4"}]
    p = parse_post(raw)
    assert p is not None
    assert p.has_video is True
    assert p.has_image is False


@pytest.mark.unit
def test_parse_post_string_mentions_list() -> None:
    """Some actor versions return mentioned profiles as plain string list."""
    raw = _mk_post(mentioned=[])
    raw["mentionedProfiles"] = ["https://www.linkedin.com/in/billgates/"]
    p = parse_post(raw)
    assert p is not None
    assert "https://www.linkedin.com/in/billgates/" in p.mentioned_profile_urls


@pytest.mark.unit
def test_parse_post_non_dict_returns_none() -> None:
    assert parse_post(None) is None
    assert parse_post("string") is None  # type: ignore[arg-type]
    assert parse_post([1, 2, 3]) is None  # type: ignore[arg-type]


# ── compute_cost_cents ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_compute_cost_cents_post_only() -> None:
    """100 posts × $1.50/1k = $0.15 = 15¢ → ceiling 15."""
    cost = compute_cost_cents({"post": 100, "actor-start": 1})
    assert cost == 15


@pytest.mark.unit
def test_compute_cost_cents_zero_posts() -> None:
    assert compute_cost_cents({"actor-start": 1}) == 0
    assert compute_cost_cents({}) == 0
    assert compute_cost_cents(None) == 0


@pytest.mark.unit
def test_compute_cost_cents_rounds_up() -> None:
    """1 post × 0.15¢ → ceiling = 1¢."""
    assert compute_cost_cents({"post": 1}) == 1


# ── scrape_profile_posts (run-async + dataset fetch) ───────────────────────


@pytest.mark.unit
async def test_scrape_profile_posts_happy_path() -> None:
    """Mock the full run lifecycle: start → poll → fetch."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if f"/acts/{ACTOR_ID}/runs?token=" in url and request.method == "POST":
            # Verify input shape
            body_str = request.read().decode("utf-8")
            assert "satyanadella" in body_str
            assert "maxPosts" in body_str
            return httpx.Response(201, json={"data": {"id": "run-posts-1", "status": "READY"}})
        if "actor-runs/run-posts-1" in url:
            return httpx.Response(200, json={"data": {
                "id": "run-posts-1",
                "status": "SUCCEEDED",
                "defaultDatasetId": "ds-posts-1",
                "chargedEventCounts": {"post": 3, "actor-start": 1},
            }})
        if "datasets/ds-posts-1/items" in url:
            return httpx.Response(200, json=[
                _mk_post(url=f"https://linkedin.com/posts/p{i}",
                         author_url="https://www.linkedin.com/in/satyanadella/")
                for i in range(3)
            ])
        return httpx.Response(404, text=f"unexpected: {url}")

    async with _client_with(handler) as client:
        result = await scrape_profile_posts(
            ["https://www.linkedin.com/in/satyanadella/"],
            max_posts_per_profile=50,
            client=client,
            poll_interval=0.01,
        )

    assert result is not None
    assert result.run_id == "run-posts-1"
    assert result.total_posts == 3
    # 3 posts × 0.15 = 0.45 → ceil = 1
    assert result.cost_cents == 1
    assert "https://www.linkedin.com/in/satyanadella/" in result.by_profile
    profile_result = result.by_profile["https://www.linkedin.com/in/satyanadella/"]
    assert len(profile_result.posts) == 3


@pytest.mark.unit
async def test_scrape_profile_posts_empty_input_returns_empty_result() -> None:
    """Empty profile_urls → empty result, no HTTP call."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    async with _client_with(handler) as client:
        result = await scrape_profile_posts([], client=client)

    assert result is not None
    assert result.by_profile == {}
    assert result.cost_cents == 0
    assert called is False


@pytest.mark.unit
async def test_scrape_profile_posts_no_token_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    async with _client_with(handler) as client:
        result = await scrape_profile_posts(
            ["https://www.linkedin.com/in/x/"],
            api_token=None,
            client=client,
        )

    assert result is None
    assert called is False


@pytest.mark.unit
async def test_scrape_profile_posts_run_failed_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if f"/acts/{ACTOR_ID}/runs?token=" in url and request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run-fail", "status": "READY"}})
        if "actor-runs/run-fail" in url:
            return httpx.Response(200, json={"data": {"id": "run-fail", "status": "FAILED"}})
        return httpx.Response(404)

    async with _client_with(handler) as client:
        result = await scrape_profile_posts(
            ["https://www.linkedin.com/in/x/"],
            client=client,
            poll_interval=0.01,
        )

    assert result is None


@pytest.mark.unit
async def test_scrape_profile_posts_groups_by_author() -> None:
    """When the dataset returns posts from multiple authors, group correctly."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if f"/acts/{ACTOR_ID}/runs?token=" in url and request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "r1", "status": "READY"}})
        if "actor-runs/r1" in url:
            return httpx.Response(200, json={"data": {
                "id": "r1",
                "status": "SUCCEEDED",
                "defaultDatasetId": "ds1",
                "chargedEventCounts": {"post": 4, "actor-start": 1},
            }})
        if "datasets/ds1/items" in url:
            return httpx.Response(200, json=[
                _mk_post(url=f"https://linkedin.com/posts/satya{i}",
                         author_url="https://www.linkedin.com/in/satyanadella/")
                for i in range(2)
            ] + [
                _mk_post(url=f"https://linkedin.com/posts/sundar{i}",
                         author_url="https://www.linkedin.com/in/sundarpichai/")
                for i in range(2)
            ])
        return httpx.Response(404)

    async with _client_with(handler) as client:
        result = await scrape_profile_posts(
            [
                "https://www.linkedin.com/in/satyanadella/",
                "https://www.linkedin.com/in/sundarpichai/",
            ],
            client=client,
            poll_interval=0.01,
        )

    assert result is not None
    assert len(result.by_profile) == 2
    assert len(result.by_profile["https://www.linkedin.com/in/satyanadella/"].posts) == 2
    assert len(result.by_profile["https://www.linkedin.com/in/sundarpichai/"].posts) == 2
    assert result.total_posts == 4
