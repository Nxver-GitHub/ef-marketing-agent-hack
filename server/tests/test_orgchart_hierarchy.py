"""Tests for `credence.orgchart.hierarchy` — Plan A Stage 1.2.

Mirrors the test split in `test_orgchart_clustering.py`: pure-logic tests
exercise the scoring functions and the cluster planner directly; DB-backed
tests use monkeypatched `fetch` + `acquire` shims to exercise the
orchestrator without touching Postgres.

Coverage:
1. _seniority_gap_score buckets (parametrized).
2. _patent_cluster_score linear scale, capped at 0.15.
3. _ic_track_compatible: non-IC report + IC manager → False.
4. _ic_track_compatible: IC report + non-IC manager → True (mixed OK).
5. _score_pair full-component sum, cap clamp, IC mismatch, implausible
   gap, and self-pair all zero out.
6. _build_cluster_hierarchy:
   - Plausible-manager picks for a small VP/SE/E cluster.
   - High-volume span cap (Director with 13 reports → 10 land, 3 violations).
   - Task 1-A 5-person spec scenario (VP / 2 Directors / 2 Managers).
   - Cycle prevention (same-seniority pairs excluded; union-find belt-and-braces).
   - Span cap rejection counted as `span_violations_resolved`.
   - IC track preservation (DistEng never manager of Director).
   - Deterministic output across repeated runs.
7. ingest_explicit_edge writes the right SQL and validates inputs.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from credence.orgchart import hierarchy
from credence.orgchart.hierarchy import (
    COMPONENT_KEYS,
    IMPLICIT_SCORE_CAP,
    SPAN_LIMITS,
    UNRESOLVED_TARGET_CONFIDENCE_FACTOR,
    ClusterMember,
    EdgeScore,
    HierarchyEdge,
    _build_cluster_hierarchy,
    _ic_track_compatible,
    _patent_cluster_score,
    _score_pair,
    _seniority_gap_score,
    ingest_explicit_edge,
)


ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")


def _member(
    *,
    pid: int,
    title: str | None = None,
    seniority: int | None = None,
    is_ic: bool = False,
    sub_domain: str | None = None,
    inferred_team: str | None = None,
) -> ClusterMember:
    return ClusterMember(
        person_id=UUID(f"00000000-0000-0000-0000-cccc{pid:08d}"),
        account_id=ACCOUNT,
        title=title,
        seniority=seniority,
        is_ic_track=is_ic,
        sub_domain=sub_domain,
        inferred_team=inferred_team,
    )


# ── Pure scoring tests ───────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("manager_seniority", "report_seniority", "expected"),
    [
        # Bucket boundaries from V3_PT2.md L102-105.
        (70, 60, 0.30),  # gap 10 — natural
        (60, 50, 0.30),  # gap 10 — natural
        (60, 53, 0.18),  # gap 7 — peer-ish
        (90, 70, 0.12),  # gap 20 — skip-level
        (70, 67, 0.0),   # gap 3 — too close
        (95, 65, 0.0),   # gap 30 — too far
        (60, 60, 0.0),   # zero gap — peers
        (50, 70, 0.0),   # reverse gap — implausible
    ],
)
def test_seniority_gap_score(
    manager_seniority: int, report_seniority: int, expected: float,
) -> None:
    assert _seniority_gap_score(manager_seniority, report_seniority) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("shared_patents", "expected"),
    [
        (0, 0.0),
        (1, 0.05),
        (2, 0.10),
        (3, 0.15),
        (5, 0.15),  # capped
        (-1, 0.0),  # defensive
    ],
)
def test_patent_cluster_score(shared_patents: int, expected: float) -> None:
    assert _patent_cluster_score(shared_patents) == pytest.approx(expected, rel=1e-6)


@pytest.mark.unit
def test_ic_track_compat_non_ic_report_excludes_ic_manager() -> None:
    # CLAUDE.md L211: a non-IC report can't have an IC-track manager.
    assert _ic_track_compatible(manager_is_ic=True, report_is_ic=False) is False


@pytest.mark.unit
def test_ic_track_compat_ic_report_accepts_either_track() -> None:
    # IC reports CAN have non-IC managers (parallel tracks rolling up under
    # a management head is the common case).
    assert _ic_track_compatible(manager_is_ic=False, report_is_ic=True) is True
    assert _ic_track_compatible(manager_is_ic=True, report_is_ic=True) is True
    # Two non-IC people: also fine.
    assert _ic_track_compatible(manager_is_ic=False, report_is_ic=False) is True


@pytest.mark.unit
def test_score_pair_full_components_sum_correctly() -> None:
    """All components fire: 0.30 (gap) + 0.25 (domain) + 0.10 (mgr title)
    + 0.05 (capacity) + 0.08 (geo) = 0.78. No sub_domain or patents."""
    manager = _member(pid=1, title="VP Engineering", seniority=70)
    report = _member(pid=2, title="Senior Software Engineer", seniority=60)

    score = _score_pair(
        manager, report,
        same_sub_domain=False,
        shared_patents=0,
        geographic_compatible=True,
        manager_has_capacity=True,
    )

    # 0.30 + 0.25 + 0.10 + 0.05 + 0.08 = 0.78
    assert score.total == pytest.approx(0.78, rel=1e-6)
    # Components sum (within float tolerance) to total when no cap clamps.
    assert sum(score.components.values()) == pytest.approx(score.total, abs=0.01)
    assert len(score.components) == 7
    assert set(score.components.keys()) == set(COMPONENT_KEYS)
    assert score.dominant_component in COMPONENT_KEYS
    # Seniority gap (0.30) is the largest contributor in this scenario.
    assert score.dominant_component == "seniority_gap"


@pytest.mark.unit
def test_score_pair_clamps_at_cap() -> None:
    """All bonuses including sub_domain (+0.15) + 3 shared patents (+0.15)
    would push score over 1.0 — must clamp at IMPLICIT_SCORE_CAP (0.95)."""
    manager = _member(pid=1, title="Director Hardware", seniority=60)
    report = _member(pid=2, title="Senior Hardware Engineer", seniority=50)

    score = _score_pair(
        manager, report,
        same_sub_domain=True,
        shared_patents=3,
        geographic_compatible=True,
        manager_has_capacity=True,
    )

    # 0.30 + 0.25 + 0.15 + 0.10 + 0.05 + 0.15 + 0.08 = 1.08 → clamped 0.95
    assert score.total == IMPLICIT_SCORE_CAP


@pytest.mark.unit
def test_score_pair_ic_mismatch_zeros_score() -> None:
    """IC manager + non-IC report → 0 even with otherwise perfect signals."""
    manager = _member(
        pid=1, title="Distinguished Engineer", seniority=70, is_ic=True,
    )
    report = _member(
        pid=2, title="Senior Manager Verification", seniority=60, is_ic=False,
    )

    score = _score_pair(manager, report, same_sub_domain=True, shared_patents=3)
    assert score.total == 0.0
    # Null-edge: every component is zero.
    assert all(v == 0.0 for v in score.components.values())
    # Canonical dominant for null-edges is 'seniority_gap'.
    assert score.dominant_component == "seniority_gap"


@pytest.mark.unit
def test_score_pair_implausible_gap_zeros_score() -> None:
    """Even with all other components firing, a 3-point gap is too tight."""
    manager = _member(pid=1, title="Director", seniority=63)
    report = _member(pid=2, title="Senior Engineer", seniority=60)

    score = _score_pair(manager, report, same_sub_domain=False)
    assert score.total == 0.0


@pytest.mark.unit
def test_score_pair_self_pair_zeros() -> None:
    """Manager and report being the same person → 0."""
    same = _member(pid=1, title="Director", seniority=70)
    score = _score_pair(same, same, same_sub_domain=False)
    assert score.total == 0.0


# ── _build_cluster_hierarchy ─────────────────────────────────────────────────


@pytest.mark.unit
def test_build_cluster_hierarchy_picks_plausible_managers() -> None:
    """A small cluster: 1 VP + 2 Senior Engineers + 2 Engineers.
    The VP should manage the Senior Engineers (gap 10), and the Senior
    Engineers should manage the Engineers (gap 5)."""
    vp = _member(pid=1, title="VP Engineering", seniority=70)
    se1 = _member(pid=2, title="Senior Software Engineer", seniority=60)
    se2 = _member(pid=3, title="Senior Software Engineer", seniority=60)
    e1 = _member(pid=4, title="Software Engineer", seniority=55)
    e2 = _member(pid=5, title="Software Engineer", seniority=55)

    edges, skipped, span_violations = _build_cluster_hierarchy(
        [vp, se1, se2, e1, e2],
    )

    assert span_violations == 0
    # Every report except the VP should have an edge — 4 edges total.
    edge_reports = {e.report_id for e in edges}
    assert vp.person_id not in edge_reports
    assert {se1.person_id, se2.person_id, e1.person_id, e2.person_id} <= edge_reports
    # Senior engineers should report to the VP (gap 10 = 0.30 base, full bonus stack).
    se_managers = {e.manager_id for e in edges if e.report_id in {se1.person_id, se2.person_id}}
    assert se_managers == {vp.person_id}


@pytest.mark.unit
def test_build_cluster_hierarchy_respects_span_cap_for_high_volume() -> None:
    """One Director + 13 Engineers (cap=10) → one report gets rerouted."""
    director = _member(pid=1, title="Director Engineering", seniority=60)
    engineers = [
        _member(pid=10 + i, title="Software Engineer", seniority=50)
        for i in range(13)
    ]

    edges, skipped, span_violations = _build_cluster_hierarchy(
        [director, *engineers],
    )

    # Span violation triggers a reroute pass. 3 of the 13 engineers should be
    # dropped from director's slate (cap 10) AND rerouted — but with no other
    # plausible manager (all peers are same-seniority engineers), the reroute
    # produces no replacement edge. Net effect: 10 edges land, 3 reports become
    # orphans. span_violations counts the dropped first-pass edges.
    assert span_violations == 3
    director_edge_count = sum(1 for e in edges if e.manager_id == director.person_id)
    assert director_edge_count == SPAN_LIMITS["director"]


# ── Global tree assignment (Task 1-A) ────────────────────────────────────────


@pytest.mark.unit
def test_build_cluster_hierarchy_five_person_spec_scenario() -> None:
    """Task 1-A spec scenario:

    VP=70, Dir-A=60, Dir-B=60, Mgr-A=50, Mgr-B=50.

    Expected: VP manages both Directors (gap 10, full bonus → 0.78); each
    Director manages one Manager (gap 10 → 0.78). Two Managers can also
    legally roll directly to VP (gap 20 → 0.12 + 0.25 + 0.10 + 0.05 + 0.08 =
    0.60), so to ensure Mgr-A → Dir-A and Mgr-B → Dir-B we make the Director
    titles carry the manager-title bonus AND share patents with their
    intended report (which lifts their score above the VP→Mgr alternative).
    """
    vp = _member(pid=1, title="VP Engineering", seniority=70)
    dir_a = _member(pid=2, title="Director Hardware", seniority=60)
    dir_b = _member(pid=3, title="Director Software", seniority=60)
    mgr_a = _member(pid=4, title="Engineering Manager A", seniority=50)
    mgr_b = _member(pid=5, title="Engineering Manager B", seniority=50)

    # Patent affinity: Dir-A co-invents with Mgr-A (3 shared patents = +0.15);
    # Dir-B with Mgr-B. This pushes Dir→Mgr score (0.78 + 0.15 = 0.93) above
    # VP→Mgr (0.60), guaranteeing the desired tree shape.
    pair_patents = {
        (dir_a.person_id, mgr_a.person_id): 3,
        (mgr_a.person_id, dir_a.person_id): 3,
        (dir_b.person_id, mgr_b.person_id): 3,
        (mgr_b.person_id, dir_b.person_id): 3,
    }

    edges, skipped, span_violations = _build_cluster_hierarchy(
        [vp, dir_a, dir_b, mgr_a, mgr_b],
        shared_patents_map=pair_patents,
    )

    assert span_violations == 0
    # 4 edges: every non-VP gets exactly one manager.
    assert len(edges) == 4
    edge_map = {e.report_id: e.manager_id for e in edges}
    assert edge_map[dir_a.person_id] == vp.person_id
    assert edge_map[dir_b.person_id] == vp.person_id
    assert edge_map[mgr_a.person_id] == dir_a.person_id
    assert edge_map[mgr_b.person_id] == dir_b.person_id
    # VP is the cluster root → counted as the lone "skipped_no_candidate".
    assert skipped == 1


@pytest.mark.unit
def test_build_cluster_hierarchy_prevents_cycles() -> None:
    """Two members at the same seniority cannot manage each other.

    Same-seniority pairs are excluded from the candidate set entirely
    (manager.seniority must be strictly greater than report.seniority),
    so this scenario produces zero edges — no cycle even attempted. We
    still assert it explicitly because cycle prevention is the whole
    point of the union-find machinery."""
    a = _member(pid=1, title="Director", seniority=60)
    b = _member(pid=2, title="Director", seniority=60)

    edges, _, _ = _build_cluster_hierarchy([a, b])

    assert edges == []
    # Belt-and-braces: even if scoring pathology surfaced a candidate, the
    # union-find pre-check would reject the second edge of any 2-cycle.
    seen_pairs = {(e.manager_id, e.report_id) for e in edges}
    for manager_id, report_id in seen_pairs:
        assert (report_id, manager_id) not in seen_pairs


@pytest.mark.unit
def test_build_cluster_hierarchy_span_cap_during_assignment() -> None:
    """1 Director (60) + 13 Engineers (50). Director's tier cap is 10.

    Under the global algorithm the 13 highest-confidence candidates are
    all (Director → Engineer_i) pairs with identical score. The first
    10 land; the remaining 3 are rejected at the span check, producing
    span_violations_resolved == 3 and 3 orphaned engineers."""
    director = _member(pid=1, title="Director Engineering", seniority=60)
    engineers = [
        _member(pid=10 + i, title="Software Engineer", seniority=50)
        for i in range(13)
    ]

    edges, skipped, span_violations = _build_cluster_hierarchy(
        [director, *engineers],
    )

    assert span_violations == 3
    director_edge_count = sum(
        1 for e in edges if e.manager_id == director.person_id
    )
    assert director_edge_count == SPAN_LIMITS["director"]
    # 3 orphaned engineers + 1 cluster root (Director) → skipped == 4.
    assert skipped == 4


@pytest.mark.unit
def test_build_cluster_hierarchy_preserves_ic_track() -> None:
    """A Distinguished Engineer (IC track) must NOT be assigned as the
    manager of a Director (management track), even when the seniority
    gap would otherwise allow it.

    DistEng=70 IC, Director=60 mgmt, IC-SrEng=50 IC.

    _ic_track_compatible returns False for (DistEng, Director) because
    the report is non-IC and the manager is IC — that pair is never
    even added to the candidate set. The Director should land as a
    cluster root rather than as DistEng's report."""
    dist_eng = _member(
        pid=1, title="Distinguished Engineer", seniority=70, is_ic=True,
    )
    director = _member(pid=2, title="Director Software", seniority=60)
    ic_sr_eng = _member(
        pid=3, title="Senior Software Engineer", seniority=50, is_ic=True,
    )

    edges, _, _ = _build_cluster_hierarchy([dist_eng, director, ic_sr_eng])

    # Zero edges from the Distinguished Engineer to the Director.
    forbidden = {
        (e.manager_id, e.report_id)
        for e in edges
        if e.manager_id == dist_eng.person_id and e.report_id == director.person_id
    }
    assert forbidden == set()
    # Sanity: the IC senior engineer CAN report to either DistEng or Director
    # (both are valid managers under IC-track rules). Just assert at least
    # one edge lands somewhere.
    assert any(e.report_id == ic_sr_eng.person_id for e in edges)


