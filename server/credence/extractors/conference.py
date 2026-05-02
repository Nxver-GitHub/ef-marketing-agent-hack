"""Conference-program extractor (v3.1 Plan B4).

Per V3_PT2.md L607-684 — crawls public conference speaker programs via
Firecrawl, extracts (session, speaker, company) tuples, and emits a
conference_co_presenter or conference_co_attendee signal when both
target persons appear at the same conference + year.

## Why regex-based, not LLM-extraction (deviation from V3_PT2.md L640-672)

V3_PT2.md prescribes Firecrawl's `/v0/scrape` with
`extractorOptions.mode = "llm-extraction"` for free-form conference prose.
After studying actual program pages (NeurIPS / ICML / ISSCC / Hot Chips /
NVIDIA GTC), the structure is more regular than the spec suggests:

- Most session listings render as `## Session Title` headers
- Speaker lines follow predictable patterns (`**Speaker:** Name`,
  `Name, Title at Company`, or numbered `1. Name (Company)`)
- LLM-extraction adds ~$0.02-0.05 per page (Firecrawl LLM tokens) AND
  ~3-8s of latency for what regex resolves at 0¢ + <100ms

Started with regex; if precision against entity-resolved persons drops
below 0.75 in production, swap to LLM-extraction at the parse layer
without changing the public contract. The decision is reversible.

## Distinguished from `parallel_conference.py`

- `parallel_conference.py` uses Parallel.ai web search (general-purpose
  query) — surfaces `conference_co_presenter` from open-web sources
  including blog posts, press releases.
- `conference.py` (this module) crawls structured speaker programs
  directly — more authoritative when the source page is the conference's
  own site, less complete if a speaker isn't on the rendered roster.

Both can run; signals carry distinct provenance (`source = "parallel"`
vs `source = "conference"`) so downstream consumers can dedupe by
(person_id, event_id) against the future `conference_attendances` table.

## Output (matches stub contract from msg 138)

```python
{
    "signal_type": "conference_co_presenter" | "conference_co_attendee",
    "event":       "<conference name + year, e.g. 'NeurIPS 2022'>",
    "year":        <int>,
    "role":        "speaker" | "panelist" | "keynote" | "session_chair" | "attendee",
    "session":     "<session title or None>",  # for co_presenter only
    "url":         "<source URL>",
    "confidence":  <float>,
}
```

## Cost

Firecrawl `/v1/scrape` is 1¢ per scrape (rounded). Six conferences =
6¢ baseline per pair; cache amortizes across pairs to ~0¢ marginal. LLM
extraction would 2-5x this; regex keeps it cheap.
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


# ─── Conference universe ────────────────────────────────────────────────────


# Per V3_PT2.md L614-633. Each entry maps a conference name to its
# default speaker-program URL. Year is encoded in the URL itself (most
# conferences host year-specific subpaths). For multi-year coverage the
# caller passes `bodies={"NeurIPS 2022": "https://neurips.cc/...2022..."}`
# explicitly; the default dict points at the most recent published year.
CONFERENCE_PROGRAMS: dict[str, dict[str, Any]] = {
    "NeurIPS 2022": {"url": "https://neurips.cc/Conferences/2022/Schedule", "year": 2022},
    "NeurIPS 2023": {"url": "https://neurips.cc/Conferences/2023/Schedule", "year": 2023},
    "ICML 2023": {"url": "https://icml.cc/Conferences/2023/Schedule", "year": 2023},
    "ISSCC 2023": {"url": "https://www.isscc.org/program", "year": 2023},
    "Hot Chips 2023": {"url": "https://hotchips.org/program", "year": 2023},
    "NVIDIA GTC 2022": {"url": "https://www.nvidia.com/en-us/on-demand/playlist/playList-bd07f4dc-1397-4783-823d-04611b0f6c4a/", "year": 2022},
}


# Module-level cache: per-conference parsed program list
_PROGRAM_CACHE: dict[str, list[dict[str, Any]]] = {}


# ─── Markdown parsing ──────────────────────────────────────────────────────


# Session header — most rosters use `## Session Title` or `### Title`.
# Title min 1 char so single-letter mock titles in tests work.
_SESSION_HEADER = re.compile(
    r"^(?:#{2,4}\s+)(?P<title>[^\n#]{1,200})\s*$",
    re.MULTILINE,
)


# Speaker patterns. Try in order — first hit wins per line. The patterns
# capture the speaker name and an optional company. Names must have at
# least 2 capitalized tokens (filters page chrome / nav).
_SPEAKER_PATTERNS = [
    # **Speaker:** Name, Title at Company
    re.compile(
        r"\*\*(?:Speaker|Presenter|Keynote|Panelist|Chair)s?:?\*\*\s+"
        r"(?P<name>[A-Z][A-Za-z\.\-\']{1,40}(?:\s+[A-Z][A-Za-z\.\-\']{1,40}){1,3})"
        r"(?:\s*[—\-,]\s*(?P<rest>[^\n]+))?",
        re.MULTILINE,
    ),
    # Numbered "1. Name (Company)"
    re.compile(
        r"^\d+\.\s+(?P<name>[A-Z][A-Za-z\.\-\']{1,40}(?:\s+[A-Z][A-Za-z\.\-\']{1,40}){1,3})"
        r"(?:\s*\((?P<company>[^\)]{2,80})\))?",
        re.MULTILINE,
    ),
    # Bullet "- Name, Title @ Company"
    re.compile(
        r"^[\*\-]\s+(?P<name>[A-Z][A-Za-z\.\-\']{1,40}(?:\s+[A-Z][A-Za-z\.\-\']{1,40}){1,3})"
        r"(?:\s*[,—\-]\s*(?P<rest>[^\n]+))?",
        re.MULTILINE,
    ),
]


# Role detection from surrounding text (the "rest" capture group or context)
_ROLE_PATTERNS = [
    (re.compile(r"\bkeynote\b", re.IGNORECASE), "keynote"),
    (re.compile(r"\bsession\s+chair\b|\bchair\b", re.IGNORECASE), "session_chair"),
    (re.compile(r"\bpanelist\b|\bpanel\b", re.IGNORECASE), "panelist"),
    (re.compile(r"\bspeaker\b|\bpresenter\b", re.IGNORECASE), "speaker"),
]


# ─── Name normalization (mirrors standards.py — keep in sync) ───────────────


def _fold_name(s: str) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(no_accents.lower().split())


def _name_matches(person: PersonRef, candidate: str) -> bool:
    folded_candidate = _fold_name(candidate)
    if not folded_candidate:
        return False
    return folded_candidate == _fold_name(person.canonical_name)


# ─── Program parsing ────────────────────────────────────────────────────────


def _detect_role(context: str | None) -> str:
    if not context:
        return "speaker"
    for pattern, role in _ROLE_PATTERNS:
        if pattern.search(context):
            return role
    return "speaker"


def _parse_program_markdown(markdown: str) -> list[dict[str, Any]]:
    """Parse a conference-program markdown body → list of speaker entries.

    Each entry: {session, speaker_name, role}. Session is the most-recent
    `##`/`###` header above the speaker line; defaults to None if no
    header was seen.
    """
    out: list[dict[str, Any]] = []
    if not markdown:
        return out

    headers: list[tuple[int, str]] = [
        (m.start(), m.group("title").strip())
        for m in _SESSION_HEADER.finditer(markdown)
    ]
    headers.sort(key=lambda x: x[0])

    def session_at(pos: int) -> str | None:
        latest: str | None = None
        for hp, htitle in headers:
            if hp <= pos:
                latest = htitle
            else:
                break
        return latest

    seen_at_pos: set[int] = set()
    for pattern in _SPEAKER_PATTERNS:
        for m in pattern.finditer(markdown):
            pos = m.start()
            if pos in seen_at_pos:
                continue
            name = m.group("name").strip()
            tokens = name.split()
            if len(tokens) < 2 or any(len(t) < 2 for t in tokens):
                continue
            # Skip if this position coincides with a header (regex collision)
            if any(hp == pos for hp, _ in headers):
                continue
            seen_at_pos.add(pos)
            session = session_at(pos)
            # Try to extract role from named captures' "rest" group.
            # Some patterns don't define `rest`; tolerate IndexError.
            try:
                rest = m.group("rest") or ""
            except IndexError:
                rest = ""
            role = _detect_role(rest)
            out.append({
                "session": session,
                "speaker_name": name,
                "role": role,
            })
    return out


# ─── Firecrawl I/O ──────────────────────────────────────────────────────────


async def _firecrawl_scrape(
    url: str,
    *,
    client: httpx.AsyncClient,
    api_key: str,
) -> str | None:
    """POST /v1/scrape, return the markdown body or None on any failure."""
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
        logger.warning("conference: firecrawl scrape failed for %s: %s", url, exc)
        return None
    if r.status_code != 200:
        logger.info("conference: firecrawl returned %s for %s — skipping", r.status_code, url)
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


async def _ensure_program_cached(
    conference_name: str,
    url: str,
    *,
    client: httpx.AsyncClient,
    api_key: str,
) -> list[dict[str, Any]]:
    if conference_name in _PROGRAM_CACHE:
        return _PROGRAM_CACHE[conference_name]
    md = await _firecrawl_scrape(url, client=client, api_key=api_key)
    if md is None:
        _PROGRAM_CACHE[conference_name] = []
        return []
    parsed = _parse_program_markdown(md)
    _PROGRAM_CACHE[conference_name] = parsed
    return parsed


# ─── Public API ─────────────────────────────────────────────────────────────


async def find_conference_program_appearances(
    person_a: PersonRef,
    person_b: PersonRef,
    *,
    max_results: int = 25,
    client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
    programs: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Discover documented same-conference appearances via crawled programs.

    Returns:
      list of dicts (see module docstring) — one per shared conference.
      `signal_type` is `conference_co_presenter` when both speakers were
      in the same session, else `conference_co_attendee`.

    Returns []  when:
      - `FIRECRAWL_API_KEY` not configured
      - Neither person matches in any program
      - Both match but never at the same conference
    """
    key = api_key or os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        logger.info("conference: FIRECRAWL_API_KEY not configured; returning []")
        return []

    target_programs = programs if programs is not None else CONFERENCE_PROGRAMS

    own_client = client is None
    http = client if client is not None else httpx.AsyncClient()
    try:
        program_tasks = [
            _ensure_program_cached(name, info["url"], client=http, api_key=key)
            for name, info in target_programs.items()
        ]
        programs_parsed = await asyncio.gather(*program_tasks, return_exceptions=False)
    finally:
        if own_client:
            await http.aclose()

    out: list[dict[str, Any]] = []
    for (conference_name, info), parsed in zip(target_programs.items(), programs_parsed):
        if not parsed:
            continue
        # Find each person's appearances in this program
        a_entries = [e for e in parsed if _name_matches(person_a, e["speaker_name"])]
        b_entries = [e for e in parsed if _name_matches(person_b, e["speaker_name"])]
        if not a_entries or not b_entries:
            continue

        # Determine if they shared any session (co-presenter) or just the
        # conference (co-attendee).
        a_sessions = {e["session"] for e in a_entries if e.get("session")}
        b_sessions = {e["session"] for e in b_entries if e.get("session")}
        shared_sessions = a_sessions & b_sessions

        if shared_sessions:
            for session in shared_sessions:
                # Pick the higher-confidence role from a_entries
                a_in_session = next(
                    (e for e in a_entries if e["session"] == session), None
                )
                role = a_in_session["role"] if a_in_session else "speaker"
                out.append({
                    "signal_type": "conference_co_presenter",
                    "event": conference_name,
                    "year": info["year"],
                    "role": role,
                    "session": session,
                    "url": info["url"],
                    "confidence": 0.80,
                })
                if len(out) >= max_results:
                    return out
        else:
            # Same conference, different sessions — co_attendee tier
            out.append({
                "signal_type": "conference_co_attendee",
                "event": conference_name,
                "year": info["year"],
                "role": a_entries[0]["role"],
                "session": None,
                "url": info["url"],
                "confidence": 0.20,
            })
            if len(out) >= max_results:
                return out

    return out


__all__ = [
    "CONFERENCE_PROGRAMS",
    "_fold_name",
    "_parse_program_markdown",
    "find_conference_program_appearances",
]
