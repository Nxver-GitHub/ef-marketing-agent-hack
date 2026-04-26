"""Scoring engine — port of computeScore() from src/lib/mockStore.ts.

Pipeline:
  1. Load signals for a prospect + signal_weights from DB
  2. Normalize each signal's scalar value with sigmoid: 100 * (1 - exp(-v/15))
  3. Weighted sum per sub-score (Authenticity / Authority / Warmth)
  4. Overall = 0.4*A + 0.4*Au + 0.2*W
  5. Append four canonical falsification notes

Stays bug-for-bug compatible with the TS implementation so the demo numbers
match between mock and real modes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# OVERALL_WEIGHTS — same constants as src/lib/mockStore.ts:296-318
W_AUTHENTICITY = 0.4
W_AUTHORITY = 0.4
W_WARMTH = 0.2

FALSIFICATION_NOTES: list[str] = [
    "Authenticity assumes LinkedIn tenure is accurate — re-verify if profile was edited in the last 60 days.",
    "Authority cross-checks USPTO patents — invalid if patent attribution is wrong.",
    "Warmth depends on a fresh mutual-connections graph — re-sync if data is >7 days old.",
    "Role not cross-checked against Crunchbase — re-verify if prospect changed jobs in the last 30 days.",
]


@dataclass
class ScoreResult:
    authenticity_score: float
    authority_score: float
    warmth_score: float
    overall_score: float
    falsification_notes: list[str]


def _coerce_scalar(value: Any) -> float:
    """JSONB -> float, defensively. Mirrors TS `Number(s.value) || 0`.

    Surya's structured signals (career_history, education, conference_talk) have
    object-shaped values and aren't covered by signal_weights, so they short-circuit
    to 0 anyway. Numeric signals (tenure_years, patent_count, ...) come through cleanly.
    """
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    if isinstance(value, dict):
        # accept {"value": 5} convention if Surya ever emits it
        inner = value.get("value")
        if isinstance(inner, (int, float)):
            return float(inner)
    return 0.0


def _normalize(n: float) -> float:
    """Sigmoid-ish 0..100. Same shape as TS: 100 * (1 - exp(-n/15))."""
    return max(0.0, min(100.0, 100.0 * (1.0 - math.exp(-n / 15.0))))


def compute_score(
    signals: list[dict[str, Any]],
    weights: list[dict[str, Any]],
) -> ScoreResult:
    """Pure compute. signals + weights are plain dicts (asyncpg Records work too).

    weights row shape: {signal_type, authenticity_weight, authority_weight, warmth_weight}
    signals row shape: {signal_type, value, weight, confidence}
    """
    wmap: dict[str, dict[str, float]] = {
        w["signal_type"]: {
            "a": float(w["authenticity_weight"]),
            "au": float(w["authority_weight"]),
            "w": float(w["warmth_weight"]),
        }
        for w in weights
    }

    a_num = a_den = au_num = au_den = w_num = w_den = 0.0

    for s in signals:
        w = wmap.get(s["signal_type"])
        if not w:
            continue
        v = _normalize(_coerce_scalar(s.get("value")))
        base = float(s.get("weight") or 1.0) * float(s.get("confidence") or 1.0)
        a_num += v * base * w["a"]
        a_den += base * w["a"]
        au_num += v * base * w["au"]
        au_den += base * w["au"]
        w_num += v * base * w["w"]
        w_den += base * w["w"]

    a = a_num / a_den if a_den else 0.0
    au = au_num / au_den if au_den else 0.0
    wm = w_num / w_den if w_den else 0.0
    overall = W_AUTHENTICITY * a + W_AUTHORITY * au + W_WARMTH * wm

    return ScoreResult(
        authenticity_score=round(a, 1),
        authority_score=round(au, 1),
        warmth_score=round(wm, 1),
        overall_score=round(overall, 1),
        falsification_notes=list(FALSIFICATION_NOTES),
    )
