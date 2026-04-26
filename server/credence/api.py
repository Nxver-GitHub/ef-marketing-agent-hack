"""FastAPI app entry. Wires routes; deliverables fill in over time.

Run: `uv run uvicorn credence.api:app --reload --port 8000`
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .chat import run_chat
from .config import get_settings
from .db import close_pool, get_pool
from .models import ChatRequest, ChatResponse
from .score_runner import score_prospect
from .search import explain_prospect, filter_prospects, focus_node, neighborhood

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("credence")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    log.info("server up")
    yield
    await close_pool()
    log.info("server down")


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

    # ─── Chat ────────────────────────────────────────────────────────────────

    @app.post("/chat", response_model=ChatResponse)
    async def http_chat(req: ChatRequest) -> ChatResponse:
        msgs = [m.model_dump() for m in req.messages]
        out = await run_chat(msgs, snapshot=req.snapshot)
        return ChatResponse.model_validate(out)

    return app


app = create_app()
