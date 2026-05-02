"""Functional Domain + Seniority + IC-Track taxonomy (v3.1 Plan A).

Single source of truth for the 9 functional-domain keys, the seniority-score
ladder, and the IC-track regex. Per CLAUDE.md "Functional Domain Taxonomy"
(L297-313), "Seniority Taxonomy" (L335-348), and V3_PT2.md L52-56.

Imported by `orgchart/clustering.py` (cluster keys), `orgchart/hierarchy.py`
(seniority gap math + manager-title detection), and the future scoring
module that feeds Authority sub-score.

## Why a single module

Hardcoding the domain strings or seniority numbers in multiple places means
they drift. The CHECK constraint on `org_functional_clusters.functional_domain`
enforces the keyspace at the DB layer; this module enforces it at the Python
layer so callers can map free-text titles → canonical keys without
duplicating the alias lists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Iterable

# ─── Functional domain keyspace ──────────────────────────────────────────────


# Keyspace must match the CHECK constraint in
# `20260501_v3_orgchart_schema.sql` exactly. Adding a key here without
# updating the migration will surface as an INSERT failure at runtime.
FUNCTIONAL_DOMAINS: Final[tuple[str, ...]] = (
    "hardware_engineering",
    "software_engineering",
    "product_management",
    "manufacturing_ops",
    "sales_marketing",
    "research",
    "finance_legal",
    "people_ops",
    "general_management",
)


# Title-fragment → domain mapping. Patterns are case-insensitive and matched
# in declaration order (first hit wins). Keep specific patterns above generic
# ones — e.g., "engineering manager" before "manager", "VP engineering"
# before "VP". The patterns are anchored on word boundaries via `\b` to
# avoid matching inside other words.
_DOMAIN_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = tuple(
    (re.compile(pat, re.IGNORECASE), domain)
    for pat, domain in [
        # research — placed first because "principal research" should not
        # collapse to hardware/software despite the principal-engineer prefix
        (r"\bresearch\b|\bscientist\b|\badvanced development\b|\bpathfinding\b", "research"),
        # ── C-suite full-form titles ─────────────────────────────────────────
        # "Chief X Officer" — listed before bare acronyms and before generic
        # buckets so "Chief Marketing Officer" doesn't accidentally get caught
        # by the generic "marketing" pattern below (which would still bucket
        # it correctly here, but explicit > implicit). Each maps the C-suite
        # form to its functional domain when it can be inferred from the
        # role wording. Ambiguous forms (Chief Product Officer = product OR
        # people, depending on org) are left out so they fall through.
        (r"\bchief technology officer\b|\bchief information officer\b|"
         r"\bchief information security officer\b|\bchief security officer\b",
         "software_engineering"),
        (r"\bchief financial officer\b", "finance_legal"),
        (r"\bchief marketing officer\b|\bchief revenue officer\b|"
         r"\bchief sales officer\b|\bchief commercial officer\b|"
         r"\bchief growth officer\b",
         "sales_marketing"),
        (r"\bchief people officer\b|\bchief human resources officer\b|"
         r"\bchief talent officer\b|\bchief diversity officer\b",
         "people_ops"),
        (r"\bchief product officer\b", "product_management"),
        (r"\bchief operating officer\b|\bchief executive officer\b|"
         r"\bchief of staff\b",
         "general_management"),
        # general management — match GM/President/business-unit-owner titles
        # plus bare CEO/COO acronyms (no specific functional domain implied).
        # "Co-founder" / "Founder" land here too because a founder without
        # a specialty title runs the company.
        (r"\bgeneral manager\b|\bp&l\b|\bbusiness unit\b|\bpresident\b|"
         r"\bGM,\b|\bCEO\b|\bCOO\b|\bco[- ]?founder\b|\bfounder\b",
         "general_management"),
        # Bare-acronym C-suite titles — these are very common in early-stage
        # companies where everyone titles themselves with the 3-letter form.
        # CTO → software (the most common modern interpretation; chip-design
        # CTOs exist but are far rarer than software CTOs in our prospect set).
        (r"\bCTO\b|\bCIO\b|\bCISO\b|\bCSO\b", "software_engineering"),
        (r"\bCMO\b|\bCRO\b|\bCCO\b", "sales_marketing"),
        # Note: CPO is intentionally omitted — ambiguous between Chief People
        # Officer (people_ops) and Chief Product Officer (product_management).
        # Operators should set canonical_domain when they know which one.
        # hardware engineering — chip design, verification, physical, analog
        (
            r"\b(?:chip|silicon|RTL|verification|physical design|analog|"
            r"mixed[- ]signal|memory design|SoC|microarchitecture|fabric|"
            r"hardware engineer|hardware design|ASIC|FPGA)\b",
            "hardware_engineering",
        ),
        # manufacturing / ops — fab, foundry, yield, process, supply chain,
        # quality, reliability + foreign-language and aerospace-niche
        # manufacturing terms surfaced by the zero-cluster audit:
        #   - "chef d'équipe" (FR, "team leader")
        #   - "operaio specializzato" (IT, "skilled worker")
        #   - "ingeniero/a en manufactura" (ES, "manufacturing engineer")
        #   - "ingénieur de production" (FR, "production engineer")
        #   - "inventory control", "team lead/leader" (EN, common at Tier-1
        #     aerospace plants where titles aren't standardized)
        (
            r"\b(?:manufacturing|operations|supply chain|yield|process engineer"
            r"|fab|foundry|quality|reliability"
            r"|inventory control|team lead(?:er)?"
            r"|chef d['’]?équipe"
            r"|operaio"
            r"|ingenier[oa] en manufactura"
            r"|ingénieur (?:de )?production"
            r"|production engineer"
            r"|maintenance planner)\b",
            "manufacturing_ops",
        ),
        # software / firmware / drivers / SDK
        # "Member of Technical Staff" / MTS is a common IC-track title at
        # AI labs (Cerebras, OpenAI, Anthropic) and semiconductor companies
        # (Intel, AMD). It's ambiguous between hardware and software in
        # principle, but in practice the modern AI-lab MTS is overwhelmingly
        # software/ML — and even chip-design MTS would still benefit from
        # being clustered (the seniority taxonomy already maps MTS=40 IC).
        (
            r"\b(?:software engineer|firmware|embedded|SDK|driver|BSP|"
            r"developer|programmer|backend|frontend|fullstack|full stack|"
            r"AI compiler|ML compiler|compiler engineer|machine learning engineer|"
            r"infrastructure engineer|platform engineer"
            r"|member of (?:the )?technical staff|MTS)\b",
            "software_engineering",
        ),
        # product management — PM, program, TPM, roadmap
        (
            r"\b(?:product manager|product management|TPM|technical program manager"
            r"|program manager|roadmap|principal pm|product lead)\b",
            "product_management",
        ),
        # sales / marketing / BD
        (
            r"\b(?:sales|marketing|business development|BD lead|GTM|"
            r"go-to-market|account manager|partnerships|partner manager)\b",
            "sales_marketing",
        ),
        # finance / legal
        (r"\b(?:finance|legal|compliance|accounting|tax|controller|CFO|general counsel)\b", "finance_legal"),
        # people ops / HR
        (r"\b(?:HR|human resources|recruiting|people operations|talent|culture lead)\b", "people_ops"),
        # generic engineering / architecture — fall through to hardware if
        # no other domain hit (semis-heavy dataset bias). Keep last among
        # the engineering-leaning patterns.
        (r"\b(?:engineer|architect|engineering)\b", "hardware_engineering"),
    ]
)


def domain_from_title(title: str | None) -> str | None:
    """Map a free-text job title to a canonical functional_domain key.

    Returns None when no pattern matches — callers should fall through to
    `employment_periods.functional_domain` (the canonical column) before
    invoking this NLP heuristic.
    """
    if not title:
        return None
    for pattern, domain in _DOMAIN_PATTERNS:
        if pattern.search(title):
            return domain
    return None


# ─── Seniority taxonomy ──────────────────────────────────────────────────────


# Title-fragment → seniority score. Patterns are case-insensitive, matched in
# order, longer-more-specific phrases first so "Senior Director" doesn't
# collapse to "Director". Numbers come from CLAUDE.md L335-348 verbatim.
_SENIORITY_PATTERNS: Final[tuple[tuple[re.Pattern[str], int], ...]] = tuple(
    (re.compile(pat, re.IGNORECASE), score)
    for pat, score in [
        # Board-level (chair = 95, parallel to president).
        (r"\bchair(?:man|woman|person)?\b(?:[\s,].*\bof the board\b)?", 95),
        # C-suite (88-100)
        (r"\bCEO\b|\bchief executive officer\b", 100),
        (r"\bpresident\b", 95),
        # Bare-acronym CXOs (90). The previous list omitted CMO/CIO/CISO/CSO/CCO
        # which surfaced in the backfill audit as unclassified — see
        # backfill rollup 2026-05-01: 9,182 persons.current_title were
        # untouched, dominated by bare-acronym CXOs and "Senior X" specialists.
        (r"\bCOO\b|\bCTO\b|\bCFO\b|\bCPO\b|\bCRO\b|"
         r"\bCMO\b|\bCIO\b|\bCISO\b|\bCSO\b|\bCCO\b", 90),
        (r"\bchief\s+\w+\s+officer\b", 88),
        # EVP / SVP
        (r"\bEVP\b|\bexecutive vice president\b", 82),
        (r"\bSVP\b|\bsenior vice president\b", 80),
        # VP
        (r"\bgroup VP\b|\bgroup vice president\b", 72),
        (r"\bVP\b|\bvice president\b", 70),
        # "Head of X" — modern director/VP-equivalent. Common at scale-ups
        # where titles skip the formal VP ladder. Placed before specific
        # director patterns so "Head of Director-of-X" (rare) still matches
        # at this tier rather than collapsing to plain "director".
        (r"\bhead of\b", 65),
        # Director ladder
        (r"\bprincipal director\b", 63),
        (r"\bsenior director\b", 62),
        (r"\bdirector\b", 60),
        # Distinguished Engineer parallels VP-tier (55, IC track)
        (r"\bdistinguished engineer\b", 55),
        # Manager ladder
        (r"\bgroup manager\b|\bsenior manager\b", 52),
        (r"\bengineering manager\b|\bmanager\b", 50),
        # Principal Engineer / Staff Engineer (IC)
        (r"\bprincipal engineer\b", 48),
        # Generic "Principal X" — Principal Architect / Principal Scientist /
        # Principal Systems Lead / etc. Placed AFTER principal director +
        # principal engineer so those specifics still win.
        (r"\bprincipal\b", 48),
        (r"\bstaff engineer\b", 45),
        # IC fallback — specific senior engineer phrasings first, then
        # the generic "senior X" catch-all (covers "Senior Cybersecurity
        # Specialist", "Senior Architect", etc. — surfaced in the same
        # backfill audit). Placed AFTER all senior-{director,manager,VP,
        # engineer} specifics so those keep their higher scores.
        (r"\bsenior engineer\b|\bsenior software engineer\b|\bsenior hardware engineer\b", 40),
        (r"\bsenior\b", 40),
        # Member of Technical Staff — common at semi/research orgs (ASML,
        # Intel, etc.) and uniformly maps to senior IC level.
        (r"\bmember of technical staff\b|\bMTS\b", 40),
        (r"\bengineer\b|\bdeveloper\b|\bdesigner\b", 35),
    ]
)


def seniority_from_title(title: str | None) -> int | None:
    """Map a free-text title → integer seniority score (0-100), or None.

    None means the title didn't match any known pattern; callers should
    prefer `employment_periods.seniority_score` (canonical) and fall through
    to this heuristic only when that column is NULL.
    """
    if not title:
        return None
    for pattern, score in _SENIORITY_PATTERNS:
        if pattern.search(title):
            return score
    return None


def seniority_tier(score: int) -> str:
    """Bucket a seniority score into the span-of-control tier key.

    Mirrors V3_PT2.md L132-139 — the SPAN_LIMITS dict is keyed off these
    tier strings.
    """
    if score >= 85:
        return "c_suite"
    if score >= 75:
        return "svp"
    if score >= 65:
        return "vp"
    if score >= 55:
        return "director"
    return "manager"


# ─── IC track detection ──────────────────────────────────────────────────────


# CLAUDE.md Decision 2: IC track parallels management track at the same
# seniority. A Distinguished Engineer (55) doesn't report to a Director (60)
# just because of the gap — they're peers. Hierarchy code MUST consult this
# regex and never assign IC-track persons as managers of non-IC personnel.
IC_TRACK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"distinguished engineer|principal engineer|staff engineer"
    r"|chief architect|principal architect"
    r"|principal scientist|distinguished scientist|fellow"
    r"|distinguished researcher"
    r")\b",
    re.IGNORECASE,
)


def is_ic_track(title: str | None) -> bool:
    """True iff the title indicates an individual-contributor parallel track.

    The IC ladder (Distinguished Engineer / Principal Engineer / Staff
    Engineer / Fellow / Architect / Principal Scientist) runs alongside the
    management ladder. Hierarchy inference must never cross the boundary at
    the same or lower seniority level.
    """
    if not title:
        return False
    return bool(IC_TRACK_PATTERN.search(title))


# ─── Manager-title detection ─────────────────────────────────────────────────


# Used by hierarchy.py implicit-scoring's "manager_title signal" component.
# A title containing one of these tokens contributes +0.10 to the candidate
# manager's edge score (V3_PT2.md L112).
MANAGER_TITLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(manager|director|VP|head of|lead)\b",
    re.IGNORECASE,
)


def is_manager_title(title: str | None) -> bool:
    """True iff the title's surface form suggests a management role."""
    if not title:
        return False
    return bool(MANAGER_TITLE_PATTERN.search(title))


# ─── Convenience dataclass ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TitleClassification:
    """Bundled classification of a single title.

    Useful for hierarchy.py and clustering.py callers that need all four
    properties at once. Pure-functional — call `classify_title(...)` to
    construct.
    """

    domain: str | None
    seniority: int | None
    is_ic: bool
    is_manager: bool


def classify_title(title: str | None) -> TitleClassification:
    """One-shot classification of a free-text title.

    None inputs return a frozen all-None / False classification — caller
    decides whether that's OK or worth a fallback (canonical column).
    """
    return TitleClassification(
        domain=domain_from_title(title),
        seniority=seniority_from_title(title),
        is_ic=is_ic_track(title),
        is_manager=is_manager_title(title),
    )


__all__ = [
    "FUNCTIONAL_DOMAINS",
    "IC_TRACK_PATTERN",
    "MANAGER_TITLE_PATTERN",
    "TitleClassification",
    "classify_title",
    "domain_from_title",
    "is_ic_track",
    "is_manager_title",
    "seniority_from_title",
    "seniority_tier",
]
