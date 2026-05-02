"""Module-level contract invariants for the org-chart suite.

These tests guard contracts that, if broken, will cause downstream readers
(DB CHECK constraints, optimizer, UI) to misbehave or fail at runtime.
Each test pins a small, load-bearing fact that production code depends on.
"""
from __future__ import annotations

import pytest

from credence.orgchart import hierarchy, scope
from credence import taxonomy


@pytest.mark.unit
def test_span_limits_keyspace_matches_seniority_tier_keyspace():
    """Guards: SPAN_LIMITS must contain a key for every value seniority_tier
    can return; otherwise the planner KeyErrors on lookup at score time."""
    sample_scores = [0, 30, 50, 60, 70, 80, 95]
    produced_tiers = {taxonomy.seniority_tier(s) for s in sample_scores}
    span_keys = set(hierarchy.SPAN_LIMITS.keys())
    missing = produced_tiers - span_keys
    assert not missing, (
        f"seniority_tier produced tiers absent from SPAN_LIMITS: {missing}. "
        f"SPAN_LIMITS keys: {span_keys}"
    )


@pytest.mark.unit
def test_implicit_score_cap_below_explicit_floor():
    """Guards: implicit edges must always lose to explicit edges in
    confidence-sort. CLAUDE.md Decision 3."""
    # IMPLICIT_SCORE_CAP is documented as 0.95; typical explicit confidence
    # is ~0.85-0.95. The crucial invariant is that the cap stays strictly
    # below 1.0 so explicit signals retain priority semantics.
    assert hierarchy.IMPLICIT_SCORE_CAP == 0.95, (
        f"IMPLICIT_SCORE_CAP changed to {hierarchy.IMPLICIT_SCORE_CAP}; "
        "review explicit-vs-implicit priority semantics in Decision 3."
    )
    assert hierarchy.IMPLICIT_SCORE_CAP < 1.0, (
        "IMPLICIT_SCORE_CAP must stay < 1.0 so explicit edges (which can "
        "carry confidence up to 1.0) outrank implicit edges in sort order."
    )


@pytest.mark.unit
def test_functional_domain_keyspace_matches_migration():
    """Guards: the 9-key functional domain keyspace must match the
    org_functional_clusters CHECK constraint exactly."""
    domains = taxonomy.FUNCTIONAL_DOMAINS
    assert len(domains) == 9, f"Expected 9 domains, got {len(domains)}: {domains}"
    assert len(set(domains)) == 9, f"Duplicate domain key: {domains}"
    for d in domains:
        assert d == d.lower(), f"Domain {d!r} must be lowercase"
        assert " " not in d, f"Domain {d!r} must be snake_case (no spaces)"
        assert d.replace("_", "").isalpha(), (
            f"Domain {d!r} must be snake_case alpha only"
        )
    expected = {
        "hardware_engineering",
        "software_engineering",
        "product_management",
        "manufacturing_ops",
        "sales_marketing",
        "research",
        "finance_legal",
        "people_ops",
        "general_management",
    }
    assert set(domains) == expected, (
        f"Domain keyspace drift: got {set(domains)}, expected {expected}"
    )


@pytest.mark.unit
def test_seniority_taxonomy_monotonic():
    """Guards: rule reorder in _SENIORITY_PATTERNS that breaks the natural
    ladder (e.g. accidentally placing a Director rule above Senior Director)."""
    titles = [
        "Engineer",
        "Senior Engineer",
        "Staff Engineer",
        "Manager",
        "Senior Manager",
        "Director",
        "Senior Director",
        "VP",
        "SVP",
        "EVP",
        "President",
        "CEO",
    ]
    scores = [taxonomy.seniority_from_title(t) for t in titles]
    for t, s in zip(titles, scores):
        assert s is not None, f"seniority_from_title({t!r}) returned None"
    for i in range(1, len(titles)):
        assert scores[i] >= scores[i - 1], (
            f"Non-monotonic seniority at {titles[i - 1]}={scores[i - 1]} "
            f"-> {titles[i]}={scores[i]}; full ladder: {list(zip(titles, scores))}"
        )


@pytest.mark.unit
def test_ic_track_titles_classify_correctly():
    """Guards: IC-track regex drift breaking the Decision 2 parallel-ladder
    invariant in hierarchy._ic_track_compatible."""
    ic_titles = [
        "Distinguished Engineer",
        "Principal Engineer",
        "Staff Engineer",
        "Chief Architect",
        "Principal Architect",
        "Principal Scientist",
        "Distinguished Scientist",
        "Fellow",
    ]
    non_ic_titles = [
        "VP Engineering",
        "Director",
        "Senior Manager",
        "CEO",
        "Sales Manager",
        "Engineer",
    ]
    for t in ic_titles:
        assert taxonomy.is_ic_track(t), f"is_ic_track({t!r}) should be True"
    for t in non_ic_titles:
        assert not taxonomy.is_ic_track(t), f"is_ic_track({t!r}) should be False"


@pytest.mark.unit
def test_budget_authority_keyspace():
    """Guards: scope._TIER_TO_BUDGET values must satisfy the
    person_scope_estimates.budget_authority_level CHECK constraint."""
    allowed = {"individual", "team", "department", "division", "company"}
    for tier, level in scope._TIER_TO_BUDGET.items():
        assert level in allowed, (
            f"_TIER_TO_BUDGET[{tier!r}] = {level!r} not in CHECK keyspace {allowed}"
        )


@pytest.mark.unit
def test_score_components_have_seven_keys():
    """Guards: COMPONENT_KEYS must match the migration's CHECK keyspace for
    org_reporting_edges.dominant_signal (excluding the 'unknown' sentinel
    used for explicit edges)."""
    expected = {
        "seniority_gap",
        "domain_match",
        "subdomain_match",
        "manager_title",
        "span_capacity",
        "patent_cluster",
        "geographic_scope",
    }
    assert len(hierarchy.COMPONENT_KEYS) == 7, (
        f"COMPONENT_KEYS must have 7 entries; got {len(hierarchy.COMPONENT_KEYS)}: "
        f"{hierarchy.COMPONENT_KEYS}"
    )
    assert set(hierarchy.COMPONENT_KEYS) == expected, (
        f"COMPONENT_KEYS drift: got {set(hierarchy.COMPONENT_KEYS)}, "
        f"expected {expected}"
    )
    assert "unknown" not in hierarchy.COMPONENT_KEYS, (
        "'unknown' is a sentinel for explicit edges only — must NOT be a "
        "real implicit-component key in COMPONENT_KEYS."
    )
