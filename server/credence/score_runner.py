"""DB-backed score recomputation. Called by the worker and the
`GET /score/{prospect_id}` lazy-recompute route (Wave 6 Contract 6 —
DarkBeaver's read endpoint in api.py).

## Persistence

Each `score_prospect()` call atomically writes two rows in one transaction:

1. Legacy `scores` row, preserved for v2-era NodeInspector reads. Will be
   dropped post-cutover. The `account_id` column was added by the M1
   multitenancy migration (`20260430_v3_multitenant.sql`); this writer
   populates it so the table stays insertable once M1 enforces NOT NULL.

2. New `score_records` row keyed by `(prospect_id, weight_version_id)` per
   Contract 6. The active `weight_version_id` is resolved per-account from
   `score_weights WHERE is_active = TRUE` (exactly one active row per
   account, enforced by partial unique index from
   `20260430_v3_score_versioning.sql`).

`falsification_note` (singular TEXT) on `score_records` is the four
canonical notes joined by ``\\n``, with a ``length(trim(...)) > 0`` CHECK.
The legacy `scores.falsification_notes` stays TEXT[].

## Tenancy

`account_id` for both writes is resolved from `prospects.account_id`
(source of truth) rather than the caller's session. The route layer
authorizes "is this caller a member of the prospect's account?"; this
function is purely a persistence primitive and works the same way for
the worker (no session) and the route (has session).

## Error contract

Raises `ScoreSetupError` for configuration problems the caller cannot
fix at runtime (prospect missing, no active weight version for tenant).
The route translates this to 4xx/5xx; the worker logs and skips.
asyncpg errors propagate unchanged for transient DB failures.
"""
from __future__ import annotations

import logging
from uuid import UUID

from .db import acquire, fetch, fetchrow
from .score import ScoreResult, compute_score

log = logging.getLogger(__name__)


class ScoreSetupError(RuntimeError):
    """Raised when persistence cannot proceed because configuration is missing.

    Distinct from runtime errors so callers can distinguish "tenant has no
    active weight version — fix the seed" from "DB is down". The Contract 6
    seed runs in the migration; a missing row signals an onboarding gap
    (new account created without a weight-version seed) rather than a
    transient failure.
    """


async def load_weights() -> list[dict]:
    rows = await fetch(
        "SELECT signal_type, authenticity_weight, authority_weight, warmth_weight FROM signal_weights"
    )
    return [dict(r) for r in rows]


async def _resolve_prospect_account(prospect_id: UUID) -> UUID:
    """Read the prospect's owning account_id.

    Prospect ownership is the source of truth for score persistence — both
    the legacy `scores` and the new `score_records` row are tagged with
    this account_id. The route layer separately enforces caller membership;
    this helper just persists.
    """
    row = await fetchrow(
        "SELECT account_id FROM prospects WHERE id = $1",
        prospect_id,
    )
    if row is None:
        raise ScoreSetupError(f"prospect {prospect_id} not found")
    return UUID(str(row["account_id"]))


async def _resolve_active_weight_version(account_id: UUID) -> UUID:
    """Look up the (single) active score_weights row for an account.

    Per Contract 6, `score_weights` has a partial unique index
    `(account_id) WHERE is_active = TRUE` — exactly zero or one row matches.
    Zero is a setup bug (the migration's seed didn't run for this tenant)
    and is reported as `ScoreSetupError` rather than fallback default,
    because silently picking a default version would make score history
    impossible to audit.
    """
    row = await fetchrow(
        "SELECT id FROM score_weights WHERE account_id = $1 AND is_active = TRUE",
        account_id,
    )
    if row is None:
        raise ScoreSetupError(
            f"no active score_weights row for account {account_id}; "
            "did the Contract 6 migration seed run for this tenant?"
        )
    return UUID(str(row["id"]))


async def score_prospect(
    prospect_id: UUID,
    weights: list[dict] | None = None,
) -> ScoreResult:
    """Compute a prospect's score and persist it (dual-write, single tx).

    Workflow:
      1. Resolve the prospect's owning `account_id`
      2. Resolve the active `weight_version_id` for that account
      3. Compute from current `signals` rows + `signal_weights`
      4. Atomically write to `scores` (legacy) and `score_records` (v3)

    `score_records` write uses ``ON CONFLICT (prospect_id, weight_version_id)
    DO UPDATE`` so repeated computes for the same (prospect, version) are
    idempotent — the route's lazy-recompute path won't fire twice in a row,
    but the worker may rescore on signal updates within the same version.

    Returns the computed `ScoreResult`. The route reads this via the
    `score_records` row it just wrote rather than the return value, but the
    return makes the function usable from non-DB callers (e.g., dry-run).
    """
    if weights is None:
        weights = await load_weights()

    account_id = await _resolve_prospect_account(prospect_id)
    weight_version_id = await _resolve_active_weight_version(account_id)

    sigs = await fetch(
        "SELECT signal_type, value, weight, confidence FROM signals WHERE prospect_id = $1",
        prospect_id,
    )
    result = compute_score([dict(s) for s in sigs], weights)

    # score_records.falsification_note is singular TEXT with length>0 CHECK;
    # legacy scores.falsification_notes is TEXT[] — keep both shapes intact.
    falsification_note_joined = "\n".join(result.falsification_notes)

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO scores (account_id, prospect_id, authenticity_score, authority_score,
                                    warmth_score, overall_score, falsification_notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                account_id,
                prospect_id,
                result.authenticity_score,
                result.authority_score,
                result.warmth_score,
                result.overall_score,
                result.falsification_notes,
            )
            await conn.execute(
                """
                INSERT INTO score_records (
                    account_id, prospect_id, weight_version_id,
                    authenticity_score, authority_score, warmth_score, overall_score,
                    falsification_note
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (prospect_id, weight_version_id) DO UPDATE
                  SET authenticity_score = EXCLUDED.authenticity_score,
                      authority_score    = EXCLUDED.authority_score,
                      warmth_score       = EXCLUDED.warmth_score,
                      overall_score      = EXCLUDED.overall_score,
                      falsification_note = EXCLUDED.falsification_note,
                      computed_at        = now()
                """,
                account_id,
                prospect_id,
                weight_version_id,
                result.authenticity_score,
                result.authority_score,
                result.warmth_score,
                result.overall_score,
                falsification_note_joined,
            )

    return result