@pytest.mark.unit
def test_build_cluster_hierarchy_is_deterministic() -> None:
    """Same input → same output across repeated invocations.

    Critical for reproducible builds and stable diffs. The candidate
    sort uses (-confidence, manager_id, report_id) as the key, which
    is total and stable."""
    members = [
        _member(pid=1, title="VP Engineering", seniority=70),
        _member(pid=2, title="Director Hardware", seniority=60),
        _member(pid=3, title="Director Software", seniority=60),
        _member(pid=4, title="Engineering Manager", seniority=50),
        _member(pid=5, title="Engineering Manager", seniority=50),
        _member(pid=6, title="Senior Engineer", seniority=45),
    ]

    runs = [
        tuple(
            (e.manager_id, e.report_id, e.confidence)
            for e in _build_cluster_hierarchy(members)[0]
        )
        for _ in range(3)
    ]
    assert runs[0] == runs[1] == runs[2]


# ── Explicit edge ingestion ──────────────────────────────────────────────────


class _FakeTx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: Any) -> None:
        return None


class _RecordingConn:
    """Async-conn double that records every fetchrow/execute call.

    ``fetchrow_responses`` is a list of dicts (or None) returned in order
    for each ``fetchrow`` call. ``execute_calls`` accumulates (sql, args)
    tuples for assertion.
    """

    def __init__(self, fetchrow_responses: list[Any] | None = None) -> None:
        self.fetchrow_responses: list[Any] = list(fetchrow_responses or [])
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.fetchrow_calls.append((sql, args))
        if self.fetchrow_responses:
            return self.fetchrow_responses.pop(0)
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "OK"

    def transaction(self) -> _FakeTx:
        return _FakeTx()


