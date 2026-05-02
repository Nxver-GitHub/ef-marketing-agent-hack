"""Server-side search + neighborhood queries.

Backs both:
- The chat agent's tools (chat.py): `focus_node`, `filter`, `expand_node`,
  `explain`, `find_warm_paths`, `get_org_context`.
- The frontend's REST endpoints (api.py — `/search`, `/neighborhood/:id`).

Returns plain dicts so the same shape can be serialized by FastAPI or fed
into the chat tool loop as JSON.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from .db import fetch, fetchrow

# ─── warm-path constants ──────────────────────────────────────────────────────
#
# CHAT_TOOLS_PLAN.md Tool 1 — kept narrower than the full STRENGTH_TABLE to
# exclude `alumni_network` and `conference_co_attendee` (baseStrength < 0.5,
# noisy at scale). The chat agent can override via `connection_types`.
WARM_CONNECTION_TYPES: frozenset[str] = frozenset({
    "patent_co_inventor",
    "academic_co_author_multi",
    "academic_co_author_single",
    "career_overlap_same_team",
    "career_overlap_same_domain",
    "career_overlap_general",
    "conference_co_presenter",
    "standards_committee_peer",
    "same_phd_advisor",
    "co_board_member",
    "co_investor",
})

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


# ─── explain_company helpers ────────────────────────────────────────────────
#
# Pure helpers (no DB). Live above explain_company so unit tests can import
# them without spinning up asyncpg. Keep them small + side-effect-free.

# Map of `person_connections.connection_type` → human-readable "via" string
# used in the warm-paths summary the company-card mockup renders. Keys MUST
# match the enum values written by the extractors (see CLAUDE.md
# STRENGTH_TABLE for the canonical list).
_VIA_HUMANIZED: dict[str, str] = {
    "patent_co_inventor":         "co-invented a patent",
    "academic_co_author_multi":   "co-authored multiple papers",
    "academic_co_author_single":  "co-authored a paper",
    "career_overlap_same_team":   "worked on the same team",
    "career_overlap_same_domain": "worked in the same functional domain",
    "career_overlap_general":     "shared career history",
    "conference_co_presenter":    "co-presented at a conference",
    "standards_committee_peer":   "served on a standards committee together",
    "same_phd_advisor":           "shared a PhD advisor",
    "co_board_member":            "served on the same board",
    "co_investor":                "co-invested in a company",
    "alumni_network":             "shared an alma mater",
    "conference_co_attendee":     "co-attended a conference",
}


def _humanize_connection_type(connection_type: str | None) -> str:
    """Map a `person_connections.connection_type` to display text.

    Unknown / None types fall back to a snake-case-stripped version so the
    UI never renders a raw enum. Pure function — safe for unit tests.
    """
    if not connection_type:
        return "shared a connection"
    text = _VIA_HUMANIZED.get(connection_type)
    if text is not None:
        return text
    # Defensive fallback for any future connection_type the extractors may
    # introduce before this map is updated.
    return connection_type.replace("_", " ")


def _split_exec_name(name: str | None) -> tuple[str, str] | None:
    """Split an executive's display name into (first, last) lowercase tokens.

    Returns None when we can't extract both halves (single-word names,
    empty strings). Used to build the persons-table ILIKE match for the
    `matched_person_id` resolution. Strictly pure.
    """
    if not name:
        return None
    parts = [p for p in str(name).strip().split() if p]
    if len(parts) < 2:
        return None
    return parts[0].lower(), parts[-1].lower()


async def _match_executives_to_persons(
    executives: list[dict[str, Any]],
    company_id: UUID,
) -> dict[str, str]:
    """Resolve each executive name to a `persons.id` at this company.

    Returns a dict keyed by the original executive name → matching person
    UUID string. One bulk query — we OR together per-exec name predicates
    so we never round-trip per-executive.

    Match rule (per the company-card spec): persons row at this company
    whose `canonical_name` contains both the first AND last token of the
    executive name (case-insensitive). When multiple persons match a name
    we pick the first (deterministic via ORDER BY id).
    """
    if not executives:
        return {}

    # Build (name, first, last) tuples once; skip names we can't split.
    splits: list[tuple[str, str, str]] = []
    for exec_row in executives:
        raw_name = exec_row.get("name")
        parts = _split_exec_name(raw_name)
        if parts is None:
            continue
        splits.append((str(raw_name), parts[0], parts[1]))

    if not splits:
        return {}

    # One round-trip: pull every persons row at this company, then do the
    # token-match in Python. N here is bounded by the company headcount in
    # the persons table (typically ≤ a few thousand even for FAANG); the
    # alternative — a custom per-exec OR-clause query — was brittle to
    # parameter-index drift and offered no measurable speedup at our scale.
    rows = await fetch(
        """
        SELECT id, canonical_name
        FROM persons
        WHERE current_company_id = $1
        ORDER BY id
        """,
        company_id,
    )

    matched: dict[str, str] = {}
    for original_name, first, last in splits:
        if original_name in matched:
            continue
        for row in rows:
            cname = (row["canonical_name"] or "").lower()
            if first in cname and last in cname:
                matched[original_name] = str(row["id"])
                break
    return matched


async def explain_company(company_id_or_handle: str) -> dict[str, Any] | None:
    """Rich bundle for a company node — companion to `explain_prospect`.

    Accepts either a UUID string or a `co:<slug>` handle (the historical
    handle the GraphCanvas hands out for company nodes that pre-date the
    v3 entity-resolution layer).

    Returns ``None`` if the company can't be resolved — caller should
    surface that as a 404 to the UI.

    ## Why this lives in `search.py` next to `explain_prospect`

    Same shape, same caller. The chat dispatch (chat.py — Step 5) routes
    based on the node-id prefix. Keeping the two functions side by side
    makes the symmetry obvious to anyone tweaking either signature.

    ## Tenancy

    Reads `companies` + `company_signals` + `org_reporting_edges` +
    `persons`. RLS enforces tenant isolation on all four; this function
    doesn't filter by account_id explicitly because the caller's session
    binding handles that at the connection level.
    """
    company = await _resolve_company(company_id_or_handle)
    if company is None:
        return None

    company_id: UUID = company["id"]

    # Pull the latest 50 signals — small fixed bound so a company with
    # 1000s of historical press releases doesn't blow up the response.
    signals = await fetch(
        """
        SELECT signal_type, structured_value, confidence, fetched_at
        FROM company_signals
        WHERE company_id = $1
        ORDER BY fetched_at DESC
        LIMIT 50
        """,
        company_id,
    )

    executives = [
        dict(s["structured_value"])
        for s in signals
        if s["signal_type"] == "executive_profile"
    ]
    # Per-executive persons match → drives the green/orange dot in the UI.
    # One bulk query inside the helper.
    exec_matches = await _match_executives_to_persons(executives, company_id)
    for exec_row in executives:
        exec_row["matched_person_id"] = exec_matches.get(exec_row.get("name"))

    # Cap recent press at 10 for the chat-side payload — older items live
    # in the DB but rarely matter to a sales rep evaluating a fresh meeting.
    # Each item now carries `category` (added by the press-classifier
    # subagent — defaults to None until backfill runs).
    recent_press_signals = [
        s for s in signals if s["signal_type"] == "press_release"
    ][:10]
    recent_press: list[dict[str, Any]] = []
    for s in recent_press_signals:
        payload = dict(s["structured_value"] or {})
        payload["category"] = payload.get("category")
        recent_press.append(payload)

    # Org-chart summary — current-only edge count + the cluster count +
    # average edge confidence. Single query so we don't double-scan.
    edge_row = await fetchrow(
        """
        SELECT COUNT(*) AS edge_count,
               AVG(e.confidence) AS avg_confidence
        FROM org_reporting_edges e
        WHERE e.is_current = TRUE
          AND EXISTS (
            SELECT 1 FROM org_cluster_members ocm
            JOIN org_functional_clusters ofc ON ofc.id = ocm.cluster_id
            WHERE ocm.person_id = e.manager_id AND ofc.company_id = $1
          )
        """,
        company_id,
    )
    edge_count = int(edge_row["edge_count"]) if edge_row else 0
    avg_edge_confidence = (
        float(edge_row["avg_confidence"])
        if edge_row and edge_row["avg_confidence"] is not None
        else None
    )

    cluster_row = await fetchrow(
        "SELECT COUNT(*) AS n FROM org_functional_clusters WHERE company_id = $1",
        company_id,
    )
    cluster_count = int(cluster_row["n"]) if cluster_row else 0

    # Prospects: total + LinkedIn-enriched count, single query so we keep
    # the per-page-load latency budget.
    prospect_row = await fetchrow(
        """
        SELECT COUNT(*) AS n,
               COUNT(*) FILTER (WHERE linkedin_url IS NOT NULL) AS enriched
        FROM persons
        WHERE current_company_id = $1
        """,
        company_id,
    )
    prospect_count = int(prospect_row["n"]) if prospect_row else 0
    enriched_count = int(prospect_row["enriched"]) if prospect_row else 0
    enriched_percent = (
        round(enriched_count / prospect_count, 2) if prospect_count else 0.0
    )

    # Executives summary — total exec_profile signals + count where we
    # found a matching persons row (Bridge A populated this).
    executives_total = len(executives)
    matched_to_persons = sum(
        1 for e in executives if e.get("matched_person_id")
    )

    # Press summary — total + last_30_days. Counted off the full signals
    # table (not just the 50-cap above) so the badge stays accurate even
    # for press-heavy companies.
    press_summary_row = await fetchrow(
        """
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (
            WHERE fetched_at >= NOW() - INTERVAL '30 days'
          ) AS last_30_days
        FROM company_signals
        WHERE company_id = $1
          AND signal_type = 'press_release'
        """,
        company_id,
    )
    press_total = int(press_summary_row["total"]) if press_summary_row else 0
    press_30d = (
        int(press_summary_row["last_30_days"]) if press_summary_row else 0
    )

    # Warm paths — count + avg_strength + the strongest single edge.
    # Strategy: pre-resolve the set of person IDs at this company (single
    # indexed scan on persons.current_company_id), then probe
    # person_connections with ANY() against the indexed person_a_id /
    # person_b_id columns. This avoids the OR-with-double-JOIN plan that
    # falls back to a full sequential scan on large tenants and trips
    # the statement-timeout.
    company_persons = await fetch(
        "SELECT id FROM persons WHERE current_company_id = $1",
        company_id,
    )
    company_person_ids = [r["id"] for r in company_persons]

    warm_count = 0
    warm_avg_strength = 0.0
    strongest: dict[str, Any] | None = None

    if company_person_ids:
        # Two index-friendly scans (one per side of the symmetric edge),
        # combined as a UNION ALL inside a subquery. Each branch hits the
        # btree on `person_a_id` / `person_b_id` directly. The "cross-
        # company" filter is enforced by checking that the OTHER endpoint
        # is NOT one of our ids — also an indexed ANY() lookup.
        # No edge is counted twice because each row appears in at most one
        # branch (the branch matching its in-company endpoint side).
        warm_agg = await fetchrow(
            """
            SELECT COUNT(*) AS n, AVG(strength) AS avg_strength FROM (
                SELECT pc.computed_strength AS strength
                FROM person_connections pc
                WHERE pc.person_a_id = ANY($1::uuid[])
                  AND NOT (pc.person_b_id = ANY($1::uuid[]))
                UNION ALL
                SELECT pc.computed_strength AS strength
                FROM person_connections pc
                WHERE pc.person_b_id = ANY($1::uuid[])
                  AND NOT (pc.person_a_id = ANY($1::uuid[]))
            ) edges
            """,
            company_person_ids,
        )
        warm_count = int(warm_agg["n"]) if warm_agg else 0
        warm_avg_strength = (
            round(float(warm_agg["avg_strength"]), 2)
            if warm_agg and warm_agg["avg_strength"] is not None
            else 0.0
        )

        if warm_count > 0:
            strongest_row = await fetchrow(
                """
                SELECT pc.connection_type, pc.computed_strength,
                       pa.canonical_name AS from_name,
                       pb.canonical_name AS to_name
                FROM (
                    SELECT id, computed_strength, connection_type,
                           person_a_id, person_b_id
                    FROM person_connections
                    WHERE person_a_id = ANY($1::uuid[])
                      AND NOT (person_b_id = ANY($1::uuid[]))
                    UNION ALL
                    SELECT id, computed_strength, connection_type,
                           person_a_id, person_b_id
                    FROM person_connections
                    WHERE person_b_id = ANY($1::uuid[])
                      AND NOT (person_a_id = ANY($1::uuid[]))
                ) pc
                JOIN persons pa ON pa.id = pc.person_a_id
                JOIN persons pb ON pb.id = pc.person_b_id
                ORDER BY pc.computed_strength DESC
                LIMIT 1
                """,
                company_person_ids,
            )
            if strongest_row is not None:
                strongest = {
                    "from_name": strongest_row["from_name"],
                    "to_name":   strongest_row["to_name"],
                    "via":       _humanize_connection_type(
                        strongest_row["connection_type"]
                    ),
                    "strength":  round(
                        float(strongest_row["computed_strength"]), 2
                    ),
                }

    return {
        "company": {
            "id":                      str(company_id),
            "canonical_name":          company.get("canonical_name"),
            "description":             company.get("description"),
            "industry":                company.get("industry"),
            "industry_tags":           list(company.get("industry_tags") or []),
            "domains":                 list(company.get("domains") or []),
            "hq_city":                 company.get("hq_city"),
            "hq_state":                company.get("hq_state"),
            "hq_country":              company.get("hq_country"),
            "employee_count_estimate": company.get("employee_count_estimate"),
            "founded_year":            company.get("founded_year"),
            "partnerships":            list(company.get("partnerships") or []),
        },
        "executives":   executives,
        "executives_summary": {
            "total":              executives_total,
            "matched_to_persons": matched_to_persons,
        },
        "recent_press": recent_press,
        "press_summary": {
            "total":        press_total,
            "last_30_days": press_30d,
        },
        "org_chart": {
            "edge_count":          edge_count,
            "cluster_count":       cluster_count,
            "avg_edge_confidence": avg_edge_confidence,
            "confidence":          company.get("org_chart_confidence"),
            "last_built":          (
                company["org_chart_last_built"].isoformat()
                if company.get("org_chart_last_built") else None
            ),
        },
        "prospect_count":    prospect_count,
        "prospects": {
            "total":             prospect_count,
            "enriched_count":    enriched_count,
            "enriched_percent":  enriched_percent,
        },
        "warm_paths": {
            "count":        warm_count,
            "avg_strength": warm_avg_strength,
            "strongest":    strongest,
        },
        "enrichment_status": company.get("enrichment_status"),
        "enrichment_last_run": (
            company["enrichment_last_run"].isoformat()
            if company.get("enrichment_last_run") else None
        ),
    }


async def _resolve_company(handle: str) -> dict[str, Any] | None:
    """Resolve `co:<slug>` or a UUID string to a `companies` row.

    The `co:` form was the v0 handle convention (slugified canonical_name);
    UUIDs are the v3 entity-resolved IDs. We accept both because the
    GraphCanvas mixes them depending on which build of the demo is live.
    """
    if handle.startswith("co:"):
        slug = handle[3:].strip()
        if not slug:
            return None
        # Slug → canonical_name reverse: replace dashes with spaces and
        # ILIKE the result. ILIKE is fine here — N is small (~600 companies)
        # and we don't have trigram indexing on canonical_name yet.
        candidate = slug.replace("-", " ")
        row = await fetchrow(
            """
            SELECT * FROM companies
            WHERE canonical_name ILIKE $1
            ORDER BY (canonical_name = $2) DESC
            LIMIT 1
            """,
            candidate,
            candidate,
        )
        return dict(row) if row else None

    # UUID path — try a clean UUID parse, fall through to None on failure
    # rather than raising so the caller can return a 404.
    try:
        uuid_obj = UUID(handle)
    except (ValueError, TypeError):
        return None
    row = await fetchrow("SELECT * FROM companies WHERE id = $1", uuid_obj)
    return dict(row) if row else None


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


# ─── find_warm_paths (CHAT_TOOLS_PLAN.md Tool 1, Contract 12) ────────────────
#
# BFS over `person_connections` from a target person outward. The table is
# pre-materialized (Decision 7) and indexed on `(person_a_id, computed_strength
# DESC)` + symmetric — we exploit both indexes by querying with
# `person_a_id = ANY($ids) OR person_b_id = ANY($ids)` per hop, NOT one-row-
# per-person which would explode to N round trips.
#
# Caps:
#   - max_hops ≤ 4 (enforced; matches the plan)
#   - branches per hop ≤ 200 (strongest first) so a hub person doesn't blow up
#   - hop-1 frontier capped at 200 rows; deeper hops at 1000 to allow fan-out


_MAX_HOPS_HARD_CAP = 4
_HOP1_LIMIT = 200
_HOPK_LIMIT = 1000
_BRANCH_CAP_PER_HOP = 200
_TOP_PATHS = 10


async def _fetch_connections_bulk(
    person_ids: list[str],
    allowed_types: list[str],
    min_strength: float,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Bulk-fetch all `person_connections` rows touching any id in `person_ids`.

    Issued as UNION ALL of two index-friendly SELECTs (one per side of the
    `person_a_id < person_b_id` invariant) so each leg can use a single B-tree
    index — `idx_person_connections_a_strength (person_a_id, computed_strength
    DESC)` and `idx_person_connections_b_strength (person_b_id, computed_strength
    DESC)`. A single OR-over-both-columns query forces a bitmap-OR plan that
    can't satisfy `ORDER BY computed_strength DESC LIMIT N` from the index
    alone — under live concurrent load it tripped the txn-pooler statement
    timeout. Splitting the legs keeps both ends on the indexed fast path.

    Dedup happens in Python by `id` (a row can match both legs when both
    person_a_id and person_b_id sit in `person_ids`).
    """
    if not person_ids or not allowed_types:
        return []
    rows = await fetch(
        """
        (
          SELECT id, person_a_id, person_b_id, connection_type,
                 computed_strength, evidence_ids
          FROM person_connections
          WHERE person_a_id = ANY($1::uuid[])
            AND connection_type = ANY($2::text[])
            AND computed_strength >= $3
          ORDER BY computed_strength DESC
          LIMIT $4
        )
        UNION ALL
        (
          SELECT id, person_a_id, person_b_id, connection_type,
                 computed_strength, evidence_ids
          FROM person_connections
          WHERE person_b_id = ANY($1::uuid[])
            AND connection_type = ANY($2::text[])
            AND computed_strength >= $3
          ORDER BY computed_strength DESC
          LIMIT $4
        )
        """,
        person_ids,
        allowed_types,
        min_strength,
        limit,
    )
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        eid = r["id"]
        if eid in seen:
            continue
        seen.add(eid)
        out.append(dict(r))
    out.sort(key=lambda d: float(d["computed_strength"]), reverse=True)
    return out[:limit]


