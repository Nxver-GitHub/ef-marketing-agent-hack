"""Connection strength model — Python sibling of `src/lib/strength.ts`.

Pure functions and constants. No I/O, no DB. Mirrors the tables in CLAUDE.md
(`STRENGTH_TABLE`, `DECAY_RATES`) and the strength formula documented there.

Track J deliverable. Stays byte-for-byte aligned with the TypeScript module:
keys, values, formula, and 0.99 cap are identical. If one side changes, both
sides must change in lock-step (CONTRACTS.md cross-cutting source-of-truth rule).

Formula (CLAUDE.md, "Connection Graph"):

    computed_strength = min(0.99,
        base
      * exp(-decay_rate * years_since_active)
      * (1 + log(corroboration_count) * 0.15)
      * (1 + source_type_count * 0.10)
    )
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

# ── ConnectionType union ─────────────────────────────────────────────────────

ConnectionType = Literal[
    "patent_co_inventor",
    "same_phd_advisor",
    "co_board_member",
    "academic_co_author_multi",
    "academic_co_author_single",
    "career_overlap_same_team",
    "standards_committee_peer",
    "conference_co_presenter",
    "co_investor",
    "career_overlap_same_domain",
    "career_overlap_general",
    "alumni_network",
    "conference_co_attendee",
    # Education-cohort kinds (V3_PT2.md §"New Edge Kinds" L376-381). Mirror
    # of the TypeScript `ConnectionType` union in `src/lib/strength.ts`.
    # The B3 education extractor emits these signal_types (signals.py L106).
    "same_mba_cohort",
    "same_phd_program",
    "executive_education",
    "same_undergrad_cohort",
]

# ── STRENGTH_TABLE — verbatim from CLAUDE.md ─────────────────────────────────

STRENGTH_TABLE: Final[Mapping[str, float]] = MappingProxyType(
    {
        "patent_co_inventor": 0.95,
        "same_phd_advisor": 0.92,
        "co_board_member": 0.90,
        "academic_co_author_multi": 0.90,
        "academic_co_author_single": 0.85,
        "career_overlap_same_team": 0.88,
        "standards_committee_peer": 0.82,
        "conference_co_presenter": 0.80,
        "co_investor": 0.78,
        "career_overlap_same_domain": 0.72,
        "career_overlap_general": 0.60,
        "alumni_network": 0.25,
        "conference_co_attendee": 0.20,
        # Education-cohort kinds — V3_PT2.md L391-422.
        "same_mba_cohort": 0.85,
        "same_phd_program": 0.78,
        "executive_education": 0.70,
        "same_undergrad_cohort": 0.62,
    }
)

# ── DECAY_RATES ──────────────────────────────────────────────────────────────
#
# Two derived entries (matching strength.ts behavior):
#
# - `academic_co_author_multi/single`: CLAUDE.md uses single key
#   `academic_co_author` (0.02). We apply that to both variants — base strength
#   differs by paper count, decay semantics don't.
# - `career_overlap_same_domain`: CLAUDE.md omits this from DECAY_RATES.
#   Interpolated to 0.05 between same_team (0.04) and general (0.06).

DECAY_RATES: Final[Mapping[str, float]] = MappingProxyType(
    {
        "patent_co_inventor": 0.01,
        "same_phd_advisor": 0.01,
        "co_board_member": 0.02,
        "academic_co_author_multi": 0.02,
        "academic_co_author_single": 0.02,
        "career_overlap_same_team": 0.04,
        "standards_committee_peer": 0.03,
        "conference_co_presenter": 0.05,
        "co_investor": 0.04,
        "career_overlap_same_domain": 0.05,
        "career_overlap_general": 0.06,
        "alumni_network": 0.08,
        "conference_co_attendee": 0.20,
        # Education-cohort kinds — V3_PT2.md L391-422.
        "same_mba_cohort": 0.02,
        "same_phd_program": 0.02,
        "executive_education": 0.03,
        "same_undergrad_cohort": 0.04,
    }
)

ALL_CONNECTION_TYPES: Final[tuple[str, ...]] = tuple(STRENGTH_TABLE.keys())

STRENGTH_CAP: Final[float] = 0.99
_FREQUENCY_COEFF: Final[float] = 0.15
_CORROBORATION_COEFF: Final[float] = 0.10


@dataclass(frozen=True, slots=True)
class ComputeStrengthInput:
    """Inputs for `compute_strength`. Frozen to prevent mid-call mutation."""

    base: float
    decay_rate: float
    years_since_active: float
    corroboration_count: int = 1
    source_type_count: int = 1


def compute_strength(args: ComputeStrengthInput) -> float:
    """Return connection strength in [0, 0.99].

    Raises:
        ValueError: if any input is outside its valid range.
    """
    base = args.base
    if not math.isfinite(base) or base < 0.0 or base > 1.0:
        raise ValueError(f"base must be in [0, 1], got {base!r}")

    decay = args.decay_rate
    if not math.isfinite(decay) or decay < 0.0:
        raise ValueError(f"decay_rate must be >= 0, got {decay!r}")

    years = args.years_since_active
    if not math.isfinite(years) or years < 0.0:
        raise ValueError(f"years_since_active must be >= 0, got {years!r}")

    cc = args.corroboration_count
    if not isinstance(cc, int) or isinstance(cc, bool) or cc < 1:
        raise ValueError(f"corroboration_count must be int >= 1, got {cc!r}")

    sc = args.source_type_count
    if not isinstance(sc, int) or isinstance(sc, bool) or sc < 1:
        raise ValueError(f"source_type_count must be int >= 1, got {sc!r}")

    recency = math.exp(-decay * years)
    frequency = 1.0 + math.log(cc) * _FREQUENCY_COEFF
    corroboration = 1.0 + sc * _CORROBORATION_COEFF
    return min(STRENGTH_CAP, base * recency * frequency * corroboration)


def compute_strength_for_type(
    connection_type: str,
    years_since_active: float,
    corroboration_count: int = 1,
    source_type_count: int = 1,
) -> float:
    """Convenience wrapper that uses canonical STRENGTH_TABLE / DECAY_RATES lookups."""
    if connection_type not in STRENGTH_TABLE:
        raise ValueError(f"unknown connection_type: {connection_type!r}")
    return compute_strength(
        ComputeStrengthInput(
            base=STRENGTH_TABLE[connection_type],
            decay_rate=DECAY_RATES[connection_type],
            years_since_active=years_since_active,
            corroboration_count=corroboration_count,
            source_type_count=source_type_count,
        )
    )
