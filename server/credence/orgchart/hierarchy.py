"""Org-chart Stage 1.2 — hierarchy inference (v3.1 Plan A, Tasks 1-A/1-B/1-C/4-A).

Per CLAUDE.md Decision 2 (functional clustering before hierarchy assignment),
Decision 3 (explicit > implicit), Decision 4 (unknown nodes rendered), and
V3_PT2.md L67-150: take the per-cluster member sets that ``clustering.py``
materializes and assign manager → report edges within each cluster.

## Two paths, never combined

**Explicit signals win.** When a job-posting parser, SEC-proxy ingester, or
press-release scraper finds a literal "X reports to Y" statement, the route
calls ``ingest_explicit_edge(...)`` which writes the edge with
``inference_method = "explicit_<signal_type>"`` and the source's own
confidence (typically 0.85-0.95).

**Implicit scoring runs only when no explicit edge exists** for the (manager,
report) pair. The scoring weights are V3_PT2.md L102-115 verbatim. The
``EdgeScore`` dataclass returned by ``_score_pair`` exposes per-component
contributions for downstream optimizer feedback (Task 4-A).

## Temporal model on edge writes (Task 1-B)

When the inference pipeline produces a new manager for a report, the
existing current edge is **historicized** (``is_current=FALSE``,
``valid_to=NOW()``) and a fresh row is inserted with ``is_current=TRUE`` and
``valid_from=NOW()``. The partial unique index
``org_edges_one_current_manager_per_report (report_id) WHERE is_current=TRUE``
excludes ``is_current=FALSE`` rows so both rows coexist after the
transaction commits. A skip-write check elides no-op rewrites
(same manager, same method, confidence within 0.02) to avoid history churn.

## Unknown node stubs (Task 1-C)

When an explicit signal references a manager by title only (e.g., "reports
to the VP of Manufacturing") and no matching person record exists, we
materialize a stub row in ``persons`` with ``is_unresolved_target=TRUE``
and ``canonical_name='[Unknown <title>]'``. The explicit edge then points
to this stub, with confidence multiplied by 0.7 and inference_method
suffixed with ``_unresolved_target``.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ..db import acquire, fetch
from ..taxonomy import is_ic_track, is_manager_title, seniority_tier

log = logging.getLogger(__name__)


# ─── Tunables ────────────────────────────────────────────────────────────────


# Implicit-scoring weight cap. CLAUDE.md L250: "Total possible implicit: 0.95
# (capped)". Setting this to 1.0 would make implicit scoring indistinguishable
# from explicit signals, which violates Decision 3 priority semantics.
IMPLICIT_SCORE_CAP: float = 0.95

# Span-of-control caps per V3_PT2.md L132-139. Keys must match the strings
# returned by ``taxonomy.seniority_tier``.
SPAN_LIMITS: dict[str, int] = {
    "c_suite": 8,
    "svp": 7,
    "vp": 8,
    "director": 10,
    "manager": 12,
}


# Component keyspace for ``EdgeScore.components`` (Task 4-A). These keys are
# the canonical taxonomy used by the migration's CHECK constraint on
# ``org_reporting_edges.dominant_signal`` (plus ``'unknown'`` for explicit
# edges that did not run implicit scoring). Order is fixed.
COMPONENT_KEYS: tuple[str, ...] = (
    "seniority_gap",
    "domain_match",
    "subdomain_match",
    "manager_title",
    "span_capacity",
    "patent_cluster",
    "geographic_scope",
)

# Confidence delta below which we treat two writes as equivalent for the
# Task 1-B skip-write check. 0.02 keeps minor numerical jitter (e.g., a
# 0.781 → 0.793 reweight) from churning the history table.
SKIP_WRITE_CONFIDENCE_EPSILON: float = 0.02

# Stub-edge confidence haircut when an explicit edge resolves through a
# title-only manager (Task 1-C). The unresolved target carries less
# information than a known person, so the explicit confidence is scaled.
UNRESOLVED_TARGET_CONFIDENCE_FACTOR: float = 0.7


# ─── Public types ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ClusterMember:
    """In-memory slice of (org_cluster_members JOIN persons JOIN employment_periods).

    Fields are exactly what the implicit scoring needs — no DB connection
    held by the dataclass itself. Constructed in the orchestrator from a
    single SQL fetch.
    """

    person_id: UUID
    account_id: UUID
    title: str | None
    seniority: int | None
    is_ic_track: bool
    sub_domain: str | None      # the cluster's sub_domain, NULL for main cluster
    inferred_team: str | None   # employment_periods.inferred_team
    patent_count: int = 0       # joined from patent_inventors


@dataclass(frozen=True, slots=True)
class EdgeScore:
    """Per-component decomposition of an implicit edge confidence.

    ``total`` is the clamped sum that the planner uses for ranking and
    threshold checks. ``components`` carries the raw contribution of each
    of the seven scoring axes — written to ``score_components`` JSONB so
    the optimizer can reweight signals against ground-truth corrections
    without re-running inference. ``dominant_component`` is the largest
    contributor (or ``'seniority_gap'`` when all components are zero,
    e.g., IC mismatch null-edges).
    """

    total: float
    components: dict[str, float]
    dominant_component: str


@dataclass(frozen=True, slots=True)
class HierarchyEdge:
    """A manager → report edge produced by the implicit scorer or an
    explicit-signal ingester.

    ``score_components`` and ``dominant_signal`` mirror the columns added
    by migration ``20260501_v3_orgchart_score_components.sql``. Implicit
    edges populate both from the ``EdgeScore`` returned by ``_score_pair``;
    explicit edges leave ``score_components=None`` and set
    ``dominant_signal='unknown'`` (per the migration CHECK keyspace).
    """

    manager_id: UUID
    report_id: UUID
    confidence: float
    inference_method: str
    score_components: dict[str, float] | None = None
    dominant_signal: str | None = None


@dataclass(frozen=True, slots=True)
class HierarchyRollup:
    """Per-cluster summary of a hierarchy pass — what was written."""

    cluster_id: UUID
    edges_written: int
    edges_skipped_no_candidate: int
    span_violations_resolved: int


# ─── Pure scoring (no DB) ────────────────────────────────────────────────────


def _seniority_gap_score(manager_seniority: int, report_seniority: int) -> float:
    """V3_PT2.md L102-105 + CLAUDE.md L253-256: gap-bucket → naturalness.

    Manager must be more senior than report (positive gap). The gap-of-zero
    case is "peers" and gets 0; reverse gap is implausible and gets 0.
    """
    gap = manager_seniority - report_seniority
    if 8 <= gap <= 15:
        return 0.30
    if 5 <= gap < 8:
        return 0.18
    if 15 < gap <= 25:
        return 0.12
    return 0.0


def _patent_cluster_score(shared_patents: int) -> float:
    """V3_PT2.md L114: patent cluster membership scales linearly to 3 shared,
    capped at +0.15. Useful for hardware-eng / research clusters where the
    inventor lattice carries strong "this team works together" signal.
    """
    if shared_patents <= 0:
        return 0.0
    return min(0.15, 0.15 * shared_patents / 3.0)


def _ic_track_compatible(manager_is_ic: bool, report_is_ic: bool) -> bool:
    """CLAUDE.md L211: a non-IC report can't have an IC manager.

    An IC report can have an IC OR non-IC manager (mixed tracks rolling up
    under a single management head is the common case). A non-IC report
    with an IC manager would suggest the IC is functioning as a people
    manager, which the parallel-ladders convention forbids.
    """
    if not report_is_ic and manager_is_ic:
        return False
    return True


def _zero_edge_score() -> EdgeScore:
    """Canonical null EdgeScore for structurally-invalid pairs.

    All component contributions are 0.0. ``dominant_component`` defaults
    to ``'seniority_gap'`` because that's the gating signal — when it's
    zero, the entire pair is rejected regardless of other components.
    """
    components = {key: 0.0 for key in COMPONENT_KEYS}
    return EdgeScore(total=0.0, components=components, dominant_component="seniority_gap")


def _score_pair(
    manager: ClusterMember,
    report: ClusterMember,
    *,
    same_sub_domain: bool,
    shared_patents: int = 0,
    geographic_compatible: bool = True,
    manager_has_capacity: bool = True,
) -> EdgeScore:
    """Compute the implicit edge confidence for a (manager, report) pair.

    Returns an ``EdgeScore`` whose ``total`` is in [0, IMPLICIT_SCORE_CAP].
    Returns the canonical zero score (all components 0.0, total 0.0) when
    the pair is structurally invalid (same person, IC-track mismatch,
    missing seniority, seniority gap implausible). The caller filters
    pairs with ``score.total`` below a minimum threshold.
    """
    if manager.person_id == report.person_id:
        return _zero_edge_score()
    if manager.seniority is None or report.seniority is None:
        return _zero_edge_score()
    if not _ic_track_compatible(manager.is_ic_track, report.is_ic_track):
        return _zero_edge_score()

    gap_score = _seniority_gap_score(manager.seniority, report.seniority)
    if gap_score == 0.0:
        # Implausible gap — short-circuit. Even other component bonuses
        # can't rescue an 0-gap or reverse-gap pair.
        return _zero_edge_score()

    components: dict[str, float] = {key: 0.0 for key in COMPONENT_KEYS}
    components["seniority_gap"] = gap_score

    # Same functional domain — clusters scope to a single domain by design,
    # so within-cluster scoring always gets +0.25.
    components["domain_match"] = 0.25

    if same_sub_domain:
        components["subdomain_match"] = 0.15

    if is_manager_title(manager.title):
        components["manager_title"] = 0.10

    if manager_has_capacity:
        components["span_capacity"] = 0.05

    components["patent_cluster"] = _patent_cluster_score(shared_patents)

    if geographic_compatible:
        components["geographic_scope"] = 0.08

    raw_total = sum(components.values())
    total = min(IMPLICIT_SCORE_CAP, raw_total)
    dominant = max(components, key=lambda k: components[k])
    return EdgeScore(total=total, components=components, dominant_component=dominant)


class _UnionFind:
    """Tiny union-find (disjoint-set) over UUIDs for cycle detection.

    Path compression on ``find``; union-by-size on ``union``. We use it
    during global edge assignment to reject any candidate edge whose
    manager and report are already in the same connected component — that
    would close a cycle in the manager→report DAG.
    """

    __slots__ = ("_parent", "_size")

    def __init__(self) -> None:
        self._parent: dict[UUID, UUID] = {}
        self._size: dict[UUID, int] = {}

    def add(self, node: UUID) -> None:
        if node not in self._parent:
            self._parent[node] = node
            self._size[node] = 1

    def find(self, node: UUID) -> UUID:
        self.add(node)
        # Iterative path compression.
        root = node
        while self._parent[root] != root:
            root = self._parent[root]
        cur = node
        while self._parent[cur] != root:
            nxt = self._parent[cur]
            self._parent[cur] = root
            cur = nxt
        return root

    def connected(self, a: UUID, b: UUID) -> bool:
        return self.find(a) == self.find(b)

    def union(self, a: UUID, b: UUID) -> bool:
        """Merge a and b's components. Returns False if they were already
        in the same component (would-be cycle), True otherwise."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        # Union by size — attach smaller under larger.
        if self._size[ra] < self._size[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        self._size[ra] += self._size[rb]
        return True


def _build_cluster_hierarchy(
    members: list[ClusterMember],
    *,
    same_sub_domain_map: dict[UUID, bool] | None = None,
    shared_patents_map: dict[tuple[UUID, UUID], int] | None = None,
    min_confidence: float = 0.45,
) -> tuple[list[HierarchyEdge], int, int]:
    """Pure planner: given a single cluster's members, produce manager→report
    edges respecting span caps, IC-track rules, and acyclicity.

    Returns ``(edges, skipped_no_candidate, span_violations)``. No DB.

    Algorithm — global constrained tree assignment (Task 1-A):
      1. Build candidate edge set: every (manager, report) pair where
         ``manager.seniority > report.seniority`` AND IC-track compatible
         AND ``_score_pair(...).total`` ≥ ``min_confidence``.
      2. Sort all candidate edges by confidence descending. Tie-break on
         (manager_id, report_id) ascending for deterministic output.
      3. Walk the sorted list; greedily accept an edge iff:
           a. The report has no manager yet (single-manager invariant).
           b. Adding it does not create a cycle (union-find check).
           c. The manager has not already hit its span cap.
         Edges rejected for span-cap reasons increment ``span_violations``.
      4. After assignment, count unmanaged members as ``skipped`` orphans.
    """
    same_sub = same_sub_domain_map or {}
    pair_patents = shared_patents_map or {}

    members_by_id = {m.person_id: m for m in members}

    # Step 1: build candidate edge set. We carry the full EdgeScore so we
    # can populate score_components on the final HierarchyEdge.
    # Each entry: (confidence, manager_id, report_id, edge_score) for stable sort.
    candidates: list[tuple[float, UUID, UUID, EdgeScore]] = []
    for manager in members:
        if manager.seniority is None:
            continue
        for report in members:
            if manager.person_id == report.person_id:
                continue
            if report.seniority is None:
                continue
            # Strict ordering: manager must be more senior.
            if manager.seniority <= report.seniority:
                continue
            if not _ic_track_compatible(manager.is_ic_track, report.is_ic_track):
                continue
            shared = pair_patents.get((manager.person_id, report.person_id), 0)
            score = _score_pair(
                manager,
                report,
                same_sub_domain=same_sub.get(manager.person_id, False)
                and same_sub.get(report.person_id, False),
                shared_patents=shared,
                manager_has_capacity=True,
            )
            if score.total < min_confidence:
                continue
            candidates.append((score.total, manager.person_id, report.person_id, score))

    # Step 2: sort by confidence desc, then by (manager_id, report_id) asc.
    candidates.sort(key=lambda t: (-t[0], t[1], t[2]))

    # Step 3: greedy assignment with single-manager + acyclicity + span checks.
    assigned_manager: dict[UUID, UUID] = {}
    span_used: dict[UUID, int] = defaultdict(int)
    uf = _UnionFind()
    for m in members:
        uf.add(m.person_id)

    edges: list[HierarchyEdge] = []
    span_violations = 0

    for confidence, manager_id, report_id, edge_score in candidates:
        # (a) Single-manager invariant.
        if report_id in assigned_manager:
            continue
        # (c) Span cap.
        manager = members_by_id[manager_id]
        if manager.seniority is None:
            continue
        cap = SPAN_LIMITS[seniority_tier(manager.seniority)]
        if span_used[manager_id] >= cap:
            span_violations += 1
            continue
        # (b) Cycle check via union-find.
        if uf.connected(manager_id, report_id):
            continue
        # Accept the edge.
        uf.union(manager_id, report_id)
        assigned_manager[report_id] = manager_id
        span_used[manager_id] += 1
        edges.append(
            HierarchyEdge(
                manager_id=manager_id,
                report_id=report_id,
                confidence=confidence,
                inference_method="implicit_scoring",
                score_components=dict(edge_score.components),
                dominant_signal=edge_score.dominant_component,
            )
        )

    skipped = sum(1 for m in members if m.person_id not in assigned_manager)
    return edges, skipped, span_violations


# ─── Public DB orchestrator API ──────────────────────────────────────────────


async def infer_cluster_hierarchy(cluster_id: UUID) -> HierarchyRollup:
    """Run hierarchy inference for one cluster.

    Loads members + their seniority + IC flag + per-pair patent counts,
    runs the pure planner, writes results to ``org_reporting_edges`` under
    a single transaction. Idempotent via the temporal-historicization
    flow in ``_upsert_edge`` (Task 1-B).
    """
    members = await _load_cluster_members(cluster_id)
    if len(members) < 2:
        log.info("hierarchy: cluster %s has %d members; skipping", cluster_id, len(members))
        return HierarchyRollup(
            cluster_id=cluster_id,
            edges_written=0,
            edges_skipped_no_candidate=0,
            span_violations_resolved=0,
        )

    member_ids = [m.person_id for m in members]
    pair_patents = await _load_pair_patent_counts(member_ids)

    is_sub_cluster = members and members[0].sub_domain is not None
    same_sub_domain_map: dict[UUID, bool] = (
        {m.person_id: True for m in members} if is_sub_cluster else {}
    )

    edges, skipped, span_violations = _build_cluster_hierarchy(
        members,
        same_sub_domain_map=same_sub_domain_map,
        shared_patents_map=pair_patents,
    )

    edges = await _filter_against_explicit_edges(edges)

    written = 0
    async with acquire() as conn:
        async with conn.transaction():
            written = await _bulk_write_edges(
                conn,
                account_id=members[0].account_id,
                edges=edges,
            )

    return HierarchyRollup(
        cluster_id=cluster_id,
        edges_written=written,
        edges_skipped_no_candidate=skipped,
        span_violations_resolved=span_violations,
    )


async def infer_company_hierarchy(company_id: UUID) -> list[HierarchyRollup]:
    """Run hierarchy across every cluster of a single company."""
    cluster_ids = await _company_cluster_ids(company_id)
    rollups: list[HierarchyRollup] = []
    for cid in cluster_ids:
        rollup = await infer_cluster_hierarchy(cid)
        rollups.append(rollup)
    return rollups


async def infer_all_hierarchies() -> list[HierarchyRollup]:
    """Run hierarchy inference across every cluster in the DB."""
    cluster_ids = await _all_cluster_ids()
    log.info("hierarchy: %d clusters eligible", len(cluster_ids))
    rollups: list[HierarchyRollup] = []
    for cid in cluster_ids:
        rollup = await infer_cluster_hierarchy(cid)
        rollups.append(rollup)
    return rollups


# ─── Explicit-edge ingestion ─────────────────────────────────────────────────


async def ingest_explicit_edge(
    *,
    report_id: UUID,
    account_id: UUID,
    signal_type: str,
    confidence: float,
    company_id: UUID | None = None,
    manager_id: UUID | None = None,
    manager_title: str | None = None,
) -> None:
    """Write an explicit edge per Decision 3 priority.

    Two resolution modes:

    1. **Resolved manager** — caller passes ``manager_id``. The edge writes
       directly with the source's confidence and ``inference_method =
       "explicit_<signal_type>"``.

    2. **Unresolved title-only manager** (Task 1-C) — caller passes
       ``manager_title`` (and ``company_id`` for the stub lookup) but no
       ``manager_id``. We materialize / reuse a stub person row keyed by
       ``(company_id, '[Unknown <title>]')`` with
       ``is_unresolved_target=TRUE``. The edge confidence is scaled by
       ``UNRESOLVED_TARGET_CONFIDENCE_FACTOR`` (0.7) and the inference
       method gains a ``_unresolved_target`` suffix so downstream
       consumers can distinguish stub edges.

    Idempotency is provided by ``_upsert_edge``'s temporal historicization.
    Re-ingesting the same explicit edge with identical fields is a no-op
    (skip-write check); reingestion with a different manager flips the
    old edge to ``is_current=FALSE`` and inserts the new current row.
    """
    if manager_id is None and not manager_title:
        raise ValueError(
            "ingest_explicit_edge requires either manager_id or manager_title"
        )
    if manager_id == report_id:
        raise ValueError("manager_id == report_id — self-reporting is invalid")
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"confidence {confidence} out of [0, 1]")

    final_confidence = confidence
    final_method = f"explicit_{signal_type}"

    if manager_id is not None:
        actual_manager_id: UUID = manager_id
    else:
        if company_id is None:
            raise ValueError(
                "ingest_explicit_edge requires company_id when resolving via manager_title"
            )
        # mypy: manager_title is non-empty per the first guard above.
        assert manager_title is not None
        actual_manager_id = await _resolve_or_create_stub(
            company_id=company_id,
            title=manager_title,
            account_id=account_id,
        )
        final_confidence = confidence * UNRESOLVED_TARGET_CONFIDENCE_FACTOR
        final_method = f"{final_method}_unresolved_target"

    edge = HierarchyEdge(
        manager_id=actual_manager_id,
        report_id=report_id,
        confidence=final_confidence,
        inference_method=final_method,
        score_components=None,
        dominant_signal="unknown",
    )
    async with acquire() as conn:
        async with conn.transaction():
            await _upsert_edge(conn, account_id=account_id, edge=edge)


