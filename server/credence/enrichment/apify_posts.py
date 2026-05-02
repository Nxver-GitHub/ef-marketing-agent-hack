"""Tier-2 enrichment: LinkedIn posts via Apify ``harvestapi/linkedin-profile-posts``.

Runs on the **top 10% of prospects only** (those promoted to
``persons.enrichment_tier = 3`` by ``prioritize.promote_top_decile``).
Bulk-running this on every prospect would cost ~$7.5k+ which the user
explicitly rejected (see PROSPECT_ENRICHMENT_TASK chain msg confirming
"top 10% only").

## Why posts matter

LinkedIn posts feed three scoring dimensions:

- **Authority** — post frequency, follower count, engagement-per-post
  → real influence signals beyond title alone
- **Warmth** — who they engage with (mentions, comments on others' posts);
  produces ``linkedin_engagement`` edges (base_strength 0.35 — stronger
  than ``conference_co_attendee``, weaker than ``career_overlap``)
- **Authenticity** — domain-specific posting (semiconductor exec who
  actually posts about chip architecture vs an exec who only reposts
  HR fluff)

## Pricing

| Lever | Price |
|---|---|
| Base post scrape | $1.50/1,000 posts |
| ``scrapeReactions=True`` | charged separately per reaction batch |
| ``scrapeComments=True`` | charged separately per comment batch |

For Tier-2 default config (``maxPosts=50``, no reactions/comments):
- 2,000 prospects × 50 posts × $1.50/1k = **$150**

Posts-only at this price is the sweet spot. Reactions and comments add
incremental Authority/Warmth signal but multiply cost — defer until
the posts-only data shows it's worth pursuing.

## Idempotency

The actor is read-only against LinkedIn. Re-running for the same
profile pulls fresh posts (LinkedIn data is constantly updating). The
caller is responsible for deduplicating against existing rows in
``signals`` (we use ``post.url`` as the natural key — UNIQUE on
``signals.value->>url`` is enforced by application logic, not DB).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from .apify import (
    APIFY_API_BASE,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    fetch_run_dataset,  # we reuse the dataset-fetch helper
    wait_for_run,       # and the polling helper
)

logger = logging.getLogger(__name__)

ACTOR_ID = "harvestapi~linkedin-profile-posts"

# Per-post cost (verified live in the actor schema). Future modes (reactions,
# comments) add incremental cost — keep them disabled by default.
COST_PER_POST_CENTS = 0.15  # $1.50 / 1000 = 0.15¢

# Max posts per profile in default Tier-2 config. 50 is enough for Authority
# signals (avg active LinkedIn user posts ~1/wk = 50 posts ≈ 1 year).
DEFAULT_MAX_POSTS_PER_PROFILE = 50


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LinkedInPost:
    """One post extracted from a profile.

    Stored as ``signal_type=linkedin_post`` in the signals table with
    these fields as ``structured_value``.
    """

    post_url: str                  # canonical LinkedIn URL — natural key
    author_linkedin_url: str       # original profile we scraped
    text: str                      # full post text
    posted_at: str | None          # ISO datetime
    likes_count: int
    comments_count: int
    reposts_count: int

    # Engagement-graph signals
    mentioned_profile_urls: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)

    # Media presence — engagement signal
    has_image: bool = False
    has_video: bool = False
    has_document: bool = False


@dataclass(frozen=True, slots=True)
class ProfilePostsResult:
    """Per-profile result handed back to the caller."""

    author_linkedin_url: str
    posts: list[LinkedInPost]
    cost_cents: int


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """Per-run result for a batch of profiles."""

    by_profile: dict[str, ProfilePostsResult]  # keyed by linkedin_url
    cost_cents: int
    run_id: str | None
    cache_hit: bool = False

    @property
    def total_posts(self) -> int:
        return sum(len(r.posts) for r in self.by_profile.values())


# ─── Field-extraction helpers ──────────────────────────────────────────────


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _int_or_zero(v: Any) -> int:
    if isinstance(v, bool):
        return 0
    return int(v) if isinstance(v, int) else 0


def _list_of_str(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [s for s in v if isinstance(s, str) and s.strip()]


def _extract_mentioned_urls(post: dict[str, Any]) -> list[str]:
    """harvestapi puts mentions under ``mentionedProfiles[]`` or
    ``entities[].profile.url``. Cover both shapes defensively."""
    out: list[str] = []
    for candidate in post.get("mentionedProfiles") or []:
        if isinstance(candidate, dict):
            url = _str_or_none(candidate.get("url") or candidate.get("profileUrl"))
            if url:
                out.append(url)
        elif isinstance(candidate, str) and candidate.strip():
            out.append(candidate.strip())
    for ent in post.get("entities") or []:
        if isinstance(ent, dict):
            profile = ent.get("profile") or {}
            url = _str_or_none(profile.get("url"))
            if url and url not in out:
                out.append(url)
    return out


def _detect_media(post: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Returns ``(has_image, has_video, has_document)`` from the media field."""
    has_image = has_video = has_doc = False
    media = post.get("media") or post.get("attachments") or []
    if isinstance(media, list):
        for m in media:
            if not isinstance(m, dict):
                continue
            kind = (m.get("type") or m.get("kind") or "").lower()
            if "image" in kind or "photo" in kind:
                has_image = True
            elif "video" in kind:
                has_video = True
            elif "document" in kind or "pdf" in kind:
                has_doc = True
    if isinstance(post.get("images"), list) and post["images"]:
        has_image = True
    return has_image, has_video, has_doc


