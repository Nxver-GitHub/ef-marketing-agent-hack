"""Unit tests for `press_classifier.classify_press_release`.

Pure-function tests — no DB, no network, no fixtures. Each parametrized
case is a real-world style headline so the regex set stays tied to data
that actually shows up in `company_signals`.
"""
from __future__ import annotations

from typing import Any

import pytest

from credence.enrichment.press_classifier import (
    CATEGORY_CO_MENTION,
    CATEGORY_EARNINGS,
    CATEGORY_GENERAL,
    CATEGORY_PARTNERSHIP,
    CATEGORY_PRODUCT_LAUNCH,
    CATEGORY_RESEARCH,
    classify_press_release,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        # ── earnings ──────────────────────────────────────────────────────
        ("NVIDIA Reports First Quarter Fiscal 2026 Financial Results", CATEGORY_EARNINGS),
        ("Acme Corp Announces Q3 FY2025 Earnings", CATEGORY_EARNINGS),
        ("Globex Releases Full-Year Results for Fiscal 2024", CATEGORY_EARNINGS),
        ("Initech reports fourth quarter and full year financial results", CATEGORY_EARNINGS),
        # ── product_launch ────────────────────────────────────────────────
        ("Acme Unveils Next-Generation AI Inference Platform", CATEGORY_PRODUCT_LAUNCH),
        ("Globex Launches New Cloud Database Service", CATEGORY_PRODUCT_LAUNCH),
        ("Initech Introduces Industry-First Quantum Compiler", CATEGORY_PRODUCT_LAUNCH),
        ("Hooli Announces General Availability of Hooli SearchPro", CATEGORY_PRODUCT_LAUNCH),
        ("Pied Piper Now Shipping Middle-Out Compression SDK", CATEGORY_PRODUCT_LAUNCH),
        # ── partnership ───────────────────────────────────────────────────
        ("Acme and Globex Announce Strategic Partnership", CATEGORY_PARTNERSHIP),
        ("Initech to Partner with NVIDIA on Edge Inference", CATEGORY_PARTNERSHIP),
        ("Hooli and Pied Piper Expand Their Strategic Partnership", CATEGORY_PARTNERSHIP),
        ("Acme Forms Joint Venture with Stark Industries", CATEGORY_PARTNERSHIP),
        # ── research ──────────────────────────────────────────────────────
        ("Acme Research Publishes White Paper on Sparse Transformers", CATEGORY_RESEARCH),
        ("Globex Tops MLPerf Benchmark for Inference v4.0", CATEGORY_RESEARCH),
        ("Initech Paper Accepted at NeurIPS 2025", CATEGORY_RESEARCH),
    ],
)
def test_classify_known_headlines(headline: str, expected: str) -> None:
    assert classify_press_release({"headline": headline}) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"headline": ""},
        {"headline": None},
        {"unrelated_field": "no headline here"},
    ],
)
def test_empty_or_missing_payload_returns_general(payload: Any) -> None:
    assert classify_press_release(payload) == CATEGORY_GENERAL


@pytest.mark.unit
def test_co_mention_signal_when_two_named_executives() -> None:
    payload = {
        "headline": "Acme Hosts Industry Roundtable",  # generic, no rule fires
        "mentioned_executives": [
            {"name": "Jane Doe", "title": "CEO"},
            {"name": "John Smith", "title": "CTO"},
        ],
    }
    assert classify_press_release(payload) == CATEGORY_CO_MENTION


@pytest.mark.unit
def test_co_mention_signal_accepts_string_entries() -> None:
    payload = {
        "headline": "Acme Hosts Industry Roundtable",
        "mentioned_executives": ["Jane Doe", "John Smith"],
    }
    assert classify_press_release(payload) == CATEGORY_CO_MENTION


@pytest.mark.unit
def test_single_executive_does_not_trigger_co_mention() -> None:
    payload = {
        "headline": "Acme Names New Board Member",
        "mentioned_executives": [{"name": "Jane Doe", "title": "Director"}],
    }
    assert classify_press_release(payload) == CATEGORY_GENERAL


@pytest.mark.unit
def test_priority_partnership_beats_product_launch() -> None:
    # Headline matches BOTH partnership ("partner with") and product_launch
    # ("launch"). Priority order says partnership wins.
    payload = {"headline": "Acme partners with Globex to launch new AI accelerator"}
    assert classify_press_release(payload) == CATEGORY_PARTNERSHIP


@pytest.mark.unit
def test_priority_earnings_beats_product_launch() -> None:
    # "Releases" alone would be product_launch, but Q1 FY2026 makes it earnings.
    payload = {"headline": "Acme releases Q1 FY2026 financial results"}
    assert classify_press_release(payload) == CATEGORY_EARNINGS


@pytest.mark.unit
def test_headline_rules_outrank_co_mention() -> None:
    # Even with two named execs, an actionable headline rule wins.
    payload = {
        "headline": "Acme Launches New AI Platform",
        "mentioned_executives": [
            {"name": "Jane Doe", "title": "CEO"},
            {"name": "John Smith", "title": "CTO"},
        ],
    }
    assert classify_press_release(payload) == CATEGORY_PRODUCT_LAUNCH
