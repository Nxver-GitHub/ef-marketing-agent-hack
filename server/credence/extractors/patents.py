"""USPTO patent co-invention extractor (PatentsView API).

API: https://search.patentsview.org/api/v1/  (free, no auth, 45 req/min)

## ⚠ Endpoint migration in progress (2026-04-30 finding, LavenderPrairie)

The legacy `search.patentsview.org` host now returns NXDOMAIN; the
older `api.patentsview.org/patents/query` returns HTTP 301 →
`https://data.uspto.gov/support/transition-guide/patentsview`.
PatentsView has been deprecated and migrated to USPTO's Open Data
Portal at `api.uspto.gov` (which currently returns 403 without a
registered API key).

Concrete migration tasks (DarkBeaver — patents.py owner):
- Register at the new ODP for an API key (data.uspto.gov registration)
- Update `PATENTSVIEW_BASE_URL` to the new ODP root + auth header
- Update the response-shape parsing to whatever ODP returns (the unit
  tests use mocks, so this won't show until the live integration runs)

In the meantime: every live patent query collapses to `httpx.ConnectError`
or HTTP 403, which is already swallowed by `except httpx.HTTPError` in
`fetch_patents_for_inventor()`. The extractor degrades to "no patent
co-invention found" rather than crashing — Contract 1 partial-results
semantics. Mock-backed unit tests still pass.

## Implementation strategy

Find patents where both `person_a` and `person_b` are listed as inventors.
Strategy:

1. Build a name-search query for `person_a` from `canonical_name` (split on
   first whitespace into first / last). When `uspto_inventor_id` is present,
   prefer it for precision.
2. Hit `GET /patent/?q=...&f=...&o=...` to retrieve candidate patents that
   include `person_a` as an inventor, with the full `inventors[]` array
   populated so we can check `person_b`'s presence.
3. For each candidate, filter to those whose `inventors[]` also contains a
   match for `person_b` (name-based or inventor-id-based).
4. Map each match to a Contract 1 `structured_value` shape and return.

## Response-shape assumption (v1 docs + CLAUDE.md L341-349)

```json
{
  "patents": [
    {
      "patent_id": "10234567",
      "patent_number": "10,234,567",          // optional alias
      "patent_title": "Method for ...",
      "patent_date": "2018-04-21",            // grant date
      "patent_filing_date": "2017-08-12",     // sometimes present
      "inventors": [
        {"inventor_id": "fl:we_ln:chen-1",
         "inventor_name_first": "Wei",
         "inventor_name_last": "Chen"},
        ...
      ],
      "assignees": [
        {"assignee_organization": "Intel Corporation"}
      ]
    }
  ],
  "count": N
}
```

All fields are accessed defensively — missing keys produce empty / null
result fields rather than exceptions.

## Sandbox / live-API status

Implementation built against the documented v1 schema; **live integration
test deferred to J.4.5** (one real `httpx.get` against the production API to
confirm the canned shape still matches reality). The unit tests below use
`httpx.MockTransport` returning canned JSON so the parsing logic is locked
in regardless of API availability.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

# Legacy PatentsView base. As of 2026-04-30 this hostname is NXDOMAIN — the
# API has been migrated to USPTO's Open Data Portal (ODP) at api.uspto.gov.
# We retain the constant for offline tests (httpx.MockTransport intercepts
# before DNS) and as the default code path until the ODP migration verifies
# the new URL + response shape end-to-end (see header `⚠ Endpoint migration`).
PATENTSVIEW_BASE_URL = "https://search.patentsview.org/api/v1/"

# USPTO Open Data Portal — the migration target. **URL not yet verified
# against a registered ODP key.** LavenderPrairie's msg 122 probing
# narrowed it to api.uspto.gov but the exact endpoint path needs
# confirmation post-key-registration. Keeping this constant + the toggle
# below dormant until someone with a key validates one live call.
USPTO_ODP_BASE_URL = "https://api.uspto.gov/api/v1/"
USPTO_ODP_PATENT_PATH = "patent/search"  # tentative — verify after key landing

DEFAULT_TIMEOUT_SECONDS = 8.0


def _resolve_endpoint_config() -> tuple[str, str, dict[str, str]]:
    """Return ``(base_url, patent_path, extra_headers)`` for the current run.

    Selection logic (amended 2026-04-30 by user directive — no fallbacks):

    - If env var ``USPTO_USE_ODP=1`` AND ``USPTO_ODP_API_KEY`` is set →
      route to the new ODP endpoint with an ``X-API-KEY`` header.
    - If ``USPTO_USE_ODP=1`` AND key missing → raise ``RuntimeError``
      with a registration pointer. Caller must catch (signals.py wraps
      extractor exceptions per Contract 1 partial-results semantics).
    - If ``USPTO_USE_ODP`` not set → raise ``RuntimeError`` because the
      legacy PatentsView host is NXDOMAIN since 2026-04 and falling back
      to it would silently produce zero patent edges in production. The
      caller (signals.py) catches this and skips patents with a documented
      ``patents_skipped: true, reason: "USPTO_ODP_API_KEY not configured"``
      in the response — visible to operators rather than hidden.

    This is intentional. Stubbing fake data or falling back to a dead
    endpoint hides the operational gap; raising surfaces it.
    """
    use_odp = os.environ.get("USPTO_USE_ODP", "").strip() in {"1", "true", "TRUE"}
    odp_key = os.environ.get("USPTO_ODP_API_KEY", "").strip()

    if use_odp and odp_key:
        return (
            USPTO_ODP_BASE_URL,
            USPTO_ODP_PATENT_PATH,
            {"X-API-KEY": odp_key},
        )

    raise RuntimeError(
        "USPTO_ODP_API_KEY not set — register at data.uspto.gov "
        "(ID.me identity verification required, ~15 min) and set "
        "USPTO_USE_ODP=1 + USPTO_ODP_API_KEY=<key> in .env.local. "
        "Legacy PatentsView (search.patentsview.org) is NXDOMAIN since "
        "2026-04 and is not a fallback."
    )


@dataclass(frozen=True, slots=True)
class PersonRef:
    """Minimal identifier set for an extractor query."""

    person_id: str
    canonical_name: str
    uspto_inventor_id: str | None = None
    linkedin_url: str | None = None
    orcid: str | None = None


# ─── Name parsing ───────────────────────────────────────────────────────────


def _split_first_last(canonical_name: str) -> tuple[str, str] | None:
    """Naive first/last split: tokenize on whitespace, take first + last token.

    Returns None when the input doesn't look like a personal name (single
    token, empty, etc.). The caller then skips the search rather than
    issuing a bad query.

    Edge cases:
    - "Wei Chen" → ("Wei", "Chen")
    - "Wei W. Chen" → ("Wei", "Chen")  [middle initial dropped]
    - "Lin"        → None              [single token, can't disambiguate]
    - ""           → None
    """
    if not canonical_name:
        return None
    parts = canonical_name.split()
    if len(parts) < 2:
        return None
    return parts[0], parts[-1]


# ─── PatentsView query ──────────────────────────────────────────────────────


def _build_query_for_person(person: PersonRef) -> dict[str, Any] | None:
    """Build a PatentsView `q` dict that filters to patents whose inventors
    include the given person. Returns None when we can't build a meaningful
    query (e.g., single-name persons with no inventor_id).

    Prefer `uspto_inventor_id` when available — exact match. Otherwise
    fall back to first/last name match.
    """
    if person.uspto_inventor_id:
        return {
            "_contains": {"inventors.inventor_id": person.uspto_inventor_id},
        }
    name = _split_first_last(person.canonical_name)
    if name is None:
        return None
    first, last = name
    return {
        "_and": [
            {"_contains": {"inventors.inventor_name_first": first}},
            {"_contains": {"inventors.inventor_name_last": last}},
        ]
    }


# ─── Inventor matching (after fetch, filter for person_b's presence) ────────


def _inventor_matches_person(inventor: dict[str, Any], person: PersonRef) -> bool:
    """Return True if a single inventors[] record corresponds to `person`.

    Match priority:
    1. inventor_id == person.uspto_inventor_id (when both present) — exact
    2. first/last name pair (case-insensitive) — fallback
    """
    if person.uspto_inventor_id:
        inv_id = inventor.get("inventor_id")
        if isinstance(inv_id, str) and inv_id == person.uspto_inventor_id:
            return True
    name = _split_first_last(person.canonical_name)
    if name is None:
        return False
    first_want, last_want = name[0].lower(), name[1].lower()
    first_got = str(inventor.get("inventor_name_first", "")).lower()
    last_got = str(inventor.get("inventor_name_last", "")).lower()
    return first_got == first_want and last_got == last_want


def _patent_includes_inventor(
    patent: dict[str, Any], person: PersonRef
) -> bool:
    inventors = patent.get("inventors")
    if not isinstance(inventors, list):
        return False
    return any(
        _inventor_matches_person(inv, person) for inv in inventors if isinstance(inv, dict)
    )


# ─── Output shaping ─────────────────────────────────────────────────────────


def _format_patent_record(patent: dict[str, Any]) -> dict[str, Any] | None:
    """Map a PatentsView patent dict → Contract 1 `structured_value` shape.

    Returns None when the record lacks the minimum identifying field
    (`patent_id`/`patent_number`). Other fields tolerate absence — empty
    strings or null land in the output and the downstream renderer falls
    back to documented placeholder strings.
    """
    patent_id = (
        patent.get("patent_id")
        or patent.get("patent_number")
        or patent.get("number")
    )
    if not isinstance(patent_id, str) or not patent_id:
        return None

    title = patent.get("patent_title") or patent.get("title") or ""
    grant_date = patent.get("patent_date") or patent.get("grant_date") or None
    filing_date = patent.get("patent_filing_date") or patent.get("filing_date") or ""

    assignees = patent.get("assignees")
    assignee = ""
    if isinstance(assignees, list) and assignees:
        first = assignees[0]
        if isinstance(first, dict):
            assignee = (
                first.get("assignee_organization")
                or first.get("organization")
                or ""
            )
            if not isinstance(assignee, str):
                assignee = ""

    uspto_url = f"https://patents.google.com/patent/US{patent_id.replace(',', '')}/"

    return {
        "patent_number": patent_id,
        "patent_title": title if isinstance(title, str) else "",
        "filing_date": filing_date if isinstance(filing_date, str) else "",
        "grant_date": grant_date if isinstance(grant_date, str) else None,
        "assignee": assignee,
        "uspto_url": uspto_url,
    }


# ─── HTTP I/O ────────────────────────────────────────────────────────────────


# Default fields requested. Includes inventors[] and assignees[] so we can
# filter for person_b's presence and extract assignee organization without
# a second API call per patent.
_DEFAULT_FIELDS: list[str] = [
    "patent_id",
    "patent_title",
    "patent_date",
    "patent_filing_date",
    "inventors.inventor_id",
    "inventors.inventor_name_first",
    "inventors.inventor_name_last",
    "assignees.assignee_organization",
]


async def _fetch_patents(
    client: httpx.AsyncClient,
    query: dict[str, Any],
    *,
    page_size: int,
) -> list[dict[str, Any]]:
    """Hit `/patent/?q=...&f=...&o=...`. Returns the `patents` array on
    success; empty list on any failure mode. Network errors are logged but
    don't propagate — Contract 1 wants partial-results semantics.
    """
    params = {
        "q": json.dumps(query),
        "f": json.dumps(_DEFAULT_FIELDS),
        "o": json.dumps({"size": page_size}),
    }
    base_url, patent_path, extra_headers = _resolve_endpoint_config()
    url = urljoin(base_url, patent_path)
    try:
        r = await client.get(
            url,
            params=params,
            headers=extra_headers or None,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("PatentsView request failed: %s", exc)
        return []
    if r.status_code != 200:
        logger.warning("PatentsView HTTP %d: %s", r.status_code, r.text[:200])
        return []
    try:
        body = r.json()
    except ValueError:
        logger.warning("PatentsView returned non-JSON body")
        return []
    patents = body.get("patents")
    if not isinstance(patents, list):
        return []
    return [p for p in patents if isinstance(p, dict)]


# ─── Public API ─────────────────────────────────────────────────────────────


async def find_patent_co_inventions(
    person_a: PersonRef,
    person_b: PersonRef,
    *,
    max_results: int = 25,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Return a list of patent dicts where both persons are co-inventors.

    Each dict matches Contract 1 `structured_value` for
    `signal_type="patent_co_inventor"`. The signals route adds `connected_to`
    and persists.

    The optional `client` arg lets tests inject a mocked transport. In
    production the route layer can pass a shared client too (saves the
    per-call connection setup), but the default behavior is to spin up a
    short-lived client.
    """
    query_a = _build_query_for_person(person_a)
    if query_a is None:
        logger.info(
            "patent extractor: cannot build query for %s (single-name? no inventor_id?)",
            person_a.canonical_name,
        )
        return []

    own_client = client is None
    http = client if client is not None else httpx.AsyncClient()
    try:
        # Pull ~5x max_results candidates; we'll filter most out by checking
        # for person_b's presence on each patent. A successful pair tends to
        # cluster on a small number of patents, so 5x is a reasonable bound.
        candidates = await _fetch_patents(
            http, query_a, page_size=max(max_results * 5, 25)
        )
    finally:
        if own_client:
            await http.aclose()

    matches: list[dict[str, Any]] = []
    for patent in candidates:
        if not _patent_includes_inventor(patent, person_b):
            continue
        record = _format_patent_record(patent)
        if record is None:
            continue
        matches.append(record)
        if len(matches) >= max_results:
            break

    logger.info(
        "patent extractor: %s × %s → %d co-invention(s) from %d candidate(s)",  # noqa: RUF001 — multiplication-sign reads more naturally here
        person_a.canonical_name,
        person_b.canonical_name,
        len(matches),
        len(candidates),
    )
    return matches
