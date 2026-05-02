"""FastAPI app entry. Wires routes; deliverables fill in over time.

Run: `uv run uvicorn credence.api:app --reload --port 8000`
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .auth import Session, get_session, install_session_middleware
from .chat import run_chat
from .config import get_settings
from .db import close_pool, fetchrow, get_pool
from .enrich import router as enrich_router
from .models import ChatRequest, ChatResponse
from .orgchart.active_sampling import (
    DEFAULT_CONFIDENCE_CEILING,
    MAX_LIMIT as UNCERTAIN_EDGES_MAX_LIMIT,
    select_uncertain_edges,
)
from .orgchart.corrections import (
    CorrectionInput,
    CorrectionPersistError,
    EdgeNotFoundError,
    record_correction,
)
from .score_runner import score_prospect
from .search import explain_prospect, filter_prospects, focus_node, neighborhood
from .signals import router as signals_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("credence")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    log.info("server up")
    yield
    await close_pool()
    log.info("server down")


class CorrectionRequest(BaseModel):
    """POST /orgchart/correction body — V3_PT2.md L196-204.

    `component_attributions` (plan.md D.1) is optional — when present, it
    maps a per-component blame share over the seven implicit-scoring
    components (`seniority_gap`, `domain_match`, etc.). The optimizer
    (D.2) reads these to nudge specific components rather than applying a
    flat global multiplier when corrections accumulate.
    """

    person_a_id: UUID
    correction_type: str  # validated by CorrectionInput.__post_init__
    person_b_id: UUID | None = None
    edge_id: UUID | None = None
    correct_value: str | None = None
    component_attributions: dict[str, float] | None = None


class CorrectionResponse(BaseModel):
    correction_id: UUID


# ── /orgchart/uncertain-edges response shapes (Phase D.3 backend) ───────────


class UncertainEdgePerson(BaseModel):
    """Manager or report-side person bundled with the edge.

    `name` and `title` may be None when the underlying `persons` row hasn't
    been resolved (Decision 4 — unresolved-target placeholders are still
    rendered). The UI styles those distinctly.
    """

    id: UUID
    name: str | None = None
    title: str | None = None
    company_id: UUID | None = None


class UncertainEdgeOut(BaseModel):
    """One uncertain edge in the active-sampling response.

    Mirrors `credence.orgchart.active_sampling.UncertainEdge` but with the
    JSON shape the UI consumes — manager + report bundled into nested
    objects rather than flat columns. Keeps the OpenAPI schema legible.
    """

    edge_id: UUID
    account_id: UUID
    manager: UncertainEdgePerson
    report: UncertainEdgePerson
    confidence: float
    path_confidence: float | None = None
    inference_method: str
    dominant_signal: str | None = None
    score_components: dict[str, float] | None = None
    manager_span: int
    uncertainty_score: float


class UncertainEdgesResponse(BaseModel):
    """Wrapping envelope for the active-sampling endpoint.

    `count` is the actual returned length (≤ `limit`); pagination is
    cursor-free for now — the UI re-requests when it runs out, and the
    ranking is stable enough that cursor semantics aren't worth the
    complexity.
    """

    count: int
    account_id: UUID
    limit: int
    confidence_ceiling: float
    edges: list[UncertainEdgeOut]


class ScoreResponse(BaseModel):
    """Contract 6 read-side response shape — `GET /score/{prospect_id}`.

    `weight_version_id` is the *active* tenant weight version at read time.
    `recomputed` is True iff this call triggered a fresh compute (no cached
    row matched the active version); False if served from `score_records`.
    """

    prospect_id: UUID
    weight_version_id: UUID
    authenticity_score: float
    authority_score: float
    warmth_score: float
    overall_score: float
    falsification_note: str
    computed_at: datetime
    recomputed: bool


async def _select_score_record(
    prospect_id: UUID, weight_version_id: UUID,
) -> dict | None:
    """Fetch the score_records row for (prospect_id, weight_version_id), if any.

    Returns the most-recent row when multiple exist (the UNIQUE constraint
    means there should only be one, but ORDER BY computed_at DESC is a cheap
    safety net during the cutover window when score_runner is dual-writing).
    """
    row = await fetchrow(
        """
        SELECT authenticity_score, authority_score, warmth_score, overall_score,
               falsification_note, computed_at
        FROM score_records
        WHERE prospect_id = $1 AND weight_version_id = $2
        ORDER BY computed_at DESC
        LIMIT 1
        """,
        prospect_id, weight_version_id,
    )
    return dict(row) if row else None


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Credence backend", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Wave 6 M2 — resolve Supabase JWT / demo / service-role headers into a
    # Session and attach to request.state.session before any route runs.
    install_session_middleware(app)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/db")
    async def health_db() -> dict[str, object]:
        from .db import fetchrow

        row = await fetchrow("SELECT now() AS now, version() AS version")
        return {"now": str(row["now"]), "version": row["version"]} if row else {}

    # ─── Search ──────────────────────────────────────────────────────────────

    @app.get("/focus")
    async def http_focus(q: str = Query(..., min_length=1), limit: int = 5) -> dict:
        return {"results": await focus_node(q, limit=limit)}

    @app.get("/search")
    async def http_search(
        company: str | None = None,
        role: str | None = None,
        industry: str | None = None,
        name_contains: str | None = None,
        min_score: float | None = None,
        has_past_employer: str | None = None,
        has_school: str | None = None,
        limit: int = 50,
    ) -> dict:
        rows = await filter_prospects(
            company=company,
            role=role,
            industry=industry,
            name_contains=name_contains,
            min_score=min_score,
            has_past_employer=has_past_employer,
            has_school=has_school,
            limit=limit,
        )
        return {"count": len(rows), "prospects": rows}

    @app.get("/prospect/{prospect_id}")
    async def http_prospect(prospect_id: UUID) -> dict:
        bundle = await explain_prospect(prospect_id)
        if not bundle:
            raise HTTPException(404, "prospect not found")
        return bundle

    @app.get("/neighborhood/{prospect_id}")
    async def http_neighborhood(prospect_id: UUID, hops: int = 1) -> dict:
        return await neighborhood(prospect_id, hops=hops)

    # ─── Scoring ─────────────────────────────────────────────────────────────

    @app.post("/score/{prospect_id}")
    async def http_score(prospect_id: UUID) -> dict:
        result = await score_prospect(prospect_id)
        return {
            "authenticity_score": result.authenticity_score,
            "authority_score": result.authority_score,
            "warmth_score": result.warmth_score,
            "overall_score": result.overall_score,
            "falsification_notes": result.falsification_notes,
        }

    @app.get("/score/{prospect_id}", response_model=ScoreResponse)
    async def http_score_get(prospect_id: UUID) -> ScoreResponse:
        """Lazy-recompute score read per Contract 6.

        Behavior:
          1. Resolve the prospect's account_id (via prospects table, not the
             session — defense-in-depth so an authorized user requesting a
             cross-tenant prospect can't materialize a record under their
             own active weights).
          2. Look up the active `score_weights.id` for that account.
          3. SELECT the score_records row keyed (prospect_id, active_version_id).
          4. If found → return as-is, `recomputed=false`.
          5. If missing → call `score_prospect()` (which writes via
             score_runner's persistence path to score_records), then re-SELECT
             and return with `recomputed=true`.

        Errors:
          - 404 if prospect doesn't exist (or RLS hides it from the session).
          - 503 if the tenant has no active score_weights row (operational
             error — every account is seeded with one at M1+Contract6 apply).
          - 500 if score_prospect succeeds but score_records still has no
             matching row (the writer didn't materialize what it should have).
        """
        prospect_row = await fetchrow(
            "SELECT account_id FROM prospects WHERE id = $1", prospect_id,
        )
        if prospect_row is None:
            raise HTTPException(404, "prospect not found")
        account_id: UUID = prospect_row["account_id"]

        weight_row = await fetchrow(
            """
            SELECT id FROM score_weights
            WHERE account_id = $1 AND is_active = TRUE
            LIMIT 1
            """,
            account_id,
        )
        if weight_row is None:
            # Every account should have an active row from the Contract 6
            # seed. Missing one is an operational anomaly, not a user error.
            raise HTTPException(
                503,
                "no active score_weights for this tenant — re-run Contract 6 seed",
            )
        weight_version_id: UUID = weight_row["id"]

        existing = await _select_score_record(prospect_id, weight_version_id)
        if existing is not None:
            return ScoreResponse(
                prospect_id=prospect_id,
                weight_version_id=weight_version_id,
                authenticity_score=existing["authenticity_score"],
                authority_score=existing["authority_score"],
                warmth_score=existing["warmth_score"],
                overall_score=existing["overall_score"],
                falsification_note=existing["falsification_note"],
                computed_at=existing["computed_at"],
                recomputed=False,
            )

        # Cache miss — compute fresh. score_prospect writes to score_records
        # via SwiftElk's score_runner persistence path.
        await score_prospect(prospect_id)

        fresh = await _select_score_record(prospect_id, weight_version_id)
        if fresh is None:
            # Writer didn't materialize the row we expected. Either the active
            # weight version flipped between our SELECT and the compute, or
            # score_runner's persistence is broken. Either way, the caller
            # gets a 500 — this is an internal contract violation.
            raise HTTPException(
                500,
                "score_prospect did not write a score_records row for the active weight version",
            )

        return ScoreResponse(
            prospect_id=prospect_id,
            weight_version_id=weight_version_id,
            authenticity_score=fresh["authenticity_score"],
            authority_score=fresh["authority_score"],
            warmth_score=fresh["warmth_score"],
            overall_score=fresh["overall_score"],
            falsification_note=fresh["falsification_note"],
            computed_at=fresh["computed_at"],
            recomputed=True,
        )

    # ─── Org-chart corrections (Wave 6 / v3.1 Plan A4) ──────────────────────

    @app.post("/orgchart/correction", response_model=CorrectionResponse)
    async def http_orgchart_correction(
        req: CorrectionRequest,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> CorrectionResponse:
        """Record a user-submitted correction to a reporting edge.

        Authorization is implicit — the session middleware already
        established the caller is a member of some account; the
        persistence layer derives the correction's account_id from the
        referenced edge (if any) or the prospect's owning account, so a
        cross-tenant correction attempt would silently land in the
        prospect's tenant and be visible to that tenant — not the
        caller's. This is the same source-of-truth pattern as
        score_runner.score_prospect.

        4xx behavior:
          - 400 if correction_type isn't in the keyspace
          - 400 if `reports_to_other` / `team_wrong` lack `correct_value`
          - 404 if `edge_id` references a non-existent edge
        """
        # Resolve `submitted_by` from session — email when authenticated,
        # synthetic markers for demo / service paths.
        if session.is_demo:
            submitted_by = "demo"
        elif session.is_service:
            submitted_by = "service"
        elif session.user_id is not None:
            # In a future iteration we'd resolve email via auth.users;
            # for now the user_id UUID stamp is sufficient audit trail.
            submitted_by = f"user:{session.user_id}"
        else:
            submitted_by = "anonymous"

        try:
            correction_input = CorrectionInput(
                person_a_id=req.person_a_id,
                correction_type=req.correction_type,
                submitted_by=submitted_by,
                person_b_id=req.person_b_id,
                edge_id=req.edge_id,
                correct_value=req.correct_value,
                component_attributions=req.component_attributions,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_correction", "message": str(exc)},
            ) from exc

        try:
            correction_id = await record_correction(correction_input)
        except EdgeNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"error": "edge_not_found", "edge_id": str(req.edge_id)},
            ) from exc
        except CorrectionPersistError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": "correction_persist_failed", "message": str(exc)},
            ) from exc

        return CorrectionResponse(correction_id=correction_id)

    # ─── Org-chart active sampling (plan.md Phase D.3 — backend half) ───────
    #
    # SunnyRidge owns the NodeInspector UI for D.3; this endpoint is the
    # query surface they consume. Returns the highest-impact uncertain edges
    # for one tenant, ranked by `(1 - confidence) * (1 + log1p(manager_span))`.
    # The UI gets enough evidence per row (manager + report titles, dominant
    # signal, score-component breakdown) to make a confirm/reject call without
    # follow-up fetches.

    @app.get(
        "/orgchart/uncertain-edges",
        response_model=UncertainEdgesResponse,
    )
    async def http_uncertain_edges(
        account_id: UUID,
        limit: int = Query(default=20, ge=1, le=UNCERTAIN_EDGES_MAX_LIMIT),
        confidence_ceiling: float = Query(
            default=DEFAULT_CONFIDENCE_CEILING, ge=0.0, le=1.0
        ),
        session: Session = Depends(get_session),  # noqa: B008
    ) -> UncertainEdgesResponse:
        """List uncertain edges in the account's current chart, top-K first.

        Auth model mirrors `/orgchart/correction`: the session middleware
        confirms the caller has a valid identity. The caller passes
        `account_id` explicitly because some operator workflows review
        charts across tenants they own (multi-account organizations).
        Cross-tenant leakage is prevented by the SQL `WHERE account_id = $1`
        clause; if the caller passes an account they don't own, they get
        an empty list.
        """
        edges = await select_uncertain_edges(
            account_id,
            limit=limit,
            confidence_ceiling=confidence_ceiling,
        )
        return UncertainEdgesResponse(
            count=len(edges),
            account_id=account_id,
            limit=limit,
            confidence_ceiling=confidence_ceiling,
            edges=[
                UncertainEdgeOut(
                    edge_id=e.edge_id,
                    account_id=e.account_id,
                    manager=UncertainEdgePerson(
                        id=e.manager_id,
                        name=e.manager_name,
                        title=e.manager_title,
                        company_id=e.manager_company_id,
                    ),
                    report=UncertainEdgePerson(
                        id=e.report_id,
                        name=e.report_name,
                        title=e.report_title,
                    ),
                    confidence=e.confidence,
                    path_confidence=e.path_confidence,
                    inference_method=e.inference_method,
                    dominant_signal=e.dominant_signal,
                    score_components=e.score_components,
                    manager_span=e.manager_span,
                    uncertainty_score=e.uncertainty_score,
                )
                for e in edges
            ],
        )

    # ─── Chat ────────────────────────────────────────────────────────────────

    @app.post("/chat", response_model=ChatResponse)
    async def http_chat(req: ChatRequest) -> ChatResponse:
        msgs = [m.model_dump() for m in req.messages]
        out = await run_chat(msgs, snapshot=req.snapshot)
        return ChatResponse.model_validate(out)

    # ─── Signals (Track J — POST /signals/discover-connections) ──────────────
    app.include_router(signals_router)

    # ─── Enrich (Wave 5 Phase 1 — POST /enrich/{prospect_id}) ────────────────
    app.include_router(enrich_router)

    return app


app = create_app()
