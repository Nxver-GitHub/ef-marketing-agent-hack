"""Server-side search + neighborhood queries.

Backs both:
- The chat agent's `focus_node` / `filter` / `expand_node` / `explain` tools (chat.py)
- The frontend's REST endpoints (api.py — `/search`, `/neighborhood/:id`)

Returns plain dicts so the same shape can be serialized by FastAPI or fed
into the chat tool loop as JSON.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from .db import fetch, fetchrow

# ─── focus_node ──────────────────────────────────────────────────────────────


async def focus_node(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Fuzzy match across people, companies, schools, cities (industry too).

    For v0 we just ILIKE on names. Postgres trigram (`pg_trgm` + `<%`) can
    replace this once we have it enabled — keeps the API stable.
    """
    q = f"%{query.strip()}%"
    rows = await fetch(
        """
        WITH ppl AS (
          SELECT 'person' AS kind, p.id::text AS id, p.name AS name,
                 jsonb_build_object('company', p.company, 'role', p.role,
                                    'industry', p.industry) AS extras
          FROM prospects p
          WHERE p.name ILIKE $1
          LIMIT $2
        ),
        cos AS (
          SELECT 'company' AS kind, 'co:' || lower(p.company) AS id, p.company AS name,
                 jsonb_build_object('headcount', count(*)) AS extras
          FROM prospects p
          WHERE p.company ILIKE $1
          GROUP BY p.company
          LIMIT $2
        ),
        inds AS (
          SELECT 'industry' AS kind, 'in:' || lower(p.industry) AS id, p.industry AS name,
                 jsonb_build_object('headcount', count(*)) AS extras
          FROM prospects p
          WHERE p.industry ILIKE $1
          GROUP BY p.industry
          LIMIT $2
        )
        SELECT * FROM ppl
        UNION ALL SELECT * FROM cos
        UNION ALL SELECT * FROM inds
        """,
        q,
        limit,
    )
    return [dict(r) for r in rows]


# ─── filter ──────────────────────────────────────────────────────────────────


async def filter_prospects(
    *,
    company: str | None = None,
    role: str | None = None,
    industry: str | None = None,
    min_score: float | None = None,
    name_contains: str | None = None,
    has_past_employer: str | None = None,
    has_school: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return matching prospects with their latest score.

    All filters are AND-combined. Strings use case-insensitive ILIKE so
    'tsmc' matches 'TSMC'.
    """
    where: list[str] = []
    params: list[Any] = []

    def add(clause: str, value: Any) -> None:
        params.append(value)
        where.append(clause.replace("$?", f"${len(params)}"))

    if company:
        add("p.company ILIKE $?", f"%{company}%")
    if role:
        add("p.role ILIKE $?", f"%{role}%")
    if industry:
        add("p.industry ILIKE $?", f"%{industry}%")
    if name_contains:
        add("p.name ILIKE $?", f"%{name_contains}%")
    if has_past_employer:
        add(
            "EXISTS (SELECT 1 FROM unnest(p.past_companies) pc WHERE pc ILIKE $?)",
            f"%{has_past_employer}%",
        )
    if has_school:
        add(
            "EXISTS (SELECT 1 FROM jsonb_array_elements(p.education) e WHERE e->>'school' ILIKE $?)",
            f"%{has_school}%",
        )
    if min_score is not None:
        add("s.overall_score >= $?", min_score)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    limit_n = len(params)

    sql = f"""
    SELECT
      p.id, p.name, p.company, p.role, p.industry,
      p.past_companies, p.education, p.talks,
      s.authenticity_score, s.authority_score, s.warmth_score, s.overall_score
    FROM prospects_enriched p
    LEFT JOIN LATERAL (
      SELECT * FROM scores WHERE prospect_id = p.id
      ORDER BY computed_at DESC LIMIT 1
    ) s ON TRUE
    {where_sql}
    ORDER BY s.overall_score DESC NULLS LAST, p.name
    LIMIT ${limit_n}
    """
    rows = await fetch(sql, *params)
    return [dict(r) for r in rows]


# ─── explain ─────────────────────────────────────────────────────────────────


async def explain_prospect(prospect_id: UUID) -> dict[str, Any] | None:
    """Rich bundle for the right-rail inspector + chat answers.

    Returns: identity, sub-scores, falsification notes, top signals (by confidence).
    """
    row = await fetchrow(
        """
        SELECT
          p.id, p.name, p.company, p.role, p.industry, p.linkedin_url,
          p.past_companies, p.education, p.talks, p.career_history,
          s.authenticity_score, s.authority_score, s.warmth_score, s.overall_score,
          s.falsification_notes, s.computed_at
        FROM prospects_enriched p
        LEFT JOIN LATERAL (
          SELECT * FROM scores WHERE prospect_id = p.id
          ORDER BY computed_at DESC LIMIT 1
        ) s ON TRUE
        WHERE p.id = $1
        """,
        prospect_id,
    )
    if not row:
        return None

    sigs = await fetch(
        """
        SELECT signal_type, source, value, confidence, collected_at
        FROM signals
        WHERE prospect_id = $1
        ORDER BY confidence DESC, collected_at DESC
        LIMIT 12
        """,
        prospect_id,
    )

    out = dict(row)
    out["top_signals"] = [dict(s) for s in sigs]
    return out


# ─── expand_node / neighborhood ──────────────────────────────────────────────


async def neighborhood(prospect_id: UUID, hops: int = 1) -> dict[str, Any]:
    """1-hop neighbors of a person, derived from shared edges:

    - colleagues: same company
    - co-alums:   shared past_companies entry
    - schoolmates: shared education[].school

    For hops>1 we'd recurse; for v0 we cap at 1 (covers the demo prompts).
    """
    row = await fetchrow(
        "SELECT id, name, company, past_companies, education FROM prospects_enriched WHERE id = $1",
        prospect_id,
    )
    if not row:
        return {"center": None, "neighbors": []}

    schools = [e["school"] for e in (row["education"] or []) if e.get("school")]
    past = list(row["past_companies"] or [])

    rows = await fetch(
        """
        WITH center AS (
          SELECT $1::uuid AS id
        ),
        cands AS (
          -- colleagues: same company
          SELECT p.id, p.name, p.company, p.role, 'colleague'::text AS via
          FROM prospects_enriched p
          WHERE p.id <> $1 AND lower(p.company) = lower($2)

          UNION
          -- co-alumni from a past employer
          SELECT p.id, p.name, p.company, p.role, 'past_employer'::text AS via
          FROM prospects_enriched p
          WHERE p.id <> $1
            AND EXISTS (SELECT 1 FROM unnest(p.past_companies) pc WHERE pc = ANY($3))

          UNION
          -- schoolmates
          SELECT p.id, p.name, p.company, p.role, 'education'::text AS via
          FROM prospects_enriched p, jsonb_array_elements(p.education) AS e
          WHERE p.id <> $1 AND (e->>'school') = ANY($4)
        )
        SELECT c.*, s.overall_score
        FROM cands c
        LEFT JOIN LATERAL (
          SELECT overall_score FROM scores WHERE prospect_id = c.id
          ORDER BY computed_at DESC LIMIT 1
        ) s ON TRUE
        ORDER BY s.overall_score DESC NULLS LAST
        LIMIT 60
        """,
        prospect_id,
        row["company"],
        past or [""],  # asyncpg dislikes empty arrays in some drivers
        schools or [""],
    )

    return {
        "center": dict(row),
        "neighbors": [dict(r) for r in rows],
        "hops": hops,
    }
