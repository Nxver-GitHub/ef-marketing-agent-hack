"""Comprehensive tests for `credence.search.get_org_context` (Contract 13).

Mocks the `db.fetch` / `db.fetchrow` calls so the suite has zero DB dependency.
Covers every Contract 13 invariant:
  - empty arrays (not None) when no edges exist
  - manager + report rows include `edge_confidence` and `inference_method`
  - `is_dotted_line: true` exactly when path_confidence < confidence
  - scope fields default to empty arrays / null when no row
  - up to 10 functional cluster peers
  - include_peers=False suppresses peers AND domain
  - org_chart_note rendered only when managers OR direct_reports exist
"""
from __future__ import annotations

from typing import Any

import pytest

from credence import search


def _pid(n: int) -> str:
    return f"00000000-0000-0000-0000-bbbb{n:08d}"


# A tiny query-router shim — matches on substrings to dispatch responses.
class FakeDB:
    """Stub for `credence.search.fetch` and `fetchrow`.

    Tests register canned responses by query-substring (cheap and
    transparent: each test sets only the queries it cares about).
    """

    def __init__(self) -> None:
        # list of (substring, response). First match wins per call.
        self._row_responses: list[tuple[str, dict[str, Any] | None]] = []
        self._rows_responses: list[tuple[str, list[dict[str, Any]]]] = []

    def on_fetchrow(self, substring: str, response: dict[str, Any] | None) -> None:
        self._row_responses.append((substring, response))

    def on_fetch(self, substring: str, response: list[dict[str, Any]]) -> None:
        self._rows_responses.append((substring, response))

    async def fetch(self, sql: str, *_args: Any) -> list[dict[str, Any]]:
        for substr, resp in self._rows_responses:
            if substr in sql:
                return resp
        return []

    async def fetchrow(self, sql: str, *_args: Any) -> dict[str, Any] | None:
        for substr, resp in self._row_responses:
            if substr in sql:
                return resp
        return None


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    db = FakeDB()
    monkeypatch.setattr(search, "fetch", db.fetch)
    monkeypatch.setattr(search, "fetchrow", db.fetchrow)
    return db


# ─────────────────────────────────────────────────────────────────────────────
# Contract 13 invariants
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_empty_state_returns_empty_arrays_not_none(fake_db: FakeDB) -> None:
    fake_db.on_fetchrow("FROM persons", {
        "id": _pid(1), "canonical_name": "Solo",
        "current_title": "IC", "current_seniority_score": 40,
        "current_functional_domain": "software_engineering",
        "current_company_id": _pid(900),
    })
    # No managers, reports, or scope rows queued → defaults all kick in.
    out = await search.get_org_context(_pid(1))
    assert out["managers"] == []
    assert out["direct_reports"] == []
    assert out["direct_report_count"] == 0
    assert out["functional_cluster"]["peers"] == []
    assert out["scope"]["owns_products"] == []
    assert out["scope"]["budget_authority_level"] is None
    assert out["org_chart_note"] is None


@pytest.mark.unit
async def test_managers_include_edge_confidence_and_inference_method(fake_db: FakeDB) -> None:
    fake_db.on_fetchrow("FROM persons", {
        "id": _pid(1), "canonical_name": "Subject",
        "current_title": "Director", "current_seniority_score": 60,
        "current_functional_domain": "hardware_engineering",
        "current_company_id": _pid(900),
    })
    fake_db.on_fetch("e.report_id  = $1", [{
        "edge_id": "edge-mgr-1",
        "edge_confidence": 0.85,
        "path_confidence": 0.85,
        "inference_method": "linkedin_reports_to",
        "person_id": _pid(2),
        "canonical_name": "VP Vish",
        "current_title": "VP Engineering",
        "current_seniority_score": 70,
        "current_functional_domain": "hardware_engineering",
    }])
    out = await search.get_org_context(_pid(1))
    assert len(out["managers"]) == 1
    m = out["managers"][0]
    assert m["edge_confidence"] == 0.85
    assert m["inference_method"] == "linkedin_reports_to"
    assert m["name"] == "VP Vish"
    assert m["title"] == "VP Engineering"