def parse_post(raw: dict[str, Any], *, fallback_author_url: str = "") -> LinkedInPost | None:
    """Map one harvestapi profile-post item → ``LinkedInPost``.

    Returns None when the row lacks a usable URL (the natural key for
    dedup against existing ``signals`` rows).
    """
    if not isinstance(raw, dict):
        return None
    post_url = _str_or_none(raw.get("url") or raw.get("postUrl") or raw.get("permalink"))
    if not post_url:
        return None

    author_url = _str_or_none(
        ((raw.get("author") or {}).get("profileUrl"))
        or ((raw.get("author") or {}).get("url"))
        or raw.get("authorProfileUrl")
    ) or fallback_author_url
    if not author_url:
        return None

    text = _str_or_none(raw.get("text") or raw.get("content")) or ""

    has_image, has_video, has_document = _detect_media(raw)

    return LinkedInPost(
        post_url=post_url,
        author_linkedin_url=author_url,
        text=text,
        posted_at=_str_or_none(raw.get("publishedAt") or raw.get("postedAt") or raw.get("createdAt")),
        likes_count=_int_or_zero(raw.get("likesCount") or raw.get("reactionsCount")),
        comments_count=_int_or_zero(raw.get("commentsCount")),
        reposts_count=_int_or_zero(raw.get("repostsCount") or raw.get("sharesCount")),
        mentioned_profile_urls=_extract_mentioned_urls(raw),
        hashtags=_list_of_str(raw.get("hashtags")),
        has_image=has_image,
        has_video=has_video,
        has_document=has_document,
    )


# ─── Cost computation ──────────────────────────────────────────────────────


def compute_cost_cents(charged_event_counts: dict[str, Any] | None) -> int:
    """Sum cents from Apify's chargedEventCounts.

    Event keys observed (all per-item rates):
      - ``post`` — base post scrape
      - ``actor-start`` — flat $0
      - reaction/comment events when those flags are enabled
        (we leave them disabled by default; rate unknown until enabled)
    """
    if not isinstance(charged_event_counts, dict):
        return 0
    cents = 0.0
    for event, count in charged_event_counts.items():
        if not isinstance(count, int) or count <= 0:
            continue
        if event == "post":
            cents += COST_PER_POST_CENTS * count
        # actor-start: $0
        # reaction/comment: not surfaced in default config
    return int(cents + 0.999) if cents > 0 else 0


# ─── HTTP / run orchestration ──────────────────────────────────────────────


def _resolve_token(api_token: str | None) -> str | None:
    return api_token or os.environ.get("APIFY_TOKEN")