# ─── DB I/O — small helpers, all mockable via monkeypatch ───────────────────


async def _load_cluster_members(cluster_id: UUID) -> list[ClusterMember]:
    """Pull org_cluster_members JOIN persons JOIN employment_periods."""
    rows = await fetch(
        """
        SELECT
          ocm.person_id                                          AS person_id,
          ocm.account_id                                         AS account_id,
          COALESCE(p.current_title, ep.title)                    AS title,
          COALESCE(p.current_seniority_score, ep.seniority_score) AS seniority,
          ocm.is_ic_track                                        AS is_ic_track,
          ofc.sub_domain                                         AS sub_domain,
          ep.inferred_team                                       AS inferred_team
        FROM org_cluster_members ocm
        JOIN org_functional_clusters ofc ON ofc.id = ocm.cluster_id
        JOIN persons p ON p.id = ocm.person_id
        LEFT JOIN employment_periods ep
          ON ep.person_id = ocm.person_id
         AND ep.company_id = ofc.company_id
         AND ep.is_current = TRUE
        WHERE ocm.cluster_id = $1
        """,
        cluster_id,
    )
    return [
        ClusterMember(
            person_id=row["person_id"],
            account_id=row["account_id"],
            title=row["title"],
            seniority=row["seniority"],
            is_ic_track=row["is_ic_track"],
            sub_domain=row["sub_domain"],
            inferred_team=row["inferred_team"],
        )
        for row in rows
    ]


