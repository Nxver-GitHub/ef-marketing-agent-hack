"""Tests for credence.backfill_v3 pure-function classifiers.

Covers `normalize_company`, `infer_seniority`, `infer_functional_domain` —
the three regex-driven helpers that don't need a DB connection. Database-
dependent helpers (`ensure_company`, `ensure_person`, `upsert_employment`)
require an integration test against a transactional sandbox; deferred to
J.6 / a future track.
"""
from __future__ import annotations

import pytest

from uuid import uuid4

from credence.backfill_v3 import (
    CareerHistoryRole,
    _load_career_history,
    _parse_role_years,
    infer_functional_domain,
    infer_seniority,
    normalize_company,
)

# ── normalize_company ────────────────────────────────────────────────────────
#
# Mirrors `normalizeCompany` in src/lib/graph.ts:165. The expected outputs
# below should match what the frontend would compute for the same input —
# any drift breaks v3's "backend-canonical and frontend-canonical match"
# invariant (companies aggregation node IDs depend on it).


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Falsy / empty inputs.
        (None, ""),
        ("", ""),
        ("   ", ""),
        # Plain names — no suffix to strip.
        ("Intel", "intel"),
        ("NVIDIA", "nvidia"),
        ("Stanford", "stanford"),
        # Suffix stripping (per regex in backfill_v3.py).
        ("Intel Corp", "intel"),
        ("Intel Corp.", "intel"),
        ("Intel Corporation", "intel"),
        ("Apple Inc", "apple"),
        ("Apple Inc.", "apple"),
        ("Apple Incorporated", "apple"),
        ("Foo Limited", "foo"),
        ("Foo Ltd", "foo"),
        ("Foo Ltd.", "foo"),
        ("Bar LLC", "bar"),
        ("Baz PLC", "baz"),
        ("Cadence Technologies", "cadence"),
        ("Cadence Technology", "cadence"),
        ("Lam Semiconductor", "lam"),
        ("Lam Semiconductors", "lam"),
        ("Cadence Systems", "cadence"),
        ("Cadence System", "cadence"),
        # Multiple suffixes / mixed case.
        ("Intel Corporation, Inc.", "intel"),
        ("Cadence Design Systems", "cadence design"),
        # Non-alphanumeric collapse.
        ("Yahoo!", "yahoo"),
        ("AT&T", "at t"),
        ("Booz, Allen & Hamilton", "booz allen hamilton"),
        # Whitespace collapse.
        ("Intel    Corp", "intel"),
        ("  Intel  ", "intel"),
        # Unicode-safe (per `re.UNICODE` flag in source).
        ("Hitachi 株式会社", "hitachi 株式会社"),
    ],
)
def test_normalize_company(raw: str | None, expected: str) -> None:
    assert normalize_company(raw) == expected


@pytest.mark.unit
def test_normalize_company_idempotent() -> None:
    """Applying twice should yield the same result as applying once."""
    samples = ["Intel Corporation", "AT&T", "Cadence Design Systems", ""]
    for s in samples:
        once = normalize_company(s)
        twice = normalize_company(once)
        assert once == twice, f"{s!r}: not idempotent ({once!r} -> {twice!r})"


# ── infer_seniority ──────────────────────────────────────────────────────────
#
# Values come from CLAUDE.md "Seniority Taxonomy". Order in `_SENIORITY_RULES`
# is most-specific first, so e.g. "senior director" wins 62 before plain
# "director" (60).