def _patch_acquire(monkeypatch: pytest.MonkeyPatch, conn: _RecordingConn) -> None:
    class _FakeAcquire:
        async def __aenter__(self) -> _RecordingConn:
            return conn

        async def __aexit__(self, *_: Any) -> None:
            return None

    monkeypatch.setattr(hierarchy, "acquire", lambda: _FakeAcquire())


@pytest.mark.unit
async def test_ingest_explicit_edge_writes_with_explicit_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit edge writes one row with `inference_method = 'explicit_<type>'`."""
    # No existing current edge (first fetchrow returns None).
    conn = _RecordingConn(fetchrow_responses=[None])
    _patch_acquire(monkeypatch, conn)

    manager = UUID("00000000-0000-0000-0000-aaaa00000001")
    report = UUID("00000000-0000-0000-0000-bbbb00000002")

    await ingest_explicit_edge(
        manager_id=manager,
        report_id=report,
        account_id=ACCOUNT,
        signal_type="job_posting",
        confidence=0.88,
    )

    # One execute call (the INSERT) — no UPDATE because no existing row.
    assert len(conn.execute_calls) == 1
    sql, args = conn.execute_calls[0]
    assert "INSERT INTO org_reporting_edges" in sql
    # New positional order: account_id, manager_id, report_id, confidence,
    # inference_method, components_json, dominant_signal.
    assert args[0] == ACCOUNT
    assert args[1] == manager
    assert args[2] == report
    assert args[3] == 0.88
    assert args[4] == "explicit_job_posting"
    assert args[5] is None  # explicit edges set score_components=NULL
    assert args[6] == "unknown"


@pytest.mark.unit
async def test_ingest_explicit_edge_validates_inputs() -> None:
    same = UUID("00000000-0000-0000-0000-aaaa00000001")
    with pytest.raises(ValueError, match="self-reporting"):
        await ingest_explicit_edge(
            manager_id=same,
            report_id=same,
            account_id=ACCOUNT,
            signal_type="job_posting",
            confidence=0.9,
        )

    with pytest.raises(ValueError, match=r"out of \[0, 1\]"):
        await ingest_explicit_edge(
            manager_id=UUID("00000000-0000-0000-0000-aaaa00000001"),
            report_id=UUID("00000000-0000-0000-0000-bbbb00000002"),
            account_id=ACCOUNT,
            signal_type="job_posting",
            confidence=1.5,
        )


# ── Task 1-B: temporal model on edge writes ──────────────────────────────────


@pytest.mark.unit
async def test_upsert_edge_historicizes_existing_when_manager_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reingesting a different manager for the same report must:
       1. Flip the existing row to is_current=FALSE / valid_to=NOW().
       2. Insert a new is_current=TRUE row with valid_from=NOW().
    """
    report = UUID("00000000-0000-0000-0000-bbbb00000001")
    old_manager = UUID("00000000-0000-0000-0000-aaaa00000001")
    new_manager = UUID("00000000-0000-0000-0000-aaaa00000002")
    existing_row = {
        "id": UUID("00000000-0000-0000-0000-eeee00000001"),
        "manager_id": old_manager,
        "confidence": 0.72,
        "inference_method": "implicit_scoring",
    }
    conn = _RecordingConn(fetchrow_responses=[existing_row])

    edge = HierarchyEdge(
        manager_id=new_manager,
        report_id=report,
        confidence=0.81,
        inference_method="implicit_scoring",
        score_components={"seniority_gap": 0.3, "domain_match": 0.25},
        dominant_signal="seniority_gap",
    )
    await hierarchy._upsert_edge(conn, account_id=ACCOUNT, edge=edge)

    # Two execute calls: UPDATE (historicize) + INSERT (new current).
    assert len(conn.execute_calls) == 2
    update_sql, update_args = conn.execute_calls[0]
    assert "UPDATE org_reporting_edges" in update_sql
    assert "is_current = FALSE" in update_sql
    assert "valid_to" in update_sql
    assert update_args[0] == existing_row["id"]

    insert_sql, insert_args = conn.execute_calls[1]
    assert "INSERT INTO org_reporting_edges" in insert_sql
    assert "valid_from" in insert_sql
    assert insert_args[1] == new_manager
    assert insert_args[2] == report
    assert insert_args[3] == 0.81


