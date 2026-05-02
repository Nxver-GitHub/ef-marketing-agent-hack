"""Org-chart active sampling — surface uncertain edges for user verification.

Per `plan.md` Phase D.3: the highest-information correction is on a
low-confidence edge near a high-degree node. One verified edge at the
right place beats ten verified edges on confident leaves. This module
selects those edges and bundles the evidence operators need to judge
them — title strings, dominant scoring signal, score-component
breakdown — so the UI doesn't have to do follow-up fetches.

## Selection model

We rank candidate edges by an "uncertainty score" that combines:

  1. **Local edge confidence** — how unsure the inference engine is
     about *this specific edge*. Implicit edges below 0.55 are the
     fattest target; we don't show explicit edges (those came from
     authoritative sources, verifying them is wasted operator time).
  2. **Manager span** — how many reports the manager has. A wrong edge
     under a 12-report manager is ten times more impactful than a wrong
     edge under a 1-report stub.

The combined score is::

    uncertainty = (1.0 - confidence) * (1 + log1p(manager_span))

Lower confidence and higher span both push the score up. This is just
a ranking; absolute values don't matter. The top-K rows go to the UI.

## Why this lives in `orgchart`, not `routes`

The selection logic is the interesting piece — the FastAPI surface in
api.py just calls `select_uncertain_edges` and serializes the result.
Keeping the SQL + ranking in this module lets the unit-test suite
exercise it without spinning up FastAPI.

## Tenancy

Scoped to one `account_id` per call. Cross-tenant queries would leak
edge content; the caller is responsible for passing only their own
tenant's UUID.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import log1p
from typing import Any
from uuid import UUID

from ..db import fetch


# ── Tunables ─────────────────────────────────────────────────────────────────


# Implicit edges with confidence at or below this threshold are candidates.
# 0.55 is the natural "uncertain" band — implicit_scoring caps at 0.95 and
# floor-clamps at 0.50; everything in [0.50, 0.55] is by definition the
# weakest implicit signal.
DEFAULT_CONFIDENCE_CEILING: float = 0.55

# Hard ceiling on results returned. The UI will paginate or filter further;
# this just bounds the round trip + payload. 50 is enough that a operator
# can spend a long session reviewing without the backend having to recompute.
MAX_LIMIT: int = 50


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UncertainEdge:
    """One candidate edge, fully self-describing for the review UI.

    No `Optional` overlay — every field is populated by the SQL or carried
    as `None` for the implicit-only fields (score_components, dominant_signal)
    when an explicit edge somehow ends up in the candidate set. We exclude
    those at query time, so in practice the implicit fields are always set,
    but the type allows None for safety.
    """

    edge_id: UUID
    account_id: UUID
    manager_id: UUID
    manager_name: str | None
    manager_title: str | None
    manager_company_id: UUID | None
    report_id: UUID
    report_name: str | None
    report_title: str | None
    confidence: float
    path_confidence: float | None
    inference_method: str
    dominant_signal: str | None
    score_components: dict[str, float] | None
    manager_span: int
    uncertainty_score: float


# ── Pure ranking ─────────────────────────────────────────────────────────────


def _uncertainty_score(confidence: float, manager_span: int) -> float:
    """Pure: combine local uncertainty with downstream blast radius.

    `(1 - confidence)` is in [0, 1]. `log1p(span)` damps so a 50-report
    manager doesn't dominate a 5-report manager by 10x — span matters but
    sub-linearly. `+ 1` so a 0-span edge still gets a non-zero score,
    falling back to pure confidence ranking.
    """
    span_factor = 1.0 + log1p(max(0, manager_span))
    return (1.0 - confidence) * span_factor


# ── Public API ───────────────────────────────────────────────────────────────


async def select_uncertain_edges(
    account_id: UUID,
    *,
    limit: int = 20,
    confidence_ceiling: float = DEFAULT_CONFIDENCE_CEILING,
) -> list[UncertainEdge]:
    """Return the top-`limit` uncertain edges for one tenant, ranked.

    Filters:
      * `is_current = TRUE` (skip historized edges — operators only
        review the live chart)
      * `inference_method = 'implicit_scoring'` (skip explicit edges from
        authoritative sources — verifying those wastes time)
      * `confidence <= confidence_ceiling`
      * Edges with at least one resolvable manager + report person row

    Ranking: descending by uncertainty score. Ties broken by ascending
    confidence (lower confidence first), then ascending manager_id (for
    stable test output).
    """
    # Clamp limit defensively. The route layer should already do this but
    # the module's contract is "safe to call directly with any int".
    capped_limit = max(1, min(limit, MAX_LIMIT))

    rows = await fetch(
        """
        WITH spans AS (
            SELECT manager_id, COUNT(*) AS manager_span
            FROM org_reporting_edges
            WHERE account_id = $1 AND is_current = TRUE
            GROUP BY manager_id
        )
        SELECT
            e.id                  AS edge_id,
            e.account_id          AS account_id,
            e.manager_id,
            mp.canonical_name     AS manager_name,
            mp.current_title      AS manager_title,
            mp.current_company_id AS manager_company_id,
            e.report_id,
            rp.canonical_name     AS report_name,
            rp.current_title      AS report_title,
            e.confidence,
            e.path_confidence,
            e.inference_method,
            e.dominant_signal,
            e.score_components,
            COALESCE(s.manager_span, 0) AS manager_span
        FROM org_reporting_edges e
        LEFT JOIN persons mp ON mp.id = e.manager_id
        LEFT JOIN persons rp ON rp.id = e.report_id
        LEFT JOIN spans   s  ON s.manager_id = e.manager_id
        WHERE e.account_id = $1
          AND e.is_current = TRUE
          AND e.inference_method = 'implicit_scoring'
          AND e.confidence <= $2
        ORDER BY e.confidence ASC, e.manager_id ASC
        LIMIT $3
        """,
        account_id,
        confidence_ceiling,
        # Pull a generous candidate pool so the in-memory rerank by
        # uncertainty (which adds the span factor) has options. Capping at
        # 4× the requested limit keeps memory bounded.
        capped_limit * 4,
    )

    edges: list[UncertainEdge] = []
    for row in rows:
        confidence = float(row["confidence"])
        manager_span = int(row["manager_span"])
        score_components_raw = row["score_components"]
        # asyncpg returns JSONB as already-decoded dict via the codec wired
        # in db.py:_init_connection. Belt-and-suspenders cast in case a
        # caller wires up a different connection.
        components = (
            dict(score_components_raw)
            if isinstance(score_components_raw, dict)
            else None
        )
        edges.append(
            UncertainEdge(
                edge_id=row["edge_id"],
                account_id=row["account_id"],
                manager_id=row["manager_id"],
                manager_name=row["manager_name"],
                manager_title=row["manager_title"],
                manager_company_id=row["manager_company_id"],
                report_id=row["report_id"],
                report_name=row["report_name"],
                report_title=row["report_title"],
                confidence=confidence,
                path_confidence=(
                    float(row["path_confidence"])
                    if row["path_confidence"] is not None
                    else None
                ),
                inference_method=row["inference_method"],
                dominant_signal=row["dominant_signal"],
                score_components=components,
                manager_span=manager_span,
                uncertainty_score=_uncertainty_score(confidence, manager_span),
            )
        )

    # Re-rank by combined uncertainty score and trim to the user's limit.
    # SQL already filtered + sorted by confidence; this just folds in the
    # span factor so the UI sees the highest-impact edges first.
    edges.sort(
        key=lambda e: (-e.uncertainty_score, e.confidence, str(e.manager_id))
    )
    return edges[:capped_limit]


__all__ = [
    "DEFAULT_CONFIDENCE_CEILING",
    "MAX_LIMIT",
    "UncertainEdge",
    "_uncertainty_score",
    "select_uncertain_edges",
]
