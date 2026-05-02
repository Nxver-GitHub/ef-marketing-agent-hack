"""Customer onboarding pipeline orchestrator (CUSTOMER_ONBOARDING_PLAN.md
§"The Four Stages", lines 21-141; LP delegation msg 274 + 275 + 281).

Drives a `onboarding_jobs` row through the state machine:

    identity  →  company  →  team  →  connections  →  complete

Stage 0 (identity) is sync and BLOCKING — if the rep can't be resolved at all
the signup is aborted. Stages 1-3 are async + catch-and-continue: a failure
writes `error_message` and advances the stage so the user is never stuck on
a dead-end progress bar.

Idempotency: re-entering with the same `job_id` reads the current `stage` and
skips already-completed work. Safe to retry under flaky network / cron-driven
crash recovery.

Cost accounting threads `OnboardingCostLedger` through every stage and
persists the rolled-up totals into `onboarding_jobs.progress.cost`.

Wave A modules wired in:
- `rep_resolver.resolve_rep_linkedin`         — Stage 0
- `enrichment.company_site.scrape_company_site` — Stage 1
- `team_scraper.scrape_team_for_account`      — Stage 2
- `entity_resolver.resolve_or_insert_team_member` — Stage 2
- `search.find_warm_paths(source_person_ids=...)` — Stage 3 smoke check
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import asyncpg

from .. import db
from ..enrichment.company_site import scrape_company_site
from ..search import find_warm_paths
from .cost import (
    STAGE_REP_LOOKUP,
    STAGE_TEAM_SCRAPING,
    OnboardingCostLedger,
    track_apify_cost,
)
from .entity_resolver import resolve_or_insert_team_member
from .rep_resolver import resolve_rep_linkedin
from .team_scraper import scrape_team_for_account

log = logging.getLogger(__name__)

# Stage transitions in canonical order. Used by `_should_skip_stage` to decide
# whether a re-entry should pick up where the previous run left off.
_STAGE_ORDER: tuple[str, ...] = ("identity", "company", "team", "connections", "complete")


# ─── public entry ────────────────────────────────────────────────────────────


async def run_onboarding_pipeline(
    job_id: UUID,
    user_id: UUID,
    email: str,
    full_name: str,
    account_id: UUID,
) -> None:
    """Drive `onboarding_jobs[job_id]` through the four stages.

    Reads the current `stage` on entry — already-completed stages are skipped
    so this is safe to retry. Stage 0 failures abort with status='error';
    later stage failures write `error_message` and continue.
    """
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        job = await _load_job(conn, job_id)
        if job is None:
            log.error("onboarding_jobs row %s not found — aborting pipeline", job_id)
            return
        if job.get("status") == "done" and job.get("stage") == "complete":
            log.info("job %s already complete — no-op retry", job_id)
            return

    # Fresh ledger per pipeline run; serialised state in
    # onboarding_jobs.progress.cost lets a re-entry rebuild it.
    ledger = OnboardingCostLedger.from_progress_dict(job.get("progress") or {})

    # Re-acquire per stage to avoid holding a single connection for an hour.
    pool = await db.get_pool()

    # ── Stage 0: identity (sync, blocking) ─────────────────────────────────
    if not _should_skip_stage(job, "identity"):
        async with pool.acquire() as conn:
            try:
                await _stage_0_identity(
                    conn,
                    job_id=job_id,
                    user_id=user_id,
                    email=email,
                    full_name=full_name,
                    account_id=account_id,
                    ledger=ledger,
                )
            except Exception as exc:  # noqa: BLE001 — Stage 0 is the gate.
                log.exception("Stage 0 (identity) failed for job %s", job_id)
                await _persist_error(conn, job_id, "identity", str(exc), halt=True)
                return
            await _advance_stage(conn, job_id, "company", ledger=ledger)

    # ── Stage 1: company enrichment ────────────────────────────────────────
    if not await _should_skip_stage_after_reload(pool, job_id, "company"):
        async with pool.acquire() as conn:
            try:
                strategy = await _stage_1_company(
                    conn,
                    job_id=job_id,
                    email=email,
                    account_id=account_id,
                    ledger=ledger,
                )
            except Exception as exc:  # noqa: BLE001 — graceful degradation.
                log.exception("Stage 1 (company) failed for job %s — continuing", job_id)
                await _persist_error(conn, job_id, "company", str(exc), halt=False)
                strategy = "gtm_only"  # safe default for the next stage
            await _advance_stage(conn, job_id, "team", strategy=strategy, ledger=ledger)

    # ── Stage 2: team scraping ─────────────────────────────────────────────
    if not await _should_skip_stage_after_reload(pool, job_id, "team"):
        async with pool.acquire() as conn:
            try:
                await _stage_2_team(
                    conn,
                    job_id=job_id,
                    account_id=account_id,
                    ledger=ledger,
                )
            except Exception as exc:  # noqa: BLE001 — graceful degradation.
                log.exception("Stage 2 (team) failed for job %s — continuing", job_id)
                await _persist_error(conn, job_id, "team", str(exc), halt=False)
            await _advance_stage(conn, job_id, "connections", ledger=ledger)

    # ── Stage 3: connection discovery ──────────────────────────────────────
    if not await _should_skip_stage_after_reload(pool, job_id, "connections"):
        async with pool.acquire() as conn:
            try:
                await _stage_3_connections(
                    conn,
                    job_id=job_id,
                    account_id=account_id,
                    ledger=ledger,
                )
            except Exception as exc:  # noqa: BLE001 — graceful degradation.
                log.exception("Stage 3 (connections) failed for job %s", job_id)
                await _persist_error(conn, job_id, "connections", str(exc), halt=False)

        async with pool.acquire() as conn:
            await _mark_complete(conn, job_id, ledger=ledger)


# ─── stage helpers ──────────────────────────────────────────────────────────


async def _stage_0_identity(
    conn: asyncpg.Connection,
    *,
    job_id: UUID,
    user_id: UUID,
    email: str,
    full_name: str,
    account_id: UUID,
    ledger: OnboardingCostLedger,
) -> None:
    """Resolve the rep's LinkedIn, INSERT/UPDATE persons + account_team_members.

    Per plan §"On failure": LinkedIn-no-match falls back to enrichment_tier=0
    minimal persons row. Never blocks signup on LinkedIn lookup failure.
    """
    resolved = await resolve_rep_linkedin(
        full_name=full_name,
        email=email,
        account_id=account_id,
    )

    # Upsert the persons row. Either a real LinkedIn result or a tier-0
    # placeholder so downstream stages have a person_id to anchor on.
    if resolved is not None:
        person_row = await conn.fetchrow(
            """
            INSERT INTO persons (canonical_name, linkedin_url, current_title,
                                 enrichment_tier)
            VALUES ($1, $2, $3, 1)
            ON CONFLICT (linkedin_url) DO UPDATE SET
                canonical_name = COALESCE(persons.canonical_name, EXCLUDED.canonical_name),
                current_title  = COALESCE(EXCLUDED.current_title, persons.current_title),
                enrichment_tier = GREATEST(persons.enrichment_tier, EXCLUDED.enrichment_tier)
            RETURNING id
            """,
            full_name,
            resolved.linkedin_url,
            resolved.current_title,
        )
    else:
        # Tier-0 placeholder — name + email domain only.
        person_row = await conn.fetchrow(
            """
            INSERT INTO persons (canonical_name, enrichment_tier)
            VALUES ($1, 0)
            RETURNING id
            """,
            full_name,
        )

    person_id = person_row["id"] if person_row else None
    if person_id is None:
        raise RuntimeError("persons INSERT returned no id")

    # Link rep → account as 'owner' (NOT 'member' — distinguishes the rep
    # from scraped teammates for the warm-path source-person filter).
    await conn.execute(
        """
        INSERT INTO account_team_members
            (account_id, person_id, linkedin_url, role, scrape_status, scraped_at)
        VALUES ($1, $2, $3, 'owner', 'done', now())
        ON CONFLICT (account_id, person_id) DO UPDATE SET
            scrape_status = 'done',
            scraped_at = now(),
            linkedin_url = COALESCE(EXCLUDED.linkedin_url,
                                    account_team_members.linkedin_url)
        """,
        account_id,
        person_id,
        resolved.linkedin_url if resolved else None,
    )


async def _stage_1_company(
    conn: asyncpg.Connection,
    *,
    job_id: UUID,
    email: str,
    account_id: UUID,
    ledger: OnboardingCostLedger,
) -> str:
    """Scrape the company site for executives + press, set the team strategy.

    Returns the strategy ('all_employees' or 'gtm_only') for Stage 2.
    """
    domain = _extract_email_domain(email)
    company_url = f"https://{domain}"
    # The scraper persists signals to company_signals; we only need it for
    # the side effect + cost telemetry here.
    await scrape_company_site(company_url=company_url)

    # Probe employee_count_estimate to pick the team-scrape strategy.
    company_row = await conn.fetchrow(
        """
        SELECT employee_count_estimate
        FROM companies
        WHERE $1 = ANY(domains)
        LIMIT 1
        """,
        domain,
    )
    employee_count = (
        int(company_row["employee_count_estimate"])
        if company_row and company_row["employee_count_estimate"] is not None
        else 0
    )

    return "all_employees" if 0 < employee_count < 500 else "gtm_only"


async def _stage_2_team(
    conn: asyncpg.Connection,
    *,
    job_id: UUID,
    account_id: UUID,
    ledger: OnboardingCostLedger,
) -> None:
    """Scrape the rep's company employee roster + entity-resolve each into
    `persons` + `account_team_members`."""
    job = await _load_job(conn, job_id)
    if job is None:
        raise RuntimeError(f"job {job_id} disappeared mid-pipeline")
    strategy = job.get("strategy") or "gtm_only"

    # The rep's company_id was upserted in Stage 1; look it up by joining
    # back through account_team_members → persons → companies.
    company_row = await conn.fetchrow(
        """
        SELECT p.current_company_id AS company_id, c.canonical_name
        FROM account_team_members atm
        JOIN persons p ON p.id = atm.person_id
        LEFT JOIN companies c ON c.id = p.current_company_id
        WHERE atm.account_id = $1 AND atm.role = 'owner'
        LIMIT 1
        """,
        account_id,
    )
    if company_row is None or company_row["company_id"] is None:
        raise RuntimeError("no rep / company mapping found for Stage 2")

    company_id: UUID = company_row["company_id"]
    company_url = f"https://www.linkedin.com/company/{company_row['canonical_name'] or company_id}"

    scrape = await scrape_team_for_account(
        account_id=account_id,
        company_url=company_url,
        strategy=strategy,
        onboarding_job_id=job_id,
    )

    # Cost accounting (apimaestro per-item). Synthetic chargedEventCounts
    # so the central accumulator pipeline works uniformly.
    track_apify_cost(
        ledger,
        {"chargedEventCounts": {"apimaestro-employee-item": scrape.total_returned}},
        STAGE_TEAM_SCRAPING,
    )

    # Entity-resolve every scraped employee inside ONE transaction so
    # partial failures don't half-link teammates.
    new_count = 0
    matched_count = 0
    async with conn.transaction():
        for raw in scrape.employees:
            try:
                resolved = await resolve_or_insert_team_member(
                    raw_employee=raw,
                    account_id=account_id,
                    company_id=company_id,
                    conn=conn,
                )
                if resolved.was_new:
                    new_count += 1
                else:
                    matched_count += 1
            except Exception:  # noqa: BLE001 — log, drop, continue.
                log.exception("entity_resolver failed on a scraped employee")

    await _persist_progress(
        conn,
        job_id,
        {
            "total": scrape.total_returned,
            "scraped": scrape.total_returned,
            "matched": matched_count,
            "new_persons": new_count,
        },
        ledger=ledger,
    )


async def _stage_3_connections(
    conn: asyncpg.Connection,
    *,
    job_id: UUID,
    account_id: UUID,
    ledger: OnboardingCostLedger,
) -> None:
    """Smoke-check that warm paths exist for the freshly-scraped team.

    The actual connection-extractor fan-out (USPTO + Scholar + career-overlap)
    is delegated to existing modules in `enrichment/` and `jobs/` — they're
    already cron-driven and will pick up new persons on the next run. This
    stage just calls `find_warm_paths(source_person_ids=team_ids)` with one
    or two prospects to confirm at least one path renders end-to-end.

    Smoke check is non-blocking: a 0-path return doesn't fail the stage.
    """
    team_ids = await conn.fetch(
        """
        SELECT person_id FROM account_team_members
        WHERE account_id = $1 AND scrape_status = 'done'
        """,
        account_id,
    )
    team_id_strs = [str(r["person_id"]) for r in team_ids]
    if not team_id_strs:
        log.info("Stage 3: no team members yet — skipping warm-path smoke check")
        return

    # Pick up to 3 sample target prospects (any non-team person — just a sanity
    # check that the BFS is wired to live data after onboarding).
    sample_targets = await conn.fetch(
        """
        SELECT id FROM persons
        WHERE id <> ALL($1::uuid[])
        LIMIT 3
        """,
        [UUID(s) for s in team_id_strs],
    )

    # Run BFS in parallel against each sample. Each returns a dict; we just
    # log + count for the smoke metric.
    bfs_results = await asyncio.gather(
        *(
            find_warm_paths(
                target_person_id=str(row["id"]),
                source_person_ids=team_id_strs,
            )
            for row in sample_targets
        ),
        return_exceptions=True,
    )
    paths_found = sum(
        r["paths_found"] for r in bfs_results
        if isinstance(r, dict) and "paths_found" in r
    )
    log.info(
        "Stage 3 smoke check: %d sample targets · %d total warm paths surfaced",
        len(sample_targets),
        paths_found,
    )


# ─── helpers ────────────────────────────────────────────────────────────────


async def _load_job(
    conn: asyncpg.Connection, job_id: UUID
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        SELECT id, account_id, status, stage, strategy, progress
        FROM onboarding_jobs WHERE id = $1
        """,
        job_id,
    )
    return dict(row) if row else None