async def _load_pair_patent_counts(
    person_ids: list[UUID],
) -> dict[tuple[UUID, UUID], int]:
    """Count patents jointly inventored by every pair within `person_ids`."""
    if len(person_ids) < 2:
        return {}
    rows = await fetch(
        """
        SELECT
          pi1.person_id AS a,
          pi2.person_id AS b,
          COUNT(*)      AS shared_patents
        FROM patent_inventors pi1
        JOIN patent_inventors pi2
          ON pi1.patent_id = pi2.patent_id
         AND pi1.person_id < pi2.person_id
        WHERE pi1.person_id = ANY($1::uuid[])
          AND pi2.person_id = ANY($1::uuid[])
        GROUP BY pi1.person_id, pi2.person_id
        """,
        person_ids,
    )
    out: dict[tuple[UUID, UUID], int] = {}
    for row in rows:
        a, b, count = row["a"], row["b"], int(row["shared_patents"])
        out[(a, b)] = count
        out[(b, a)] = count
    return out


async def _company_cluster_ids(company_id: UUID) -> list[UUID]:
    rows = await fetch(
        "SELECT id FROM org_functional_clusters WHERE company_id = $1",
        company_id,
    )
    return [row["id"] for row in rows]


async def _all_cluster_ids() -> list[UUID]:
    rows = await fetch("SELECT id FROM org_functional_clusters")
    return [row["id"] for row in rows]


