"""Org-chart Stage 1.4 — post-write validation (v3.1 Plan A, Task A7).

Per CLAUDE.md L188-189 ("Decision 4: Unknown nodes are rendered, not
omitted") and V3_PT2.md L132-150: walk the materialized
``org_reporting_edges`` graph and surface any constraint violations that
slipped past the write-time guards in ``hierarchy.py``.

## Why a separate validator

``hierarchy.py`` enforces span-of-control limits + IC-track rules at write
time when it's the producer of an edge. But edges can also land via:

- ``ingest_explicit_edge()`` from job-posting / SEC / press-release scrapers,
  which carry their own confidence scores and can in principle violate the
  span cap (a scraped LinkedIn ``reports_to`` page may legitimately list 15
  reports under one VP if the company actually has 15 — the cap is heuristic).
- Manual corrections via ``POST /orgchart/correction`` (SwiftElk's A4) that
  insert/override edges based on user feedback.
- Future bulk imports (CSV uploads, API integrations).

This validator runs after the fact: it catches violations as a report rather
than blocking writes. The triage UI uses the report to surface anomalies; an
admin decides whether to accept the violation (override the cap) or correct
the edge.

## Three checks

1. **Span-of-control violations** — managers with direct-report counts
   exceeding the cap for their seniority tier.
2. **Cycle detection** — DFS from every report up the manager chain to
   detect any reachable cycle (A → B → A, or longer loops).
3. **IC misclassification** — non-IC report whose current manager is on
   the IC track (CLAUDE.md L211: parallel-ladders rule).

## What it doesn't do

- **Auto-fix.** The validator emits a `ValidationReport`; the operator
  decides whether to demote one manager, re-route a report, or override
  the rule.
- **Rescore.** Edges retain their original `confidence`. A separate flag
  in `inference_method` (e.g., `inference_method LIKE 'override_%'`) is
  the conventional way to mark accepted overrides.
- **Repeat hierarchy.py's write-time enforcement.** The two are
  complementary: hierarchy.py is "best-effort don't violate," validation
  is "surface what slipped through."

## Tenancy

Reads scoped to a single ``account_id`` so cross-tenant cycles can't be
reported from one tenant's perspective. Pure-functional planner accepts
edges + persons as inputs and returns the report; orchestrator wraps with
the per-tenant fetch.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ..db import fetch
from ..taxonomy import seniority_tier
from .hierarchy import SPAN_LIMITS

log = logging.getLogger(__name__)


# ─── Public types ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SpanViolation:
    """A manager has more direct reports than their seniority tier allows."""

    manager_id: UUID
    seniority_tier: str
    direct_report_count: int
    span_cap: int


@dataclass(frozen=True, slots=True)
class CycleViolation:
    """A reporting chain that loops back on itself.

    `cycle` lists person UUIDs in the order discovered: the first id is
    where the cycle was entered (the head); the last id is the same
    person again (the close). For an A→B→A cycle, `cycle` is `[A, B, A]`.
    For triangles it's `[A, B, C, A]`. Always at least 3 entries.
    """

    cycle: list[UUID]


@dataclass(frozen=True, slots=True)
class ICMisclassification:
    """A non-IC report has an IC-track manager — violates parallel-ladders."""

    manager_id: UUID
    report_id: UUID


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Aggregate output of one validator pass.

    `is_clean` is a convenience predicate; True means no violations of any
    kind. Callers can check it before bothering to render the violation
    sections.
    """

    span_violations: list[SpanViolation] = field(default_factory=list)
    cycle_violations: list[CycleViolation] = field(default_factory=list)
    ic_violations: list[ICMisclassification] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return (
            not self.span_violations
            and not self.cycle_violations
            and not self.ic_violations
        )

    @property
    def total_violations(self) -> int:
        return (
            len(self.span_violations)
            + len(self.cycle_violations)
            + len(self.ic_violations)
        )


@dataclass(slots=True)
class _PersonInfo:
    """In-memory slice of persons + IC-track flag for the validator's input."""

    person_id: UUID
    seniority: int | None
    is_ic_track: bool


# ─── Pure validation logic (no DB) ───────────────────────────────────────────


def _check_span_violations(
    edges: list[tuple[UUID, UUID]],
    persons_by_id: dict[UUID, _PersonInfo],
) -> list[SpanViolation]:
    """Walk the manager → reports map, flag managers over the per-tier cap."""
    direct_reports: dict[UUID, int] = defaultdict(int)
    for manager_id, _report_id in edges:
        direct_reports[manager_id] += 1

    violations: list[SpanViolation] = []
    for manager_id, count in direct_reports.items():
        info = persons_by_id.get(manager_id)
        if info is None or info.seniority is None:
            # Manager seniority unknown — can't apply a tier cap. Skip
            # rather than guess; a separate "unknown seniority" surface
            # belongs in a follow-up audit.
            continue
        tier = seniority_tier(info.seniority)
        cap = SPAN_LIMITS[tier]
        if count > cap:
            violations.append(SpanViolation(
                manager_id=manager_id,
                seniority_tier=tier,
                direct_report_count=count,
                span_cap=cap,
            ))
    return violations


