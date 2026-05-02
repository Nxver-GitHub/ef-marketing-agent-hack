"""Org-chart corrections capture (v3.1 Plan A4 — backend half).

Per CLAUDE.md L221-224 + V3_PT2.md L184-208: every wrong-relationship
correction the user submits becomes a labeled training example for the
optimizer (A6). This module is the persistence primitive; the FastAPI
route in `api.py` calls it from the `POST /orgchart/correction` handler.

## Row shape (matches A0 schema migration)

```python
{
    "id":                UUID (auto),
    "account_id":        UUID,
    "person_a_id":       UUID,           # the report (subject of the correction)
    "person_b_id":       UUID | None,    # the manager (target), if relevant
    "edge_id":           UUID | None,    # FK into org_reporting_edges, if known
    "correction_type":   str,            # 4-value keyspace check
    "correct_value":     str | None,     # free text or JSON-encoded override
    "submitted_by":      str,            # user email (or "demo" / "service")
    "inference_method":  str | None,     # copied from the edge being corrected
    "submitted_at":      timestamptz (auto),
}
```

`correction_type` keyspace per V3_PT2.md L189-192:
- `not_reports_to` — "This person does not report to that person"
- `reports_to_other` — "This person reports to someone else" + correct_value=who
- `are_peers` — "These two people are peers, not manager/report"
- `team_wrong` — "This person's team is wrong" + correct_value=team override

## Tenancy

`account_id` for the correction row is read from the prospect's owning
account (when an `edge_id` is provided, we look up the edge's account).
This mirrors the same source-of-truth pattern `score_runner` and
`clustering` use — application-layer enforcement that corrections always
write to the correct tenant regardless of the caller's session claim.

The route layer enforces the *authorization* check (caller must be a
member of the prospect's account); this module is purely the persistence
primitive.

## Idempotency

Corrections are append-only by design — the optimizer (A6) reads
historical corrections to compute accuracy per inference_method, so
re-submitting the same correction twice records it twice. That's
intentional: a user clicking "wrong" twice on the same edge IS a
stronger signal than once.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from ..db import acquire, fetchrow

logger = logging.getLogger(__name__)


# Keyspace must match the A0 migration's CHECK constraint exactly.
VALID_CORRECTION_TYPES: Final[frozenset[str]] = frozenset({
    "not_reports_to",
    "reports_to_other",
    "are_peers",
    "team_wrong",
})


# ─── Public types ───────────────────────────────────────────────────────────


# Component-attribution keyspace — must match the CHECK constraint added by
# `20260501_v3_orgchart_correction_attributions.sql` AND the keyspace used
# in `org_reporting_edges.score_components` (so an attribution can blame the
# same component the inference engine credited at edge-write time).
VALID_ATTRIBUTION_COMPONENTS: Final[frozenset[str]] = frozenset({
    "seniority_gap",
    "domain_match",
    "subdomain_match",
    "manager_title",
    "span_capacity",
    "patent_cluster",
    "geographic_scope",
})


@dataclass(frozen=True, slots=True)
class CorrectionInput:
    """All fields the route layer hands to `record_correction`.

    Validation happens in the constructor — invalid correction_type,
    missing person_a_id, etc. raise ValueError before the DB is touched.
    """

    person_a_id: UUID
    correction_type: str
    submitted_by: str
    person_b_id: UUID | None = None
    edge_id: UUID | None = None
    correct_value: str | None = None
    component_attributions: dict[str, float] | None = None

    def __post_init__(self) -> None:
        if self.correction_type not in VALID_CORRECTION_TYPES:
            raise ValueError(
                f"correction_type must be one of {sorted(VALID_CORRECTION_TYPES)}, "
                f"got {self.correction_type!r}"
            )
        if not self.submitted_by or not self.submitted_by.strip():
            raise ValueError("submitted_by must be a non-empty string")
        # Type-specific shape checks: `reports_to_other` and `team_wrong`
        # require a `correct_value`; the other two don't.
        if self.correction_type in {"reports_to_other", "team_wrong"} and not self.correct_value:
            raise ValueError(
                f"correction_type={self.correction_type!r} requires correct_value"
            )
        # Validate the component_attributions shape early (the DB CHECK is
        # the safety net; failing here gives the route a clean 400 with a
        # specific message instead of a Postgres constraint violation).
        if self.component_attributions is not None:
            if not isinstance(self.component_attributions, dict):
                raise ValueError(
                    "component_attributions must be a dict[str, float] or None"
                )
            unknown = set(self.component_attributions) - VALID_ATTRIBUTION_COMPONENTS
            if unknown:
                raise ValueError(
                    f"component_attributions contains unknown keys "
                    f"{sorted(unknown)}; valid keys: "
                    f"{sorted(VALID_ATTRIBUTION_COMPONENTS)}"
                )
            for key, value in self.component_attributions.items():
                if not isinstance(value, (int, float)):
                    raise ValueError(
                        f"component_attributions[{key!r}] must be a number, "
                        f"got {type(value).__name__}"
                    )
                if not (0.0 <= float(value) <= 1.0):
                    raise ValueError(
                        f"component_attributions[{key!r}]={value} must be in [0, 1]"
                    )


class CorrectionPersistError(RuntimeError):
    """Raised when persistence fails for a reason the caller might want to handle.

    Distinct from generic asyncpg / DB errors; the route translates this
    to 4xx based on the specific subclass.
    """


class EdgeNotFoundError(CorrectionPersistError):
    """The edge_id provided doesn't exist in org_reporting_edges."""


