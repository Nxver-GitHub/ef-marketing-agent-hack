"""Entity resolution + canonicalization across enrichment sources.

The hardest part of multi-source enrichment is deciding when two records
refer to the same human. ``Phebe Novakovic`` (Apify LinkedIn) and
``{name: "Phebe Novakovic", title: "CEO"}`` (Apollo) are the same person —
the merger needs to recognize that and produce one ``CanonicalPerson``
with provenance.

## Resolution priority

When the same field appears in multiple records, pick the one from the
higher-priority source:

    Apify     (LinkedIn — current, structured, comprehensive)
       ↓
    Apollo    (verified email, often-fresher title)
       ↓
    PDL       (deep history when LinkedIn URL known — on-demand only)
       ↓
    manual    (lowest priority)

## Matching strategy

Two records are the same person if ANY of:

1. ``linkedin_url`` exact match (after normalization — strip /, lowercase)
2. ``email`` exact match (lowercased)
3. ``(first, last, company)`` tuple match (normalized — middle initials
   stripped, suffixes removed, company aliases resolved)
4. Fuzzy ``(first + last)`` ≥ 0.88 + same company

Per CLAUDE.md "Decision 4" — when none of the above match but a record
references an unresolvable role (e.g., job-posting "VP of Manufacturing"),
emit an ``UnresolvedTarget`` row instead of dropping silently.

## What this module does NOT do

- Persistence (``writer.py``)
- Vendor I/O (``apollo.py`` / ``apify.py`` / ``pdl.py``)
- Scoring (``score_runner.py``)

Pure functional: input records → output ``CanonicalPerson`` list. Easily
unit-testable without DB or HTTP.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Literal

from ..taxonomy import (
    domain_from_title,
    seniority_from_title,
)

logger = logging.getLogger(__name__)


# ─── Source priority ────────────────────────────────────────────────────────

SourceName = Literal["apify", "apollo", "pdl", "manual"]

_SOURCE_PRIORITY: dict[str, int] = {
    "apify": 80,
    "apollo": 60,
    "pdl": 40,
    "manual": 20,
}


def _src_rank(source: str) -> int:
    return _SOURCE_PRIORITY.get(source, 0)


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class CanonicalPerson:
    """One real human, merged across all source records seen for them.

    All fields except ``sources`` are mutable so the merger can fill them
    in incrementally as records arrive. Once write happens this is frozen
    in DB land.
    """

    # Identity
    canonical_name: str                    # "Phebe Novakovic"
    first_name: str
    last_name: str
    name_variants: list[str] = field(default_factory=list)  # all raw names seen

    # Contact / identifiers
    linkedin_url: str | None = None
    linkedin_id: str | None = None         # LinkedIn internal ID
    email: str | None = None
    email_status: str | None = None        # "verified" | "unverified" | None
    pdl_person_id: str | None = None
    uspto_inventor_id: str | None = None
    orcid: str | None = None

    # Current-job summary
    current_title: str | None = None
    current_company_name: str | None = None
    current_seniority_score: int | None = None
    current_functional_domain: str | None = None

    # Geographic
    location_text: str | None = None
    country_code: str | None = None

    # LinkedIn-derived attributes (Authority + engagement signals)
    headline: str | None = None
    connections_count: int | None = None
    followers_count: int | None = None
    premium: bool = False
    verified: bool = False
    open_to_work: bool = False
    hiring: bool = False
    registered_at: str | None = None

    # Structured history (matches our schema directly)
    employment_periods: list[dict[str, Any]] = field(default_factory=list)
    education_periods: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    certifications: list[dict[str, Any]] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)

    # Authenticity / Authority signal pools
    publications: list[dict[str, Any]] = field(default_factory=list)
    patents: list[dict[str, Any]] = field(default_factory=list)
    honors_and_awards: list[dict[str, Any]] = field(default_factory=list)
    organizations: list[dict[str, Any]] = field(default_factory=list)

    # Provenance — every field knows which source set it
    sources: dict[str, str] = field(default_factory=dict)


# ─── Name normalization ─────────────────────────────────────────────────────


_SUFFIX_PATTERNS = (
    r"\s*,?\s*(?:Jr\.?|Sr\.?|II|III|IV|V|Ph\.?D\.?|Ed\.?D\.?|M\.?D\.?|"
    r"Esq\.?|CFA|CPA)\s*$"
)
_PREFIX_PATTERNS = r"^(?:Dr\.?|Mr\.?|Mrs\.?|Ms\.?|Prof\.?)\s+"
_MIDDLE_INITIAL = r"\s+[A-Z]\.?\s+"


def normalize_name(raw_name: str) -> tuple[str, str]:
    """Strip prefixes, suffixes, middle initials → ``(first, last)``.

    Returns ``("", "")`` when the input doesn't look like a personal name
    (single token, empty, etc.). Caller should treat that as "drop this
    record" since downstream entity-resolution can't disambiguate.

    Examples:
      "Dr. James R. Clarke, Jr." → ("James", "Clarke")
      "Phebe N. Novakovic"        → ("Phebe", "Novakovic")
      "Lin Wei"                   → ("Lin", "Wei")
      "Madonna"                   → ("", "")
    """
    if not isinstance(raw_name, str) or not raw_name.strip():
        return "", ""

    name = raw_name.strip()
    name = re.sub(_PREFIX_PATTERNS, "", name)
    name = re.sub(_SUFFIX_PATTERNS, "", name, flags=re.IGNORECASE)
    # Collapse multiple spaces, strip middle initials
    name = re.sub(_MIDDLE_INITIAL, " ", name)
    name = re.sub(r"\s+", " ", name).strip()

    parts = name.split(" ")
    if len(parts) < 2:
        return "", ""
    return parts[0], parts[-1]


# ─── Company normalization ──────────────────────────────────────────────────

# Top-30 / Tier-1+2+3 alias map. Matches PROSPECT_ENRICHMENT_TASK.md target
# list. ``canonical_name`` is what we store in ``companies.canonical_name``;
# all aliases resolve to that.
_COMPANY_ALIASES: dict[str, list[str]] = {
    # Tier 1 — Semiconductor
    "NVIDIA": ["NVIDIA Corporation", "NVIDIA Corp", "NVIDIA Inc"],
    "Intel": ["Intel Corporation", "Intel Corp", "Intel Inc"],
    "AMD": ["Advanced Micro Devices", "Advanced Micro Devices Inc",
            "Advanced Micro Devices, Inc.", "AMD Inc"],
    "Qualcomm": ["Qualcomm Incorporated", "Qualcomm Inc",
                  "Qualcomm Technologies"],
    "TSMC": ["Taiwan Semiconductor Manufacturing Company",
              "Taiwan Semiconductor Manufacturing", "TSMC North America"],
    "ASML": ["ASML Holding", "ASML Holding NV", "ASML Inc"],
    "Broadcom": ["Broadcom Inc", "Broadcom Limited", "Broadcom Corporation"],
    "Marvell Technology": ["Marvell", "Marvell Technology Group",
                            "Marvell Semiconductor"],
    "Micron Technology": ["Micron", "Micron Technology Inc"],
    "Applied Materials": ["Applied Materials Inc"],
    "Lam Research": ["Lam Research Corporation"],
    "KLA Corporation": ["KLA", "KLA-Tencor", "KLA Tencor"],
    "Synopsys": ["Synopsys Inc", "Synopsys, Inc."],
    "Cadence Design Systems": ["Cadence", "Cadence Design", "Cadence Inc"],
    "Texas Instruments": ["TI", "Texas Instruments Inc",
                           "Texas Instruments Incorporated"],
    "NXP Semiconductors": ["NXP", "NXP Semiconductor"],
    "Infineon Technologies": ["Infineon", "Infineon AG"],
    "Arm Holdings": ["Arm", "ARM Holdings", "ARM Limited"],
    "SK Hynix": ["SK hynix", "Hynix"],
    "Samsung Semiconductor": ["Samsung Electronics", "Samsung Semi"],

    # Tier 2 — Defense
    "Lockheed Martin": ["Lockheed Martin Corporation", "Lockheed",
                         "LMT", "Lockheed Martin Corp"],
    "RTX": ["Raytheon Technologies", "Raytheon", "Raytheon Company",
             "Raytheon Technologies Corporation"],
    "Northrop Grumman": ["Northrop Grumman Corporation", "NOC",
                          "Northrop Grumman Corp"],
    "General Dynamics": ["General Dynamics Corporation",
                          "General Dynamics Corp", "GD"],
    "L3Harris Technologies": ["L3Harris", "L3 Harris", "Harris Corporation"],
    "BAE Systems": ["BAE", "BAE Systems Inc", "BAE Systems plc"],
    "Leidos": ["Leidos Holdings", "Leidos Inc"],
    "SAIC": ["Science Applications International", "SAIC Inc"],
    "Booz Allen Hamilton": ["Booz Allen", "Booz Allen Hamilton Inc"],
    "Palantir Technologies": ["Palantir", "Palantir Technologies Inc"],
    "Anduril Industries": ["Anduril"],
    "Shield AI": [],
    "MITRE Corporation": ["MITRE", "MITRE Corp"],

    # Tier 3 — Aerospace
    "Boeing": ["The Boeing Company", "Boeing Company", "Boeing Inc"],
    "Airbus": ["Airbus SE", "Airbus Group"],
    "SpaceX": ["Space Exploration Technologies",
                "Space Exploration Technologies Corp"],
    "Rocket Lab": ["Rocket Lab USA", "Rocket Lab Inc"],
    "Aerojet Rocketdyne": ["Aerojet"],
    "Textron Aviation": ["Textron"],
    "Honeywell Aerospace": ["Honeywell"],
    "Collins Aerospace": ["Collins Aerospace (RTX)"],
    "GE Aerospace": ["GE Aviation", "General Electric Aerospace"],
    "Joby Aviation": ["Joby"],
    "Archer Aviation": ["Archer"],
}

# Build the reverse lookup once at module load: lower(alias) → canonical
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for canonical, aliases in _COMPANY_ALIASES.items():
    _ALIAS_TO_CANONICAL[canonical.lower()] = canonical
    for alias in aliases:
        _ALIAS_TO_CANONICAL[alias.lower()] = canonical

# Generic suffixes to strip when no alias hits. These are the patterns that
# appear after any company name in legal/SEC filings.
_COMPANY_SUFFIX_PATTERNS = re.compile(
    r"\s*[,.]?\s*(?:Inc\.?|Incorporated|Corp\.?|Corporation|LLC|"
    r"L\.L\.C\.|Limited|Ltd\.?|Co\.?|Company|plc|PLC|GmbH|S\.A\.?|"
    r"NV|N\.V\.|AG|SE|SA)\s*$",
    re.IGNORECASE,
)


def normalize_company(raw_company: str | None) -> str | None:
    """Map any company string to its canonical form.

    Returns the input (trimmed) when no alias matches and no suffix could
    be stripped. Returns None for empty/None input.

    Examples:
      "Lockheed Martin Corporation" → "Lockheed Martin"
      "AMD Inc"                     → "AMD"
      "Marvell Semiconductor"       → "Marvell Technology"
      "Some Random Co"              → "Some Random"  (suffix stripped)
    """
    if not isinstance(raw_company, str) or not raw_company.strip():
        return None

    raw = raw_company.strip()
    # Direct alias hit?
    canonical = _ALIAS_TO_CANONICAL.get(raw.lower())
    if canonical:
        return canonical

    # Strip generic suffix and try again
    stripped = _COMPANY_SUFFIX_PATTERNS.sub("", raw).strip()
    canonical = _ALIAS_TO_CANONICAL.get(stripped.lower())
    if canonical:
        return canonical

    # No alias hit — return the cleanest form we have
    return stripped or raw


# ─── Same-person matching ──────────────────────────────────────────────────


def _normalize_url(url: str | None) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    return url.strip().rstrip("/").lower()


def _normalize_email(email: str | None) -> str | None:
    if not isinstance(email, str) or "@" not in email:
        return None
    return email.strip().lower()


def same_person(a: CanonicalPerson, b: CanonicalPerson, *, fuzzy_threshold: float = 88.0) -> bool:
    """Decide whether two CanonicalPerson records refer to the same human.

    Order of cheapest-most-precise checks first:
    1. LinkedIn URL exact (when both have one)
    2. LinkedIn ID exact
    3. Email exact
    4. PDL person_id exact
    5. (first, last, company) — case-insensitive
    6. Fuzzy first+last (rapidfuzz token_sort_ratio ≥ threshold) AND same company
    """
    # 1. LinkedIn URL
    a_url = _normalize_url(a.linkedin_url)
    b_url = _normalize_url(b.linkedin_url)
    if a_url and b_url:
        return a_url == b_url

    # 2. LinkedIn internal ID
    if a.linkedin_id and b.linkedin_id and a.linkedin_id == b.linkedin_id:
        return True

    # 3. Email
    a_email = _normalize_email(a.email)
    b_email = _normalize_email(b.email)
    if a_email and b_email:
        return a_email == b_email

    # 4. PDL person id
    if a.pdl_person_id and b.pdl_person_id and a.pdl_person_id == b.pdl_person_id:
        return True

    # 5. (first, last, company) exact, case-insensitive
    a_first, a_last = a.first_name.lower(), a.last_name.lower()
    b_first, b_last = b.first_name.lower(), b.last_name.lower()
    a_co = (a.current_company_name or "").lower()
    b_co = (b.current_company_name or "").lower()
    if a_first and a_last and (a_first, a_last) == (b_first, b_last):
        if a_co and b_co and a_co == b_co:
            return True

    # 6. Fuzzy name + same company (stdlib difflib — no rapidfuzz dep)
    if a_co and b_co and a_co == b_co and a.first_name and b.first_name:
        # Token-sort: sort tokens before comparing to handle "John A Doe"
        # ≈ "John Doe" or "Sarah Kim" ≈ "Sarah J Kim"
        toks_a = sorted(f"{a.first_name} {a.last_name}".lower().split())
        toks_b = sorted(f"{b.first_name} {b.last_name}".lower().split())
        ratio = SequenceMatcher(None, " ".join(toks_a), " ".join(toks_b)).ratio() * 100
        if ratio >= fuzzy_threshold:
            return True

    return False


# ─── Source-specific record builders ───────────────────────────────────────


def from_apify(profile: Any) -> CanonicalPerson | None:
    """Map ``apify.ApifyProfile`` → CanonicalPerson."""
    if profile is None:
        return None
    linkedin_url = getattr(profile, "linkedin_url", None)
    if not linkedin_url:
        return None

    first = getattr(profile, "first_name", "") or ""
    last = getattr(profile, "last_name", "") or ""
    if not first or not last:
        # Fall back to name-parsing the headline (rare)
        return None

    employment = list(getattr(profile, "employment_periods", []) or [])
    current_emp = next(
        (e for e in employment if e.get("is_current")), employment[0] if employment else None
    )
    current_title = (current_emp or {}).get("title")
    current_company = (current_emp or {}).get("company_name")
    canon_company = normalize_company(current_company)

    return CanonicalPerson(
        canonical_name=f"{first} {last}",
        first_name=first,
        last_name=last,
        name_variants=[f"{first} {last}"],
        linkedin_url=linkedin_url,
        linkedin_id=getattr(profile, "linkedin_id", None),
        email=getattr(profile, "email", None),
        current_title=current_title,
        current_company_name=canon_company,
        current_seniority_score=seniority_from_title(current_title),
        current_functional_domain=domain_from_title(current_title),
        location_text=getattr(profile, "location_text", None),
        country_code=getattr(profile, "country_code", None),
        headline=getattr(profile, "headline", None),
        connections_count=getattr(profile, "connections_count", None),
        followers_count=getattr(profile, "followers_count", None),
        premium=getattr(profile, "premium", False),
        verified=getattr(profile, "verified", False),
        open_to_work=getattr(profile, "open_to_work", False),
        hiring=getattr(profile, "hiring", False),
        registered_at=getattr(profile, "registered_at", None),
        employment_periods=employment,
        education_periods=list(getattr(profile, "education_periods", []) or []),
        skills=list(getattr(profile, "skills", []) or []),
        certifications=list(getattr(profile, "certifications", []) or []),
        languages=list(getattr(profile, "languages", []) or []),
        publications=list(getattr(profile, "publications", []) or []),
        patents=list(getattr(profile, "patents", []) or []),
        honors_and_awards=list(getattr(profile, "honors_and_awards", []) or []),
        organizations=list(getattr(profile, "organizations", []) or []),
        sources={
            "canonical_name": "apify",
            "linkedin_url": "apify",
            "current_title": "apify",
            "current_company_name": "apify",
            "employment_periods": "apify",
            "education_periods": "apify",
            "skills": "apify",
        },
    )


def from_apollo(record: dict[str, Any]) -> CanonicalPerson | None:
    """Map Apollo's flat dict shape → CanonicalPerson."""
    if not isinstance(record, dict):
        return None
    name = record.get("name") or record.get("full_name")
    if not isinstance(name, str):
        return None
    first, last = normalize_name(name)
    if not first or not last:
        return None
    canon_company = normalize_company(record.get("organization_name") or record.get("company"))
    title = record.get("title") or record.get("current_title")
    return CanonicalPerson(
        canonical_name=f"{first} {last}",
        first_name=first,
        last_name=last,
        name_variants=[name],
        linkedin_url=record.get("linkedin_url"),
        email=record.get("email"),
        email_status=record.get("email_status"),
        current_title=title,
        current_company_name=canon_company,
        current_seniority_score=seniority_from_title(title),
        current_functional_domain=domain_from_title(title),
        sources={
            "canonical_name": "apollo",
            "email": "apollo",
            "current_title": "apollo",
        },
    )


