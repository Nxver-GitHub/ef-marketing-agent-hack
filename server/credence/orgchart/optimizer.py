"""Org-chart weight optimizer (v3.1 Plan A6 — Stage 3.1).

Per V3_PT2.md L242-275: Bayesian weight tuning over the 7 implicit-scoring
component weights, driven by per-method accuracy from
``org_signal_performance`` (which A5 populates).

## Approach

This is **lightweight Bayesian-style adjustment, not gradient descent**.
The dataset is dozens of corrections, not thousands. Full ML is overkill.

The signal we actually have today is **per-method accuracy** (success /
total) tallied by ``performance.compute_method_performance``. We use that
as a global multiplier on the implicit-scoring components — high
accuracy nudges all 7 components up, low accuracy nudges them down.

A future v3.2 enhancement attributes each correction to the *decisive
component* (which one of the 7 contributed the most to that pair's
score) and updates per-component. That requires retaining the
score-breakdown at correction-time, which neither the schema nor
hierarchy.py track today. Out of scope.

## Math

```
implicit_accuracy = perf.accuracy for inference_method = 'implicit_scoring'

if implicit_accuracy < 0.6:
    # all components dialed down
    delta = -learning_rate * (0.6 - implicit_accuracy)
elif implicit_accuracy > 0.85:
    # all components dialed up
    delta = learning_rate * (implicit_accuracy - 0.85)
else:
    # in the calibration sweet-spot — no change
    delta = 0.0

for component, weight in current_weights.items():
    new_weight = clamp(weight + delta, 0.01, 0.40)

# Re-normalize so sum stays at TARGET_SUM (1.08 per V3_PT2.md L259)
factor = TARGET_SUM / sum(new_weights.values())
new_weights = {k: v * factor for k, v in new_weights.items()}
```

## Persistence

Per V3_PT2.md L272-274: write new sub_weights into
`score_weights.sub_weights` JSONB. The optimizer flips the previous
active row to `is_active = false` and inserts a fresh row carrying the
SAME top-level (authenticity_w / authority_w / warmth_w) and the
updated `sub_weights`. This piggybacks on the Contract 6 versioning so
the org-chart weight history is queryable alongside the top-level
weight history.

The flip-then-insert is in one transaction — same atomicity guarantee
as the `replace_active_score_weights` RPC.

## Trigger condition

Per V3_PT2.md L274: "Each optimizer run that changes any weight by >
0.02 inserts a new score_weights row."

When max shift across all components is ≤ 0.02, the optimizer is a
no-op (no DB write, returns the existing row's sub_weights unchanged).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from ..db import acquire, fetch, fetchrow
from .performance import compute_method_performance

logger = logging.getLogger(__name__)


# Target sum per V3_PT2.md L259 — components are nominally 1.08 (max
# implicit score is capped at 0.95 by `hierarchy._implicit_score`).
TARGET_SUM: Final[float] = 1.08

# Per-component bounds (V3_PT2.md L264).
COMPONENT_FLOOR: Final[float] = 0.01
COMPONENT_CEILING: Final[float] = 0.40

# Default learning rate (V3_PT2.md L264).
DEFAULT_LEARNING_RATE: Final[float] = 0.05

# Minimum shift to trigger a new score_weights row (V3_PT2.md L274).
MIN_SHIFT_FOR_INSERT: Final[float] = 0.02

# Accuracy bands. Below LOW → dial all components down; above HIGH → up.
# In between, no-op (the calibration sweet spot).
ACCURACY_LOW: Final[float] = 0.60
ACCURACY_HIGH: Final[float] = 0.85

# Default starting component weights — match
# `hierarchy.COMPONENT_WEIGHTS` so a fresh tenant with no prior tuning
# sees the documented v3.1 defaults.
DEFAULT_COMPONENT_WEIGHTS: Final[dict[str, float]] = {
    "seniority_gap": 0.30,
    "same_domain": 0.25,
    "same_sub_domain": 0.15,
    "manager_title": 0.10,
    "team_capacity": 0.05,
    "patent_cluster": 0.15,
    "geographic_scope": 0.08,
}


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    """Outcome of one optimizer run for one tenant."""

    account_id: UUID
    old_weights: dict[str, float]
    new_weights: dict[str, float]
    max_shift: float
    inserted_new_version: bool
    new_weight_version_id: UUID | None
    accuracy_used: float | None


# ─── Pure math (no DB) ──────────────────────────────────────────────────────


def _clamp(v: float) -> float:
    return max(COMPONENT_FLOOR, min(COMPONENT_CEILING, v))


def _delta_for_accuracy(
    accuracy: float | None,
    *,
    learning_rate: float = DEFAULT_LEARNING_RATE,
) -> float:
    """Compute the global per-component nudge from accuracy."""
    if accuracy is None:
        return 0.0
    if accuracy < ACCURACY_LOW:
        return -learning_rate * (ACCURACY_LOW - accuracy)
    if accuracy > ACCURACY_HIGH:
        return learning_rate * (accuracy - ACCURACY_HIGH)
    return 0.0


def _renormalize(
    weights: dict[str, float],
    *,
    target_sum: float = TARGET_SUM,
) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        # Pathological — return defaults
        return dict(DEFAULT_COMPONENT_WEIGHTS)
    factor = target_sum / total
    return {k: round(v * factor, 4) for k, v in weights.items()}


def compute_new_weights(
    current: dict[str, float],
    accuracy: float | None,
    *,
    learning_rate: float = DEFAULT_LEARNING_RATE,
) -> dict[str, float]:
    """Pure: return updated component weights given current + accuracy.

    Bounds each weight to [COMPONENT_FLOOR, COMPONENT_CEILING] then
    re-normalizes to TARGET_SUM. Returns a new dict — does not mutate.
    """
    delta = _delta_for_accuracy(accuracy, learning_rate=learning_rate)
    if delta == 0.0:
        # No accuracy signal worth acting on
        return dict(current)
    nudged = {k: _clamp(v + delta) for k, v in current.items()}
    return _renormalize(nudged)


def max_component_shift(
    old_weights: dict[str, float],
    new_weights: dict[str, float],
) -> float:
    """Largest absolute change across components (any key)."""
    keys = set(old_weights.keys()) | set(new_weights.keys())
    return max(
        (abs(new_weights.get(k, 0.0) - old_weights.get(k, 0.0)) for k in keys),
        default=0.0,
    )


# ─── Public API ─────────────────────────────────────────────────────────────


async def optimize_account_weights(
    account_id: UUID,
    *,
    learning_rate: float = DEFAULT_LEARNING_RATE,
) -> OptimizationResult:
    """Run one optimization pass for one tenant.

    Reads the tenant's current active `score_weights` (existing
    sub_weights), the latest `implicit_scoring` accuracy from A5, computes
    new sub_weights, and — if `max_shift > MIN_SHIFT_FOR_INSERT` — flips
    the active row + inserts a new one carrying same top-level weights
    + new sub_weights. Atomic via single transaction.

    Returns an `OptimizationResult` summarizing what happened. Idempotent:
    re-running with the same accuracy produces the same new weights and
    the second run sees max_shift = 0 (no-op).
    """
    # 1. Pull current active score_weights row
    active_row = await fetchrow(
        """
        SELECT id, authenticity_w, authority_w, warmth_w, sub_weights, created_by
        FROM score_weights
        WHERE account_id = $1 AND is_active = TRUE
        LIMIT 1
        """,
        account_id,
    )
    if active_row is None:
        # No active row — orgchart optimization needs the Contract 6 seed
        # to have run. Surface as a no-op rather than fail; the next A5
        # tally + A6 run will succeed once the seed lands.
        logger.info(
            "optimizer: no active score_weights for account %s; skipping",
            account_id,
        )
        return OptimizationResult(
            account_id=account_id,
            old_weights=dict(DEFAULT_COMPONENT_WEIGHTS),
            new_weights=dict(DEFAULT_COMPONENT_WEIGHTS),
            max_shift=0.0,
            inserted_new_version=False,
            new_weight_version_id=None,
            accuracy_used=None,
        )

    sub_weights_raw = active_row["sub_weights"]
    if isinstance(sub_weights_raw, dict) and sub_weights_raw:
        # Filter to component keyspace + coerce
        old_weights = {
            k: float(sub_weights_raw[k])
            for k in DEFAULT_COMPONENT_WEIGHTS.keys()
            if k in sub_weights_raw
        }
        # Backfill any missing components from defaults
        for k, default_v in DEFAULT_COMPONENT_WEIGHTS.items():
            old_weights.setdefault(k, default_v)
    else:
        old_weights = dict(DEFAULT_COMPONENT_WEIGHTS)

    # 2. Pull latest implicit_scoring accuracy
    perf = await compute_method_performance(account_id, "implicit_scoring")
    accuracy = perf.accuracy

    # 3. Compute new weights
    new_weights = compute_new_weights(old_weights, accuracy, learning_rate=learning_rate)
    shift = max_component_shift(old_weights, new_weights)

    if shift <= MIN_SHIFT_FOR_INSERT:
        return OptimizationResult(
            account_id=account_id,
            old_weights=old_weights,
            new_weights=new_weights,
            max_shift=shift,
            inserted_new_version=False,
            new_weight_version_id=None,
            accuracy_used=accuracy,
        )

    # 4. Flip-then-insert in one tx
    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE score_weights
                SET is_active = FALSE
                WHERE account_id = $1 AND is_active = TRUE
                """,
                account_id,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO score_weights
                  (account_id, authenticity_w, authority_w, warmth_w,
                   sub_weights, created_by, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, TRUE)
                RETURNING id
                """,
                account_id,
                float(active_row["authenticity_w"]),
                float(active_row["authority_w"]),
                float(active_row["warmth_w"]),
                _serialize_sub_weights(new_weights, old_sub=sub_weights_raw),
                f"system:orgchart_optimizer (acc={accuracy})",
            )
    new_id = UUID(str(row["id"])) if row else None

    return OptimizationResult(
        account_id=account_id,
        old_weights=old_weights,
        new_weights=new_weights,
        max_shift=shift,
        inserted_new_version=True,
        new_weight_version_id=new_id,
        accuracy_used=accuracy,
    )


def _serialize_sub_weights(
    new_components: dict[str, float],
    old_sub: dict | None,
) -> dict:
    """Merge new component weights into the existing sub_weights JSONB.

    Preserves any non-component keys the existing sub_weights might
    carry (forward-compatibility — Settings UI may eventually write
    other tunables here).
    """
    base: dict = dict(old_sub) if isinstance(old_sub, dict) else {}
    base.update(new_components)
    return base


# ─── Per-component optimizer (Task 4-B) ─────────────────────────────────────
#
# The legacy `optimize_account_weights` above applies a single uniform delta
# to all 7 components, driven by a global `implicit_scoring` accuracy. That's
# fine when we don't know *which* component drove a wrong edge.
#
# Task 4-A added `score_components` JSONB and `dominant_signal` columns to
# `org_reporting_edges`. With those populated, we can attribute each
# correction to the component that contributed the most to the bad edge —
# and nudge that component's weight harder than the others.
#
# This optimizer:
#  1. Joins corrections to their corrected edges' `dominant_signal`
#  2. Per-component error rate = corrections wrong on that component / total
#     corrections on that component
#  3. Dominant component nudge: `w * (1 - 0.15 * error_rate_dominant)`
#     Other components nudge: `w * (1 - 0.05 * error_rate_global)`
#  4. Each component is clamped to [0.01, 0.50]
#  5. Components with fewer than `min_corrections_per_component` corrections
#     fall back to the global error rate
#  6. Persists via the same flip-then-insert pattern used by the legacy
#     optimizer (preserves `score_weights` versioning history)
#  7. Tracks per-component nudges in `org_signal_performance` with
#     `inference_method = 'component:<name>'`


# Component keys per Task 4-B spec (matches Task 4-A `EdgeScore.components`).
# Defined locally — Task 4-A is in flight in another subagent and may not
# have landed `hierarchy.COMPONENT_KEYS` yet. We will not import from
# hierarchy.py until 4-A merges.
COMPONENT_KEYS: Final[tuple[str, ...]] = (
    "seniority_gap",
    "domain_match",
    "subdomain_match",
    "manager_title",
    "span_capacity",
    "patent_cluster",
    "geographic_scope",
)

PER_COMPONENT_DEFAULT_WEIGHTS: Final[dict[str, float]] = {
    "seniority_gap": 0.30,
    "domain_match": 0.25,
    "subdomain_match": 0.15,
    "manager_title": 0.10,
    "span_capacity": 0.05,
    "patent_cluster": 0.15,
    "geographic_scope": 0.08,
}

# Per-component bounds per Task 4-B spec.
PER_COMPONENT_FLOOR: Final[float] = 0.01
PER_COMPONENT_CEILING: Final[float] = 0.50

# Learning rates — dominant component is nudged 3x harder than others.
DEFAULT_LR_DOMINANT: Final[float] = 0.15
DEFAULT_LR_OTHER: Final[float] = 0.05

# Per Task 4-B: minimum corrections before per-component error_rate is used.
DEFAULT_MIN_CORRECTIONS_PER_COMPONENT: Final[int] = 5

# correction_type that means "the edge was right" (per Task 4-B spec).
RIGHT_CORRECTION_TYPE: Final[str] = "manager_correct"

PER_COMPONENT_METHOD_PREFIX: Final[str] = "component:"


@dataclass(frozen=True, slots=True)
class PerComponentNudge:
    """One component's old → new weight delta after a per-component pass."""

    component: str
    old_weight: float
    new_weight: float
    delta: float
    error_rate_used: float
    correction_count: int
    used_global_fallback: bool


