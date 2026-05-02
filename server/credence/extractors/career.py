"""Career-overlap extractor.

Reads from `employment_periods` (v3 schema, migration 20260430_v3_connection_graph.sql)
to find pairs where person_a and person_b worked at the same company with
overlapping date ranges. SQL adapted from CLAUDE.md L893-935.

Adaptations from CLAUDE.md's global pair-finding query:
- Constrained to two specific persons via `a.person_id = $1 AND b.person_id = $2`
  (CLAUDE.md's `a.person_id < b.person_id` was global de-dup; here irrelevant).
- `LEAST(COALESCE(a.end_year, current_year), ...)` uses the actual current year
  rather than hard-coded 2025.

Output contract — list of dicts feeding Contract 1's `structured_value` for
`signal_type` ∈ {`career_overlap_same_team`, `career_overlap_same_domain`,
`career_overlap_general`}. The `signals` route reads `signal_type` from the
dict and uses it directly (the only source whose extractor decides sub-type).

KNOWN GAP (DarkBeaver msg 36 §3): v2 `past_companies` is `string[]` — no
years. Backfilled past `employment_periods` rows have NULL years; the
`a.start_year IS NOT NULL` filter excludes them. So career-overlap on past
employers will only surface for prospects whose past jobs have year data,
which today is none of the v2 set. Returning `[]` in that case is correct
behavior under Contract 1's `connections_found: 0`.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from ..db import fetch
from .patents import PersonRef

logger = logging.getLogger(__name__)


# Tier breakpoints from CLAUDE.md L924-933.
_BASE_STRENGTH_SQL = """
  CASE
    WHEN team_a IS NOT NULL AND team_a = team_b
      THEN LEAST(0.92, 0.70 + (overlap_years * 0.03))
    WHEN domain_a = domain_b AND seniority_gap < 10
      THEN LEAST(0.80, 0.55 + (overlap_years * 0.03))
    ELSE
      LEAST(0.70, 0.40 + (overlap_years * 0.04))
  END
"""

# Sub-type classifier from CLAUDE.md L920-923.
_SIGNAL_TYPE_SQL = """
  CASE
    WHEN team_a IS NOT NULL AND team_a = team_b THEN 'career_overlap_same_team'
    WHEN domain_a = domain_b AND seniority_gap < 10 THEN 'career_overlap_same_domain'
    ELSE 'career_overlap_general'
  END
"""

# Two-person specialization of the career-overlap CTE.
# Note: $1 = person_a_id (UUID), $2 = person_b_id (UUID), $3 = current_year (int),
# $4 = max_results (int).
_CAREER_OVERLAP_SQL = f"""
WITH overlapping_pairs AS (
  SELECT
      a.company_id                                                    AS company_id,
      c.canonical_name                                                AS company_name,
      GREATEST(a.start_year, b.start_year)                            AS overlap_start,
      LEAST(COALESCE(a.end_year, $3), COALESCE(b.end_year, $3))       AS overlap_end,
      LEAST(COALESCE(a.end_year, $3), COALESCE(b.end_year, $3))
        - GREATEST(a.start_year, b.start_year)                        AS overlap_years,
      a.inferred_team                                                 AS team_a,
      b.inferred_team                                                 AS team_b,
      a.functional_domain                                             AS domain_a,
      b.functional_domain                                             AS domain_b,
      ABS(COALESCE(a.seniority_score, 0) - COALESCE(b.seniority_score, 0))
                                                                      AS seniority_gap
  FROM public.employment_periods a
  JOIN public.employment_periods b
    ON a.company_id = b.company_id
   AND a.start_year <= COALESCE(b.end_year, $3)
   AND b.start_year <= COALESCE(a.end_year, $3)
  JOIN public.companies c ON c.id = a.company_id
  WHERE a.person_id = $1
    AND b.person_id = $2
    AND a.start_year IS NOT NULL
    AND b.start_year IS NOT NULL
)
SELECT
    company_id::text                              AS company_id,
    company_name,
    overlap_start::int                            AS overlap_start_year,
    overlap_end::int                              AS overlap_end_year,
    overlap_years::int                            AS overlap_years,
    team_a,
    team_b,
    domain_a,
    domain_b,
    seniority_gap::int                            AS seniority_gap,
    {_SIGNAL_TYPE_SQL}                            AS signal_type,
    {_BASE_STRENGTH_SQL}                          AS base_strength
FROM overlapping_pairs
WHERE overlap_years >= 1
ORDER BY base_strength DESC
LIMIT $4
"""


async def find_career_overlaps(
    person_a: PersonRef,
    person_b: PersonRef,
    *,
    max_results: int = 25,
) -> list[dict[str, Any]]:
    """Return career-overlap records between two persons.

    Returns `[]` if either person has no employment_periods rows with year
    data, or if no overlap of ≥1 year exists at any shared company.
    """
    current_year = _dt.date.today().year
    try:
        rows = await fetch(
            _CAREER_OVERLAP_SQL,
            person_a.person_id,
            person_b.person_id,
            current_year,
            max_results,
        )
    except Exception:
        # The route handles partial-results: re-raise so _run_one_source
        # logs and marks "career" as failed without taking down the whole call.
        logger.exception(
            "career overlap query failed for a=%s b=%s",
            person_a.person_id,
            person_b.person_id,
        )
        raise

    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "signal_type": row["signal_type"],
                "company_id": row["company_id"],
                "company_name": row["company_name"],
                "overlap_start_year": row["overlap_start_year"],
                "overlap_end_year": row["overlap_end_year"],
                "overlap_years": row["overlap_years"],
                "team_a": row["team_a"],
                "team_b": row["team_b"],
                "domain_a": row["domain_a"],
                "domain_b": row["domain_b"],
                "seniority_gap": row["seniority_gap"],
                # Surface the SQL-computed base_strength so downstream code can
                # promote it from confidence-of-signal to strength-of-connection
                # without re-deriving the tier breakpoints.
                "base_strength": float(row["base_strength"]),
            }
        )

    logger.info(
        "find_career_overlaps: a=%s b=%s → %d hits",
        person_a.canonical_name,
        person_b.canonical_name,
        len(out),
    )
    return out