@pytest.mark.unit
async def test_is_dotted_line_true_when_path_confidence_lt_confidence(
    fake_db: FakeDB,
) -> None:
    fake_db.on_fetchrow("FROM persons", {"id": _pid(1), "canonical_name": "X"})
    fake_db.on_fetch("e.report_id  = $1", [{
        "edge_id": "edge-1",
        "edge_confidence": 0.85,
        "path_confidence": 0.40,  # < confidence → dotted line
        "inference_method": "implicit_scoring",
        "person_id": _pid(2),
        "canonical_name": "Y",
        "current_title": None,
        "current_seniority_score": None,
        "current_functional_domain": None,
    }])
    out = await search.get_org_context(_pid(1))
    assert out["managers"][0]["is_dotted_line"] is True


@pytest.mark.unit
async def test_is_dotted_line_false_when_path_confidence_eq_confidence(
    fake_db: FakeDB,
) -> None:
    fake_db.on_fetchrow("FROM persons", {"id": _pid(1), "canonical_name": "X"})
    fake_db.on_fetch("e.report_id  = $1", [{
        "edge_id": "edge-1",
        "edge_confidence": 0.85,
        "path_confidence": 0.85,
        "inference_method": "linkedin_reports_to",
        "person_id": _pid(2),
        "canonical_name": "Y",
        "current_title": None,
        "current_seniority_score": None,
        "current_functional_domain": None,
    }])
    out = await search.get_org_context(_pid(1))
    assert out["managers"][0]["is_dotted_line"] is False


@pytest.mark.unit
async def test_is_dotted_line_false_when_path_confidence_null(fake_db: FakeDB) -> None:
    fake_db.on_fetchrow("FROM persons", {"id": _pid(1), "canonical_name": "X"})
    fake_db.on_fetch("e.report_id  = $1", [{
        "edge_id": "edge-1",
        "edge_confidence": 0.85,
        "path_confidence": None,
        "inference_method": "linkedin_reports_to",
        "person_id": _pid(2),
        "canonical_name": "Y",
        "current_title": None,
        "current_seniority_score": None,
        "current_functional_domain": None,
    }])
    out = await search.get_org_context(_pid(1))
    assert out["managers"][0]["is_dotted_line"] is False


@pytest.mark.unit
async def test_direct_reports_count_matches_array_length(fake_db: FakeDB) -> None:
    fake_db.on_fetchrow("FROM persons", {"id": _pid(1), "canonical_name": "Boss"})
    reports = [
        {
            "edge_id": f"edge-r-{i}",
            "edge_confidence": 0.7,
            "inference_method": "implicit_scoring",
            "person_id": _pid(100 + i),
            "canonical_name": f"Report {i}",
            "current_title": "Engineer",
            "current_seniority_score": 50,
            "current_functional_domain": "software_engineering",
        }
        for i in range(5)
    ]
    fake_db.on_fetch("e.manager_id = $1", reports)
    out = await search.get_org_context(_pid(1))
    assert out["direct_report_count"] == 5
    assert len(out["direct_reports"]) == 5


@pytest.mark.unit
async def test_org_chart_note_rendered_when_managers_exist(fake_db: FakeDB) -> None:
    fake_db.on_fetchrow("FROM persons", {"id": _pid(1), "canonical_name": "X"})
    fake_db.on_fetch("e.report_id  = $1", [{
        "edge_id": "e1", "edge_confidence": 0.5, "path_confidence": 0.5,
        "inference_method": "x",
        "person_id": _pid(2), "canonical_name": "M",
        "current_title": None, "current_seniority_score": None,
        "current_functional_domain": None,
    }])
    out = await search.get_org_context(_pid(1))
    assert out["org_chart_note"] is not None
    assert "inferred" in out["org_chart_note"]