def _should_skip_stage(job: Mapping[str, Any], stage: str) -> bool:
    """Skip if the job has already advanced past `stage`."""
    current = job.get("stage")
    if current is None:
        return False  # never started — must run
    try:
        return _STAGE_ORDER.index(current) > _STAGE_ORDER.index(stage)
    except ValueError:
        return False  # unknown stage label, run it


async def _should_skip_stage_after_reload(
    pool: asyncpg.Pool, job_id: UUID, stage: str
) -> bool:
    """Re-fetch the row from a fresh connection. Used between stage acquires."""
    async with pool.acquire() as conn:
        job = await _load_job(conn, job_id)
        if job is None:
            return True
    return _should_skip_stage(job, stage)


async def _advance_stage(
    conn: asyncpg.Connection,
    job_id: UUID,
    next_stage: str,
    *,
    strategy: str | None = None,
    ledger: OnboardingCostLedger | None = None,
) -> None:
    """Update stage + status + (optional) strategy + cost rollup."""
    progress_patch = ledger.to_progress_dict() if ledger else {}
    if strategy is None:
        await conn.execute(
            """
            UPDATE onboarding_jobs SET
                stage = $2,
                status = 'running',
                progress = COALESCE(progress, '{}'::jsonb) || $3::jsonb
            WHERE id = $1
            """,
            job_id,
            next_stage,
            progress_patch,
        )
    else:
        await conn.execute(
            """
            UPDATE onboarding_jobs SET
                stage = $2,
                status = 'running',
                strategy = $3,
                progress = COALESCE(progress, '{}'::jsonb) || $4::jsonb
            WHERE id = $1
            """,
            job_id,
            next_stage,
            strategy,
            progress_patch,
        )


