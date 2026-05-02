"""Comprehensive tests for `credence.search.find_warm_paths` (Contract 12).

Exercises:
  - The pure rendering helpers (`_build_explanation`, `_build_opener`)
    against every connection type in `WARM_CONNECTION_TYPES` plus the
    generic-fallback long tail.
  - The BFS engine in `find_warm_paths`, with `_fetch_connections_bulk`,
    `_fetch_persons_by_ids`, and `_fetch_evidence_summaries` monkeypatched
    so the suite has zero DB dependency.
  - Every Contract 12 invariant: product-of-strengths, ≤10 cap, sort order
    descending, empty-array shape on no paths, no cycles, branch dedup by
    connector_id keeping strongest path, min_strength pruning, max_hops=1
    direct-only behavior, allowed-types intersection.

These tests are the safety net for the chat-tools layer. They MUST stay
independent of live Supabase so they run in any CI environment.
"""
from __future__ import annotations

from typing import Any

import pytest

from credence import search


# ── Tiny helpers (deterministic UUIDs + edge factories) ────────────────────


def _pid(n: int) -> str:
    """Stable test UUID — `00000000-0000-0000-0000-aaaa{n:08d}`."""
    return f"00000000-0000-0000-0000-aaaa{n:08d}"


def _edge(
    edge_n: int,
    a: int,
    b: int,
    *,
    strength: float = 0.8,
    ctype: str = "career_overlap_general",
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Mimic a row from `person_connections` (`person_a_id < person_b_id`)."""
    a_id, b_id = (_pid(a), _pid(b)) if _pid(a) < _pid(b) else (_pid(b), _pid(a))
    return {
        "id": f"edge-{edge_n}",
        "person_a_id": a_id,
        "person_b_id": b_id,
        "connection_type": ctype,
        "computed_strength": strength,
        "evidence_ids": evidence_ids or [],
    }


def _person(n: int, name: str | None = None) -> dict[str, Any]:
    return {
        "id": _pid(n),
        "canonical_name": name or f"Person {n}",
        "current_title": "Engineer",
        "current_company_id": _pid(900 + n),
        "current_seniority_score": 50,
        "current_functional_domain": "hardware_engineering",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pure helper tests — `_build_explanation`
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestBuildExplanation:
    def test_no_edges_returns_direct_connection_default(self) -> None:
        out = search._build_explanation([_person(1)], [], {})
        assert out == "Direct connection."

    def test_patent_co_inventor_uses_evidence_fields(self) -> None:
        nodes = [_person(1, "Wei Chen"), _person(2, "Sarah Kim")]
        edges = [{"id": "e1", "connection_type": "patent_co_inventor"}]
        ev = {
            "e1": {
                "patent_title": "Method for Tile Sparsity",
                "assignee": "NVIDIA",
                "year": 2018,
            }
        }
        out = search._build_explanation(nodes, edges, ev)
        assert "Wei Chen" in out and "Sarah Kim" in out
        assert "Method for Tile Sparsity" in out
        assert "NVIDIA" in out
        assert "2018" in out

    def test_patent_co_inventor_falls_back_when_evidence_missing(self) -> None:
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": "patent_co_inventor"}]
        out = search._build_explanation(nodes, edges, {})
        assert "a patent" in out  # default placeholder
        assert "shared employer" in out
        assert "year unknown" in out

    @pytest.mark.parametrize(
        "ctype",
        ["academic_co_author_multi", "academic_co_author_single"],
    )
    def test_academic_co_author_both_variants(self, ctype: str) -> None:
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": ctype}]
        ev = {
            "e1": {
                "paper_title": "Attention Is All You Need",
                "venue": "NeurIPS",
                "year": 2017,
                "citation_count": 100000,
            }
        }
        out = search._build_explanation(nodes, edges, ev)
        assert "Attention Is All You Need" in out
        assert "NeurIPS" in out
        assert "100000" in out

    def test_standards_committee_peer(self) -> None:
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": "standards_committee_peer"}]
        ev = {"e1": {"committee": "JEDEC JC-42", "years": "2020-2023"}}
        out = search._build_explanation(nodes, edges, ev)
        assert "JEDEC JC-42" in out
        assert "2020-2023" in out

    def test_conference_co_presenter(self) -> None:
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": "conference_co_presenter"}]
        ev = {"e1": {"event": "Hot Chips 35", "year": 2023}}
        out = search._build_explanation(nodes, edges, ev)
        assert "Hot Chips 35" in out
        assert "2023" in out

    @pytest.mark.parametrize(
        "ctype",
        [
            "career_overlap_same_team",
            "career_overlap_same_domain",
            "career_overlap_general",
        ],
    )
    def test_career_overlap_uses_company_name_key(self, ctype: str) -> None:
        """Live writer uses `company_name`, not `company`. Both must work."""
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": ctype}]
        ev = {
            "e1": {
                "company_name": "Lam Research",
                "overlap_start": 2005,
                "overlap_end": 2008,
                "overlap_years": 3,
            }
        }
        out = search._build_explanation(nodes, edges, ev)
        assert "Lam Research" in out
        assert "2005" in out and "2008" in out
        assert "3 yr overlap" in out

    def test_career_overlap_falls_back_to_company_key_for_compat(self) -> None:
        """Plan-spec `company` key still works (forward-compat)."""
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": "career_overlap_general"}]
        ev = {"e1": {"company": "Old Co", "overlap_start": 2010, "overlap_end": 2012, "overlap_years": 2}}
        out = search._build_explanation(nodes, edges, ev)
        assert "Old Co" in out

    def test_career_overlap_handles_null_overlap_start(self) -> None:
        """`overlap_start` is nullable in live DB; render as `?` cleanly."""
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": "career_overlap_general"}]
        ev = {
            "e1": {
                "company_name": "Intel",
                "overlap_start": None,  # nullable!
                "overlap_end": 2026,
                "overlap_years": 0,
            }
        }
        out = search._build_explanation(nodes, edges, ev)
        assert "Intel" in out
        assert "?–2026" in out  # null start renders as ?

    def test_same_phd_advisor(self) -> None:
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": "same_phd_advisor"}]
        ev = {"e1": {"advisor_name": "Prof. Hennessy", "institution": "Stanford"}}
        out = search._build_explanation(nodes, edges, ev)
        assert "Prof. Hennessy" in out
        assert "Stanford" in out

    def test_co_board_member(self) -> None:
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": "co_board_member"}]
        ev = {"e1": {"organization": "Acme Inc.", "years": "2020-2024"}}
        out = search._build_explanation(nodes, edges, ev)
        assert "Acme Inc." in out

    def test_co_investor(self) -> None:
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": "co_investor"}]
        ev = {"e1": {"company": "Cerebras", "round": "Series D", "year": 2023}}
        out = search._build_explanation(nodes, edges, ev)
        assert "Cerebras" in out

    def test_unknown_type_falls_back_to_generic(self) -> None:
        """Future warm types must not crash — render a readable fallback."""
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": "future_unknown_type"}]
        out = search._build_explanation(nodes, edges, {})
        assert "future unknown type" in out  # underscores → spaces

    def test_every_warm_type_has_a_branch_or_fallback(self) -> None:
        """Smoke: no warm-type call ever raises or returns None."""
        nodes = [_person(1), _person(2)]
        for ctype in search.WARM_CONNECTION_TYPES:
            edges = [{"id": "e1", "connection_type": ctype}]
            out = search._build_explanation(nodes, edges, {})
            assert isinstance(out, str)
            assert len(out) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Pure helper tests — `_build_opener`
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestBuildOpener:
    def test_no_edges_returns_empty_string(self) -> None:
        assert search._build_opener([], [], {}) == ""

    def test_no_nodes_returns_empty_string(self) -> None:
        assert search._build_opener([_person(1)], [{"id": "e1"}], {}) == ""

    def test_patent_opener_uses_first_person_as_addressee(self) -> None:
        nodes = [_person(1, "Wei Chen"), _person(2, "Sarah Kim")]
        edges = [{"id": "e1", "connection_type": "patent_co_inventor"}]
        ev = {"e1": {"patent_title": "X", "assignee": "Intel", "year": 2018}}
        out = search._build_opener(nodes, edges, ev)
        # Connector is the LAST node in the path (path[0]=target, path[-1]=connector).
        assert out.startswith("Sarah Kim")

    def test_career_overlap_uses_company_name_in_opener(self) -> None:
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": "career_overlap_general"}]
        ev = {"e1": {"company_name": "Lam Research"}}
        out = search._build_opener(nodes, edges, ev)
        assert "Lam Research" in out

    def test_unknown_type_opener_falls_back_gracefully(self) -> None:
        nodes = [_person(1, "A"), _person(2, "B")]
        edges = [{"id": "e1", "connection_type": "future_unknown_type"}]
        out = search._build_opener(nodes, edges, {})
        assert "B" in out  # the connector name is at least addressed
        assert isinstance(out, str)

    def test_every_warm_type_produces_an_opener(self) -> None:
        nodes = [_person(1, "Source"), _person(2, "Connector")]
        for ctype in search.WARM_CONNECTION_TYPES:
            edges = [{"id": "e1", "connection_type": ctype}]
            out = search._build_opener(nodes, edges, {})
            assert isinstance(out, str)
            assert "Connector" in out


# ─────────────────────────────────────────────────────────────────────────────
# `find_warm_paths` BFS engine — Contract 12 invariants
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def patched_search(monkeypatch: pytest.MonkeyPatch):
    """Yields (set_edges, set_persons, set_evidence) callables for the test
    body to inject fixtures, plus replays the actual `find_warm_paths`."""
    edges_by_pid: dict[str, list[dict[str, Any]]] = {}
    persons_by_id: dict[str, dict[str, Any]] = {}
    evidence_by_edge: dict[str, dict[str, Any]] = {}

    async def fake_fetch_connections_bulk(
        person_ids: list[str], allowed_types: list[str], min_strength: float, *, limit: int
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for pid in person_ids:
            for e in edges_by_pid.get(pid, []):
                if e["id"] in seen:
                    continue
                if e["connection_type"] not in allowed_types:
                    continue
                if float(e["computed_strength"]) < min_strength:
                    continue
                seen.add(e["id"])
                out.append(dict(e))
        out.sort(key=lambda d: float(d["computed_strength"]), reverse=True)
        return out[:limit]

    async def fake_fetch_persons_by_ids(ids):
        return [persons_by_id[pid] for pid in ids if pid in persons_by_id]

    async def fake_fetch_evidence_summaries(edge_ids):
        return {eid: evidence_by_edge.get(eid, {}) for eid in edge_ids}

    monkeypatch.setattr(search, "_fetch_connections_bulk", fake_fetch_connections_bulk)
    monkeypatch.setattr(search, "_fetch_persons_by_ids", fake_fetch_persons_by_ids)
    monkeypatch.setattr(search, "_fetch_evidence_summaries", fake_fetch_evidence_summaries)

    def set_edges(edges: list[dict[str, Any]]) -> None:
        for e in edges:
            edges_by_pid.setdefault(e["person_a_id"], []).append(e)
            edges_by_pid.setdefault(e["person_b_id"], []).append(e)

    def set_persons(persons: list[dict[str, Any]]) -> None:
        for p in persons:
            persons_by_id[p["id"]] = p

    def set_evidence(ev: dict[str, dict[str, Any]]) -> None:
        evidence_by_edge.update(ev)

    return set_edges, set_persons, set_evidence


@pytest.mark.unit
async def test_no_paths_returns_empty_list_with_message(patched_search) -> None:
    set_edges, set_persons, _ = patched_search
    set_persons([_person(1, "Alice")])
    res = await search.find_warm_paths(_pid(1))
    assert res["paths_found"] == 0
    assert res["paths"] == []
    assert isinstance(res["paths"], list)
    assert "message" in res
    assert res["target_id"] == _pid(1)
    assert res["target_name"] == "Alice"


@pytest.mark.unit
async def test_single_direct_connection_returns_one_path(patched_search) -> None:
    set_edges, set_persons, _ = patched_search
    set_persons([_person(1, "A"), _person(2, "B")])
    set_edges([_edge(0, 1, 2, strength=0.8)])
    res = await search.find_warm_paths(_pid(1), max_hops=2)
    assert res["paths_found"] == 1
    assert res["paths"][0]["hops"] == 1
    assert res["paths"][0]["path_strength"] == 0.8
    assert res["paths"][0]["connector_id"] == _pid(2)


@pytest.mark.unit
async def test_path_strength_is_product_not_sum_or_avg(patched_search) -> None:
    """Contract 12: path_strength = product of edge strengths."""
    set_edges, set_persons, _ = patched_search
    set_persons([_person(i) for i in [1, 2, 3]])
    set_edges([
        _edge(0, 1, 2, strength=0.8),
        _edge(1, 2, 3, strength=0.6),
    ])
    res = await search.find_warm_paths(_pid(1), max_hops=3)
    # The 2-hop path 1 → 2 → 3 has strength 0.8 * 0.6 = 0.48 (rounded to 3dp).
    paths = sorted(res["paths"], key=lambda p: p["hops"])
    one_hop = [p for p in paths if p["hops"] == 1]
    two_hop = [p for p in paths if p["hops"] == 2]
    assert any(abs(p["path_strength"] - 0.8) < 0.001 for p in one_hop)
    assert any(abs(p["path_strength"] - 0.48) < 0.001 for p in two_hop)


@pytest.mark.unit
async def test_min_strength_prunes_paths_below_threshold(patched_search) -> None:
    set_edges, set_persons, _ = patched_search
    set_persons([_person(i) for i in [1, 2, 3]])
    set_edges([
        _edge(0, 1, 2, strength=0.6),  # 1-hop strength 0.6
        _edge(1, 2, 3, strength=0.5),  # 2-hop product 0.30
    ])
    # min_strength=0.5 keeps the 1-hop (0.6) but drops the 2-hop product (0.30).
    res = await search.find_warm_paths(_pid(1), max_hops=3, min_strength=0.5)
    for p in res["paths"]:
        assert p["path_strength"] >= 0.5


@pytest.mark.unit
async def test_max_hops_one_returns_only_direct_connections(patched_search) -> None:
    set_edges, set_persons, _ = patched_search
    set_persons([_person(i) for i in [1, 2, 3]])
    set_edges([
        _edge(0, 1, 2, strength=0.8),  # direct
        _edge(1, 2, 3, strength=0.7),  # 2-hop
    ])
    res = await search.find_warm_paths(_pid(1), max_hops=1)
    assert all(p["hops"] == 1 for p in res["paths"])


@pytest.mark.unit
async def test_paths_are_sorted_by_strength_descending(patched_search) -> None:
    set_edges, set_persons, _ = patched_search
    set_persons([_person(i) for i in range(1, 6)])
    set_edges([
        _edge(0, 1, 2, strength=0.5),
        _edge(1, 1, 3, strength=0.9),
        _edge(2, 1, 4, strength=0.7),
        _edge(3, 1, 5, strength=0.3),
    ])
    res = await search.find_warm_paths(_pid(1), max_hops=1)
    strengths = [p["path_strength"] for p in res["paths"]]
    assert strengths == sorted(strengths, reverse=True)


@pytest.mark.unit
async def test_path_count_capped_at_ten(patched_search) -> None:
    """Contract 12: at most 10 paths returned."""
    set_edges, set_persons, _ = patched_search
    set_persons([_person(i) for i in range(1, 25)])
    # 23 distinct connectors at strength 0.5 — should cap at 10.
    set_edges([_edge(i, 1, i + 2, strength=0.5) for i in range(23)])
    res = await search.find_warm_paths(_pid(1), max_hops=1)
    assert len(res["paths"]) == 10
    assert res["paths_found"] == 10


@pytest.mark.unit
async def test_paths_contain_no_cycles(patched_search) -> None:
    """No node should appear twice in any path."""
    set_edges, set_persons, _ = patched_search
    set_persons([_person(i) for i in range(1, 6)])
    set_edges([
        _edge(0, 1, 2, strength=0.8),
        _edge(1, 2, 3, strength=0.8),
        _edge(2, 3, 1, strength=0.8),  # would create cycle 1→2→3→1
    ])
    res = await search.find_warm_paths(_pid(1), max_hops=4)
    for p in res["paths"]:
        assert len(set(p["path_names"])) == len(p["path_names"])


@pytest.mark.unit
async def test_dedup_keeps_strongest_path_per_connector(patched_search) -> None:
    """If two paths reach the same connector, keep the stronger one."""
    set_edges, set_persons, _ = patched_search
    set_persons([_person(i) for i in [1, 2, 3]])
    # Two ways to reach person 3:
    #   1 → 3 directly (strength 0.5)
    #   1 → 2 → 3       (strength 0.7 * 0.7 = 0.49)
    set_edges([
        _edge(0, 1, 3, strength=0.5),  # direct
        _edge(1, 1, 2, strength=0.7),
        _edge(2, 2, 3, strength=0.7),
    ])
    res = await search.find_warm_paths(_pid(1), max_hops=3, min_strength=0.3)
    # Connector 3 should appear exactly once.
    connector_3_paths = [p for p in res["paths"] if p["connector_id"] == _pid(3)]
    assert len(connector_3_paths) == 1
    # And the kept path should be the stronger 1-hop direct (0.5 > 0.49).
    assert connector_3_paths[0]["hops"] == 1


@pytest.mark.unit
async def test_each_path_has_all_eight_required_keys(patched_search) -> None:
    set_edges, set_persons, _ = patched_search
    set_persons([_person(1, "A"), _person(2, "B")])
    set_edges([_edge(0, 1, 2, strength=0.8)])
    res = await search.find_warm_paths(_pid(1))
    required = {
        "path_strength", "hops", "connector", "connector_id",
        "path_names", "connection_types", "explanation", "suggested_opener",
    }
    for p in res["paths"]:
        assert required.issubset(p.keys()), f"missing keys: {required - p.keys()}"


@pytest.mark.unit
async def test_connection_types_filter_intersects_with_warm_set(patched_search) -> None:
    """A non-warm type passed via `connection_types` is silently dropped."""
    set_edges, set_persons, _ = patched_search
    set_persons([_person(1, "A"), _person(2, "B")])
    set_edges([_edge(0, 1, 2, strength=0.9, ctype="patent_co_inventor")])
    # Caller passes a noisy type AND a warm one — only patent_co_inventor survives.
    res = await search.find_warm_paths(
        _pid(1), connection_types=["alumni_network", "patent_co_inventor"]
    )
    assert res["paths_found"] == 1
    assert res["paths"][0]["connection_types"] == ["patent_co_inventor"]


@pytest.mark.unit
async def test_connection_types_empty_intersection_falls_back_to_full_warm_set(
    patched_search,
) -> None:
    """If filter would leave 0 allowed types, default back to ALL warm types."""
    set_edges, set_persons, _ = patched_search
    set_persons([_person(1, "A"), _person(2, "B")])
    set_edges([_edge(0, 1, 2, strength=0.9, ctype="patent_co_inventor")])
    res = await search.find_warm_paths(
        _pid(1), connection_types=["alumni_network", "conference_co_attendee"]
    )
    # Both filters are non-warm → fallback to all warm → patent_co_inventor matches.
    assert res["paths_found"] == 1


@pytest.mark.unit
async def test_path_strength_rounded_to_three_decimals(patched_search) -> None:
    set_edges, set_persons, _ = patched_search
    set_persons([_person(1), _person(2)])
    set_edges([_edge(0, 1, 2, strength=0.7234567)])
    # Lower min_strength so the high-precision strength survives.
    res = await search.find_warm_paths(_pid(1), min_strength=0.1)
    s = res["paths"][0]["path_strength"]
    assert round(s, 3) == s


@pytest.mark.unit
async def test_evidence_threaded_through_to_explanation(patched_search) -> None:
    set_edges, set_persons, set_ev = patched_search
    set_persons([_person(1, "Wei Chen"), _person(2, "Sarah Kim")])
    set_edges([_edge(0, 1, 2, strength=0.95, ctype="patent_co_inventor",
                     evidence_ids=["ev-1"])])
    set_ev({
        "edge-0": {
            "patent_title": "Method for Tile Sparsity",
            "assignee": "NVIDIA",
            "year": 2018,
        }
    })
    res = await search.find_warm_paths(_pid(1))
    expl = res["paths"][0]["explanation"]
    assert "Method for Tile Sparsity" in expl
    assert "NVIDIA" in expl


@pytest.mark.unit
async def test_target_name_returned_even_on_empty_paths(patched_search) -> None:
    set_edges, set_persons, _ = patched_search
    set_persons([_person(1, "Solo Person")])
    res = await search.find_warm_paths(_pid(1))
    assert res["paths_found"] == 0
    assert res["target_name"] == "Solo Person"


@pytest.mark.unit
async def test_unknown_target_name_is_none(patched_search) -> None:
    """Target person not in the persons hydration set → name is None."""
    res = await search.find_warm_paths(_pid(999))
    # Should not crash; should return graceful empty result.
    assert res["paths_found"] == 0
    assert res["target_name"] is None