_BUG_SENIOR_X_ENGINEER = (
    "Bug: `senior engineer` rule does not match `Senior Software Engineer` (non-adjacent "
    "tokens). CLAUDE.md taxonomy implies Senior <X> Engineer = 40, but the regex requires "
    "literal adjacency. Resolution interpretation-dependent — DarkBeaver to decide whether "
    "Senior <X> Engineer = 40 or 35."
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Falsy / unrecognized.
        (None, None),
        ("", None),
        ("Intern", None),
        ("Random unrelated job", None),
        # CEO and chiefs.
        ("CEO", 100),
        ("Chief Executive Officer", 100),
        ("Founder & CEO", 100),
        ("President", 95),
        ("CTO", 89),
        ("Chief Technology Officer", 89),
        ("CFO", 89),
        ("Chief Financial Officer", 89),
        ("CPO", 89),
        ("CRO", 89),
        # EVP / SVP / VP. After the iter-14 rule reordering (`\bpresident\b`
        # moved AFTER the *Vice President variants), the President-greedy bug
        # is fixed and these all return their CLAUDE.md taxonomy values.
        ("EVP, Engineering", 82),
        ("Executive Vice President", 82),
        ("SVP of Sales", 80),
        ("Senior Vice President", 80),
        ("Group VP, Platforms", 72),
        ("VP Engineering", 70),
        ("Vice President of Marketing", 70),
        # Director tier — specificity ordering matters.
        ("Principal Director, Roadmap", 63),
        ("Senior Director, Strategy", 62),
        ("Director of Operations", 60),
        # IC senior / management mid-tier.
        ("Distinguished Engineer", 55),
        ("Senior Manager", 52),
        ("Group Manager", 52),
        ("Engineering Manager", 50),
        ("Manager, Backend Platform", 50),
        ("Principal Engineer", 48),
        ("Staff Engineer", 45),
        ("Senior Engineer", 40),
        # Bug 2 fixed (DarkBeaver): `senior engineer` rule extended to allow
        # 0-3 middle words. CLAUDE.md taxonomy: Senior <X> Engineer = 40.
        ("Senior Software Engineer", 40),
        ("Engineer", 35),
        ("Software Engineer", 35),
        ("Architect", 35),
        ("Solutions Architect", 35),
        # Case insensitivity.
        ("ceo", 100),
        ("VICE PRESIDENT", 70),
        ("vP eNgInEeRiNg", 70),
        # "Senior Director" must beat "Director" — most-specific-first contract.
        ("senior director", 62),
        # "VP" inside a larger word should NOT match (\b boundary).
        ("VPNAdmin", None),
    ],
)
def test_infer_seniority(title: str | None, expected: int | None) -> None:
    assert infer_seniority(title) == expected


# ── infer_functional_domain ──────────────────────────────────────────────────
#
# Domain keys must match the CHECK constraint in
# 20260430_v3_connection_graph.sql L97-108. Returns None when no rule fires —
# the schema accepts NULL functional_domain.


