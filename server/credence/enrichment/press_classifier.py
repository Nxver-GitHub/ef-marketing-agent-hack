"""Classify a press_release signal payload into one of six display categories.

The frontend renders each press release with a category tag. Rather than
re-derive the tag at render time, we precompute it once and store it inside
the existing `company_signals.structured_value` JSONB blob (no schema change).

Classification is deliberately rule-based: simple substring/regex matches
against the headline are dramatically cheaper than an LLM call, fully
deterministic, and easy to extend. Edge cases that don't match any pattern
fall through to `general`.

Priority order (most specific → least specific):

    earnings → partnership → product_launch → research → co_mention_signal → general

Earnings is checked first because phrases like "launches Q1 results" would
otherwise mis-route to product_launch. Partnership beats product_launch
because joint-launch headlines ("Acme partners with Globex to launch X") are
more useful as partnership signals. `co_mention_signal` is a structural rule
(≥2 named executives) rather than a headline rule, so it runs near the end
where it only fires when nothing else matched.
"""
from __future__ import annotations

import re
from typing import Any, Final

# ── Public category constants ───────────────────────────────────────────────

CATEGORY_EARNINGS: Final[str] = "earnings"
CATEGORY_PRODUCT_LAUNCH: Final[str] = "product_launch"
CATEGORY_PARTNERSHIP: Final[str] = "partnership"
CATEGORY_RESEARCH: Final[str] = "research"
CATEGORY_CO_MENTION: Final[str] = "co_mention_signal"
CATEGORY_GENERAL: Final[str] = "general"

ALL_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        CATEGORY_EARNINGS,
        CATEGORY_PRODUCT_LAUNCH,
        CATEGORY_PARTNERSHIP,
        CATEGORY_RESEARCH,
        CATEGORY_CO_MENTION,
        CATEGORY_GENERAL,
    }
)


# ── Compiled patterns (one regex per category, lowercased input) ────────────

# `\b` word boundaries keep "earnings" from matching "yearningstone" and
# similar substrings. Patterns operate on lowercased text — no IGNORECASE
# needed, which is marginally faster and keeps matching logic explicit.

_EARNINGS_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"earnings"
    r"|q[1-4]\s*(fy)?\s*(20)?\d{2}"          # Q1 FY2026 / Q3 2025 / Q4FY26
    r"|(first|second|third|fourth)\s+quarter"
    r"|fiscal\s+(year|quarter)"
    r"|financial\s+results"
    r"|quarterly\s+results"
    r"|annual\s+results"
    r"|reports\s+(first|second|third|fourth|q[1-4]|fy|fiscal|full[- ]year|quarterly|annual)"
    r"|fy\s*(20)?\d{2}\s+results"
    r"|full[- ]year\s+results"
    r")\b"
)

_PARTNERSHIP_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"partner(s|ed|ing)?\s+with"
    r"|partnership"
    r"|collaborat(e|es|ed|ing|ion)"
    r"|joint\s+venture"
    r"|alliance"
    r"|teams?\s+up\s+with"
    r"|join\s+forces"
    r"|strategic\s+(deal|agreement|relationship)"
    r"|expand(s|ed|ing)?\s+(its\s+)?(strategic\s+)?partnership"
    r")\b"
)

_PRODUCT_LAUNCH_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"unveil(s|ed|ing)?"
    r"|launch(es|ed|ing)?"
    r"|introduc(e|es|ed|ing)"
    r"|announc(e|es|ed|ing)\s+(the\s+)?(availability|general\s+availability|release)"
    r"|now\s+shipping"
    r"|general\s+availability"
    r"|releas(e|es|ed|ing)"
    r"|debut(s|ed|ing)?"
    r"|rolls?\s+out"
    r")\b"
)

_RESEARCH_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"research"
    r"|publish(es|ed|ing)?"
    r"|white\s*paper"
    r"|benchmark(s|ed|ing)?"
    r"|mlperf"
    r"|academic"
    r"|paper\s+accepted"
    r"|stud(y|ies)"
    r"|peer[- ]reviewed"
    r"|conference\s+paper"
    r")\b"
)


# Ordered (priority, category, pattern) tuples — order is load-bearing.
_ORDERED_HEADLINE_RULES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (CATEGORY_EARNINGS, _EARNINGS_RE),
    (CATEGORY_PARTNERSHIP, _PARTNERSHIP_RE),
    (CATEGORY_PRODUCT_LAUNCH, _PRODUCT_LAUNCH_RE),
    (CATEGORY_RESEARCH, _RESEARCH_RE),
)

# A "named executive" needs at least a non-empty name string. The signal
# extractor stores them as dicts like `{"name": "...", "title": "..."}` but
# older rows may be plain strings — we accept either shape.
_CO_MENTION_THRESHOLD: Final[int] = 2


def _count_named_executives(payload: dict[str, Any]) -> int:
    """Return the number of non-empty named executives in the payload.

    Tolerates both dict-shaped (`{"name": "X"}`) and plain-string entries.
    Anything missing/empty is skipped, so a list with two entries where one
    has a blank name only counts as one.
    """
    raw = payload.get("mentioned_executives") or []
    if not isinstance(raw, list):
        return 0
    count = 0
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            count += 1
        elif isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                count += 1
    return count


def classify_press_release(payload: dict[str, Any] | None) -> str:
    """Return one of {earnings, product_launch, partnership, research, co_mention_signal, general}.

    Args:
        payload: The `structured_value` dict from a `company_signals` row.
            Expected keys (all optional): `headline`, `mentioned_executives`.
            Missing or non-dict input → `general`.

    Returns:
        One of the six category constants. Always a non-empty string.
    """
    if not payload or not isinstance(payload, dict):
        return CATEGORY_GENERAL

    headline_raw = payload.get("headline")
    headline = headline_raw.lower() if isinstance(headline_raw, str) else ""

    if headline:
        for category, pattern in _ORDERED_HEADLINE_RULES:
            if pattern.search(headline):
                return category

    if _count_named_executives(payload) >= _CO_MENTION_THRESHOLD:
        return CATEGORY_CO_MENTION

    return CATEGORY_GENERAL


__all__ = [
    "ALL_CATEGORIES",
    "CATEGORY_CO_MENTION",
    "CATEGORY_EARNINGS",
    "CATEGORY_GENERAL",
    "CATEGORY_PARTNERSHIP",
    "CATEGORY_PRODUCT_LAUNCH",
    "CATEGORY_RESEARCH",
    "classify_press_release",
]
