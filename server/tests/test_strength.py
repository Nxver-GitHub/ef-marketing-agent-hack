"""Tests for credence.strength — TypeScript parity must be exact."""
from __future__ import annotations

import math

import pytest

from credence.strength import (
    ALL_CONNECTION_TYPES,
    DECAY_RATES,
    STRENGTH_CAP,
    STRENGTH_TABLE,
    ComputeStrengthInput,
    compute_strength,
    compute_strength_for_type,
)

# ── Tables ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_strength_decay_keys_match() -> None:
    assert sorted(STRENGTH_TABLE.keys()) == sorted(DECAY_RATES.keys())


@pytest.mark.unit
def test_all_connection_types_enumerates_keys() -> None:
    assert sorted(ALL_CONNECTION_TYPES) == sorted(STRENGTH_TABLE.keys())


@pytest.mark.unit
def test_strength_table_matches_claude_md() -> None:
    assert STRENGTH_TABLE["patent_co_inventor"] == 0.95
    assert STRENGTH_TABLE["same_phd_advisor"] == 0.92
    assert STRENGTH_TABLE["career_overlap_same_team"] == 0.88
    assert STRENGTH_TABLE["alumni_network"] == 0.25
    assert STRENGTH_TABLE["conference_co_attendee"] == 0.20


@pytest.mark.unit
def test_decay_rates_match_claude_md() -> None:
    assert DECAY_RATES["patent_co_inventor"] == 0.01
    assert DECAY_RATES["career_overlap_same_team"] == 0.04
    assert DECAY_RATES["alumni_network"] == 0.08
    assert DECAY_RATES["conference_co_attendee"] == 0.20


@pytest.mark.unit
def test_strength_table_matches_v3_pt2_education_cohorts() -> None:
    """V3_PT2.md L391-422 cohort kinds — must match strength.ts parity."""
    assert STRENGTH_TABLE["same_mba_cohort"] == 0.85
    assert STRENGTH_TABLE["same_phd_program"] == 0.78
    assert STRENGTH_TABLE["executive_education"] == 0.70
    assert STRENGTH_TABLE["same_undergrad_cohort"] == 0.62


@pytest.mark.unit
def test_decay_rates_match_v3_pt2_education_cohorts() -> None:
    assert DECAY_RATES["same_mba_cohort"] == 0.02
    assert DECAY_RATES["same_phd_program"] == 0.02
    assert DECAY_RATES["executive_education"] == 0.03
    assert DECAY_RATES["same_undergrad_cohort"] == 0.04


@pytest.mark.unit
def test_tables_are_immutable() -> None:
    with pytest.raises(TypeError):
        STRENGTH_TABLE["patent_co_inventor"] = 1.0  # type: ignore[index]
    with pytest.raises(TypeError):
        DECAY_RATES["patent_co_inventor"] = 1.0  # type: ignore[index]


# ── compute_strength ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_claude_md_worked_example_caps_at_0_99() -> None:
    """0.95 * exp(-0.01*7) * (1+ln2*0.15) * (1+2*0.10) ≈ 1.173 → cap 0.99."""
    result = compute_strength(
        ComputeStrengthInput(
            base=0.95,
            decay_rate=0.01,
            years_since_active=7,
            corroboration_count=2,
            source_type_count=2,
        )
    )
    assert result == STRENGTH_CAP


@pytest.mark.unit
def test_zero_years_one_corrob_one_source_yields_base_times_1_10() -> None:
    """recency=1, frequency=1, corroboration=1.10 → base * 1.10."""
    result = compute_strength(
        ComputeStrengthInput(
            base=0.5, decay_rate=0.05, years_since_active=0
        )
    )
    assert math.isclose(result, 0.55, rel_tol=1e-12)


@pytest.mark.unit
def test_decays_exponentially_with_years() -> None:
    """0.8 * exp(-0.05*20) * 1.10 = 0.8 * exp(-1) * 1.10 ≈ 0.32373."""
    result = compute_strength(
        ComputeStrengthInput(base=0.8, decay_rate=0.05, years_since_active=20)
    )
    expected = 0.8 * math.exp(-1) * 1.10
    assert math.isclose(result, expected, rel_tol=1e-12)


@pytest.mark.unit
def test_caps_at_strength_cap() -> None:
    result = compute_strength(
        ComputeStrengthInput(
            base=0.99,
            decay_rate=0,
            years_since_active=0,
            corroboration_count=100,
            source_type_count=5,
        )
    )
    assert result == STRENGTH_CAP


@pytest.mark.unit
def test_deterministic() -> None:
    args = ComputeStrengthInput(
        base=0.7,
        decay_rate=0.04,
        years_since_active=3,
        corroboration_count=4,
        source_type_count=2,
    )
    assert compute_strength(args) == compute_strength(args)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kwargs", "expected_msg"),
    [
        ({"base": -0.1, "decay_rate": 0.01, "years_since_active": 1}, "base"),
        ({"base": 1.5, "decay_rate": 0.01, "years_since_active": 1}, "base"),
        ({"base": 0.5, "decay_rate": -0.01, "years_since_active": 1}, "decay_rate"),
        ({"base": 0.5, "decay_rate": 0.01, "years_since_active": -1}, "years_since_active"),
        (
            {
                "base": 0.5,
                "decay_rate": 0.01,
                "years_since_active": 1,
                "corroboration_count": 0,
            },
            "corroboration_count",
        ),
        (
            {
                "base": 0.5,
                "decay_rate": 0.01,
                "years_since_active": 1,
                "source_type_count": 0,
            },
            "source_type_count",
        ),
    ],
)
def test_invalid_input_raises(kwargs: dict, expected_msg: str) -> None:
    with pytest.raises(ValueError, match=expected_msg):
        compute_strength(ComputeStrengthInput(**kwargs))


# ── compute_strength_for_type ────────────────────────────────────────────────


@pytest.mark.unit
def test_for_type_matches_direct_computation() -> None:
    direct = compute_strength(
        ComputeStrengthInput(
            base=STRENGTH_TABLE["patent_co_inventor"],
            decay_rate=DECAY_RATES["patent_co_inventor"],
            years_since_active=5,
        )
    )
    via_type = compute_strength_for_type("patent_co_inventor", 5)
    assert direct == via_type


@pytest.mark.unit
def test_for_type_orders_correctly() -> None:
    assert compute_strength_for_type("patent_co_inventor", 0) > compute_strength_for_type(
        "alumni_network", 0
    )


@pytest.mark.unit
def test_conference_co_attendee_decays_sharply() -> None:
    fresh = compute_strength_for_type("conference_co_attendee", 0)
    aged = compute_strength_for_type("conference_co_attendee", 10)
    # decay 0.20: ratio = exp(-2) ≈ 0.1353
    assert math.isclose(aged / fresh, math.exp(-2), rel_tol=1e-12)


@pytest.mark.unit
def test_for_type_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown connection_type"):
        compute_strength_for_type("not_a_real_type", 0)
