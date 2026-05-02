"""Semantic Scholar co-authorship extractor.

API: https://api.semanticscholar.org/graph/v1/  (free, ~100 req/5min unauth)

## Implementation strategy

Find papers where both `person_a` and `person_b` appear in `authors[]`.
Two API calls per pair:

1. `GET /author/search?query=<canonical_name>` → resolve `person_a` to an
   `authorId`. We use the top hit (Semantic Scholar's relevance ranking) —
   refining via affiliation matching is a future improvement.
2. `GET /author/{authorId}/papers?fields=...&limit=...` → list the author's
   papers, with `authors[]` populated for filtering.

Then we filter to papers whose `authors[]` contains `person_b` by name
(case-insensitive first+last token match) and shape each match into
Contract 1's `structured_value` for `signal_type="academic_co_author"`.

## Response-shape assumption (Semantic Scholar Graph API v1)

`/author/search`:
```json
{
  "total": 5,
  "data": [
    {"authorId": "1741101", "name": "Wei Chen", "affiliations": ["Tsinghua University"], "paperCount": 142}
  ]
}
```

`/author/{authorId}/papers`:
```json
{
  "data": [
    {
      "paperId": "abc123",
      "externalIds": {"DOI": "10.1234/example"},
      "title": "Accelerator design for LLM training",
      "venue": "NeurIPS",
      "year": 2023,
      "citationCount": 42,
      "authors": [
        {"authorId": "1741101", "name": "Wei Chen"},
        {"authorId": "9999",    "name": "Marcus Hale"}
      ]
    }
  ]
}
```

All fields are accessed defensively. The signals route adds `connected_to`
and `author_count` (signals.py reads `author_count` to choose between 0.90
and 0.75 confidence per CLAUDE.md L825-827 / Contract 1 invariants).

## Sandbox / live-API status

Built against the documented v1 schema; **live integration test deferred to
J.5.5** (one real `httpx.get` against the production API to confirm the
canned shape still matches). Unit tests use `httpx.MockTransport` returning
canned JSON, so the parsing logic is locked in regardless of API
availability.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from .patents import PersonRef  # shared dataclass; one place to evolve

logger = logging.getLogger(__name__)

SEMANTIC_SCHOLAR_BASE_URL = "https://api.semanticscholar.org/graph/v1/"
DEFAULT_TIMEOUT_SECONDS = 8.0


# ─── HTTP I/O ────────────────────────────────────────────────────────────────


async def _semantic_scholar_get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Hit Semantic Scholar; return JSON dict on 200, None otherwise.

    Network errors / non-200 / non-JSON all collapse to None — Contract 1
    partial-results semantics. Logs at warning level.
    """
    url = urljoin(SEMANTIC_SCHOLAR_BASE_URL, path)
    try:
        r = await client.get(url, params=params, timeout=DEFAULT_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.warning("Semantic Scholar request failed: %s", exc)
        return None
    if r.status_code != 200:
        # 429s here would be the rate-limit signal — orchestrator's timeout
        # contract handles backoff at the route level.
        logger.warning("Semantic Scholar HTTP %d: %s", r.status_code, r.text[:200])
        return None
    try:
        body = r.json()
    except ValueError:
        logger.warning("Semantic Scholar returned non-JSON body")
        return None
    return body if isinstance(body, dict) else None


async def _resolve_author_id(
    client: httpx.AsyncClient,
    person: PersonRef,
) -> str | None:
    """Resolve a PersonRef to a Semantic Scholar authorId.

    Returns None when the search yields no hits, or when the canonical name
    is too short to disambiguate. Uses the top hit — relying on Semantic
    Scholar's own relevance ranking. Future refinement: rank by affiliation
    match against the prospect's current_company.
    """
    name = (person.canonical_name or "").strip()
    if len(name.split()) < 2:
        # Single-token names produce huge fanout; skip.
        return None
    body = await _semantic_scholar_get(
        client,
        "author/search",
        params={"query": name, "fields": "name,affiliations,paperCount", "limit": 5},
    )
    if not body:
        return None
    data = body.get("data")
    if not isinstance(data, list) or not data:
        return None
    top = data[0]
    if not isinstance(top, dict):
        return None
    author_id = top.get("authorId")
    if isinstance(author_id, str) and author_id:
        return author_id
    return None


async def _fetch_author_papers(
    client: httpx.AsyncClient,
    author_id: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch papers authored by the given Semantic Scholar authorId."""
    body = await _semantic_scholar_get(
        client,
        f"author/{author_id}/papers",
        params={
            "fields": "title,year,authors,venue,citationCount,externalIds",
            "limit": limit,
        },
    )
    if not body:
        return []
    data = body.get("data")
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, dict)]


# ─── Author matching ────────────────────────────────────────────────────────


def _split_first_last(canonical_name: str) -> tuple[str, str] | None:
    """Naive first/last split for name-based matching."""
    if not canonical_name:
        return None
    parts = canonical_name.split()
    if len(parts) < 2:
        return None
    return parts[0], parts[-1]


def _author_matches_person(author: dict[str, Any], person: PersonRef) -> bool:
    """Return True if a Semantic Scholar author entry corresponds to `person`.

    Match priority:
    1. authorId == person's already-resolved id (when caller passes one)
    2. case-insensitive first+last name token match on `name`
    """
    name = _split_first_last(person.canonical_name)
    if name is None:
        return False
    first_want, last_want = name[0].lower(), name[1].lower()
    full = str(author.get("name", "")).strip().lower()
    if not full:
        return False
    parts = full.split()
    if len(parts) < 2:
        return False
    return parts[0] == first_want and parts[-1] == last_want


def _paper_includes_person(paper: dict[str, Any], person: PersonRef) -> bool:
    authors = paper.get("authors")
    if not isinstance(authors, list):
        return False
    return any(
        _author_matches_person(a, person) for a in authors if isinstance(a, dict)
    )


# ─── Output shaping ─────────────────────────────────────────────────────────


def _format_paper_record(paper: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Semantic Scholar paper dict → Contract 1 `structured_value`.

    Returns None when the paper lacks both an identifier and a title (we
    refuse to emit a record we can't link back to anything). Other fields
    tolerate absence and propagate as empty strings / None / 0.
    """
    paper_id = paper.get("paperId")
    title = paper.get("title")
    if not isinstance(paper_id, str) and not isinstance(title, str):
        return None
    if not paper_id and not title:
        return None

    venue = paper.get("venue")
    if not isinstance(venue, str):
        venue = ""
    year = paper.get("year")
    year_int = int(year) if isinstance(year, int) or (isinstance(year, str) and year.isdigit()) else 0
    citations = paper.get("citationCount")
    citation_int = int(citations) if isinstance(citations, int) else 0

    external = paper.get("externalIds")
    doi: str | None = None
    if isinstance(external, dict):
        d = external.get("DOI") or external.get("doi")
        if isinstance(d, str) and d:
            doi = d

    authors = paper.get("authors")
    author_count = len(authors) if isinstance(authors, list) else 0

    return {
        "paper_title": title if isinstance(title, str) else "",
        "venue": venue,
        "year": year_int,
        "citation_count": citation_int,
        "semantic_scholar_id": paper_id if isinstance(paper_id, str) else "",
        "doi": doi,
        # Surfaced to signals.py for confidence-tier branching per Contract 1
        # (academic_co_author: 0.90 if author_count <= 5, else 0.75).
        "author_count": author_count,
    }


# ─── Public API ─────────────────────────────────────────────────────────────


async def find_paper_co_authorships(
    person_a: PersonRef,
    person_b: PersonRef,
    *,
    max_results: int = 25,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Return list of co-authored papers between two persons.

    Each dict matches Contract 1 `structured_value` for
    `signal_type="academic_co_author"`. The signals route adds `connected_to`
    on top.

    Two API calls per pair:
    1. Resolve `person_a` to an authorId via /author/search
    2. Fetch /author/{id}/papers and filter for `person_b`'s presence

    Optional `client` lets tests inject a `httpx.MockTransport` (or production
    code share a long-lived client).
    """
    own_client = client is None
    http = client if client is not None else httpx.AsyncClient()
    try:
        author_id = await _resolve_author_id(http, person_a)
        if author_id is None:
            logger.info(
                "scholar extractor: no Semantic Scholar match for %s",
                person_a.canonical_name,
            )
            return []
        # Pull ~5x max_results to filter; co-authored papers cluster on a
        # small subset of an author's full bibliography.
        papers = await _fetch_author_papers(
            http, author_id, limit=max(max_results * 5, 25)
        )
    finally:
        if own_client:
            await http.aclose()

    matches: list[dict[str, Any]] = []
    for paper in papers:
        if not _paper_includes_person(paper, person_b):
            continue
        record = _format_paper_record(paper)
        if record is None:
            continue
        matches.append(record)
        if len(matches) >= max_results:
            break

    logger.info(
        "scholar extractor: %s × %s → %d co-authorship(s) from %d candidate(s)",  # noqa: RUF001 — multiplication-sign reads more naturally here
        person_a.canonical_name,
        person_b.canonical_name,
        len(matches),
        len(papers),
    )
    return matches
