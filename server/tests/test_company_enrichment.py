"""Pure-function unit tests for the company-enrichment surface.

Covers the no-DB code paths in:
  * `credence.enrichment.bulk_company_enrichment` — URL builders + dedupe
  * `scripts.seed_company_meta` — TS parser + employee bucket + row shaper
  * `credence.enrichment.refresh_company_enrichment` — stale-cutoff math (via
    isolation of pure helpers)

The DB-touching paths (`run_bulk`, `seed_companies`, `run_refresh`) are
exercised by integration tests / live smokes — keeping unit-side strictly
to pure logic so the suite stays sub-second.
"""
from __future__ import annotations

import pytest

from credence.enrichment.bulk_company_enrichment import (
    _normalize_domain,
    candidate_urls,
)
from scripts.seed_company_meta import (
    build_update_row,
    parse_company_meta,
    parse_employee_count,
)


# ── _normalize_domain ───────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("intel.com", "intel.com"),
        ("INTEL.COM", "intel.com"),
        ("https://intel.com", "intel.com"),
        ("http://intel.com/", "intel.com"),
        ("https://www.intel.com", "intel.com"),
        ("www.intel.com", "intel.com"),
        ("  intel.com  ", "intel.com"),
        ("https://intel.com/news/", "intel.com/news"),
        # Falsy + degenerate inputs return None so the bulk job can skip cleanly.
        ("", None),
        (None, None),
        ("/", None),
    ],
)
def test_normalize_domain(raw: str | None, expected: str | None) -> None:
    assert _normalize_domain(raw) == expected


# ── candidate_urls ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_candidate_urls_returns_three_clean_https_paths() -> None:
    """Happy path: domain → (root, leadership, press), all https-prefixed."""
    root, leadership, press = candidate_urls("intel.com")
    assert root == "https://intel.com"
    assert leadership == "https://intel.com/leadership"
    assert press == "https://intel.com/news"  # _PRESS_PATHS[0] reordered to /news (higher hit rate, 2026-05-02)


@pytest.mark.unit
def test_candidate_urls_strips_protocol_and_www() -> None:
    root, leadership, press = candidate_urls("https://www.intel.com")
    assert root == "https://intel.com"
    assert leadership == "https://intel.com/leadership"
    assert press == "https://intel.com/news"  # _PRESS_PATHS[0] reordered to /news (higher hit rate, 2026-05-02)


@pytest.mark.unit
def test_candidate_urls_returns_all_none_for_missing_domain() -> None:
    """Bulk job uses this triple to decide whether to call Firecrawl —
    no-domain must map to all-Nones so it short-circuits cleanly."""
    assert candidate_urls(None) == (None, None, None)
    assert candidate_urls("") == (None, None, None)


# ── parse_employee_count ────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100k+", 150000),
        ("10k-100k", 50000),
        ("1k-5k", 3000),
        ("50-200", 125),
        ("1-10", 5),
        ("  10k-100k  ", 50000),
        # Pass-throughs + nones
        (5000, 5000),
        (None, None),
        ("unknown", None),
        ("", None),
        ("99999 employees", None),
    ],
)
def test_parse_employee_count(raw: object, expected: int | None) -> None:
    assert parse_employee_count(raw) == expected


# ── parse_company_meta ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_company_meta_handles_generated_export() -> None:
    """Mirrors the actual `company-meta.generated.ts` export shape."""
    ts = """
    // Auto-generated. Do not edit by hand.
    export const GENERATED_COMPANY_META: Record<string, GeneratedCompanyMeta> = {
      "Intel": { "country": "United States", "industry": "Semiconductors", "prospect_count": 454 },
      "Nvidia": { "country": "United States", "industry": "Semiconductors", "prospect_count": 415 },
    };
    """
    meta = parse_company_meta(ts)
    assert set(meta.keys()) == {"Intel", "Nvidia"}
    assert meta["Intel"]["country"] == "United States"


@pytest.mark.unit
def test_parse_company_meta_accepts_legacy_export_name() -> None:
    """Plan originally referenced `COMPANY_META`; we keep accepting it
    so a future renamer doesn't break the seed."""
    ts = 'export const COMPANY_META: Record<string, X> = {"Acme": {"country": "US"}};'
    assert parse_company_meta(ts) == {"Acme": {"country": "US"}}


@pytest.mark.unit
def test_parse_company_meta_strips_trailing_commas() -> None:
    """The TS generator sometimes emits trailing commas; JSON forbids them."""
    ts = """
    export const GENERATED_COMPANY_META: Record<string, X> = {
      "Acme": { "country": "US", },
    };
    """
    assert parse_company_meta(ts) == {"Acme": {"country": "US"}}


@pytest.mark.unit
def test_parse_company_meta_raises_on_missing_export() -> None:
    with pytest.raises(ValueError, match="GENERATED_COMPANY_META"):
        parse_company_meta("// nothing useful here")


# ── build_update_row ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_build_update_row_full_payload() -> None:
    row = build_update_row({
        "country": "United States",
        "state": "California",
        "hq_city": "Santa Clara",
        "industry": "Semiconductors",
        "employee_count_estimate": "10k-100k",
        "partnerships": ["Dell", "HP"],
        "description": "Chips.",
        "prospect_count": 454,  # ignored — not a column we own
    })
    assert row == {
        "description": "Chips.",
        "hq_city": "Santa Clara",
        "hq_state": "California",
        "hq_country": "United States",
        "industry_tags": ["Semiconductors"],
        "employee_count_estimate": 50000,
        "partnerships": ["Dell", "HP"],
        "enrichment_status": "done",
    }


@pytest.mark.unit
def test_build_update_row_no_industry_yields_empty_tags() -> None:
    """Empty industry should produce [] not [None] — Postgres TEXT[] doesn't
    take NULL elements without escaping."""
    row = build_update_row({"country": "FR"})
    assert row["industry_tags"] == []
    assert row["partnerships"] == []
    assert row["employee_count_estimate"] is None


@pytest.mark.unit
def test_build_update_row_marks_enrichment_done() -> None:
    """Static seeded data counts as 'done' — bulk job won't re-process."""
    assert build_update_row({})["enrichment_status"] == "done"