async def _fetch_persons_by_ids(ids: set[str]) -> list[dict[str, Any]]:
    """Batch lookup against `persons` for hydration of path node names."""
    if not ids:
        return []
    rows = await fetch(
        """
        SELECT id, canonical_name, current_title, current_company_id,
               current_seniority_score, current_functional_domain
        FROM persons
        WHERE id = ANY($1::uuid[])
        """,
        list(ids),
    )
    return [dict(r) for r in rows]


async def _fetch_evidence_summaries(
    edge_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Map first-edge UUID → `connection_evidence.structured_value` for that edge.

    `person_connections.evidence_ids` is a UUID[]. For each edge in `edge_ids`
    we want the first matching `connection_evidence.structured_value` (any one
    is acceptable for explanation copy — they all describe the same connection).
    Done as a single query with `unnest` so we stay in one round trip even when
    we render the full top-10.
    """
    if not edge_ids:
        return {}
    rows = await fetch(
        """
        SELECT pc.id AS edge_id,
               ce.structured_value
        FROM person_connections pc
        LEFT JOIN LATERAL (
            SELECT structured_value
            FROM connection_evidence
            WHERE id = ANY(pc.evidence_ids)
            LIMIT 1
        ) ce ON TRUE
        WHERE pc.id = ANY($1::uuid[])
        """,
        edge_ids,
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        edge_id = str(row["edge_id"])
        sv = row["structured_value"] or {}
        out[edge_id] = sv if isinstance(sv, dict) else {}
    return out


def _build_explanation(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    evidence_by_edge: dict[str, dict[str, Any]],
) -> str:
    """Render the spec'd per-connection-type explanation for the FIRST edge.

    Plan reference: lines 267–303 of CHAT_TOOLS_PLAN.md. We use the
    `connection_evidence.structured_value` (joined out of band) instead of the
    plan's nonexistent `first_edge.evidence_summary`.
    """
    if not edges:
        return "Direct connection."
    first_edge = edges[0]
    a_name = nodes[0].get("canonical_name", "Person A") if nodes else "Person A"
    b_name = nodes[1].get("canonical_name", "Person B") if len(nodes) > 1 else "Person B"
    ev = evidence_by_edge.get(str(first_edge["id"]), {}) or {}
    ctype = first_edge.get("connection_type", "")

    if ctype == "patent_co_inventor":
        return (
            f"{a_name} and {b_name} co-invented "
            f"{ev.get('patent_title', 'a patent')} "
            f"({ev.get('assignee', 'shared employer')}, "
            f"{ev.get('year', 'year unknown')})"
        )
    if ctype in ("academic_co_author_multi", "academic_co_author_single"):
        return (
            f"{a_name} and {b_name} co-authored "
            f"\"{ev.get('paper_title', 'a paper')}\" "
            f"at {ev.get('venue', 'a conference')} "
            f"({ev.get('year', 'year unknown')}, "
            f"{ev.get('citation_count', 0)} citations)"
        )
    if ctype == "standards_committee_peer":
        return (
            f"{a_name} and {b_name} served on the "
            f"{ev.get('committee', 'standards committee')} together "
            f"({ev.get('years', 'active period unknown')})"
        )
    if ctype == "conference_co_presenter":
        return (
            f"{a_name} and {b_name} co-presented at "
            f"{ev.get('event', 'a conference')} "
            f"({ev.get('year', 'year unknown')})"
        )
    if ctype in (
        "career_overlap_same_team",
        "career_overlap_same_domain",
        "career_overlap_general",
    ):
        # Live DB key for the company name is `company_name` (writer in
        # career_overlap_clustering.py); plan spec said `company`. Read
        # both for forward-compat. `overlap_start` can be NULL when the
        # employment_periods row had no start_year — render as `?` only
        # when truly missing, otherwise show the integer year.
        company = ev.get("company_name") or ev.get("company") or "a shared employer"
        ostart = ev.get("overlap_start")
        oend = ev.get("overlap_end")
        oyears = ev.get("overlap_years")
        start_str = str(ostart) if ostart is not None else "?"
        end_str = str(oend) if oend is not None else "?"
        years_str = str(oyears) if oyears is not None else "?"
        return (
            f"{a_name} and {b_name} worked together at {company} "
            f"({start_str}–{end_str}, {years_str} yr overlap)"
        )
    if ctype == "same_phd_advisor":
        return (
            f"{a_name} and {b_name} share a PhD advisor: "
            f"{ev.get('advisor_name', 'same advisor')} at "
            f"{ev.get('institution', 'their shared institution')}"
        )
    if ctype == "co_board_member":
        return (
            f"{a_name} and {b_name} sit on the board of "
            f"{ev.get('organization', 'a shared organization')} "
            f"({ev.get('years', 'active period unknown')})"
        )
    if ctype == "co_investor":
        return (
            f"{a_name} and {b_name} co-invested in "
            f"{ev.get('company', 'a shared portfolio company')} "
            f"({ev.get('round', 'round')}, {ev.get('year', 'year unknown')})"
        )
    # Long-tail fallback for any other warm type the extractors may add later.
    return (
        f"{a_name} and {b_name} have a "
        f"{ctype.replace('_', ' ')} connection"
    )


def _build_opener(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    evidence_by_edge: dict[str, dict[str, Any]],
) -> str:
    """Render the suggested first sentence of the outreach email.

    Plan reference: lines 306–333 of CHAT_TOOLS_PLAN.md. As above, evidence
    comes from the joined `connection_evidence.structured_value`.
    """
    if not edges or len(nodes) < 2:
        return ""
    connector = nodes[-1].get("canonical_name", "Your contact") or "Your contact"
    first_edge = edges[0]
    ev = evidence_by_edge.get(str(first_edge["id"]), {}) or {}
    ctype = first_edge.get("connection_type", "")

    if ctype == "patent_co_inventor":
        return (
            f"{connector} — we co-invented "
            f"{ev.get('patent_title', 'a patent')} "
            f"together at {ev.get('assignee', 'our shared employer')} "
            f"back in {ev.get('year', 'years past')}."
        )
    if ctype in ("academic_co_author_multi", "academic_co_author_single"):
        return (
            f"{connector} — we co-authored "
            f"\"{ev.get('paper_title', 'a paper')}\" "
            f"at {ev.get('venue', 'a conference')}."
        )
    if ctype == "standards_committee_peer":
        return (
            f"{connector} — we sat on the "
            f"{ev.get('committee', 'same standards committee')} together."
        )
    if ctype == "conference_co_presenter":
        return (
            f"{connector} — we co-presented at "
            f"{ev.get('event', 'a conference')} "
            f"in {ev.get('year', 'years past')}."
        )
    if ctype in (
        "career_overlap_same_team",
        "career_overlap_same_domain",
        "career_overlap_general",
    ):
        # Live key is `company_name`; spec said `company`. Read both.
        company = ev.get("company_name") or ev.get("company") or "the same company"
        return f"{connector} — we worked together at {company}."
    if ctype == "same_phd_advisor":
        return (
            f"{connector} — we both worked under "
            f"{ev.get('advisor_name', 'the same advisor')} at "
            f"{ev.get('institution', 'grad school')}."
        )
    if ctype == "co_board_member":
        return (
            f"{connector} — we serve on the board of "
            f"{ev.get('organization', 'a shared organization')} together."
        )
    if ctype == "co_investor":
        return (
            f"{connector} — we co-invested in "
            f"{ev.get('company', 'a portfolio company')} together."
        )
    return f"{connector} — we've crossed paths before and I wanted to reconnect."


async def find_warm_paths(
    target_person_id: str,
    max_hops: int = 3,
    min_strength: float = 0.30,
    connection_types: list[str] | None = None,
) -> dict[str, Any]:
    """BFS over `person_connections` from `target_person_id` outward.

    Returns up to 10 paths sorted by `path_strength` desc, each rendered with
    a connection-type-specific explanation and suggested opener.

    Algorithm (Contract 12):
      - Bounded BFS: at most `_MAX_HOPS_HARD_CAP` (4) hops.
      - Hop 1 is one bulk SQL query for the target node; deeper hops are one
        bulk query per layer covering the full frontier set. So total SQL
        round trips for the BFS itself = `min(max_hops, 4)`.
      - `path_strength` = product of edge `computed_strength` values.
      - Branches are pruned the moment `path_strength < min_strength`.
      - Cycle prevention: skip neighbors already in the path.
      - Top-200 strongest branches per hop survive to the next hop, so a
        single hub person can't explode the frontier.
      - All accumulated paths (1-hop direct connections through max_hops) are
        candidates; we dedup by terminal `connector_id` keeping the strongest
        path per connector and return the top 10.

    Then ONE bulk evidence join + ONE bulk persons hydrate before rendering.
    """
    # --- Input normalization -------------------------------------------------
    safe_max_hops = max(1, min(int(max_hops), _MAX_HOPS_HARD_CAP))
    if connection_types:
        # Honour caller filter, but quietly intersect with the warm whitelist —
        # we don't want a chat-side typo to widen scope to noisy types.
        allowed = [t for t in connection_types if t in WARM_CONNECTION_TYPES]
        if not allowed:
            allowed = list(WARM_CONNECTION_TYPES)
    else:
        allowed = list(WARM_CONNECTION_TYPES)

    # --- BFS -----------------------------------------------------------------
    # Each branch: connector_id, path_nodes, path_edges (UUID strs),
    #              path_strength (float), raw_edges (list[dict]).
    visited_per_branch_already_handled = True  # cycle check is per-path, not global
    _ = visited_per_branch_already_handled  # silence linters

    all_paths: list[dict[str, Any]] = []
    frontier: list[dict[str, Any]] = []

    # Hop 1 — direct connections to the target. Single bulk query.
    hop1_rows = await _fetch_connections_bulk(
        [target_person_id], allowed, min_strength, limit=_HOP1_LIMIT,
    )
    for conn in hop1_rows:
        a_id = str(conn["person_a_id"])
        b_id = str(conn["person_b_id"])
        neighbor = a_id if b_id == target_person_id else b_id
        strength = float(conn["computed_strength"])
        if strength < min_strength:
            continue
        branch = {
            "connector_id":  neighbor,
            "path_nodes":    [target_person_id, neighbor],
            "path_edges":    [str(conn["id"])],
            "path_strength": strength,
            "raw_edges":     [dict(conn, id=str(conn["id"]),
                                   person_a_id=a_id, person_b_id=b_id)],
        }
        all_paths.append(branch)
        frontier.append(branch)

    # Hops 2..max_hops — bulk-fetch the full frontier in ONE query per hop.
    for _hop in range(2, safe_max_hops + 1):
        if not frontier:
            break
        # Cap the surviving frontier so a hub doesn't blow up the next hop.
        frontier.sort(key=lambda b: b["path_strength"], reverse=True)
        frontier = frontier[:_BRANCH_CAP_PER_HOP]
        tip_ids = list({b["connector_id"] for b in frontier})

        rows = await _fetch_connections_bulk(
            tip_ids, allowed, min_strength, limit=_HOPK_LIMIT,
        )

        # Index edges by tip id (each tip may match person_a or person_b).
        edges_by_tip: dict[str, list[dict[str, Any]]] = {tid: [] for tid in tip_ids}
        for row in rows:
            a_id = str(row["person_a_id"])
            b_id = str(row["person_b_id"])
            if a_id in edges_by_tip:
                edges_by_tip[a_id].append(row)
            if b_id in edges_by_tip and a_id != b_id:
                edges_by_tip[b_id].append(row)

        next_frontier: list[dict[str, Any]] = []
        for branch in frontier:
            tip = branch["connector_id"]
            for conn in edges_by_tip.get(tip, []):
                a_id = str(conn["person_a_id"])
                b_id = str(conn["person_b_id"])
                neighbor = a_id if b_id == tip else b_id
                if neighbor in branch["path_nodes"]:
                    continue  # cycle
                new_strength = branch["path_strength"] * float(conn["computed_strength"])
                if new_strength < min_strength:
                    continue
                new_branch = {
                    "connector_id":  neighbor,
                    "path_nodes":    branch["path_nodes"] + [neighbor],
                    "path_edges":    branch["path_edges"] + [str(conn["id"])],
                    "path_strength": new_strength,
                    "raw_edges":     branch["raw_edges"] + [
                        dict(conn, id=str(conn["id"]),
                             person_a_id=a_id, person_b_id=b_id)
                    ],
                }
                all_paths.append(new_branch)
                next_frontier.append(new_branch)
        frontier = next_frontier

    # --- Dedup by connector, keep strongest ---------------------------------
    best_by_connector: dict[str, dict[str, Any]] = {}
    for path in all_paths:
        cid = path["connector_id"]
        existing = best_by_connector.get(cid)
        if existing is None or path["path_strength"] > existing["path_strength"]:
            best_by_connector[cid] = path

    top_paths = sorted(
        best_by_connector.values(),
        key=lambda p: p["path_strength"],
        reverse=True,
    )[:_TOP_PATHS]

    if not top_paths:
        # Even the empty case should hydrate the target name when we can.
        target_persons = await _fetch_persons_by_ids({target_person_id})
        target_name = (
            target_persons[0].get("canonical_name") if target_persons else None
        )
        return {
            "target_id":   target_person_id,
            "target_name": target_name,
            "paths_found": 0,
            "paths":       [],
            "message": (
                "No warm paths found in the current graph. "
                "Try expanding the graph or lowering min_strength."
            ),
        }

    # --- Bulk hydrate persons + first-edge evidence -------------------------
    all_person_ids: set[str] = {target_person_id}
    for path in top_paths:
        for pid in path["path_nodes"]:
            all_person_ids.add(pid)

    first_edge_ids = [path["path_edges"][0] for path in top_paths]

    persons = await _fetch_persons_by_ids(all_person_ids)
    person_map = {str(p["id"]): p for p in persons}
    evidence_by_edge = await _fetch_evidence_summaries(first_edge_ids)

    rendered: list[dict[str, Any]] = []
    for path in top_paths:
        nodes = [
            person_map.get(pid, {"id": pid, "canonical_name": "Unknown"})
            for pid in path["path_nodes"]
        ]
        rendered.append({
            "path_strength":    round(path["path_strength"], 3),
            "hops":             len(path["path_nodes"]) - 1,
            "connector":        nodes[-1].get("canonical_name"),
            "connector_id":     path["connector_id"],
            "path_names":       [n.get("canonical_name", "?") for n in nodes],
            "connection_types": [e["connection_type"] for e in path["raw_edges"]],
            "explanation":      _build_explanation(nodes, path["raw_edges"], evidence_by_edge),
            "suggested_opener": _build_opener(nodes, path["raw_edges"], evidence_by_edge),
        })

    target_name = (
        person_map.get(target_person_id, {}).get("canonical_name")
    )

    return {
        "target_id":   target_person_id,
        "target_name": target_name,
        "paths_found": len(rendered),
        "paths":       rendered,
    }


# ─── get_org_context (CHAT_TOOLS_PLAN.md Tool 2, Contract 13) ────────────────
#
# Schema deviations from the plan's pseudocode (call out so reviewers don't
# chase ghosts):
#   - `org_reporting_edges` columns are `manager_id` / `report_id`, not
#     `parent_person_id` / `child_person_id`. The plan got that wrong.
#   - We use raw asyncpg via `fetch`/`fetchrow` instead of the Supabase
#     PostgREST client referenced by the pseudocode — matches the rest of
#     this module.


async def get_org_context(
    person_id: str,
    include_peers: bool = True,
) -> dict[str, Any]:
    """Org-chart context for `person_id` (Contract 13).

    Pulls four signals:
      1. Managers — rows in `org_reporting_edges` where this person is the
         report. `is_dotted_line` flips when `path_confidence < confidence`.
      2. Direct reports — rows where this person is the manager.
      3. Functional cluster + up to 10 peers (only when `include_peers=True`).
      4. Scope estimates from `person_scope_estimates` (owns_*, team size,
         budget authority).

    Empty arrays / null defaults are returned in place of missing rows so the
    caller never has to special-case `None`.
    """
    # --- Person identity ----------------------------------------------------
    person_row = await fetchrow(
        """
        SELECT id, canonical_name, current_title, current_seniority_score,
               current_functional_domain, current_company_id
        FROM persons
        WHERE id = $1
        """,
        person_id,
    )
    person = dict(person_row) if person_row else {}

    # --- Managers -----------------------------------------------------------
    manager_rows = await fetch(
        """
        SELECT e.id            AS edge_id,
               e.confidence    AS edge_confidence,
               e.path_confidence,
               e.inference_method,
               m.id            AS person_id,
               m.canonical_name,
               m.current_title,
               m.current_seniority_score,
               m.current_functional_domain
        FROM org_reporting_edges e
        JOIN persons m ON m.id = e.manager_id
        WHERE e.report_id  = $1
          AND e.is_current = TRUE
        ORDER BY e.confidence DESC
        """,
        person_id,
    )
    managers: list[dict[str, Any]] = []
    for row in manager_rows:
        conf = row["edge_confidence"]
        path_conf = row["path_confidence"]
        is_dotted = (
            path_conf is not None
            and conf is not None
            and float(path_conf) < float(conf)
        )
        managers.append({
            "person_id":         str(row["person_id"]) if row["person_id"] else None,
            "name":              row["canonical_name"],
            "title":             row["current_title"],
            "seniority_score":   row["current_seniority_score"],
            "functional_domain": row["current_functional_domain"],
            "edge_confidence":   float(conf) if conf is not None else None,
            "inference_method":  row["inference_method"],
            "is_dotted_line":    is_dotted,
        })

    # --- Direct reports -----------------------------------------------------
    report_rows = await fetch(
        """
        SELECT e.id            AS edge_id,
               e.confidence    AS edge_confidence,
               e.inference_method,
               r.id            AS person_id,
               r.canonical_name,
               r.current_title,
               r.current_seniority_score,
               r.current_functional_domain
        FROM org_reporting_edges e
        JOIN persons r ON r.id = e.report_id
        WHERE e.manager_id = $1
          AND e.is_current = TRUE
        ORDER BY e.confidence DESC
        """,
        person_id,
    )
    direct_reports: list[dict[str, Any]] = []
    for row in report_rows:
        conf = row["edge_confidence"]
        direct_reports.append({
            "person_id":         str(row["person_id"]) if row["person_id"] else None,
            "name":              row["canonical_name"],
            "title":             row["current_title"],
            "seniority_score":   row["current_seniority_score"],
            "functional_domain": row["current_functional_domain"],
            "edge_confidence":   float(conf) if conf is not None else None,
            "inference_method":  row["inference_method"],
        })

    # --- Functional cluster + peers (optional) ------------------------------
    cluster_domain: str | None = None
    cluster_sub_domain: str | None = None
    cluster_member_count: int = 0
    cluster_peers: list[dict[str, Any]] = []

    if include_peers:
        membership = await fetchrow(
            """
            SELECT cluster_id, membership_confidence
            FROM org_cluster_members
            WHERE person_id = $1
            ORDER BY membership_confidence DESC
            LIMIT 1
            """,
            person_id,
        )
        if membership is not None:
            cluster_id = membership["cluster_id"]

            cluster_row = await fetchrow(
                """
                SELECT functional_domain, sub_domain, member_count
                FROM org_functional_clusters
                WHERE id = $1
                """,
                cluster_id,
            )
            if cluster_row is not None:
                cluster_domain = cluster_row["functional_domain"]
                cluster_sub_domain = cluster_row["sub_domain"]
                cluster_member_count = int(cluster_row["member_count"] or 0)

            peer_rows = await fetch(
                """
                SELECT m.membership_confidence,
                       p.id            AS person_id,
                       p.canonical_name,
                       p.current_title,
                       p.current_seniority_score
                FROM org_cluster_members m
                JOIN persons p ON p.id = m.person_id
                WHERE m.cluster_id = $1
                  AND m.person_id <> $2
                ORDER BY m.membership_confidence DESC
                LIMIT 10
                """,
                cluster_id,
                person_id,
            )
            for prow in peer_rows:
                mc = prow["membership_confidence"]
                cluster_peers.append({
                    "person_id":             str(prow["person_id"])
                                              if prow["person_id"] else None,
                    "name":                  prow["canonical_name"],
                    "title":                 prow["current_title"],
                    "seniority_score":       prow["current_seniority_score"],
                    "membership_confidence": float(mc) if mc is not None else None,
                })

    # --- Scope estimates ----------------------------------------------------
    scope_row = await fetchrow(
        """
        SELECT owns_products, owns_technologies, owns_functions, owns_regions,
               team_size_min, team_size_max, budget_authority_level
        FROM person_scope_estimates
        WHERE person_id = $1
        LIMIT 1
        """,
        person_id,
    )
    if scope_row is not None:
        scope = {
            "owns_products":          list(scope_row["owns_products"] or []),
            "owns_technologies":      list(scope_row["owns_technologies"] or []),
            "owns_functions":         list(scope_row["owns_functions"] or []),
            "owns_regions":           list(scope_row["owns_regions"] or []),
            "team_size_min":          scope_row["team_size_min"],
            "team_size_max":          scope_row["team_size_max"],
            "budget_authority_level": scope_row["budget_authority_level"],
        }
    else:
        scope = {
            "owns_products":          [],
            "owns_technologies":      [],
            "owns_functions":         [],
            "owns_regions":           [],
            "team_size_min":          None,
            "team_size_max":          None,
            "budget_authority_level": None,
        }

    org_chart_note: str | None = None
    if managers or direct_reports:
        org_chart_note = (
            "Org chart edges are inferred probabilistically. "
            "High-confidence edges (≥0.7) use explicit signals "
            "(job postings, press releases). "
            "Low-confidence edges use seniority + domain clustering."
        )

    return {
        "person": {
            "id":                str(person.get("id")) if person.get("id") else person_id,
            "name":              person.get("canonical_name"),
            "title":             person.get("current_title"),
            "seniority_score":   person.get("current_seniority_score"),
            "functional_domain": person.get("current_functional_domain"),
        },
        "managers":            managers,
        "direct_reports":      direct_reports,
        "direct_report_count": len(direct_reports),
        "functional_cluster": {
            "domain":     cluster_domain,
            "sub_domain": cluster_sub_domain,
            "peers":      cluster_peers,
            "peer_count": cluster_member_count
                          if cluster_member_count else len(cluster_peers),
        },
        "scope":          scope,
        "org_chart_note": org_chart_note,
    }
