"""Tier-1 enrichment: GitHub profile + orgs + top repos.

Runs free against the public GitHub REST API. Pulls the formal-evidence
signals that LinkedIn doesn't carry (open-source contributions, repo
ownership, org affiliations, language breadth).

## Why GitHub matters

For engineering prospects, GitHub is the closest thing to USPTO for
authenticity. A Director of Compiler Engineering at AMD whose GitHub
shows MLIR commits is a real engineer; one whose profile is empty
might be a manager who's drifted from the keyboard. Both are legit
prospects but the warm-path quality differs.

## When this runs

Per the Tier-1 budget (free): on every prospect during bulk enrichment,
gated by a title-heuristic — only run when ``current_functional_domain
∈ {hardware_engineering, software_engineering, research}`` AND we have
a discovered ``github_username``.

The ``github_username`` discovery problem is non-trivial. Sources, in
order of reliability:

1. From Apify's profile fields — the ``organizations[]`` array sometimes
   includes "GitHub: <handle>" entries
2. From the prospect's LinkedIn ``contactInfo`` (when surfaced)
3. From a free-text search: "<full_name> github <company>" — error-prone

This module ships the **lookup half** assuming a username is provided.
Discovery is a follow-on (out of scope for this iteration).

## Rate limits

Unauthenticated: 60 req/hr per IP. Easy to exhaust at 30k prospects.
Authenticated (``GITHUB_TOKEN``): 5,000 req/hr — sufficient for any
realistic Tier-1 sweep. Module accepts the token via env or kwarg.

## Cost

$0 forever — GitHub's REST API is free for our usage volume.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_TOP_REPOS = 10  # most-starred repos to pull per user


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GitHubRepo:
    """One repository owned (or co-owned) by the user."""

    name: str
    full_name: str           # "owner/repo"
    description: str | None
    stars: int
    forks: int
    language: str | None
    is_fork: bool
    is_archived: bool
    url: str


@dataclass(frozen=True, slots=True)
class GitHubOrg:
    """One org the user is a public member of."""

    login: str               # org handle
    name: str | None         # display name when set
    url: str
    description: str | None


@dataclass(frozen=True, slots=True)
class GitHubProfile:
    """Aggregate enrichment for one GitHub user.

    Stored as ``signal_type='github_profile'`` in the signals table with
    the high-cardinality fields lifted into ``structured_value``.
    """

    username: str
    name: str | None
    company: str | None      # the user's self-declared current employer
    location: str | None
    bio: str | None
    blog_url: str | None
    twitter_handle: str | None
    public_repos: int
    public_gists: int
    followers: int
    following: int
    created_at: str | None
    profile_url: str
    orgs: list[GitHubOrg] = field(default_factory=list)
    top_repos: list[GitHubRepo] = field(default_factory=list)


# ─── HTTP helpers ───────────────────────────────────────────────────────────


def _resolve_token(github_token: str | None) -> str | None:
    return github_token or os.environ.get("GITHUB_TOKEN")


def _build_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "credence-enrichment/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _gh_get(
    path: str,
    *,
    client: httpx.AsyncClient,
    token: str | None,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET to api.github.com. Returns JSON or None on any failure.

    Returns None for 404 (user not found), 403 (rate-limited),
    or any HTTPError — caller treats as "no signal" per the
    partial-results contract.
    """
    try:
        r = await client.get(
            f"{GITHUB_API_BASE}{path}",
            headers=_build_headers(token),
            params=params or {},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("github GET %s failed: %s", path, exc)
        return None
    if r.status_code == 404:
        return None
    if r.status_code == 403:
        # Rate-limited or auth issue — log loudly so operators see
        logger.warning(
            "github GET %s returned 403 — rate-limited or auth missing",
            path,
        )
        return None
    if r.status_code != 200:
        logger.warning("github GET %s HTTP %d", path, r.status_code)
        return None
    try:
        return r.json()
    except ValueError:
        return None


# ─── Field-extraction helpers ──────────────────────────────────────────────


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _int_or_zero(v: Any) -> int:
    if isinstance(v, bool):
        return 0
    return int(v) if isinstance(v, int) else 0


def parse_repo(raw: dict[str, Any]) -> GitHubRepo | None:
    if not isinstance(raw, dict):
        return None
    name = _str_or_none(raw.get("name"))
    full_name = _str_or_none(raw.get("full_name"))
    if not name or not full_name:
        return None
    return GitHubRepo(
        name=name,
        full_name=full_name,
        description=_str_or_none(raw.get("description")),
        stars=_int_or_zero(raw.get("stargazers_count")),
        forks=_int_or_zero(raw.get("forks_count")),
        language=_str_or_none(raw.get("language")),
        is_fork=bool(raw.get("fork")),
        is_archived=bool(raw.get("archived")),
        url=_str_or_none(raw.get("html_url")) or "",
    )


def parse_org(raw: dict[str, Any]) -> GitHubOrg | None:
    if not isinstance(raw, dict):
        return None
    login = _str_or_none(raw.get("login"))
    if not login:
        return None
    return GitHubOrg(
        login=login,
        name=_str_or_none(raw.get("name")),
        url=_str_or_none(raw.get("html_url")) or f"https://github.com/{login}",
        description=_str_or_none(raw.get("description")),
    )


# ─── Public API ────────────────────────────────────────────────────────────


async def enrich_github_profile(
    username: str,
    *,
    top_repos: int = DEFAULT_TOP_REPOS,
    github_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> GitHubProfile | None:
    """Fetch a GitHub user's profile + orgs + top repos.

    Returns None when the user doesn't exist (404), we hit a rate limit,
    or any individual call fails. Partial results: when /orgs or /repos
    succeed but the other fails, we still return a profile with whatever
    we got (orgs/repos may be empty).
    """
    if not isinstance(username, str) or not username.strip():
        return None
    username = username.strip()
    token = _resolve_token(github_token)

    own_client = client is None
    http = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        # 1) The user profile itself — required, return None on failure
        user = await _gh_get(f"/users/{username}", client=http, token=token)
        if not isinstance(user, dict):
            return None

        # 2) Public orgs (best-effort)
        orgs_raw = await _gh_get(
            f"/users/{username}/orgs", client=http, token=token
        )
        orgs: list[GitHubOrg] = []
        if isinstance(orgs_raw, list):
            for o in orgs_raw:
                parsed = parse_org(o)
                if parsed:
                    orgs.append(parsed)

        # 3) Top repos by stars (best-effort)
        repos_raw = await _gh_get(
            f"/users/{username}/repos",
            client=http,
            token=token,
            params={"sort": "updated", "per_page": max(1, min(100, top_repos)), "type": "owner"},
        )
        repos: list[GitHubRepo] = []
        if isinstance(repos_raw, list):
            for r in repos_raw:
                parsed = parse_repo(r)
                if parsed:
                    repos.append(parsed)
            # Sort by stars desc post-fetch (sort=updated for freshness)
            repos.sort(key=lambda r: r.stars, reverse=True)
            repos = repos[:top_repos]
    finally:
        if own_client:
            await http.aclose()

    return GitHubProfile(
        username=username,
        name=_str_or_none(user.get("name")),
        company=_str_or_none(user.get("company")),
        location=_str_or_none(user.get("location")),
        bio=_str_or_none(user.get("bio")),
        blog_url=_str_or_none(user.get("blog")),
        twitter_handle=_str_or_none(user.get("twitter_username")),
        public_repos=_int_or_zero(user.get("public_repos")),
        public_gists=_int_or_zero(user.get("public_gists")),
        followers=_int_or_zero(user.get("followers")),
        following=_int_or_zero(user.get("following")),
        created_at=_str_or_none(user.get("created_at")),
        profile_url=_str_or_none(user.get("html_url")) or f"https://github.com/{username}",
        orgs=orgs,
        top_repos=repos,
    )


__all__ = [
    "GITHUB_API_BASE",
    "GitHubRepo",
    "GitHubOrg",
    "GitHubProfile",
    "parse_repo",
    "parse_org",
    "enrich_github_profile",
]
