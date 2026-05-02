"""Education extractor (v3.1 Plan B3).

Per V3_PT2.md L484-605 — pulls education arrays from PDL for both
prospects, normalizes school names against a curated alias table, and
emits cohort-overlap signals when the two persons attended the same
institution + degree_type with overlapping years.

## Strategy

1. PDL-enrich each prospect via `/v5/person/enrich`, request the
   `education[]` array. Highest-precision identifier (LinkedIn URL)
   when present; fall back to name+company. Skipped when no usable
   identifier exists.
2. Each PDL `education` entry → canonical institution + degree type +
   graduation year. Normalization tries (in order):
   a. exact canonical_name match
   b. exact alias match (case-insensitive)
   c. fuzzy fallback via `difflib.get_close_matches` at threshold 0.88
   d. None — drop the entry (no fictional data per CLAUDE.md
      "Common Mistakes" #6)
3. Cross-reference the two normalized arrays — for every (institution,
   degree_type) pair both persons share, emit an overlap dict with
   `compute_cohort_strength` as the confidence.

## Output shape (consumed by signals.py route)

```python
{
    "signal_type":     "same_mba_cohort" | "same_phd_program"
                      | "executive_education" | "same_undergrad_cohort",
    "institution":     <canonical_name>,
    "degree_type":     "mba" | "phd" | "emba" | "bs" | "ms" | "exec_ed",
    "graduation_year": <int — person_a's year>,
    "graduation_year_other": <int — person_b's year>,
    "same_program":    <bool — same major/program inferred from PDL>,
    "year_gap":        <int — abs delta between the two graduation years>,
    "confidence":      <float in [0, 1] — compute_cohort_strength output>,
    "source_school_a": <raw PDL school string for person_a, debug>,
    "source_school_b": <raw PDL school string for person_b, debug>,
}
```

The route honors `confidence` directly; falls back to STRENGTH_TABLE
per `signal_type` if missing (see `signals._confidence_for`).

## Cost

PDL `/person/enrich` is 28¢ per call (see `enrichment/pdl.py`). This
extractor is two calls per `(person_a, person_b)` pair = 56¢ baseline
when neither prospect has cached PDL data. A future v3.2 cache layer
should sit between this extractor and PDL to amortize across pairs that
share a person.

## What this stub explicitly does NOT do

- Does NOT write to `education_overlaps` table — the signals.py route
  persists each emitted dict to v2 `signals` for now. Migration to
  the v3 table happens in a separate cohort_strength_job after the
  schema lands and B1 is applied.
- Does NOT decide person_a < person_b ordering — the signals route
  passes them in caller-stable order and the route picks the
  `connected_to` direction.
- Does NOT call rapidfuzz — uses stdlib `difflib` so the import works
  in environments without rapidfuzz installed. Future precision boost
  is a one-line swap.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import os
from typing import Any
from urllib.parse import urljoin

import httpx

from .patents import PersonRef

logger = logging.getLogger(__name__)


# ─── PDL configuration (mirrors enrichment/pdl.py to avoid the import ───────
#     coupling that would force every test that mocks PDL to re-stub there) ──

PDL_BASE_URL = "https://api.peopledatalabs.com/v5/"
PDL_DEFAULT_TIMEOUT_SECONDS = 12.0


# ─── School / institution normalization ──────────────────────────────────────
#
# Curated alias table for the schools that produce the bulk of the talent
# pool we're enriching today (semi/AI, plus the major MBA programs). When a
# school surfaces that's not in this table, we fall through to fuzzy match
# and ultimately drop the entry rather than fabricate. Per V3_PT2.md
# L523-550 + L539-550.

MBA_SCHOOL_ALIASES: dict[str, list[str]] = {
    "Harvard Business School": [
        "HBS", "Harvard University", "Harvard Business",
        "Harvard Univ Business School",
    ],
    "Wharton School": [
        "Wharton", "University of Pennsylvania Wharton",
        "UPenn Wharton", "Penn Wharton",
    ],
    "Stanford Graduate School of Business": [
        "Stanford GSB", "Stanford Business",
        "Stanford University GSB",
    ],
    "MIT Sloan School of Management": [
        "MIT Sloan", "Sloan MIT", "Sloan School",
        "Massachusetts Institute of Technology Sloan",
    ],
    "Kellogg School of Management": [
        "Kellogg", "Northwestern Kellogg",
        "Northwestern University Kellogg",
    ],
    "Booth School of Business": [
        "Booth", "Chicago Booth", "University of Chicago Booth",
    ],
    "Columbia Business School": [
        "Columbia Business", "CBS",
    ],
    "Haas School of Business": [
        "Haas", "UC Berkeley Haas", "Berkeley Haas",
    ],
    "Tuck School of Business": [
        "Tuck", "Dartmouth Tuck",
    ],
    "Fuqua School of Business": [
        "Fuqua", "Duke Fuqua",
    ],
    # PhD programs that produce semiconductor / AI talent
    "MIT EECS": [
        "MIT Electrical Engineering", "MIT Computer Science",
        "Massachusetts Institute of Technology EECS",
    ],
    "Stanford EE": [
        "Stanford Electrical Engineering", "Stanford CS",
    ],
    "Carnegie Mellon CS": [
        "CMU CS", "Carnegie Mellon Computer Science",
    ],
    "UC Berkeley EECS": [
        "Berkeley EECS", "UC Berkeley Computer Science",
    ],
    "Caltech": [
        "California Institute of Technology",
    ],
    # Executive education
    "Harvard Business School (Executive)": [
        "HBS AMP", "HBS Executive Education",
        "Harvard Advanced Management Program",
    ],
    "Kellogg Executive Education": [
        "Kellogg EMBA", "Northwestern Executive Education",
    ],
}


# Type per institution name → which `degree_type` keyspace value its PDL
# entries map to. Entries here override PDL's own degree string when the
# canonical school's institution_type makes it unambiguous (e.g., HBS is
# always MBA-or-exec_ed; never undergrad).
INSTITUTION_DEFAULT_DEGREE: dict[str, str] = {
    "Harvard Business School": "mba",
    "Wharton School": "mba",
    "Stanford Graduate School of Business": "mba",
    "MIT Sloan School of Management": "mba",
    "Kellogg School of Management": "mba",
    "Booth School of Business": "mba",
    "Columbia Business School": "mba",
    "Haas School of Business": "mba",
    "Tuck School of Business": "mba",
    "Fuqua School of Business": "mba",
    "Harvard Business School (Executive)": "exec_ed",
    "Kellogg Executive Education": "exec_ed",
}


# Cohort-size lookup → tighter cohorts = higher strength multiplier.
# V3_PT2.md L585-590 describes the bands.
INSTITUTION_TYPICAL_COHORT_SIZE: dict[str, int] = {
    "Harvard Business School": 950,
    "Wharton School": 850,
    "Stanford Graduate School of Business": 410,
    "MIT Sloan School of Management": 410,
    "Kellogg School of Management": 470,
    "Booth School of Business": 590,
    "Columbia Business School": 760,
    "Haas School of Business": 290,
    "Tuck School of Business": 290,
    "Fuqua School of Business": 440,
    "MIT EECS": 100,  # graduate program; tight cohort
    "Stanford EE": 100,
    "Carnegie Mellon CS": 80,
    "UC Berkeley EECS": 100,
    "Caltech": 60,
    "Harvard Business School (Executive)": 70,
    "Kellogg Executive Education": 80,
}


# Map degree_type → connection signal_type (mirrors V3_PT2.md L376-380).
DEGREE_TO_SIGNAL_TYPE: dict[str, str] = {
    "mba": "same_mba_cohort",
    "phd": "same_phd_program",
    "ms": "same_phd_program",  # Master's in same program treated as PhD-cohort tier
    "emba": "executive_education",
    "exec_ed": "executive_education",
    "bs": "same_undergrad_cohort",
}


def normalize_school(raw_name: str | None) -> str | None:
    """Map a free-text school name → canonical institution name, or None.

    Lookup order:
      1. exact canonical match (case-insensitive)
      2. exact alias match (case-insensitive)
      3. fuzzy match via difflib at cutoff 0.88
      4. None — drop the entry
    """
    if not raw_name or not raw_name.strip():
        return None
    raw_lower = raw_name.lower().strip()

    # 1 & 2 — exact and alias matches
    for canonical, aliases in MBA_SCHOOL_ALIASES.items():
        if raw_lower == canonical.lower():
            return canonical
        for alias in aliases:
            if raw_lower == alias.lower():
                return canonical

    # 3 — fuzzy fallback. Build the search universe lazily.
    universe: list[str] = []
    for canonical, aliases in MBA_SCHOOL_ALIASES.items():
        universe.append(canonical)
        universe.extend(aliases)
    matches = difflib.get_close_matches(raw_name, universe, n=1, cutoff=0.88)
    if not matches:
        return None
    matched_string = matches[0]
    for canonical, aliases in MBA_SCHOOL_ALIASES.items():
        if matched_string == canonical or matched_string in aliases:
            return canonical
    return None


def _classify_degree(
    institution: str,
    raw_degrees: list[str] | None,
    raw_major: str | None,
) -> str | None:
    """Decide degree_type. Institution-default wins; falls back to PDL strings.

    Returns one of the V3_PT2.md keyspace values
    (`mba`, `phd`, `emba`, `bs`, `ms`, `exec_ed`) or None when undecidable.
    """
    institutional = INSTITUTION_DEFAULT_DEGREE.get(institution)
    if institutional:
        return institutional

    if not raw_degrees:
        return None
    blob = " ".join(d.lower() for d in raw_degrees if isinstance(d, str))
    major = (raw_major or "").lower()

    if "executive" in blob or "amp" in blob.split():
        return "exec_ed"
    if "emba" in blob or "executive mba" in blob:
        return "emba"
    if "mba" in blob or "master of business" in blob:
        return "mba"
    if "phd" in blob or "doctorate" in blob or "doctor of philosophy" in blob:
        return "phd"
    if "ms " in blob + " " or "master of science" in blob or " ms" in blob:
        return "ms"
    if "bs " in blob + " " or "bachelor" in blob or " ba " in blob + " ":
        return "bs"
    return None


# ─── Cohort-strength scoring ─────────────────────────────────────────────────


def compute_cohort_strength(
    *,
    institution: str,
    degree_type: str,
    graduation_year_a: int | None,
    graduation_year_b: int | None,
    same_program: bool,
) -> float:
    """Score a cohort overlap. Per V3_PT2.md L568-595.

    base × year_factor × size_factor × program_factor, capped at 0.99.
    """
    # base — STRENGTH_TABLE values per signal_type
    signal_type = DEGREE_TO_SIGNAL_TYPE.get(degree_type, "alumni_network")
    base = {
        "same_mba_cohort": 0.85,
        "same_phd_program": 0.78,
        "executive_education": 0.70,
        "same_undergrad_cohort": 0.62,
        "alumni_network": 0.25,
    }.get(signal_type, 0.50)

    # year_factor — same year = 1.0; 1y = 0.80; 2+y = 0.50 (alumni_network tier)
    if graduation_year_a is None or graduation_year_b is None:
        # Without both years we can't compute a meaningful cohort overlap;
        # treat as 1-year-apart (conservative middle).
        year_factor = 0.80
    else:
        year_gap = abs(graduation_year_a - graduation_year_b)
        if year_gap == 0:
            year_factor = 1.0
        elif year_gap == 1:
            year_factor = 0.80
        else:
            year_factor = 0.50

    # size_factor — tighter cohorts = stronger
    cohort_size = INSTITUTION_TYPICAL_COHORT_SIZE.get(institution)
    if cohort_size is not None and cohort_size <= 100:
        size_factor = 1.10
    elif cohort_size is not None and cohort_size <= 500:
        size_factor = 1.00
    else:
        size_factor = 0.85  # cohort >500 OR unknown school

    # program_factor — same major/section adds 5%
    program_factor = 1.05 if same_program else 1.00

    return min(0.99, base * year_factor * size_factor * program_factor)


# ─── PDL fetch + parse ──────────────────────────────────────────────────────


def _coerce_year(date_str: str | None) -> int | None:
    """PDL emits YYYY or YYYY-MM strings; return the leading 4-digit year."""
    if not isinstance(date_str, str) or not date_str.strip():
        return None
    head = date_str.strip()[:4]
    if head.isdigit():
        return int(head)
    return None


def _education_entry_from_pdl(entry: dict[str, Any]) -> dict[str, Any] | None:
    """One PDL education[] item → normalized in-memory record, or None.

    Emits only when normalization succeeds; otherwise returns None to drop.
    """
    if not isinstance(entry, dict):
        return None
    school = entry.get("school") or {}
    if not isinstance(school, dict):
        return None
    raw_name = school.get("name")
    canonical = normalize_school(raw_name if isinstance(raw_name, str) else None)
    if canonical is None:
        return None

    raw_degrees = entry.get("degrees")
    if not isinstance(raw_degrees, list):
        raw_degrees = []
    raw_majors = entry.get("majors") or []
    raw_major = raw_majors[0] if isinstance(raw_majors, list) and raw_majors else None
    degree_type = _classify_degree(canonical, raw_degrees, raw_major)
    if degree_type is None:
        return None

    grad_year = _coerce_year(entry.get("end_date"))

    return {
        "institution": canonical,
        "degree_type": degree_type,
        "graduation_year": grad_year,
        "major": raw_major if isinstance(raw_major, str) else None,
        "raw_school_name": raw_name if isinstance(raw_name, str) else None,
    }


async def _fetch_education(
    person: PersonRef,
    *,
    client: httpx.AsyncClient,
    api_key: str,
) -> list[dict[str, Any]]:
    """Single PDL `/person/enrich` call → normalized education list.

    Returns [] on any failure (no key, no identifier, network error,
    non-match, malformed body) — same defensive pattern as enrichment/pdl.py.
    """
    params: dict[str, Any] = {"min_likelihood": 6, "pretty": "false"}
    if person.linkedin_url:
        params["profile"] = person.linkedin_url
    elif person.canonical_name:
        params["name"] = person.canonical_name
    else:
        logger.debug("education: %s has no identifier; skipping PDL", person.canonical_name)
        return []

    url = urljoin(PDL_BASE_URL, "person/enrich")
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    try:
        r = await client.get(url, params=params, headers=headers, timeout=PDL_DEFAULT_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.warning("education PDL fetch failed for %s: %s", person.canonical_name, exc)
        return []

    if r.status_code != 200:
        # 404 = no match (PDL convention); other codes = upstream issue
        if r.status_code != 404:
            logger.info("education PDL %s for %s — skipping", r.status_code, person.canonical_name)
        return []

    try:
        body = r.json()
    except ValueError:
        return []
    if not isinstance(body, dict):
        return []
    data = body.get("data") or {}
    if not isinstance(data, dict):
        return []
    raw_education = data.get("education")
    if not isinstance(raw_education, list):
        return []

    out: list[dict[str, Any]] = []
    for entry in raw_education:
        record = _education_entry_from_pdl(entry)
        if record is not None:
            out.append(record)
    return out


# ─── Public API ─────────────────────────────────────────────────────────────


async def find_education_overlaps(
    person_a: PersonRef,
    person_b: PersonRef,
    *,
    max_results: int = 25,
    client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Discover documented education-cohort overlaps between two persons.

    See module docstring for the output dict shape. Returns [] when:
      - No `PDL_API_KEY` configured
      - Either prospect has no usable identifier
      - PDL returns no education matches for either
      - The two normalized education arrays share no (institution,
        degree_type) pair
    """
    key = api_key or os.environ.get("PDL_API_KEY")
    if not key:
        logger.info("education: PDL_API_KEY not configured; returning []")
        return []

    own_client = client is None
    http = client if client is not None else httpx.AsyncClient()
    try:
        edu_a, edu_b = await asyncio.gather(
            _fetch_education(person_a, client=http, api_key=key),
            _fetch_education(person_b, client=http, api_key=key),
        )
    finally:
        if own_client:
            await http.aclose()

    if not edu_a or not edu_b:
        return []

    # Index B by (institution, degree_type) for O(n+m) cross-reference
    index_b: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry_b in edu_b:
        key_b = (entry_b["institution"], entry_b["degree_type"])
        index_b.setdefault(key_b, []).append(entry_b)

    out: list[dict[str, Any]] = []
    for entry_a in edu_a:
        match_key = (entry_a["institution"], entry_a["degree_type"])
        partners = index_b.get(match_key)
        if not partners:
            continue
        for entry_b in partners:
            same_program = (
                bool(entry_a.get("major"))
                and entry_a.get("major") == entry_b.get("major")
            )
            grad_a = entry_a.get("graduation_year")
            grad_b = entry_b.get("graduation_year")
            year_gap = (
                abs(grad_a - grad_b)
                if isinstance(grad_a, int) and isinstance(grad_b, int)
                else None
            )
            confidence = compute_cohort_strength(
                institution=entry_a["institution"],
                degree_type=entry_a["degree_type"],
                graduation_year_a=grad_a if isinstance(grad_a, int) else None,
                graduation_year_b=grad_b if isinstance(grad_b, int) else None,
                same_program=same_program,
            )
            signal_type = DEGREE_TO_SIGNAL_TYPE.get(
                entry_a["degree_type"], "alumni_network"
            )
            out.append({
                "signal_type": signal_type,
                "institution": entry_a["institution"],
                "degree_type": entry_a["degree_type"],
                "graduation_year": grad_a,
                "graduation_year_other": grad_b,
                "same_program": same_program,
                "year_gap": year_gap,
                "confidence": confidence,
                "source_school_a": entry_a.get("raw_school_name"),
                "source_school_b": entry_b.get("raw_school_name"),
            })
            if len(out) >= max_results:
                return out
    return out


__all__ = [
    "DEGREE_TO_SIGNAL_TYPE",
    "INSTITUTION_DEFAULT_DEGREE",
    "INSTITUTION_TYPICAL_COHORT_SIZE",
    "MBA_SCHOOL_ALIASES",
    "compute_cohort_strength",
    "find_education_overlaps",
    "normalize_school",
]
