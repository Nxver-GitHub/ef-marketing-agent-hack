"""Tests for `credence.orgchart.active_sampling` — Phase D.3 backend.

Pure-logic coverage on the ranking math + dataclass shape. The DB-touching
`select_uncertain_edges` is exercised by the integration suite under
`tests/orgchart/integration/`; here we keep the loop tight and unit-only.
"""
from __future__ import annotations

from math import isclose, log1p
from uuid import UUID

import pytest

from credence.orgchart.active_sampling import (
    DEFAULT_CONFIDENCE_CEILING,
    MAX_LIMIT,
    UncertainEdge,
    _uncertainty_score,
)


# ── _uncertainty_score ───────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("confidence", "manager_span", "expected"),
    [
        # Zero span: pure (1 - confidence) baseline. We want a non-zero
        # score so single-report managers still get ranked.
        (0.50, 0, 0.50),
        (0.95, 0, 0.05),
        (0.10, 0, 0.90),
        # Same confidence, varying span: span boost is monotonic.
        # log1p(0)=0 so factor=1; log1p(1)≈0.693 so factor≈1.693; etc.
        (0.50, 1, 0.50 * (1 + log1p(1))),
        (0.50, 8, 0.50 * (1 + log1p(8))),
        (0.50, 100, 0.50 * (1 + log1p(100))),
    ],
)
def test_uncertainty_score_math(
    confidence: float, manager_span: int, expected: float,
) -> None:
    """Pin the formula. Any change to the ranking should be intentional —
    if this test fails, the new ranking changes which edges go to the UI.
    """
    assert isclose(
        _uncertainty_score(confidence, manager_span), expected, rel_tol=1e-9
    )


@pytest.mark.unit
def test_uncertainty_score_negative_span_clamped() -> None:
    """`manager_span` should never be negative, but the function handles
    it defensively — clamps at 0 so log1p doesn't error.
    """
    s_neg = _uncertainty_score(0.5, -5)
    s_zero = _uncertainty_score(0.5, 0)
    assert s_neg == s_zero


@pytest.mark.unit
def test_uncertainty_score_higher_for_lower_confidence() -> None:
    """At fixed span, lower confidence = higher uncertainty."""
    span = 8
    high_conf = _uncertainty_score(0.90, span)
    low_conf = _uncertainty_score(0.50, span)
    assert low_conf > high_conf


@pytest.mark.unit
def test_uncertainty_score_higher_for_higher_span() -> None:
    """At fixed confidence, higher span = higher uncertainty (more blast
    radius). Span boost is sub-linear so the rate of increase damps with
    larger spans — verified separately below.
    """
    conf = 0.50
    small = _uncertainty_score(conf, 2)
    medium = _uncertainty_score(conf, 8)
    large = _uncertainty_score(conf, 50)
    assert small < medium < large


@pytest.mark.unit
def test_uncertainty_score_sublinear_in_span() -> None:
    """The span factor is `1 + log1p(span)` — sub-linear. A 50-report
    manager shouldn't get 25× the score of a 2-report manager.
    """
    conf = 0.50
    small = _uncertainty_score(conf, 2)
    huge = _uncertainty_score(conf, 50)
    # At true linear we'd expect huge/small ≈ 25; we expect well under that.
    ratio = huge / small
    assert ratio < 5.0, (
        f"span factor grew too fast: small={small:.3f}, huge={huge:.3f}, ratio={ratio:.2f}"
    )


# ── UncertainEdge dataclass shape ────────────────────────────────────────────


@pytest.mark.unit
def test_uncertain_edge_is_frozen_and_immutable() -> None:
    """The dataclass is frozen so callers can't mutate the result post-fetch.
    Unit-test the contract so a refactor that drops `frozen=True` regresses.
    """
    edge = UncertainEdge(
        edge_id=UUID(int=1),
        account_id=UUID(int=2),
        manager_id=UUID(int=3),
        manager_name="Alice",
        manager_title="EVP",
        manager_company_id=UUID(int=4),
        report_id=UUID(int=5),
        report_name="Bob",
        report_title="VP",
        confidence=0.5,
        path_confidence=0.4,
        inference_method="implicit_scoring",
        dominant_signal="domain_match",
        score_components={"domain_match": 0.25},
        manager_span=8,
        uncertainty_score=1.5,
    )
    with pytest.raises(Exception):
        edge.confidence = 0.99  # type: ignore[misc]


@pytest.mark.unit
def test_uncertain_edge_constructs_with_optional_nones() -> None:
    """Every Optional field can be None — important because the SQL LEFT
    JOINs persons (manager/report rows can be missing for unresolved-target
    placeholders) and explicit edges leave score_components None.
    """
    UncertainEdge(
        edge_id=UUID(int=1),
        account_id=UUID(int=2),
        manager_id=UUID(int=3),
        manager_name=None,
        manager_title=None,
        manager_company_id=None,
        report_id=UUID(int=5),
        report_name=None,
        report_title=None,
        confidence=0.5,
        path_confidence=None,
        inference_method="implicit_scoring",
        dominant_signal=None,
        score_components=None,
        manager_span=0,
        uncertainty_score=0.5,
    )


# ── Public constants ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_default_confidence_ceiling_is_in_uncertain_band() -> None:
    """Implicit edges floor-clamp at 0.50 and cap at 0.95. The default
    ceiling has to be inside that range for the candidate set to be
    non-trivial; 0.55 is just-above-floor and the right starting bucket.
    """
    assert 0.50 <= DEFAULT_CONFIDENCE_CEILING <= 0.95


@pytest.mark.unit
def test_max_limit_is_sane() -> None:
    """MAX_LIMIT bounds payload size for the UI. Stay between a reasonable
    page (5) and a reasonable cap (1000) — anything outside means somebody
    misread the contract.
    """
    assert 5 <= MAX_LIMIT <= 1000