async def _filter_against_explicit_edges(
    edges: list[HierarchyEdge],
) -> list[HierarchyEdge]:
    """Drop implicit edges where an explicit current edge already exists for
    the same report (Decision 3).
    """
    if not edges:
        return edges
    report_ids = [e.report_id for e in edges]
    rows = await fetch(
        """
        SELECT report_id
        FROM org_reporting_edges
        WHERE report_id = ANY($1::uuid[])
          AND is_current = TRUE
          AND inference_method LIKE 'explicit_%'
        """,
        report_ids,
    )
    explicit_reports = {row["report_id"] for row in rows}
    return [e for e in edges if e.report_id not in explicit_reports]


async def _resolve_or_create_stub(
    *,
    company_id: UUID,
    title: str,
    account_id: UUID,
) -> UUID:
    """Look up or materialize an unresolved-target stub person row.

    Stubs are keyed on ``(current_company_id, canonical_name)`` where the
    canonical name follows the ``[Unknown <title>]`` convention. They
    carry ``is_unresolved_target=TRUE`` so the UI and clustering pipeline
    can render them with distinct visual treatment (CLAUDE.md Decision 4).
    """
    canonical_name = f"[Unknown {title}]"
    async with acquire() as conn:
        existing = await conn.fetchrow(
            """
            SELECT id FROM persons
            WHERE current_company_id = $1
              AND canonical_name = $2
              AND is_unresolved_target = TRUE
            LIMIT 1
            """,
            company_id,
            canonical_name,
        )
        if existing is not None:
            return existing["id"]
        created = await conn.fetchrow(
            """
            INSERT INTO persons
              (canonical_name, is_unresolved_target, current_company_id,
               current_title, enrichment_tier, account_id)
            VALUES ($1, TRUE, $2, $3, 0, $4)
            RETURNING id
            """,
            canonical_name,
            company_id,
            title,
            account_id,
        )
        return created["id"]