def _check_cycles(
    edges: list[tuple[UUID, UUID]],
) -> list[CycleViolation]:
    """DFS from every report up the manager chain.

    Algorithm: for each report node, walk the (report → manager) edge
    repeatedly; if we revisit a node already in the current walk, we have
    a cycle. Walks have an upper bound of N (number of distinct people)
    so a degenerate chain can't run forever.

    Returns one violation per distinct cycle (we deduplicate by canonical
    rotation — the cycle [A, B, C, A] and [B, C, A, B] are the same loop).
    """
    # Build report → manager (each report has at most one current manager;
    # the partial unique index enforces this on the DB side).
    manager_of: dict[UUID, UUID] = {}
    for manager_id, report_id in edges:
        manager_of[report_id] = manager_id

    seen_cycles: set[tuple[UUID, ...]] = set()
    violations: list[CycleViolation] = []

    for start in list(manager_of.keys()):
        path: list[UUID] = [start]
        path_set: set[UUID] = {start}
        node = start
        while True:
            next_node = manager_of.get(node)
            if next_node is None:
                break
            if next_node in path_set:
                # Found a cycle. Trim the prefix that's outside the loop.
                idx = path.index(next_node)
                cycle = path[idx:] + [next_node]
                # Canonicalize rotation: the cycle [A, B, C, A] and
                # [B, C, A, B] represent the same loop. Pick the rotation
                # whose minimum-id appears first.
                core = cycle[:-1]  # drop the duplicate close
                min_idx = core.index(min(core))
                rotated = core[min_idx:] + core[:min_idx]
                key = tuple(rotated)
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    violations.append(CycleViolation(cycle=rotated + [rotated[0]]))
                break
            path.append(next_node)
            path_set.add(next_node)
            node = next_node
            if len(path) > len(manager_of) + 1:
                # Defensive: we've walked further than the graph has nodes,
                # which shouldn't happen but guards against pathological
                # input.
                break

    return violations


def _check_ic_misclassifications(
    edges: list[tuple[UUID, UUID]],
    persons_by_id: dict[UUID, _PersonInfo],
) -> list[ICMisclassification]:
    """CLAUDE.md L211: a non-IC report can't have an IC-track manager."""
    violations: list[ICMisclassification] = []
    for manager_id, report_id in edges:
        manager = persons_by_id.get(manager_id)
        report = persons_by_id.get(report_id)
        if manager is None or report is None:
            continue
        if manager.is_ic_track and not report.is_ic_track:
            violations.append(ICMisclassification(
                manager_id=manager_id,
                report_id=report_id,
            ))
    return violations


def _build_validation_report(
    edges: list[tuple[UUID, UUID]],
    persons_by_id: dict[UUID, _PersonInfo],
) -> ValidationReport:
    """Pure planner: run all three checks against the input graph + persons."""
    return ValidationReport(
        span_violations=_check_span_violations(edges, persons_by_id),
        cycle_violations=_check_cycles(edges),
        ic_violations=_check_ic_misclassifications(edges, persons_by_id),
    )


# ─── Public DB orchestrator API ──────────────────────────────────────────────


async def validate_account(account_id: UUID) -> ValidationReport:
    """Run all three checks for one tenant's current reporting graph.

    Loads edges + person info via two SQL queries (`org_reporting_edges`
    + `persons` JOIN `org_cluster_members`), then runs the pure planner.
    Read-only — no writes to any DB.
    """
    edges = await _load_edges(account_id)
    persons = await _load_persons(account_id)
    persons_by_id = {p.person_id: p for p in persons}
    return _build_validation_report(edges, persons_by_id)


async def validate_all_accounts() -> dict[UUID, ValidationReport]:
    """Run validation across every tenant. Returns a per-tenant report dict."""
    account_ids = await _all_account_ids()
    log.info("validation: %d tenants eligible", len(account_ids))
    out: dict[UUID, ValidationReport] = {}
    for aid in account_ids:
        out[aid] = await validate_account(aid)
    return out


# ─── DB I/O ──────────────────────────────────────────────────────────────────


async def _load_edges(account_id: UUID) -> list[tuple[UUID, UUID]]:
    rows = await fetch(
        """
        SELECT manager_id, report_id
        FROM org_reporting_edges
        WHERE account_id = $1 AND is_current = TRUE
        """,
        account_id,
    )
    return [(row["manager_id"], row["report_id"]) for row in rows]


async def _load_persons(account_id: UUID) -> list[_PersonInfo]:
    """Build the persons-info set from cluster membership.

    A person can appear in multiple clusters (sub-cluster + main cluster);
    we dedupe on `person_id` keeping the IC flag from any membership row
    (any IC flag flags the person — a person on the IC track in one
    cluster is on the IC track period).
    """
    rows = await fetch(
        """
        SELECT DISTINCT
          p.id                       AS person_id,
          p.current_seniority_score  AS seniority,
          BOOL_OR(ocm.is_ic_track)   AS is_ic_track
        FROM persons p
        JOIN org_cluster_members ocm ON ocm.person_id = p.id
        WHERE ocm.account_id = $1
        GROUP BY p.id, p.current_seniority_score
        """,
        account_id,
    )
    return [
        _PersonInfo(
            person_id=row["person_id"],
            seniority=row["seniority"],
            is_ic_track=bool(row["is_ic_track"]),
        )
        for row in rows
    ]


async def _all_account_ids() -> list[UUID]:
    rows = await fetch(
        "SELECT DISTINCT account_id FROM org_reporting_edges",
    )
    return [row["account_id"] for row in rows]


__all__ = [
    "CycleViolation",
    "ICMisclassification",
    "SpanViolation",
    "ValidationReport",
    "_build_validation_report",
    "_check_cycles",
    "_check_ic_misclassifications",
    "_check_span_violations",
    "validate_account",
    "validate_all_accounts",
]