@pytest.mark.unit
async def test_upsert_edge_skip_writes_when_no_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reingesting an edge with same manager / method / ~confidence is a no-op."""
    report = UUID("00000000-0000-0000-0000-bbbb00000001")
    manager = UUID("00000000-0000-0000-0000-aaaa00000001")
    existing_row = {
        "id": UUID("00000000-0000-0000-0000-eeee00000001"),
        "manager_id": manager,
        "confidence": 0.80,
        "inference_method": "implicit_scoring",
    }
    conn = _RecordingConn(fetchrow_responses=[existing_row])

    # Confidence 0.815 is within SKIP_WRITE_CONFIDENCE_EPSILON (0.02) of 0.80.
    edge = HierarchyEdge(
        manager_id=manager,
        report_id=report,
        confidence=0.815,
        inference_method="implicit_scoring",
        score_components=None,
        dominant_signal="seniority_gap",
    )
    await hierarchy._upsert_edge(conn, account_id=ACCOUNT, edge=edge)

    # Skip-write: zero execute calls.
    assert conn.execute_calls == []


@pytest.mark.unit
async def test_upsert_edge_inserts_when_no_existing_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh report (no existing current edge) → just INSERT, no UPDATE."""
    report = UUID("00000000-0000-0000-0000-bbbb00000001")
    manager = UUID("00000000-0000-0000-0000-aaaa00000001")
    conn = _RecordingConn(fetchrow_responses=[None])

    edge = HierarchyEdge(
        manager_id=manager,
        report_id=report,
        confidence=0.78,
        inference_method="implicit_scoring",
        score_components={"seniority_gap": 0.3},
        dominant_signal="seniority_gap",
    )
    await hierarchy._upsert_edge(conn, account_id=ACCOUNT, edge=edge)

    assert len(conn.execute_calls) == 1
    insert_sql, _ = conn.execute_calls[0]
    assert "INSERT INTO org_reporting_edges" in insert_sql