# ─── Public API ─────────────────────────────────────────────────────────────


async def record_correction(correction: CorrectionInput) -> UUID:
    """Persist one correction row. Returns the new row's UUID.

    Resolves `account_id` and `inference_method` from the referenced edge
    (when `edge_id` is given) — both fields are derivative of the edge,
    not caller-controlled. This is the same source-of-truth pattern as
    score_runner.score_prospect.

    Raises:
      EdgeNotFoundError — `edge_id` provided but no row found
      CorrectionPersistError — DB returned no rows after INSERT (rare)
    """
    account_id: UUID | None = None
    inference_method: str | None = None

    if correction.edge_id is not None:
        edge_row = await fetchrow(
            """
            SELECT account_id, inference_method
            FROM org_reporting_edges
            WHERE id = $1
            """,
            correction.edge_id,
        )
        if edge_row is None:
            raise EdgeNotFoundError(
                f"edge {correction.edge_id} not found in org_reporting_edges"
            )
        account_id = UUID(str(edge_row["account_id"]))
        inference_method = edge_row["inference_method"]
    else:
        # No edge_id — we need to derive account_id from the prospect.
        # Use the same person→account resolution as score_runner.
        person_row = await fetchrow(
            """
            SELECT account_id
            FROM persons
            WHERE id = $1
            """,
            correction.person_a_id,
        )
        if person_row is None:
            raise CorrectionPersistError(
                f"person {correction.person_a_id} not found"
            )
        account_id = UUID(str(person_row["account_id"]))

    # Encode the per-component attribution dict to JSON if present. asyncpg
    # binds dict→jsonb automatically when the column codec is registered
    # (see credence/db.py:_init_connection), so we pass the dict raw rather
    # than json.dumps()ing it ourselves — saves an encode/decode round-trip
    # and keeps the bind type uniform with score_components elsewhere.
    component_attributions = correction.component_attributions

    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO org_chart_corrections
              (account_id, person_a_id, person_b_id, edge_id,
               correction_type, correct_value, submitted_by, inference_method,
               component_attributions)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            account_id,
            correction.person_a_id,
            correction.person_b_id,
            correction.edge_id,
            correction.correction_type,
            correction.correct_value,
            correction.submitted_by,
            inference_method,
            component_attributions,
        )

    if row is None:
        raise CorrectionPersistError(
            "INSERT returned no rows — DB constraint or RLS may have rejected"
        )
    return UUID(str(row["id"]))


async def list_corrections_for_method(
    inference_method: str,
    *,
    limit: int = 100,
) -> list[dict]:
    """Read corrections by inference_method — used by the A5 nightly job.

    Returns a list of plain dicts (one per row). Order: most-recent first,
    capped by `limit`. The optimizer reads this to compute accuracy per
    method; A5 doesn't need full corrections, just counts.
    """
    rows = await fetchrow(
        """
        SELECT id, person_a_id, person_b_id, edge_id, correction_type,
               correct_value, submitted_by, submitted_at
        FROM org_chart_corrections
        WHERE inference_method = $1
        ORDER BY submitted_at DESC
        LIMIT $2
        """,
        inference_method,
        limit,
    )
    return list(rows) if rows else []


__all__ = [
    "CorrectionInput",
    "CorrectionPersistError",
    "EdgeNotFoundError",
    "VALID_CORRECTION_TYPES",
    "list_corrections_for_method",
    "record_correction",
]