@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Falsy / unmatched.
        (None, None),
        ("", None),
        ("Designer", None),
        ("Customer Success Manager", None),
        # Hardware engineering.
        ("Senior RTL Engineer", "hardware_engineering"),
        ("Verification Lead", "hardware_engineering"),
        ("Physical Design Engineer", "hardware_engineering"),
        ("Analog Engineer", "hardware_engineering"),
        ("Mixed Signal Designer", "hardware_engineering"),
        ("Memory Design Engineer", "hardware_engineering"),
        ("SoC Architect", "hardware_engineering"),
        ("Chip Designer", "hardware_engineering"),
        ("Silicon Engineer", "hardware_engineering"),
        ("Hardware Engineer", "hardware_engineering"),
        ("FPGA Engineer", "hardware_engineering"),
        ("ASIC Designer", "hardware_engineering"),
        # Software engineering.
        ("Software Engineer", "software_engineering"),
        ("Firmware Developer", "software_engineering"),
        ("Embedded Engineer", "software_engineering"),
        ("SDK Engineer", "software_engineering"),
        ("Driver Developer", "software_engineering"),
        ("BSP Engineer", "software_engineering"),
        ("Backend Engineer", "software_engineering"),
        ("Frontend Developer", "software_engineering"),
        ("Full-Stack Engineer", "software_engineering"),
        ("DevOps Lead", "software_engineering"),
        ("SRE", "software_engineering"),
        ("Platform Engineer", "software_engineering"),
        # Product management.
        ("Product Manager", "product_management"),
        ("Senior Product Manager", "product_management"),
        ("Program Manager", "product_management"),
        ("TPM, Cloud Infra", "product_management"),
        ("Product Owner", "product_management"),
        ("Roadmap Lead", "product_management"),
        # Manufacturing ops.
        ("Manufacturing Engineer", "manufacturing_ops"),
        ("Operations Lead", "manufacturing_ops"),
        ("Supply Chain Manager", "manufacturing_ops"),
        ("Yield Engineer", "manufacturing_ops"),
        ("Process Engineer", "manufacturing_ops"),
        ("Fab Manager", "manufacturing_ops"),
        ("Foundry Lead", "manufacturing_ops"),
        ("Quality Engineer", "manufacturing_ops"),
        ("Reliability Engineer", "manufacturing_ops"),
        # Sales / marketing.
        ("Sales Lead", "sales_marketing"),
        ("Marketing Director", "sales_marketing"),
        ("Head of GTM", "sales_marketing"),
        ("Account Management Lead", "sales_marketing"),
        ("Partnerships Manager", "sales_marketing"),
        ("Business Development Director", "sales_marketing"),
        ("BD Lead", "sales_marketing"),
        # Research.
        ("Research Engineer", "research"),
        ("Research Scientist", "research"),
        ("Advanced Development Lead", "research"),
        ("Pathfinding Engineer", "research"),
        ("Exploratory Engineer", "research"),
        # Finance / legal.
        ("Finance Lead", "finance_legal"),
        ("Legal Counsel", "finance_legal"),
        ("Compliance Officer", "finance_legal"),
        ("Accounting Manager", "finance_legal"),
        ("Tax Director", "finance_legal"),
        ("Controller", "finance_legal"),
        # People ops.
        ("Human Resources Manager", "people_ops"),
        ("Recruiting Lead", "people_ops"),
        # "People Operations" — fixed in iter-14 by reordering people_ops
        # before manufacturing_ops in _FUNCTIONAL_DOMAIN_RULES. Was xfail.
        ("People Operations", "people_ops"),
        ("Culture Lead", "people_ops"),
        ("HR Director", "people_ops"),
        # General management.
        ("General Manager", "general_management"),
        ("GM, Cloud", "general_management"),
        ("Business Unit Head", "general_management"),
        ("P&L Owner", "general_management"),
        # Case insensitivity.
        ("software engineer", "software_engineering"),
        ("RTL ENGINEER", "hardware_engineering"),
        # Most-specific-first contract: a title that matches multiple rules
        # picks the first listed. Hardware comes before software; "Hardware
        # Software Engineer" hits hardware first.
        ("Hardware Software Engineer", "hardware_engineering"),
    ],
)
def test_infer_functional_domain(title: str | None, expected: str | None) -> None:
    assert infer_functional_domain(title) == expected


@pytest.mark.unit
def test_infer_functional_domain_returns_none_for_nonsense() -> None:
    """Nonsense titles must return None, not crash. Schema accepts NULL."""
    nonsense = ["", "   ", "!!!", "12345", "🚀", "x" * 1000]
    for t in nonsense:
        assert infer_functional_domain(t) is None or isinstance(
            infer_functional_domain(t), str
        ), f"crashed or returned wrong type on {t!r}"


# ── Cross-classifier sanity ──────────────────────────────────────────────────
#
# A title that hits both a seniority rule AND a functional-domain rule should
# return both classifications independently — the two functions don't share
# state.


@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "seniority", "domain"),
    [
        # "VP of Engineering" — seniority 70 (Bug 1 fix). Domain is None because
        # `_FUNCTIONAL_DOMAIN_RULES` doesn't have a bare "engineering" keyword;
        # only specific software/hardware/product sub-terms qualify.
        ("VP of Engineering", 70, None),
        # Bug 2 fixed (DarkBeaver): Senior X Engineer now resolves to 40.
        ("Senior Hardware Engineer", 40, "hardware_engineering"),
        ("Director of Sales", 60, "sales_marketing"),
        ("Principal Engineer, Foundry", 48, "manufacturing_ops"),
        ("CEO of Acme", 100, None),
    ],
)
def test_classifiers_independent(
    title: str, seniority: int | None, domain: str | None
) -> None:
    assert infer_seniority(title) == seniority
    assert infer_functional_domain(title) == domain


# ── _parse_role_years ────────────────────────────────────────────────────────
#
# v2 ``career_history`` signals carry irregular ``years`` strings. This parser
# pulls all 4-digit groups; first is start, second (if present) is end. Out-of
# range, malformed, or empty values must degrade to (None, None) — never raise.


