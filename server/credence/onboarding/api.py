"""Onboarding HTTP route — `POST /onboarding/start` + `GET /onboarding/status/{account_id}`.

Per CUSTOMER_ONBOARDING_PLAN.md §"API Endpoints" (lines 280-311) and
§"Supabase Auth Webhook Setup" (lines 330-355). Implements Contract 14.

POST is invoked by the Supabase Auth `on_auth_user_created` webhook.
GET is polled by the frontend Onboarding progress UI every 3s.

The route does not run the pipeline synchronously — it dispatches
`run_onboarding_pipeline` as a background task so the webhook returns
fast (≤5s SLO) and the rep is unblocked from the UX.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import db
from ..config import get_settings
from .pipeline import run_onboarding_pipeline
from .webhook import verify_supabase_webhook

log = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


# ── Request / response shapes ─────────────────────────────────────────────


class OnboardingStartRequest(BaseModel):
    """Direct-call shape (used by tests + non-webhook clients).

    Webhook callers don't hit this directly — they POST raw bytes that
    `verify_supabase_webhook` parses. The router accepts either path.
    """

    user_id: UUID
    email: str
    full_name: str | None = None
    account_id: UUID


class OnboardingStartResponse(BaseModel):
    job_id: UUID
    status: str
    stage: str | None = None


class OnboardingStatusResponse(BaseModel):
    job_id: UUID | None
    status: str
    stage: str | None
    strategy: str | None
    progress: dict[str, Any]
    error_message: str | None
    started_at: str | None
    completed_at: str | None


# ── Helpers ───────────────────────────────────────────────────────────────


async def _create_onboarding_job(
    *, account_id: UUID, conn: Any
) -> UUID:
    """INSERT a fresh onboarding_jobs row with status='running' and return its id."""
    job_id = uuid4()
    await conn.execute(
        """
        INSERT INTO public.onboarding_jobs
          (id, account_id, status, stage, started_at)
        VALUES ($1::uuid, $2::uuid, 'running', 'identity', now())
        """,
        str(job_id),
        str(account_id),
    )
    return job_id


async def _latest_onboarding_job(
    *, account_id: UUID, conn: Any
) -> dict[str, Any] | None:
    """Return the newest onboarding_jobs row for this account, or None."""
    row = await conn.fetchrow(
        """
        SELECT id, status, stage, strategy, progress, error_message,
               started_at, completed_at
        FROM public.onboarding_jobs
        WHERE account_id = $1::uuid
        ORDER BY started_at DESC
        LIMIT 1
        """,
        str(account_id),
    )
    if row is None:
        return None
    return dict(row)


# ── Routes ────────────────────────────────────────────────────────────────


@router.post("/start", response_model=OnboardingStartResponse)
async def start_onboarding(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Kick off the 4-stage onboarding pipeline for a freshly-created user.

    Authentication: if the request carries `X-Webhook-Secret`, this is the
    Supabase Auth webhook path — verify the signature and parse the raw
    body. Otherwise, parse JSON directly (test path).

    Side effects:
      1. INSERT a new `onboarding_jobs` row with status='running'
      2. Schedule `run_onboarding_pipeline` as a background task

    Returns immediately with the job id — the rep can begin using the
    product as soon as Stage 0 (~2 min) marks `stage='company'`.
    """
    settings = get_settings()
    body_bytes = await request.body()
    headers = dict(request.headers)

    if "x-webhook-secret" in {k.lower() for k in headers}:
        # Webhook path — verify signature
        webhook_secret = getattr(settings, "supabase_webhook_secret", None)
        if not webhook_secret:
            log.error("/onboarding/start hit via webhook but supabase_webhook_secret unset")
            raise HTTPException(status_code=500, detail="webhook secret not configured")
        payload = verify_supabase_webhook(body_bytes, headers, webhook_secret)
        if payload is None:
            raise HTTPException(status_code=401, detail="invalid webhook signature or payload")
        user_id = UUID(payload.user_id)
        email = payload.email
        full_name = payload.full_name
        account_id = UUID(payload.account_id) if payload.account_id else None
        if account_id is None:
            # In production the webhook payload should carry an account_id
            # resolved by the trigger or a Supabase function. Tests bypass
            # this by going through the direct-call path below.
            raise HTTPException(status_code=400, detail="account_id missing from webhook payload")
    else:
        # Direct-call path (tests, internal calls)
        try:
            payload_json = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        try:
            req = OnboardingStartRequest(**payload_json)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid request: {exc}")
        user_id = req.user_id
        email = req.email
        full_name = req.full_name
        account_id = req.account_id

    # Create the job row + dispatch pipeline as background work
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        job_id = await _create_onboarding_job(account_id=account_id, conn=conn)

    background_tasks.add_task(
        run_onboarding_pipeline,
        job_id=job_id,
        user_id=user_id,
        email=email,
        full_name=full_name or "",
        account_id=account_id,
    )

    return OnboardingStartResponse(job_id=job_id, status="running", stage="identity")


@router.get(
    "/status/{account_id}",
    response_model=OnboardingStatusResponse,
)
async def get_onboarding_status(account_id: UUID):
    """Return the latest onboarding_jobs row for this account.

    Polled by the frontend Onboarding progress screen every 3s. Cheap:
    one indexed select per call.
    """
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await _latest_onboarding_job(account_id=account_id, conn=conn)

    if row is None:
        # No job yet — frontend treats this as "still pending webhook"
        return OnboardingStatusResponse(
            job_id=None,
            status="pending",
            stage=None,
            strategy=None,
            progress={},
            error_message=None,
            started_at=None,
            completed_at=None,
        )

    return OnboardingStatusResponse(
        job_id=row["id"],
        status=row["status"],
        stage=row.get("stage"),
        strategy=row.get("strategy"),
        progress=row.get("progress") or {},
        error_message=row.get("error_message"),
        started_at=row["started_at"].isoformat() if row.get("started_at") else None,
        completed_at=row["completed_at"].isoformat() if row.get("completed_at") else None,
    )