def _clamp_per_component(v: float) -> float:
    return max(PER_COMPONENT_FLOOR, min(PER_COMPONENT_CEILING, v))


def _is_wrong(correction_type: str | None) -> bool:
    """Per Task 4-B: 'manager_correct' = right, anything else = wrong."""
    return correction_type != RIGHT_CORRECTION_TYPE


def _load_active_component_weights(
    sub_weights_raw: dict | None,
) -> dict[str, float]:
    """Read per-component weights out of the active score_weights row.

    Falls back to `PER_COMPONENT_DEFAULT_WEIGHTS` when sub_weights is
    empty or missing entries (fresh tenant, or pre-4-B persistence).
    """
    if not isinstance(sub_weights_raw, dict) or not sub_weights_raw:
        return dict(PER_COMPONENT_DEFAULT_WEIGHTS)
    out: dict[str, float] = {}
    for key, default_v in PER_COMPONENT_DEFAULT_WEIGHTS.items():
        raw = sub_weights_raw.get(key, default_v)
        try:
            out[key] = float(raw)
        except (TypeError, ValueError):
            out[key] = default_v
    return out


def compute_per_component_nudges(
    *,
    weights: dict[str, float],
    by_component_counts: dict[str, dict[str, int]],
    error_rate_global: float,
    learning_rate_dominant: float = DEFAULT_LR_DOMINANT,
    learning_rate_other: float = DEFAULT_LR_OTHER,
    min_corrections_per_component: int = DEFAULT_MIN_CORRECTIONS_PER_COMPONENT,
) -> list[PerComponentNudge]:
    """Pure function: produce one nudge per component.

    `by_component_counts[component] = {'right': int, 'wrong': int}`.
    Components missing from the dict are treated as zero corrections
    and use the global fallback path.
    """
    nudges: list[PerComponentNudge] = []
    for comp in COMPONENT_KEYS:
        old_w = weights.get(comp, PER_COMPONENT_DEFAULT_WEIGHTS[comp])
        bucket = by_component_counts.get(comp, {"right": 0, "wrong": 0})
        comp_total = bucket["right"] + bucket["wrong"]
        if comp_total >= min_corrections_per_component:
            error_rate = bucket["wrong"] / comp_total
            new_w = old_w * (1 - learning_rate_dominant * error_rate)
            used_global = False
        else:
            error_rate = error_rate_global
            new_w = old_w * (1 - learning_rate_other * error_rate)
            used_global = True
        new_w = _clamp_per_component(new_w)
        nudges.append(
            PerComponentNudge(
                component=comp,
                old_weight=old_w,
                new_weight=new_w,
                delta=new_w - old_w,
                error_rate_used=error_rate,
                correction_count=comp_total,
                used_global_fallback=used_global,
            )
        )
    return nudges


