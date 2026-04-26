"""DB-backed score recomputation. Called by the worker and the future
`POST /score/{prospect_id}` endpoint.
"""
from __future__ import annotations

import logging
from uuid import UUID

from .db import acquire, fetch
from .score import ScoreResult, compute_score

log = logging.getLogger(__name__)


async def load_weights() -> list[dict]:
    rows = await fetch("SELECT signal_type, authenticity_weight, authority_weight, warmth_weight FROM signal_weights")
    return [dict(r) for r in rows]


async def score_prospect(prospect_id: UUID, weights: list[dict] | None = None) -> ScoreResult:
    if weights is None:
        weights = await load_weights()

    sigs = await fetch(
        "SELECT signal_type, value, weight, confidence FROM signals WHERE prospect_id = $1",
        prospect_id,
    )

    result = compute_score([dict(s) for s in sigs], weights)

    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO scores (prospect_id, authenticity_score, authority_score,
                                warmth_score, overall_score, falsification_notes)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            prospect_id,
            result.authenticity_score,
            result.authority_score,
            result.warmth_score,
            result.overall_score,
            result.falsification_notes,
        )

    return result