async def _bulk_write_edges(
    conn: Any,
    *,
    account_id: UUID,
    edges: list[HierarchyEdge],
) -> int:
    """Bulk variant of `_upsert_edge` for one cluster's edge batch.

    The per-row path in `_upsert_edge` issues 1 SELECT + (0..2) writes per
    edge. At pooler latency a 7k-edge tenant takes 30+ minutes (and dies
    silently on intermittent pool pressure). This batched path collapses
    each cluster to:

      1. ONE bulk SELECT pulling every existing current edge for the
         report_ids in the batch.
      2. Pure-Python classification into skip / historicize / insert sets.
      3. ONE bulk UPDATE marking the historicize set ``is_current=FALSE``.
      4. ONE COPY-into-temp + INSERT…SELECT for the insert set.

    Same temporal-historicization semantics as the per-row path:
      - Skip-write threshold = `SKIP_WRITE_CONFIDENCE_EPSILON` on confidence,
        same manager_id, same inference_method.
      - Historicize-before-insert order preserves the partial unique index
        constraint (`one_current_manager_per_report WHERE is_current = TRUE`).

    Returns the count of edges written (insert side; skips don't count).
    The caller wraps this in `conn.transaction()` so the UPDATE and INSERT
    are atomic — the partial-unique index never sees both rows current.
    """
    if not edges:
        return 0

    # ── 1. Defensive dedupe by report_id (keep max-confidence candidate) ────
    # The partial unique index forbids two current edges per report. Inputs
    # *should* already be deduped by the planner, but we belt-and-suspender
    # so a planner regression doesn't trip a CardinalityViolation.
    by_report: dict[UUID, HierarchyEdge] = {}
    for edge in edges:
        prior = by_report.get(edge.report_id)
        if prior is None or edge.confidence > prior.confidence:
            by_report[edge.report_id] = edge
    deduped = list(by_report.values())
    report_ids = [e.report_id for e in deduped]

    # ── 2. Bulk-load existing current edges keyed by report_id ──────────────
    existing_rows = await conn.fetch(
        """
        SELECT id, manager_id, report_id, confidence, inference_method
        FROM org_reporting_edges
        WHERE is_current = TRUE
          AND report_id = ANY($1::uuid[])
        """,
        report_ids,
    )
    existing_by_report: dict[UUID, dict[str, Any]] = {
        row["report_id"]: dict(row) for row in existing_rows
    }

    # ── 3. Classify ─────────────────────────────────────────────────────────
    historicize_ids: list[UUID] = []
    inserts: list[HierarchyEdge] = []
    for edge in deduped:
        prior = existing_by_report.get(edge.report_id)
        if prior is not None:
            same_manager = prior["manager_id"] == edge.manager_id
            same_method = prior["inference_method"] == edge.inference_method
            confidence_close = (
                abs(float(prior["confidence"]) - edge.confidence)
                < SKIP_WRITE_CONFIDENCE_EPSILON
            )
            if same_manager and same_method and confidence_close:
                continue  # skip-write — no-op
            historicize_ids.append(prior["id"])
        inserts.append(edge)

    # ── 4. Bulk historicize the displaced rows ──────────────────────────────
    if historicize_ids:
        await conn.execute(
            """
            UPDATE org_reporting_edges
            SET is_current = FALSE,
                valid_to   = NOW(),
                updated_at = NOW()
            WHERE id = ANY($1::uuid[])
            """,
            historicize_ids,
        )

    # ── 5. Bulk INSERT via COPY-into-temp + INSERT…SELECT ───────────────────
    if not inserts:
        return 0

    # Temp table mirrors the columns we touch — the target table has more
    # (path_confidence, valid_to, updated_at, etc.) but those are populated
    # by defaults / triggers / later passes.
    # Note: score_components is `text` in the temp table, not `jsonb`.
    # asyncpg's binary COPY protocol doesn't have a jsonb encoder; we
    # serialize to a JSON string in Python and cast on the way out via
    # `::jsonb` in the SELECT below. Same wire-shape as the per-row path.
    await conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _edge_insert_chunk (
          account_id            uuid NOT NULL,
          manager_id            uuid NOT NULL,
          report_id             uuid NOT NULL,
          confidence            numeric NOT NULL,
          inference_method      text NOT NULL,
          score_components_json text,
          dominant_signal       text
        ) ON COMMIT DROP
        """
    )
    await conn.execute("TRUNCATE _edge_insert_chunk")

    records = [
        (
            account_id,
            edge.manager_id,
            edge.report_id,
            edge.confidence,
            edge.inference_method,
            json.dumps(edge.score_components) if edge.score_components is not None else None,
            edge.dominant_signal,
        )
        for edge in inserts
    ]
    await conn.copy_records_to_table(
        "_edge_insert_chunk",
        records=records,
        columns=[
            "account_id",
            "manager_id",
            "report_id",
            "confidence",
            "inference_method",
            "score_components_json",
            "dominant_signal",
        ],
    )

    await conn.execute(
        """
        INSERT INTO org_reporting_edges (
          account_id, manager_id, report_id, confidence,
          inference_method, is_current, valid_from,
          score_components, dominant_signal
        )
        SELECT
          account_id, manager_id, report_id, confidence,
          inference_method, TRUE, NOW(),
          score_components_json::jsonb, dominant_signal
        FROM _edge_insert_chunk
        """
    )
    return len(inserts)


async def _upsert_edge(
    conn: Any,
    *,
    account_id: UUID,
    edge: HierarchyEdge,
) -> None:
    """Write a current edge for ``edge.report_id`` with temporal history.

    Task 1-B flow:
      1. Look up the existing current edge for this report (if any).
      2. If the existing edge matches the new edge on (manager_id,
         inference_method, ~confidence) — skip the write entirely. This
         elides no-op rewrites and keeps the history table small.
      3. Otherwise: historicize the existing row (``is_current=FALSE``,
         ``valid_to=NOW()``).
      4. Insert the new row with ``is_current=TRUE``, ``valid_from=NOW()``,
         and the score-component metadata (Task 4-A columns).

    The partial unique index ``org_edges_one_current_manager_per_report``
    is defined ``WHERE is_current = TRUE``, so the historicized row drops
    out of the index before the new row is inserted — no constraint
    fight inside the transaction.
    """
    existing = await conn.fetchrow(
        """
        SELECT id, manager_id, confidence, inference_method
        FROM org_reporting_edges
        WHERE report_id = $1 AND is_current = TRUE
        """,
        edge.report_id,
    )

    if existing is not None:
        same_manager = existing["manager_id"] == edge.manager_id
        same_method = existing["inference_method"] == edge.inference_method
        confidence_close = (
            abs(float(existing["confidence"]) - edge.confidence)
            < SKIP_WRITE_CONFIDENCE_EPSILON
        )
        if same_manager and same_method and confidence_close:
            return  # No-op — skip the write entirely.

        await conn.execute(
            """
            UPDATE org_reporting_edges
            SET is_current = FALSE,
                valid_to   = NOW(),
                updated_at = NOW()
            WHERE id = $1
            """,
            existing["id"],
        )

    components_json = (
        json.dumps(edge.score_components) if edge.score_components is not None else None
    )

    await conn.execute(
        """
        INSERT INTO org_reporting_edges (
          account_id, manager_id, report_id, confidence,
          inference_method, is_current, valid_from,
          score_components, dominant_signal
        )
        VALUES ($1, $2, $3, $4, $5, TRUE, NOW(), $6::jsonb, $7)
        """,
        account_id,
        edge.manager_id,
        edge.report_id,
        edge.confidence,
        edge.inference_method,
        components_json,
        edge.dominant_signal,
    )


__all__ = [
    "COMPONENT_KEYS",
    "ClusterMember",
    "EdgeScore",
    "HierarchyEdge",
    "HierarchyRollup",
    "IMPLICIT_SCORE_CAP",
    "SKIP_WRITE_CONFIDENCE_EPSILON",
    "SPAN_LIMITS",
    "UNRESOLVED_TARGET_CONFIDENCE_FACTOR",
    "_build_cluster_hierarchy",
    "_ic_track_compatible",
    "_patent_cluster_score",
    "_resolve_or_create_stub",
    "_score_pair",
    "_seniority_gap_score",
    "infer_all_hierarchies",
    "infer_cluster_hierarchy",
    "infer_company_hierarchy",
    "ingest_explicit_edge",
]