async def optimize_account_weights_per_component(
    account_id: UUID,
    *,
    learning_rate_dominant: float = DEFAULT_LR_DOMINANT,
    learning_rate_other: float = DEFAULT_LR_OTHER,
    min_corrections_per_component: int = DEFAULT_MIN_CORRECTIONS_PER_COMPONENT,
) -> list[PerComponentNudge]:
    """Run one per-component optimization pass for one tenant.

    Reads corrections joined to their edges' `dominant_signal`, tallies
    right/wrong per component, and produces a `PerComponentNudge` per
    component. Persists the new weights via the same flip-then-insert
    pattern as `optimize_account_weights` and writes per-component
    tracking rows to `org_signal_performance`.

    Falls back cleanly when:
    - No corrections exist → all deltas zero, no DB write
    - Edges have NULL `dominant_signal` (pre-Task 4-A rows) → JOIN
      filter excludes them; component error rates are zero; deltas zero
    - No active `score_weights` row exists → returns 7 zero-delta
      nudges from defaults, no DB write

    Sits alongside `optimize_account_weights` (the legacy global
    optimizer) — neither replaces the other. Both are exported.
    """
    rows = await fetch(
        """
        SELECT
          c.correction_type   AS correction_type,
          e.dominant_signal   AS dominant_signal
        FROM org_chart_corrections c
        JOIN org_reporting_edges e ON e.id = c.edge_id
        WHERE c.account_id = $1
          AND e.dominant_signal IS NOT NULL
        """,
        account_id,
    )

    by_component: dict[str, dict[str, int]] = defaultdict(
        lambda: {"right": 0, "wrong": 0}
    )
    global_right = 0
    global_wrong = 0
    for row in rows:
        comp = row["dominant_signal"]
        if not comp:
            continue
        wrong = _is_wrong(row["correction_type"])
        bucket = "wrong" if wrong else "right"
        by_component[comp][bucket] += 1
        if wrong:
            global_wrong += 1
        else:
            global_right += 1

    total_global = global_right + global_wrong
    error_rate_global = (global_wrong / total_global) if total_global else 0.0

    active_row = await fetchrow(
        """
        SELECT id, authenticity_w, authority_w, warmth_w, sub_weights, created_by
        FROM score_weights
        WHERE account_id = $1 AND is_active = TRUE
        LIMIT 1
        """,
        account_id,
    )

    if active_row is None:
        # No seed yet — nothing to flip. Return zero-delta defaults.
        logger.info(
            "per_component_optimizer: no active score_weights for %s; skipping",
            account_id,
        )
        weights = dict(PER_COMPONENT_DEFAULT_WEIGHTS)
        return compute_per_component_nudges(
            weights=weights,
            by_component_counts=dict(by_component),
            error_rate_global=error_rate_global,
            learning_rate_dominant=learning_rate_dominant,
            learning_rate_other=learning_rate_other,
            min_corrections_per_component=min_corrections_per_component,
        )

    sub_weights_raw = active_row["sub_weights"]
    weights = _load_active_component_weights(sub_weights_raw)

    nudges = compute_per_component_nudges(
        weights=weights,
        by_component_counts=dict(by_component),
        error_rate_global=error_rate_global,
        learning_rate_dominant=learning_rate_dominant,
        learning_rate_other=learning_rate_other,
        min_corrections_per_component=min_corrections_per_component,
    )

    max_shift = max((abs(n.delta) for n in nudges), default=0.0)
    if max_shift <= MIN_SHIFT_FOR_INSERT:
        # No meaningful change — skip the DB write but still surface the
        # nudges (they'll show zero-ish deltas) so callers can introspect.
        return nudges

    new_components = {n.component: round(n.new_weight, 6) for n in nudges}
    merged_sub = _serialize_sub_weights(new_components, old_sub=sub_weights_raw)

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE score_weights
                SET is_active = FALSE
                WHERE account_id = $1 AND is_active = TRUE
                """,
                account_id,
            )
            await conn.fetchrow(
                """
                INSERT INTO score_weights
                  (account_id, authenticity_w, authority_w, warmth_w,
                   sub_weights, created_by, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, TRUE)
                RETURNING id
                """,
                account_id,
                float(active_row["authenticity_w"]),
                float(active_row["authority_w"]),
                float(active_row["warmth_w"]),
                merged_sub,
                f"system:per_component_optimizer (n={total_global})",
            )
            # Per-component tracking rows in org_signal_performance.
            for nudge in nudges:
                method_label = f"{PER_COMPONENT_METHOD_PREFIX}{nudge.component}"
                wrong = by_component.get(
                    nudge.component, {"right": 0, "wrong": 0}
                )["wrong"]
                right = by_component.get(
                    nudge.component, {"right": 0, "wrong": 0}
                )["right"]
                accuracy = (
                    round(right / (right + wrong), 4)
                    if (right + wrong) > 0
                    else None
                )
                await conn.execute(
                    """
                    INSERT INTO org_signal_performance
                      (account_id, inference_method, success_count,
                       error_count, accuracy, last_computed_at)
                    VALUES ($1, $2, $3, $4, $5, now())
                    ON CONFLICT (account_id, inference_method) DO UPDATE
                      SET success_count = EXCLUDED.success_count,
                          error_count   = EXCLUDED.error_count,
                          accuracy      = EXCLUDED.accuracy,
                          last_computed_at = now()
                    """,
                    account_id,
                    method_label,
                    right,
                    wrong,
                    accuracy,
                )

    return nudges


__all__ = [
    "ACCURACY_HIGH",
    "ACCURACY_LOW",
    "COMPONENT_CEILING",
    "COMPONENT_FLOOR",
    "COMPONENT_KEYS",
    "DEFAULT_COMPONENT_WEIGHTS",
    "DEFAULT_LEARNING_RATE",
    "DEFAULT_LR_DOMINANT",
    "DEFAULT_LR_OTHER",
    "DEFAULT_MIN_CORRECTIONS_PER_COMPONENT",
    "MIN_SHIFT_FOR_INSERT",
    "OptimizationResult",
    "PER_COMPONENT_CEILING",
    "PER_COMPONENT_DEFAULT_WEIGHTS",
    "PER_COMPONENT_FLOOR",
    "PER_COMPONENT_METHOD_PREFIX",
    "PerComponentNudge",
    "RIGHT_CORRECTION_TYPE",
    "TARGET_SUM",
    "compute_new_weights",
    "compute_per_component_nudges",
    "max_component_shift",
    "optimize_account_weights",
    "optimize_account_weights_per_component",
]