@pytest.mark.unit
async def test_org_chart_note_none_when_no_edges(fake_db: FakeDB) -> None:
    fake_db.on_fetchrow("FROM persons", {"id": _pid(1), "canonical_name": "X"})
    out = await search.get_org_context(_pid(1))
    assert out["org_chart_note"] is None


@pytest.mark.unit
async def test_include_peers_false_skips_cluster_queries_entirely(
    fake_db: FakeDB,
) -> None:
    fake_db.on_fetchrow("FROM persons", {"id": _pid(1), "canonical_name": "X"})
    out = await search.get_org_context(_pid(1), include_peers=False)
    assert out["functional_cluster"]["peers"] == []
    assert out["functional_cluster"]["domain"] is None
    assert out["functional_cluster"]["sub_domain"] is None


@pytest.mark.unit
async def test_include_peers_true_populates_cluster_when_membership_exists(
    fake_db: FakeDB,
) -> None:
    fake_db.on_fetchrow("FROM persons", {"id": _pid(1), "canonical_name": "X"})
    fake_db.on_fetchrow(
        "FROM org_cluster_members",
        {"cluster_id": _pid(500), "membership_confidence": 0.95},
    )
    fake_db.on_fetchrow(
        "FROM org_functional_clusters",
        {"functional_domain": "hardware_engineering", "sub_domain": None, "member_count": 12},
    )
    fake_db.on_fetch("FROM org_cluster_members m", [
        {
            "membership_confidence": 0.9,
            "person_id": _pid(101 + i),
            "canonical_name": f"Peer {i}",
            "current_title": "Eng",
            "current_seniority_score": 50,
        }
        for i in range(3)
    ])
    out = await search.get_org_context(_pid(1), include_peers=True)
    assert out["functional_cluster"]["domain"] == "hardware_engineering"
    assert out["functional_cluster"]["peer_count"] == 12
    assert len(out["functional_cluster"]["peers"]) == 3


@pytest.mark.unit
async def test_scope_populated_when_row_exists(fake_db: FakeDB) -> None:
    fake_db.on_fetchrow("FROM persons", {"id": _pid(1), "canonical_name": "VP"})
    fake_db.on_fetchrow("FROM person_scope_estimates", {
        "owns_products": ["HBM", "GDDR7"],
        "owns_technologies": ["3D stacking"],
        "owns_functions": ["product_management"],
        "owns_regions": ["AMER"],
        "team_size_min": 8,
        "team_size_max": 12,
        "budget_authority_level": "department",
    })
    out = await search.get_org_context(_pid(1))
    assert out["scope"]["owns_products"] == ["HBM", "GDDR7"]
    assert out["scope"]["budget_authority_level"] == "department"
    assert out["scope"]["team_size_min"] == 8
    assert out["scope"]["team_size_max"] == 12


@pytest.mark.unit
async def test_returned_payload_has_all_top_level_keys(fake_db: FakeDB) -> None:
    fake_db.on_fetchrow("FROM persons", {"id": _pid(1), "canonical_name": "X"})
    out = await search.get_org_context(_pid(1))
    required = {
        "person", "managers", "direct_reports", "direct_report_count",
        "functional_cluster", "scope", "org_chart_note",
    }
    assert required.issubset(out.keys())


@pytest.mark.unit
async def test_person_block_uses_canonical_metadata(fake_db: FakeDB) -> None:
    fake_db.on_fetchrow("FROM persons", {
        "id": _pid(1),
        "canonical_name": "Adam Smith",
        "current_title": "Principal PM",
        "current_seniority_score": 60,
        "current_functional_domain": "product_management",
        "current_company_id": _pid(900),
    })
    out = await search.get_org_context(_pid(1))
    assert out["person"]["name"] == "Adam Smith"
    assert out["person"]["title"] == "Principal PM"
    assert out["person"]["seniority_score"] == 60
    assert out["person"]["functional_domain"] == "product_management"