# ── Task 1-C: unknown-node stub generation ───────────────────────────────────


@pytest.mark.unit
async def test_ingest_explicit_edge_with_manager_title_creates_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When manager_title is provided (no manager_id), the edge resolves
    via _resolve_or_create_stub, confidence is haircut by 0.7, and the
    inference_method gains the _unresolved_target suffix.
    """
    company_id = UUID("00000000-0000-0000-0000-cccc00000001")
    report = UUID("00000000-0000-0000-0000-bbbb00000001")
    stub_id = UUID("00000000-0000-0000-0000-ffff00000001")

    stub_calls: list[dict[str, Any]] = []

    async def _fake_stub(
        *, company_id: UUID, title: str, account_id: UUID,
    ) -> UUID:
        stub_calls.append({
            "company_id": company_id,
            "title": title,
            "account_id": account_id,
        })
        return stub_id

    upsert_calls: list[HierarchyEdge] = []

    async def _fake_upsert(conn: Any, *, account_id: UUID, edge: HierarchyEdge) -> None:
        upsert_calls.append(edge)

    # Even with the stub + upsert mocked, ingest_explicit_edge still
    # opens an acquire() context for the upsert call's conn arg. Provide one.
    conn = _RecordingConn()
    _patch_acquire(monkeypatch, conn)
    monkeypatch.setattr(hierarchy, "_resolve_or_create_stub", _fake_stub)
    monkeypatch.setattr(hierarchy, "_upsert_edge", _fake_upsert)

    await ingest_explicit_edge(
        report_id=report,
        account_id=ACCOUNT,
        signal_type="job_posting",
        confidence=0.85,
        company_id=company_id,
        manager_title="VP of Manufacturing",
    )

    # _resolve_or_create_stub called exactly once with the right title.
    assert len(stub_calls) == 1
    assert stub_calls[0]["title"] == "VP of Manufacturing"
    assert stub_calls[0]["company_id"] == company_id
    assert stub_calls[0]["account_id"] == ACCOUNT

    # _upsert_edge called exactly once with the haircut confidence and
    # _unresolved_target suffix on the inference_method.
    assert len(upsert_calls) == 1
    edge = upsert_calls[0]
    assert edge.manager_id == stub_id
    assert edge.report_id == report
    assert edge.confidence == pytest.approx(
        0.85 * UNRESOLVED_TARGET_CONFIDENCE_FACTOR, abs=1e-6,
    )  # ≈ 0.595
    assert edge.inference_method == "explicit_job_posting_unresolved_target"
    assert edge.dominant_signal == "unknown"
    assert edge.score_components is None


@pytest.mark.unit
async def test_ingest_explicit_edge_requires_manager_id_or_title() -> None:
    """Neither manager_id nor manager_title → ValueError."""
    with pytest.raises(ValueError, match="manager_id or manager_title"):
        await ingest_explicit_edge(
            report_id=UUID("00000000-0000-0000-0000-bbbb00000001"),
            account_id=ACCOUNT,
            signal_type="job_posting",
            confidence=0.85,
        )