async def _persist_progress(
    conn: asyncpg.Connection,
    job_id: UUID,
    progress_delta: Mapping[str, Any],
    *,
    ledger: OnboardingCostLedger,
) -> None:
    """Merge a partial progress dict + the ledger snapshot into onboarding_jobs.progress."""
    merged: dict[str, Any] = {**dict(progress_delta), **ledger.to_progress_dict()}
    await conn.execute(
        """
        UPDATE onboarding_jobs SET
            progress = COALESCE(progress, '{}'::jsonb) || $2::jsonb
        WHERE id = $1
        """,
        job_id,
        merged,
    )


async def _persist_error(
    conn: asyncpg.Connection,
    job_id: UUID,
    stage: str,
    error_message: str,
    *,
    halt: bool,
) -> None:
    """Write error_message + (optionally) flip status='error'."""
    new_status = "error" if halt else "running"
    await conn.execute(
        """
        UPDATE onboarding_jobs SET
            stage = $2,
            status = $3,
            error_message = $4
        WHERE id = $1
        """,
        job_id,
        stage,
        new_status,
        error_message[:1000],
    )


async def _mark_complete(
    conn: asyncpg.Connection,
    job_id: UUID,
    *,
    ledger: OnboardingCostLedger,
) -> None:
    progress_patch = ledger.to_progress_dict()
    await conn.execute(
        """
        UPDATE onboarding_jobs SET
            stage = 'complete',
            status = 'done',
            completed_at = now(),
            progress = COALESCE(progress, '{}'::jsonb) || $2::jsonb
        WHERE id = $1
        """,
        job_id,
        progress_patch,
    )


def _extract_email_domain(email: str) -> str:
    """`sarah@nvidia.com` → `nvidia.com`. Lowercased + trimmed."""
    if "@" not in email:
        raise ValueError(f"invalid email: missing @ in {email!r}")
    return email.split("@", 1)[1].strip().lower()


__all__ = ["run_onboarding_pipeline"]