# ─── Field-level merger ─────────────────────────────────────────────────────


def _adopt_if_higher_priority(
    target: CanonicalPerson, field_name: str, value: Any, source: str
) -> None:
    """Set ``target.<field>`` to ``value`` only when:
      - the new value is non-empty, AND
      - the existing value is empty, OR the new source outranks the existing.
    """
    if value is None or value == "" or value == [] or value == {}:
        return
    current = getattr(target, field_name, None)
    if current is None or current == "" or current == [] or current == {}:
        setattr(target, field_name, value)
        target.sources[field_name] = source
        return
    existing_source = target.sources.get(field_name, "manual")
    if _src_rank(source) > _src_rank(existing_source):
        setattr(target, field_name, value)
        target.sources[field_name] = source


def _merge_into(target: CanonicalPerson, incoming: CanonicalPerson, source: str) -> None:
    """Field-by-field merge of incoming into target with source priority."""
    # Identity fields
    for f in (
        "canonical_name", "first_name", "last_name",
        "linkedin_url", "linkedin_id", "email", "email_status",
        "pdl_person_id", "uspto_inventor_id", "orcid",
        "current_title", "current_company_name",
        "current_seniority_score", "current_functional_domain",
        "location_text", "country_code",
    ):
        _adopt_if_higher_priority(target, f, getattr(incoming, f, None), source)

    # Name variants — always accumulate (dedup case-insensitive)
    seen = {n.lower() for n in target.name_variants}
    for nv in incoming.name_variants:
        if nv and nv.lower() not in seen:
            target.name_variants.append(nv)
            seen.add(nv.lower())

    # Lists — replace only when the incoming source outranks the existing,
    # OR when the existing is empty. Don't merge entries because LinkedIn
    # employment_periods are authoritative and shouldn't be diluted by
    # less-precise sources.
    for f in (
        "employment_periods", "education_periods", "skills", "certifications",
        "publications", "patents", "honors_and_awards", "organizations",
    ):
        _adopt_if_higher_priority(target, f, list(getattr(incoming, f, []) or []), source)


# ─── Top-level merger ──────────────────────────────────────────────────────


def merge_records(
    records_by_source: dict[str, list[Any]],
) -> list[CanonicalPerson]:
    """Merge records from {apify, apollo, pdl} into one list of canonical persons.

    Order matters: process highest-priority sources first so their values
    "claim" the canonical identity, then lower-priority sources fill in
    only the gaps.
    """
    builders = {
        "apify": from_apify,
        "apollo": from_apollo,
    }

    # Iterate sources in priority order — Apify first, then Apollo, then PDL/manual
    canonical: list[CanonicalPerson] = []
    for source in ("apify", "apollo", "pdl", "manual"):
        builder = builders.get(source)
        if builder is None:
            continue
        for raw in records_by_source.get(source, []):
            new = builder(raw)
            if new is None:
                continue
            # Try to find an existing match
            match = next((c for c in canonical if same_person(c, new)), None)
            if match is None:
                canonical.append(new)
            else:
                _merge_into(match, new, source)

    return canonical


__all__ = [
    "CanonicalPerson",
    "SourceName",
    "normalize_name",
    "normalize_company",
    "same_person",
    "from_apify",
    "from_apollo",
    "merge_records",
]