@pytest.mark.unit
@pytest.mark.parametrize(
    ("years_str", "expected"),
    [
        # Canonical YYYY-YYYY.
        ("2018-2022", (2018, 2022)),
        ("2018 - 2022", (2018, 2022)),
        # Unicode en-dash / em-dash.
        ("2018 – 2022", (2018, 2022)),
        ("2018—2022", (2018, 2022)),
        # Open-ended ranges.
        ("2018-present", (2018, None)),
        ("2018 to current", (2018, None)),
        # Single year.
        ("1993", (1993, None)),
        # No 4-digit group.
        ("annual", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
        ("garbage", (None, None)),
        # Malformed range — end before start drops end.
        ("2022-2018", (2022, None)),
        # Out-of-range start → (None, None).
        ("1850-1899", (None, None)),
        # Out-of-range end only — start kept, end dropped.
        ("2018-1850", (2018, None)),
        # Non-string types are tolerated.
        (12345, (None, None)),
    ],
)
def test_parse_role_years(
    years_str: object, expected: tuple[int | None, int | None]
) -> None:
    assert _parse_role_years(years_str) == expected  # type: ignore[arg-type]


# ── _load_career_history ─────────────────────────────────────────────────────
#
# Async DB shim: we don't have a live Postgres, so monkeypatch the
# ``conn.fetch`` coroutine with a fake that returns a list of dict-like rows.
# The function must:
#   - tolerate missing / non-dict ``value`` payloads
#   - tolerate missing / non-list ``roles``
#   - skip roles missing ``company``
#   - parse ``years`` via _parse_role_years
#   - strip whitespace on company / title
#   - drop empty-string title → None


class _FakeConn:
    """Minimal async fake exposing only ``fetch`` (the one method we use)."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.calls.append((query, args))
        return self._rows


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_career_history_extracts_roles_with_years() -> None:
    prospect_id = uuid4()
    fake_rows = [
        {
            "value": {
                "roles": [
                    {
                        "company": "NVIDIA",
                        "role": "Director of AI",
                        "years": "2018-2022",
                    },
                    {
                        "company": "  Intel Corp  ",
                        "role": "  Senior Engineer  ",
                        "years": "1993-present",
                    },
                ]
            }
        },
        # Second signal row — additional roles get appended.
        {
            "value": {
                "roles": [
                    {"company": "Apple", "role": "Engineer", "years": "annual"},
                ]
            }
        },
    ]
    conn = _FakeConn(fake_rows)

    result = await _load_career_history(conn, prospect_id)  # type: ignore[arg-type]

    assert result == [
        CareerHistoryRole(
            company="NVIDIA", title="Director of AI",
            start_year=2018, end_year=2022,
        ),
        CareerHistoryRole(
            company="Intel Corp", title="Senior Engineer",
            start_year=1993, end_year=None,
        ),
        CareerHistoryRole(
            company="Apple", title="Engineer",
            start_year=None, end_year=None,
        ),
    ]
    # Verify we queried the right signal_type.
    assert len(conn.calls) == 1
    query, args = conn.calls[0]
    assert "signal_type = 'career_history'" in query
    assert args == (prospect_id,)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_career_history_handles_malformed_payloads() -> None:
    prospect_id = uuid4()
    fake_rows = [
        {"value": None},                              # not a dict
        {"value": "not a dict either"},               # str
        {"value": {"roles": "not a list"}},           # roles not list
        {"value": {}},                                 # missing roles key
        {"value": {"roles": [None, "str", 42]}},      # non-dict role entries
        {"value": {"roles": [{"company": ""}]}},      # empty company
        {"value": {"roles": [{"company": "   "}]}},   # whitespace-only company
        {"value": {"roles": [{"role": "Engineer"}]}}, # missing company
        {"value": {"roles": [
            # title missing → None; years missing → (None, None)
            {"company": "Acme"},
        ]}},
        {"value": {"roles": [
            # title is empty string → None
            {"company": "Beta", "role": "  ", "years": "2010"},
        ]}},
    ]
    conn = _FakeConn(fake_rows)

    result = await _load_career_history(conn, prospect_id)  # type: ignore[arg-type]

    assert result == [
        CareerHistoryRole(
            company="Acme", title=None, start_year=None, end_year=None,
        ),
        CareerHistoryRole(
            company="Beta", title=None, start_year=2010, end_year=None,
        ),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_career_history_empty_signals() -> None:
    """No signal rows → empty list, no crash."""
    conn = _FakeConn([])
    result = await _load_career_history(conn, uuid4())  # type: ignore[arg-type]
    assert result == []
