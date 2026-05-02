"""Admin / operator endpoints — currently company-enrichment refresh.

Module per the established `signals.py` / `enrich.py` pattern: defines an
`APIRouter` with a `/admin` prefix that `api.py` includes via
`app.include_router(admin_router)` in `create_app`. Keeping admin routes
in a sibling module keeps `api.py` as the integration shell rather than
the dumping ground for every operator-side surface.

## Auth

Routes here use the same `Session` middleware as the rest of the app —
no special admin-role check today. When the project grows a real
admin-vs-tenant role distinction, gate at the route level via a new
`require_admin` dependency instead of layering it inside the handler.

## Why background tasks instead of inline awaits

`run_refresh` re-fires Firecrawl across every stale company, which can
take several minutes. The handler dispatches the work via
`BackgroundTasks` and returns `{"status": "queued"}` immediately so the
operator UI doesn't block — same shape as a typical "kick off a long
job" admin endpoint.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from .auth import Session, get_session

log = logging.getLogger(__name__)


router = APIRouter(prefix="/admin", tags=["admin"])


# ── COMPANY_ENRICHMENT_PLAN.md Step 6 — refresh endpoint ────────────────────


@router.post("/refresh-company-enrichment")
async def refresh_company_enrichment(
    background_tasks: BackgroundTasks,
    staleness_days: int = Query(default=30, ge=1, le=365),
    concurrency: int = Query(default=10, ge=1, le=20),
    session: Session = Depends(get_session),  # noqa: B008
) -> dict[str, object]:
    """Re-enrich every company whose `enrichment_last_run` is older than
    ``staleness_days``.

    The work itself is owned by ``credence.enrichment.refresh_company_enrichment.run_refresh``
    — this handler just dispatches it onto FastAPI's background-task queue
    and returns immediately. Operators poll the `companies.enrichment_status`
    column to watch progress.

    Lazy-imports the worker so that cold-starting `api.py` doesn't pull
    in Firecrawl + httpx unless this endpoint actually fires.
    """
    from .enrichment.refresh_company_enrichment import run_refresh

    async def _run() -> None:
        try:
            rollup = await run_refresh(
                staleness_days=staleness_days,
                concurrency=concurrency,
                dry_run=False,
            )
            log.info("refresh-company-enrichment rollup: %s", rollup)
        except Exception as exc:  # noqa: BLE001
            # Background tasks bury exceptions by default; surface to the
            # server log so operators can correlate failures to API timing.
            log.error("refresh-company-enrichment failed: %s", exc)

    background_tasks.add_task(_run)
    return {
        "status": "queued",
        "staleness_days": staleness_days,
        "concurrency": concurrency,
    }


__all__ = ["router"]
