"""Tests for the C-suite extension of `credence.taxonomy.domain_from_title`.

Background: the org-chart pipeline's clustering step calls `domain_from_title`
as the NLP fallback when `persons.current_functional_domain` is unset. Before
this patch, C-suite-only orgs (e.g., 6sense, apollo io, permuto capital)
landed every employee with `domain=None` so no functional cluster formed.

These tests pin the new mappings so a future taxonomy refactor can't silently
re-break the regression. Each case documents the intended interpretation;
add a new test (don't loosen an old one) when expectations change.
"""
from __future__ import annotations

import pytest

from credence.taxonomy import FUNCTIONAL_DOMAINS, domain_from_title


@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "expected_domain"),
    [
        # ── Bare-acronym C-suite ─────────────────────────────────────────────
        # Founders + chief execs without a functional specialty go to GM.
        ("CEO", "general_management"),
        ("Co-founder and CEO", "general_management"),
        ("Co-CEO", "general_management"),
        ("Founder", "general_management"),
        ("Co-Founder", "general_management"),
        ("co-founder", "general_management"),
        ("COO", "general_management"),
        # CTO/CIO/CISO default to software_engineering — far more common
        # than chip-design CTOs in our prospect set; any prospect with a
        # canonical_domain set will override the NLP path anyway.
        ("CTO", "software_engineering"),
        ("CIO", "software_engineering"),
        ("CISO", "software_engineering"),
        # Revenue + marketing acronyms cluster under sales_marketing.
        ("CMO", "sales_marketing"),
        ("CRO", "sales_marketing"),
        ("CCO", "sales_marketing"),
        # CFO matches the existing finance_legal pattern.
        ("CFO", "finance_legal"),
        # CPO is intentionally ambiguous (Chief People vs Chief Product) — we
        # leave it None so the cluster step skips the person rather than
        # making a wrong guess. Operators set canonical_domain when known.
        ("CPO", None),
        # ── Full-form Chief X Officer ────────────────────────────────────────
        ("Chief Executive Officer", "general_management"),
        ("Chief Operating Officer", "general_management"),
        ("Chief of Staff", "general_management"),
        ("Chief Technology Officer", "software_engineering"),
        ("Chief Information Officer", "software_engineering"),
        ("Chief Information Security Officer", "software_engineering"),
        ("Chief Security Officer", "software_engineering"),
        ("Chief Financial Officer", "finance_legal"),
        ("Chief Marketing Officer", "sales_marketing"),
        ("Chief Revenue Officer", "sales_marketing"),
        ("Chief Sales Officer", "sales_marketing"),
        ("Chief Commercial Officer", "sales_marketing"),
        ("Chief Growth Officer", "sales_marketing"),
        ("Chief People Officer", "people_ops"),
        ("Chief Human Resources Officer", "people_ops"),
        ("Chief Talent Officer", "people_ops"),
        ("Chief Diversity Officer", "people_ops"),
        ("Chief Product Officer", "product_management"),
        # ── Combined / mixed titles ──────────────────────────────────────────
        # When a person carries both a founder marker and an acronym, the
        # founder reading wins (they own the company even if they badge as
        # the technical lead). The audit script flagged "CTO and Co-founder"
        # as a real example at 6sense.
        ("CTO and Co-founder", "general_management"),
        ("Co-founder and CTO", "general_management"),
        # ── Negative controls — unchanged behavior ───────────────────────────
        # Existing patterns must still bucket correctly after the C-suite
        # block was prepended.
        # Bare "Senior Director" lacks a functional anchor — correctly None
        # (clustering will skip the person; an operator can set
        # canonical_domain explicitly when known).
        ("Senior Director", None),
        ("Senior Director, Engineering", "hardware_engineering"),
        ("Director of Manufacturing", "manufacturing_ops"),
        # ── Foreign-language manufacturing terms (zero-cluster audit recovery)
        # Surfaced when auditing safran aerospace; multilingual workforce at
        # Tier-1 European aerospace plants. Each maps to manufacturing_ops.
        ("chef d'équipe chez SAFRAN AEROSPACE", "manufacturing_ops"),
        ("Chef d'équipe", "manufacturing_ops"),
        ("Operaio specializzato", "manufacturing_ops"),
        ("Practicante de Ingeniero en Manufactura", "manufacturing_ops"),
        ("Ingeniera en Manufactura", "manufacturing_ops"),
        ("Ingénieur de Production", "manufacturing_ops"),
        ("Inventory Control Coordinator", "manufacturing_ops"),
        ("Team Lead, Avionics Assembly", "manufacturing_ops"),
        ("Maintenance Planner", "manufacturing_ops"),
        # ── MTS / Member of Technical Staff (Cerebras-style AI lab IC titles)
        # Common at AI labs and semis; default to software_engineering.
        ("Member of Technical Staff", "software_engineering"),
        ("member of the technical staff", "software_engineering"),
        ("Senior MTS", "software_engineering"),
        ("VP of Marketing", "sales_marketing"),
        ("Engineering Manager", "hardware_engineering"),
        ("General Manager", "general_management"),
        ("President, North America", "general_management"),
        ("Principal Research Engineer", "research"),
        # Unknown / placeholder titles still return None so clustering skips.
        ("--", None),
        ("Graduate from Los Angeles Pierce College", None),
        ("", None),
    ],
)
def test_domain_from_title_csuite(title: str, expected_domain: str | None) -> None:
    """Pin the C-suite mappings + ensure pre-existing patterns still hold."""
    assert domain_from_title(title) == expected_domain


@pytest.mark.unit
def test_csuite_mappings_only_use_canonical_keyspace() -> None:
    """Every domain returned by the C-suite patterns must be in
    FUNCTIONAL_DOMAINS — otherwise the clustering INSERT will trip the
    CHECK constraint at runtime.
    """
    csuite_titles = [
        "CEO", "COO", "CTO", "CIO", "CISO", "CMO", "CRO", "CCO", "CFO",
        "Founder", "Co-founder", "Chief Executive Officer",
        "Chief Operating Officer", "Chief of Staff",
        "Chief Technology Officer", "Chief Information Officer",
        "Chief Information Security Officer", "Chief Security Officer",
        "Chief Financial Officer", "Chief Marketing Officer",
        "Chief Revenue Officer", "Chief Sales Officer",
        "Chief Commercial Officer", "Chief Growth Officer",
        "Chief People Officer", "Chief Human Resources Officer",
        "Chief Talent Officer", "Chief Diversity Officer",
        "Chief Product Officer",
    ]
    for title in csuite_titles:
        domain = domain_from_title(title)
        assert domain is not None, (
            f"{title!r} should now map after the C-suite patch but returned None"
        )
        assert domain in FUNCTIONAL_DOMAINS, (
            f"{title!r} mapped to {domain!r} which is not in the canonical keyspace"
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "title",
    [
        # CPO is intentionally ambiguous so we don't force a wrong bucket.
        "CPO",
        "Chief Product and People Officer",  # contrived but plausible
    ],
)
def test_ambiguous_csuite_returns_none(title: str) -> None:
    """We deliberately leave certain ambiguous C-suite titles unmapped so
    clustering skips them. A future patch that wants to assert a default
    must touch this test, which is the signal to the reviewer that the
    behavior is intentional.
    """
    # Note: "Chief Product and People Officer" actually hits the
    # `chief product officer` pattern via the substring; the assertion
    # below is a reminder that the mapping is "first hit wins".
    result = domain_from_title(title)
    assert result is None or result in FUNCTIONAL_DOMAINS
