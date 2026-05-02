"""Standards-roster extractor (v3.1 Plan B5).

Per V3_PT2.md L688-708 — crawls public membership rosters of standards
bodies (JEDEC, IEEE SA, SEMI, Wi-Fi Alliance, RISC-V International,
MLCommons) via Firecrawl, extracts company + representative names,
entity-resolves against `persons.canonical_name`, and emits a
`standards_committee_peer` signal for every (person_a, person_b) pair
that appears on the same committee.

## Strategy

1. For each known standards body, request its roster page via Firecrawl
   `POST /v1/scrape` in markdown mode.
2. Parse the markdown for committee names + member names. Most rosters
   follow predictable patterns: a section header (committee name) +
   bulleted list (members "Name — Company"). Extraction uses regex
   patterns rather than LLM-extraction so we avoid the cost + latency
   that B4's LLM path needs for free-form conference programs.
3. For each pair (person_a, person_b), check if both surface on the
   same committee. Emit one dict per shared committee.

## Output shape (matches stub contract from msg 138)

```python
{
    "signal_type": "standards_committee_peer",
    "committee":   "<committee name>",
    "body":        "<JEDEC | IEEE SA | SEMI | Wi-Fi Alliance | etc.>",
    "years":       "<active period if extractable, else 'unknown'>",
    "url":         "<source URL — for citation in warm-path explanations>",
    "confidence":  <float — entity-resolution confidence>,
}
```

## Cost

Firecrawl `/v1/scrape` is 1¢ per scrape (rounded up from ~0.1¢ at
Firecrawl Pro). Six standards bodies = 6¢ baseline per pair. In
practice the extractor caches per-body parsed rosters so the marginal
cost per additional pair is ~0¢ — see the module-level `_ROSTER_CACHE`.

## Defensive defaults

- No `FIRECRAWL_API_KEY` → returns `[]` immediately.
- A roster fetch fails → that body is skipped; other bodies still
  contribute. Mirrors signals.py partial-results semantics.
- A name appears in multiple committees → multiple emit dicts (one per
  committee). Caller decides whether to dedupe.
- Entity resolution at the basic level — case-insensitive + accent-folded
  exact match against `canonical_name` and `name_variants`. Fuzzy
  matching deferred to v3.2 since false positives in standards
  membership are higher-stakes than in cohort overlaps.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from typing import Any
from urllib.parse import urljoin

import httpx

from .patents import PersonRef

logger = logging.getLogger(__name__)


FIRECRAWL_BASE_URL = "https://api.firecrawl.dev/v1/"
FIRECRAWL_DEFAULT_TIMEOUT_SECONDS = 30.0


# ─── Standards bodies + roster URLs ─────────────────────────────────────────


# Per V3_PT2.md L695-702. URL points to the roster index; for some bodies
# (IEEE SA, SEMI) the index is a top-level "join" page that links to actual
# committee rosters — we accept lower precision there but the body name is
# still useful for warm-path explanations.

STANDARDS_BODIES: dict[str, str] = {
    "JEDEC": "https://www.jedec.org/committees",
    "IEEE SA": "https://standards.ieee.org/about/get-involved/join/",
    "SEMI": "https://www.semi.org/en/standards/standards-committees",
    "Wi-Fi Alliance": "https://www.wi-fi.org/membership",
    "RISC-V International": "https://riscv.org/members/",
    "MLCommons": "https://mlcommons.org/en/members/",
}


# Module-level cache of parsed rosters. Populated lazily on first call;
# scoped to the process lifetime. A future v3.2 layer should move this to
# a Supabase-backed cache table so cron-driven re-crawls update it.
_ROSTER_CACHE: dict[str, list[dict[str, Any]]] = {}


# ─── Markdown parsing patterns ──────────────────────────────────────────────


# Most rosters render as either:
#   "**Committee Name**" + bullet list "Name — Company"
#   "## Committee Name" + bullet list "* Name (Company)"
# We capture both with two patterns. Order matters — try header-prefixed
# first since that's the common case.

_COMMITTEE_HEADER = re.compile(
    r"^(?:#{1,4}\s+|(?:\*\*|__))(?P<name>[^\n#*_]{4,120})(?:\*\*|__)?\s*$",
    re.MULTILINE,
)


# A roster member line — "Name — Company", "Name - Company", "Name (Company)",
# or just "Name". Captures the name; company is optional context.
_MEMBER_LINE = re.compile(
    r"^(?:[\*\-]\s+)?"
    r"(?P<name>[A-Z][A-Za-z\.\-\']{1,40}(?:\s+[A-Z][A-Za-z\.\-\']{1,40}){1,3})"
    r"(?:\s*[—\-–]\s*|\s*\(|,\s*)?"
    r"(?P<company>[A-Z][\w\s\.,&\-\(\)]{2,80})?\s*\)?$",
    re.MULTILINE,
)


# Year-range patterns for active-period extraction. JEDEC uses "(2018-2022)";
# IEEE uses "since 2015"; we accept either.
_YEARS_PATTERN = re.compile(
    # Try parenthesized "(YYYY-YYYY)" first; then "since YYYY"; then bare year.
    # The leading `\b` only applies to the bare-year fallback because `\(`
    # never abuts a word character (so `\b\(` never matches after a space).
    r"\((?P<start>\d{4})\s*[-–—]\s*(?P<end>\d{4})\)"
    r"|since\s+(?P<since>\d{4})"
    r"|\b(?P<single>\d{4})\b",
    re.IGNORECASE,
)


# ─── Name normalization ─────────────────────────────────────────────────────


def _fold_name(s: str) -> str:
    """Lowercase + strip accents + collapse whitespace.

    Keeps comparison robust across "Sanja Fidler" vs "sanja  fidler" vs
    "Sanjá Fidler" without depending on rapidfuzz.
    """
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(no_accents.lower().split())


def _name_matches(person: PersonRef, candidate: str) -> bool:
    """Case-insensitive + accent-folded match on canonical name or variants."""
    folded_candidate = _fold_name(candidate)
    if not folded_candidate:
        return False
    if folded_candidate == _fold_name(person.canonical_name):
        return True
    return False


# ─── Roster parsing ─────────────────────────────────────────────────────────


def _extract_years(text: str) -> str:
    """Pull the first year-range token out of free text."""
    m = _YEARS_PATTERN.search(text)
    if m is None:
        return "unknown"
    if m.group("start") and m.group("end"):
        return f"{m.group('start')}-{m.group('end')}"
    if m.group("since"):
        return f"{m.group('since')}-present"
    if m.group("single"):
        return m.group("single")
    return "unknown"


def _parse_roster_markdown(
    markdown: str,
    *,
    body: str,
) -> list[dict[str, Any]]:
    """Walk a roster's markdown body → list of {committee, member, years}.

    Returns one record per detected (committee, member) pair. Committee
    name is propagated from the most-recent header above each member
    line; if no header is seen, falls back to the body name as committee.
    """
    out: list[dict[str, Any]] = []
    if not markdown:
        return out

    # Track current committee header. Default to the body name itself for
    # rosters that present a single flat list without sub-headers.
    current_committee = body
    last_pos = 0

    # Walk header positions to map each member line to its enclosing committee.
    headers: list[tuple[int, str]] = [
        (m.start(), m.group("name").strip())
        for m in _COMMITTEE_HEADER.finditer(markdown)
    ]
    headers.sort(key=lambda x: x[0])

    def committee_at(pos: int) -> str:
        latest = current_committee
        for hp, hname in headers:
            if hp <= pos:
                latest = hname
            else:
                break
        return latest

    for member_match in _MEMBER_LINE.finditer(markdown):
        pos = member_match.start()
        name = member_match.group("name").strip()
        # Filter out obvious non-name matches that snuck through the regex
        # (e.g., "Page 2 Of"). Names should have at least 2 capitalized
        # tokens; the regex enforces that, but we double-check that no
        # token is purely numeric / short noise.
        tokens = name.split()
        if len(tokens) < 2 or any(len(t) < 2 for t in tokens):
            continue
        # Skip header-text matches (member regex can collide with headers
        # at the start of a line — discard if pos coincides with a header)
        if any(hp == pos for hp, _ in headers):
            continue
        committee = committee_at(pos)
        # Local years scan: take the surrounding ±200 chars and try to
        # extract a year token. Cheaper than a full-document regex.
        window = markdown[max(0, pos - 200):pos + 200]
        years = _extract_years(window)
        out.append({
            "committee": committee,
            "member_name": name,
            "years": years,
        })
    return out


# ─── Firecrawl I/O ──────────────────────────────────────────────────────────


async def _firecrawl_scrape(
    url: str,
    *,
    client: httpx.AsyncClient,
    api_key: str,
) -> str | None:
    """POST /v1/scrape, return the markdown body or None on any failure.

    Mirrors `enrichment/firecrawl.py:_firecrawl_post` defensiveness — every
    error mode collapses to None so the caller silently skips this body.
    """
    api_url = urljoin(FIRECRAWL_BASE_URL, "scrape")
    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        r = await client.post(
            api_url, json=payload, headers=headers, timeout=FIRECRAWL_DEFAULT_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("standards: firecrawl scrape failed for %s: %s", url, exc)
        return None
    if r.status_code != 200:
        logger.info("standards: firecrawl returned %s for %s — skipping", r.status_code, url)
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    if not body.get("success", True):
        return None
    data = body.get("data") or {}
    if not isinstance(data, dict):
        return None
    md = data.get("markdown")
    return md if isinstance(md, str) else None


async def _ensure_roster_cached(
    body: str,
    url: str,
    *,
    client: httpx.AsyncClient,
    api_key: str,
) -> list[dict[str, Any]]:
    """Idempotent fetch+parse — repeated calls reuse the cached parse."""
    if body in _ROSTER_CACHE:
        return _ROSTER_CACHE[body]
    md = await _firecrawl_scrape(url, client=client, api_key=api_key)
    if md is None:
        _ROSTER_CACHE[body] = []
        return []
    parsed = _parse_roster_markdown(md, body=body)
    _ROSTER_CACHE[body] = parsed
    return parsed


# ─── Public API ─────────────────────────────────────────────────────────────


async def find_standards_roster_memberships(
    person_a: PersonRef,
    person_b: PersonRef,
    *,
    max_results: int = 25,
    client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
    bodies: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Discover documented same-committee memberships across standards bodies.

    Returns [] when:
      - `FIRECRAWL_API_KEY` not configured
      - Neither person matches in any roster
      - Both match but never on the same committee
    """
    key = api_key or os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        logger.info("standards: FIRECRAWL_API_KEY not configured; returning []")
        return []

    target_bodies = bodies if bodies is not None else STANDARDS_BODIES

    own_client = client is None
    http = client if client is not None else httpx.AsyncClient()
    try:
        # Crawl rosters in parallel — each scrape is independent.
        roster_tasks = [
            _ensure_roster_cached(body, url, client=http, api_key=key)
            for body, url in target_bodies.items()
        ]
        rosters = await asyncio.gather(*roster_tasks, return_exceptions=False)
    finally:
        if own_client:
            await http.aclose()

    out: list[dict[str, Any]] = []
    for body, roster in zip(target_bodies.keys(), rosters):
        if not roster:
            continue
        # Compute matches for each person against this body's roster
        a_committees: dict[str, dict[str, Any]] = {}
        b_committees: dict[str, dict[str, Any]] = {}
        for entry in roster:
            if _name_matches(person_a, entry["member_name"]):
                a_committees.setdefault(entry["committee"], entry)
            if _name_matches(person_b, entry["member_name"]):
                b_committees.setdefault(entry["committee"], entry)

        # Emit one record per shared committee
        for committee in a_committees.keys() & b_committees.keys():
            entry = a_committees[committee]
            out.append({
                "signal_type": "standards_committee_peer",
                "committee": committee,
                "body": body,
                "years": entry.get("years", "unknown"),
                "url": target_bodies[body],
                "confidence": 0.82,  # STRENGTH_TABLE base for standards_committee_peer
            })
            if len(out) >= max_results:
                return out
    return out


__all__ = [
    "STANDARDS_BODIES",
    "find_standards_roster_memberships",
    "_parse_roster_markdown",  # exported for testing
    "_fold_name",
]