async def scrape_profile_posts(
    profile_urls: list[str],
    *,
    max_posts_per_profile: int = DEFAULT_MAX_POSTS_PER_PROFILE,
    posted_limit: str | None = None,  # e.g., "month" / "year" / specific iso date
    include_quote_posts: bool = True,
    include_reposts: bool = True,
    api_token: str | None = None,
    client: httpx.AsyncClient | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_wait_seconds: float = 1800.0,
) -> EnrichmentResult | None:
    """Async one-shot — start run, poll, fetch dataset.

    The caller passes ALL Tier-3 profile URLs at once. The actor scrapes
    6 profiles concurrently internally per harvestapi docs, so a single
    bulk submission is more efficient than per-profile invocations.

    Returns None when the run failed, was aborted, or timed out.
    """
    if not profile_urls:
        return EnrichmentResult(by_profile={}, cost_cents=0, run_id=None)

    token = _resolve_token(api_token)
    if not token:
        logger.info("apify_posts: no APIFY_TOKEN set — skipping")
        return None

    payload: dict[str, Any] = {
        "targetUrls": profile_urls,
        "maxPosts": max_posts_per_profile,
        "includeQuotePosts": include_quote_posts,
        "includeReposts": include_reposts,
        # Reactions + comments default OFF — incremental cost not worth it
        # at Tier 2 scale until we see the posts-only signal pays off.
        "scrapeReactions": False,
        "scrapeComments": False,
    }
    if posted_limit:
        payload["postedLimit"] = posted_limit

    own_client = client is None
    http = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        # Start the run
        try:
            r = await http.post(
                f"{APIFY_API_BASE}/acts/{ACTOR_ID}/runs?token={token}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            logger.warning("apify_posts start_run failed: %s", exc)
            return None
        if r.status_code not in (200, 201):
            logger.warning("apify_posts start_run HTTP %d: %s", r.status_code, r.text[:200])
            return None
        try:
            run_id = ((r.json() or {}).get("data") or {}).get("id")
        except ValueError:
            return None
        if not isinstance(run_id, str) or not run_id:
            return None

        # Poll
        status, run_data = await wait_for_run(
            run_id,
            poll_interval=poll_interval,
            max_wait_seconds=max_wait_seconds,
            api_token=token,
            client=http,
        )
        if status != "SUCCEEDED" or run_data is None:
            logger.warning(
                "apify_posts run %s status=%s — partial-results contract",
                run_id, status,
            )
            return None

        # Fetch dataset directly (we reuse apify.fetch_run_dataset's
        # logic but we can't reuse the parsed-result type since posts
        # are a different schema)
        dataset_id = run_data.get("defaultDatasetId")
        if not isinstance(dataset_id, str) or not dataset_id:
            return None
        try:
            ds_r = await http.get(
                f"{APIFY_API_BASE}/datasets/{dataset_id}/items?token={token}"
            )
        except httpx.HTTPError as exc:
            logger.warning("apify_posts dataset fetch failed: %s", exc)
            return None
        if ds_r.status_code != 200:
            return None
        try:
            items = ds_r.json()
        except ValueError:
            return None
        if not isinstance(items, list):
            return None
    finally:
        if own_client:
            await http.aclose()

    # Group posts by author profile URL
    by_profile_dict: dict[str, list[LinkedInPost]] = {}
    for raw in items:
        # The actor returns one row per post; the author URL is on each
        # row. Some posts may have a fallback author URL of '' if shape
        # drift hits — skip those.
        post = parse_post(raw)
        if post is None:
            continue
        # Match the post back to one of the input profile URLs (case-
        # insensitive, trailing-slash tolerant) so downstream attribution
        # is robust.
        normalized_author = post.author_linkedin_url.rstrip("/").lower()
        matched_input = next(
            (u for u in profile_urls if u.rstrip("/").lower() == normalized_author),
            post.author_linkedin_url,
        )
        by_profile_dict.setdefault(matched_input, []).append(post)

    cost_cents = compute_cost_cents(run_data.get("chargedEventCounts"))

    return EnrichmentResult(
        by_profile={
            url: ProfilePostsResult(
                author_linkedin_url=url,
                posts=posts,
                cost_cents=int(
                    cost_cents * len(posts) / max(1, sum(len(v) for v in by_profile_dict.values()))
                ),  # apportion total cost across profiles by post count
            )
            for url, posts in by_profile_dict.items()
        },
        cost_cents=cost_cents,
        run_id=run_id,
        cache_hit=False,
    )


__all__ = [
    "ACTOR_ID",
    "DEFAULT_MAX_POSTS_PER_PROFILE",
    "LinkedInPost",
    "ProfilePostsResult",
    "EnrichmentResult",
    "parse_post",
    "compute_cost_cents",
    "scrape_profile_posts",
]
