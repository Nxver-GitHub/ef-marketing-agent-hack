"""Org-chart performance tracker (v3.1 Plan A5 — Stage 2.2).

Per V3_PT2.md L211-235: nightly job that turns
``org_chart_corrections`` rows into per-inference-method accuracy
estimates in ``org_signal_performance``.

## What it does

Walks every distinct `inference_method` that has produced edges in
`org_reporting_edges`. For each, counts:
- **success**: edges with `inference_method` X that have NO correction row
  pointing at them (no user has flagged them wrong)
- **error**: edges with `inference_method` X that have ≥1 correction row
  with `correction_type` ∈ {`not_reports_to`, `reports_to_other`,
  `are_peers`} — those are the three types that say the edge itself is
  wrong. `team_wrong` is a different kind of error (team labeling, not
  the manager-report edge), so it's tallied separately and contributes
  half-weight per V3_PT2.md L227 "edge not corrected = assumed correct".

Computes accuracy = success / (success + error), upserts one row per
(account_id, inference_method) into `org_signal_performance`.

## How A6 (optimizer) reads it

The Bayesian optimizer (V3_PT2.md L242-274) reads `accuracy` per
inference_method to decide which scoring components to up/down-weight.
This module's job is purely the tally — no weight-tuning logic here.

## Trigger conditions

Per V3_PT2.md L215: "After N corrections accumulate (threshold: 20)".
The implementation supports:
- `compute_method_performance(account_id, inference_method)` — single
  method, computed on demand (used by A6 ad hoc + tests)
- `compute_all_account_performance(account_id)` — every distinct method
  for one tenant, used by the nightly job
- A simple env-gated threshold check via `MIN_CORRECTIONS_FOR_TALLY`
  (default 20) so callers can short-circuit when there's not enough
  signal yet

## Idempotency

Upsert keyed `(account_id, inference_method)` — re-running the job
overwrites the previous tally with fresh counts. `last_computed_at`
bumps to `now()` on every run.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from ..db import acquire, fetch

logger = logging.getLogger(__name__)


# Threshold below which we don't bother upserting a performance row —
# accuracy on N<20 corrections is too noisy to inform A6's weight tuning.
# Override via env for tests / debug.
MIN_CORRECTIONS_FOR_TALLY: Final[int] = int(
    os.environ.get("ORGCHART_MIN_CORRECTIONS_FOR_TALLY", "20")
)


# `correction_type` values that indicate the edge ITSELF is wrong (vs the
# team labeling). `team_wrong` is a different kind of error and gets
# half-weight in the tally per V3_PT2.md L227 reasoning.
EDGE_WRONG_TYPES: Final[frozenset[str]] = frozenset({
    "not_reports_to", "reports_to_other", "are_peers",
})
TEAM_WRONG_TYPES: Final[frozenset[str]] = frozenset({"team_wrong"})


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MethodPerformance:
    """One performance tally for a (account_id, inference_method) pair."""

    account_id: UUID
    inference_method: str
    success_count: int
    error_count: int

    @property
    def accuracy(self) -> float | None:
        total = self.success_count + self.error_count
        if total == 0:
            return None
        return round(self.success_count / total, 4)

    @property
    def total(self) -> int:
        return self.success_count + self.error_count

    @property
    def below_tally_threshold(self) -> bool:
        return self.total < MIN_CORRECTIONS_FOR_TALLY


# ─── Pure tally logic ───────────────────────────────────────────────────────


def _compute_counts(
    *,
    edge_count: int,
    edge_wrong_corrections: int,
    team_wrong_corrections: int,
) -> tuple[int, int]:
    """Return (success_count, error_count) for one inference_method tally.

    - `edge_count` = total edges with this inference_method for the tenant
    - `edge_wrong_corrections` = count of corrections with type ∈ EDGE_WRONG_TYPES
      that point at edges with this method
    - `team_wrong_corrections` = count of corrections with type ∈ TEAM_WRONG_TYPES
      pointing at edges with this method (half-weight)

    success_count = edges that have NO correction row at all (V3_PT2.md
    L227 "edge not corrected = assumed correct")
    error_count = edge_wrong_corrections + (team_wrong / 2), clamped to
    integer floor to keep DB CHECK (counts are NOT NULL int) happy.
    """
    error_count = edge_wrong_corrections + (team_wrong_corrections // 2)
    success_count = max(0, edge_count - error_count)
    return success_count, error_count


# ─── Public API ─────────────────────────────────────────────────────────────


async def compute_method_performance(
    account_id: UUID,
    inference_method: str,
) -> MethodPerformance:
    """Compute the tally for one (account, inference_method) pair.

    Reads only — does NOT upsert. Used by tests + A6 ad-hoc. The nightly
    job uses `compute_all_account_performance` which both computes and
    upserts.
    """
    counts = await fetch(
        """
        SELECT
          (SELECT count(*)
             FROM org_reporting_edges
             WHERE account_id = $1
               AND inference_method = $2)                            AS edge_count,
          (SELECT count(*)
             FROM org_chart_corrections occ
             JOIN org_reporting_edges ore ON ore.id = occ.edge_id
             WHERE occ.account_id = $1
               AND ore.inference_method = $2
               AND occ.correction_type = ANY($3))                    AS edge_wrong,
          (SELECT count(*)
             FROM org_chart_corrections occ
             JOIN org_reporting_edges ore ON ore.id = occ.edge_id
             WHERE occ.account_id = $1
               AND ore.inference_method = $2
               AND occ.correction_type = ANY($4))                    AS team_wrong
        """,
        account_id,
        inference_method,
        list(EDGE_WRONG_TYPES),
        list(TEAM_WRONG_TYPES),
    )
    row = counts[0] if counts else {"edge_count": 0, "edge_wrong": 0, "team_wrong": 0}
    success, error = _compute_counts(
        edge_count=int(row["edge_count"]),
        edge_wrong_corrections=int(row["edge_wrong"]),
        team_wrong_corrections=int(row["team_wrong"]),
    )
    return MethodPerformance(
        account_id=account_id,
        inference_method=inference_method,
        success_count=success,
        error_count=error,
    )


async def compute_all_account_performance(
    account_id: UUID,
    *,
    upsert: bool = True,
) -> list[MethodPerformance]:
    """Tally every distinct inference_method for one tenant.

    By default also upserts each row into `org_signal_performance`.
    Pass `upsert=False` to compute without writing (useful for tests +
    dry-run audits).

    Performance rows below `MIN_CORRECTIONS_FOR_TALLY` total are computed
    + returned but NOT upserted — accuracy on a tiny sample is misleading.
    """
    method_rows = await fetch(
        """
        SELECT DISTINCT inference_method
        FROM org_reporting_edges
        WHERE account_id = $1
        """,
        account_id,
    )
    methods = [r["inference_method"] for r in method_rows]
    log.info(
        "performance: %d distinct inference_methods for account %s",
        len(methods), account_id,
    )

    out: list[MethodPerformance] = []
    upserts: list[MethodPerformance] = []
    for method in methods:
        perf = await compute_method_performance(account_id, method)
        out.append(perf)
        if upsert and not perf.below_tally_threshold:
            upserts.append(perf)

    if upsert and upserts:
        async with acquire() as conn:
            async with conn.transaction():
                for perf in upserts:
                    await _upsert_performance_row(conn, perf)

    return out


# ─── DB I/O ─────────────────────────────────────────────────────────────────


async def _upsert_performance_row(conn, perf: MethodPerformance) -> None:
    await conn.execute(
        """
        INSERT INTO org_signal_performance
          (account_id, inference_method, success_count, error_count, accuracy, last_computed_at)
        VALUES ($1, $2, $3, $4, $5, now())
        ON CONFLICT (account_id, inference_method) DO UPDATE
          SET success_count = EXCLUDED.success_count,
              error_count   = EXCLUDED.error_count,
              accuracy      = EXCLUDED.accuracy,
              last_computed_at = now()
        """,
        perf.account_id,
        perf.inference_method,
        perf.success_count,
        perf.error_count,
        perf.accuracy,
    )


log = logging.getLogger(__name__)


__all__ = [
    "EDGE_WRONG_TYPES",
    "MIN_CORRECTIONS_FOR_TALLY",
    "MethodPerformance",
    "TEAM_WRONG_TYPES",
    "compute_all_account_performance",
    "compute_method_performance",
]
