"""Pure-function unit tests for `explain_company` helpers.

The DB-touching path (`explain_company` itself) is exercised by live smoke
runs against the demo tenant — keeping the unit suite strictly to the pure
helpers and the executive-matching helper with a mocked `fetch`.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from credence.search import (
    _VIA_HUMANIZED,
    _humanize_connection_type,
    _match_executives_to_persons,
    _split_exec_name,
)


# ── _split_exec_name ────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Jensen Huang",        ("jensen", "huang")),
        ("  Jensen   Huang  ",  ("jensen", "huang")),
        ("MARK A. ZUCKERBERG",  ("mark", "zuckerberg")),
        ("",                    None),
        (None,                  None),
        ("Madonna",             None),  # single token → can't split
    ],
)
def test_split_exec_name(raw: str | None, expected: tuple[str, str] | None) -> None:
    assert _split_exec_name(raw) == expected


# ── _humanize_connection_type ───────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ctype", "expected_substr"),
    [
        ("patent_co_inventor",         "co-invented a patent"),
        ("academic_co_author_multi",   "co-authored multiple papers"),
        ("academic_co_author_single",  "co-authored a paper"),
        ("conference_co_presenter",    "co-presented at a conference"),
        ("career_overlap_general",     "shared career history"),
        ("career_overlap_same_team",   "worked on the same team"),
        ("standards_committee_peer",   "standards committee"),
        ("same_phd_advisor",           "PhD advisor"),
        ("co_board_member",            "board"),
        ("co_investor",                "co-invested"),
    ],
)
def test_humanize_connection_type_known(ctype: str, expected_substr: str) -> None:
    assert expected_substr in _humanize_connection_type(ctype)


@pytest.mark.unit
def test_humanize_connection_type_unknown_falls_back() -> None:
    """Unknown type should snake-case-strip — never raise, never return enum."""
    out = _humanize_connection_type("future_extractor_type")
    assert "_" not in out
    assert out == "future extractor type"


@pytest.mark.unit
def test_humanize_connection_type_none() -> None:
    assert _humanize_connection_type(None) == "shared a connection"


@pytest.mark.unit
def test_via_map_covers_every_warm_type() -> None:
    """Guard: every connection_type in WARM_CONNECTION_TYPES has copy."""
    from credence.search import WARM_CONNECTION_TYPES
    for ctype in WARM_CONNECTION_TYPES:
        assert ctype in _VIA_HUMANIZED, f"missing via copy for {ctype}"


# ── _match_executives_to_persons ────────────────────────────────────────────


@pytest.mark.unit
async def test_match_executives_empty_list_returns_empty() -> None:
    out = await _match_executives_to_persons([], uuid4())
    assert out == {}


@pytest.mark.unit
async def test_match_executives_single_word_names_skipped() -> None:
    """Names we can't split into first/last are silently skipped — no DB hit."""
    out = await _match_executives_to_persons(
        [{"name": "Madonna"}, {"name": ""}],
        uuid4(),
    )
    assert out == {}


@pytest.mark.unit
async def test_match_executives_resolves_name_to_uuid() -> None:
    """Happy path: exec name tokens both appear in canonical_name."""
    company_id = uuid4()
    jensen_id = uuid4()
    other_id = uuid4()

    async def fake_fetch(_sql: str, _company: Any) -> list[dict[str, Any]]:
        return [
            {"id": jensen_id, "canonical_name": "Jensen Huang"},
            {"id": other_id,  "canonical_name": "Bill Dally"},
        ]

    with patch("credence.search.fetch", side_effect=fake_fetch):
        out = await _match_executives_to_persons(
            [{"name": "Jensen Huang"}, {"name": "Lisa Su"}],
            company_id,
        )

    assert out == {"Jensen Huang": str(jensen_id)}


@pytest.mark.unit
async def test_match_executives_case_insensitive() -> None:
    """Token match should be case-insensitive on both sides."""
    company_id = uuid4()
    pid = uuid4()

    async def fake_fetch(_sql: str, _company: Any) -> list[dict[str, Any]]:
        return [{"id": pid, "canonical_name": "JENSEN HUANG"}]

    with patch("credence.search.fetch", side_effect=fake_fetch):
        out = await _match_executives_to_persons(
            [{"name": "jensen huang"}],
            company_id,
        )

    assert out == {"jensen huang": str(pid)}


@pytest.mark.unit
async def test_match_executives_no_match_omits_key() -> None:
    """When no persons row matches, the exec name is absent from the dict.

    `explain_company` then resolves `dict.get(name)` → None for the
    `matched_person_id` field — which is the contract the UI expects.
    """
    async def fake_fetch(_sql: str, _company: Any) -> list[dict[str, Any]]:
        return [{"id": uuid4(), "canonical_name": "Someone Else"}]

    with patch("credence.search.fetch", side_effect=fake_fetch):
        out = await _match_executives_to_persons(
            [{"name": "Jensen Huang"}],
            uuid4(),
        )

    assert out == {}


# ── press category passthrough (logic-level contract) ───────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"category": "funding", "headline": "x"}, "funding"),
        ({"headline": "x"},                         None),  # no category yet
        ({"category": None, "headline": "x"},       None),
    ],
)
def test_press_category_passthrough(
    payload: dict[str, Any], expected: str | None
) -> None:
    """Mirrors the per-item transform in explain_company:
    `payload['category'] = payload.get('category')`. Default None when absent.
    """
    out = dict(payload)
    out["category"] = out.get("category")
    assert out["category"] == expected
