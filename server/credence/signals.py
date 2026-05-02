"""POST /signals/discover-connections — Contract 1 implementation.

Given two prospect IDs, run all enabled extractors in parallel, persist the
discovered connections to the v2 `signals` table, and return a structured
response with partial-results semantics.

References:
- CONTRACTS.md Contract 1 (request/response shapes, error behavior, invariants)
- CLAUDE.md L770-834 (the original spec)
- CLAUDE.md L1007 ("don't compute warm paths at query time" — extractors
  produce raw signal rows; the warm-path BFS is downstream)

J.2 ships the route with stub extractors so the partial-results / timeout
behavior can be exercised end-to-end. J.3-J.5 fill in the real extractor I/O.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .auth import Session, get_session
from .db import execute, fetchrow
from .extractors import (
    find_career_overlaps,
    find_conference_co_appearances,
    find_conference_program_appearances,
    find_education_overlaps,
    find_paper_co_authorships,
    find_patent_co_inventions,
    find_standards_committee_peers,
    find_standards_roster_memberships,
)
from .extractors.patents import PersonRef

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])


# ── Request / Response models (Contract 1) ───────────────────────────────────


SourceName = Literal[
    # v3 sources (Wave 5)
    "uspto", "scholar", "career", "parallel",
    # v3.1 expansions (Plan B6 — V3_PT2.md L731-739)
    "education", "conference", "standards",
]


async def _run_parallel_extractors(
    person_a: PersonRef,
    person_b: PersonRef,
    *,
    max_results: int = 25,
) -> list[dict[str, Any]]:
    """Fan-out wrapper for the `parallel` source.

    Parallel.ai is a single vendor that we use for two distinct task types
    (conference co-appearance + standards co-membership). The route exposes
    them as one logical `parallel` source so callers can opt in/out as a
    unit; under the hood we run both tasks concurrently and concatenate.
    Each result dict already carries its own `signal_type` field per
    extractor, so the route's existing per-record dispatch handles them.
    """
    conf_results, std_results = await asyncio.gather(
        find_conference_co_appearances(person_a, person_b, max_results=max_results),
        find_standards_committee_peers(person_a, person_b, max_results=max_results),
        return_exceptions=False,
    )
    return [*conf_results, *std_results]


# Map source-name → extractor coroutine
_EXTRACTORS = {
    # v3 sources (Wave 5)
    "uspto": find_patent_co_inventions,
    "scholar": find_paper_co_authorships,
    "career": find_career_overlaps,
    "parallel": _run_parallel_extractors,
    # v3.1 expansions (Plan B6) — backed by stubs in extractors/{education,
    # conference, standards}.py until DarkBeaver's B3/B4/B5 land. Stubs
    # return [] so the route reports 0 hits for these sources rather than
    # crashing on import. Per V3_PT2.md L731-739.
    "education": find_education_overlaps,
    "conference": find_conference_program_appearances,
    "standards": find_standards_roster_memberships,
}

# Map source-name → default signal_type emitted. `None` means the extractor
# decides per-record (the dict carries `signal_type`); used by sources that
# emit multiple signal_types (career: 3 sub-types; parallel: conference vs
# standards; education: 4 cohort kinds).
_SOURCE_DEFAULT_SIGNAL_TYPE: dict[str, str | None] = {
    "uspto": "patent_co_inventor",
    "scholar": "academic_co_author",
    "career": None,
    "parallel": None,
    # education emits one of {same_mba_cohort, same_phd_program,
    # executive_education, same_undergrad_cohort} per row
    "education": None,
    # conference is multi-type (presenter vs attendee) per row, mirrors `parallel`
    "conference": None,
    # standards always emits standards_committee_peer (single signal_type)
    "standards": "standards_committee_peer",
}


class DiscoverConnectionsRequest(BaseModel):
    prospect_a_id: UUID
    prospect_b_id: UUID
    sources: list[SourceName] | None = None
    max_results_per_source: int = Field(default=25, ge=1, le=200)
    timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)

    @field_validator("prospect_b_id")
    @classmethod
    def _ne(cls, v: UUID, info: Any) -> UUID:
        a = info.data.get("prospect_a_id")
        if a is not None and a == v:
            raise ValueError("prospect_a_id and prospect_b_id must differ")
        return v


class ConnectionRecord(BaseModel):
    signal_id: UUID
    signal_type: str
    structured_value: dict[str, Any]
    confidence: float = Field(ge=0.0, le=1.0)
    source: SourceName


class DiscoverConnectionsResponse(BaseModel):
    connections_found: int
    connections: list[ConnectionRecord]
    sources_attempted: list[str]
    sources_failed: list[str]
    elapsed_ms: int
    truncated: bool


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _load_person_ref(prospect_id: UUID) -> PersonRef:
    """Resolve a prospect UUID to the minimal extractor identifier set.

    Reads `persons` (v3) if present, falling back to v2 `prospects`. v2 has
    `name` + `linkedin_url`; v3 adds `uspto_inventor_id` + `orcid`.
    """
    row = await fetchrow(
        """
        SELECT
            COALESCE(p3.id, p2.id) AS id,
            COALESCE(p3.canonical_name, p2.name) AS canonical_name,
            COALESCE(p3.linkedin_url, p2.linkedin_url) AS linkedin_url,
            p3.uspto_inventor_id AS uspto_inventor_id,
            p3.orcid AS orcid
        FROM prospects p2
        FULL OUTER JOIN persons p3 ON p3.id = p2.id
        WHERE COALESCE(p3.id, p2.id) = $1
        """,
        prospect_id,
    )
    if row is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "prospect_not_found", "field": "prospect_id", "value": str(prospect_id)},
        )
    return PersonRef(
        person_id=str(row["id"]),
        canonical_name=row["canonical_name"],
        linkedin_url=row["linkedin_url"],
        uspto_inventor_id=row["uspto_inventor_id"],
        orcid=row["orcid"],
    )


def _confidence_for(source: str, payload: dict[str, Any]) -> float:
    """Per CLAUDE.md L828-829 + Contract 1 invariants."""
    if source == "uspto":
        return 0.95
    if source == "scholar":
        # academic_co_author: 0.90 if author_count <= 5, else 0.75
        author_count = int(payload.get("author_count", 5))
        return 0.90 if author_count <= 5 else 0.75
    if source == "career":
        # base on the extractor's chosen signal_type
        sig = payload.get("signal_type", "career_overlap_general")
        return {
            "career_overlap_same_team": 0.88,
            "career_overlap_same_domain": 0.72,
            "career_overlap_general": 0.60,
        }.get(sig, 0.60)
    if source == "parallel":
        # Parallel emits 3 distinct signal_types across its 2 sub-tasks.
        # Confidences mirror CLAUDE.md STRENGTH_TABLE base values.
        sig = payload.get("signal_type", "conference_co_attendee")
        return {
            "conference_co_presenter": 0.80,
            "conference_co_attendee": 0.20,
            "standards_committee_peer": 0.82,
        }.get(sig, 0.5)
    if source == "education":
        # Per the cohort-strength model in V3_PT2.md L568-595 the extractor
        # returns the computed strength as `confidence` directly. We surface
        # it here when present, falling back to the EDGE_CONFIGS base
        # strengths from V3_PT2.md L391-422 by signal_type.
        if "confidence" in payload:
            return float(payload["confidence"])
        sig = payload.get("signal_type", "alumni_network")
        return {
            "same_mba_cohort": 0.85,
            "same_phd_program": 0.78,
            "executive_education": 0.70,
            "same_undergrad_cohort": 0.62,
            # legacy fallback when the extractor only knows it's a degree
            # overlap but not the cohort type
            "alumni_network": 0.25,
        }.get(sig, 0.5)
    if source == "conference":
        # New Firecrawl-program source uses the same STRENGTH_TABLE values
        # as parallel's conference path; provenance differs but the
        # confidence semantic is identical.
        sig = payload.get("signal_type", "conference_co_attendee")
        return {
            "conference_co_presenter": 0.80,
            "conference_co_attendee": 0.20,
        }.get(sig, 0.4)
    if source == "standards":
        # New Firecrawl-roster source — same as parallel-standards.
        return 0.82
    return 0.5


def _signal_type_for(source: str, payload: dict[str, Any]) -> str:
    """Source's default signal_type, with multi-type sources reading from the payload."""
    default = _SOURCE_DEFAULT_SIGNAL_TYPE[source]
    if default is not None:
        return default
    # career / parallel / education decide per-row; fall back to a
    # conservative default consistent with each source's lowest-tier
    # signal_type so a misshapen payload doesn't fail the persist.
    fallback_by_source = {
        "career": "career_overlap_general",
        "parallel": "conference_co_attendee",
        "education": "alumni_network",  # weakest cohort signal
        "conference": "conference_co_attendee",
    }
    fallback = fallback_by_source.get(source, "conference_co_attendee")
    return str(payload.get("signal_type", fallback))


async def _persist_signal(
    prospect_id: UUID,
    account_id: UUID,
    source: str,
    signal_type: str,
    structured_value: dict[str, Any],
    confidence: float,
) -> UUID:
    """Insert into v2 `signals` table; idempotent on (prospect_id, signal_type, key fields).

    The v2 table schema accepts our shape directly. `value` carries
    `structured_value`. `raw_data` left null (raw blobs go to S3 in a
    follow-up; the 4KB cap is not yet enforced at the DB level).

    `account_id` lands per Wave 6 M1 — every domain-table row carries the
    tenant. The route reads it from `request.state.session.account_id`
    (populated by `SessionMiddleware`) and threads it through here.
    """
    signal_id = uuid.uuid4()
    await execute(
        """
        INSERT INTO signals (id, prospect_id, account_id, source, signal_type, value, raw_data, weight, confidence, collected_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, NULL, 1.0, $7, NOW())
        ON CONFLICT DO NOTHING
        """,
        signal_id,
        prospect_id,
        account_id,
        source,
        signal_type,
        structured_value,
        confidence,
    )
    return signal_id


async def _run_one_source(
    source: SourceName,
    person_a: PersonRef,
    person_b: PersonRef,
    *,
    max_results: int,
) -> tuple[str, list[dict[str, Any]] | BaseException]:
    """Run a single extractor; on exception, return the exception (don't raise)."""
    try:
        extractor = _EXTRACTORS[source]
        result = await extractor(person_a, person_b, max_results=max_results)
        return source, result
    except Exception as exc:
        logger.warning("extractor %s raised: %s", source, exc, exc_info=True)
        return source, exc


# ── Route ────────────────────────────────────────────────────────────────────


@router.post(
    "/discover-connections",
    response_model=DiscoverConnectionsResponse,
    summary="Discover documented relationships between two prospects.",
)
async def discover_connections(
    req: DiscoverConnectionsRequest,
    session: Session = Depends(get_session),  # noqa: B008 — FastAPI dependency injection
) -> DiscoverConnectionsResponse:
    """Run extractors in parallel, persist hits, return structured response.

    Always returns within `timeout_seconds + 0.5s` (cleanup grace) per
    Contract 1's timeout invariant. External-API failures are swallowed and
    surfaced via `sources_failed`; only DB write failures bubble as 502.

    Tenancy: every persisted signal carries `session.account_id`. The
    middleware (`SessionMiddleware`) resolves the session before this
    route runs — demo header binds to `DEMO_ACCOUNT_ID`, valid Bearer JWT
    binds to the user's account.
    """
    started = time.monotonic()
    account_id = session.account_id

    # 1. Resolve prospects to PersonRefs in parallel
    try:
        person_a, person_b = await asyncio.gather(
            _load_person_ref(req.prospect_a_id),
            _load_person_ref(req.prospect_b_id),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("prospect resolution failed")
        raise HTTPException(status_code=500, detail={"error": "prospect_resolution_failed"}) from exc

    # 2. Decide which sources to run
    sources: list[SourceName] = list(req.sources or list(_EXTRACTORS.keys()))
    sources_attempted: list[str] = list(sources)
    sources_failed: list[str] = []

    # 3. Run extractors in parallel under a hard timeout
    extractor_tasks = [
        _run_one_source(s, person_a, person_b, max_results=req.max_results_per_source)
        for s in sources
    ]

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*extractor_tasks, return_exceptions=False),
            timeout=req.timeout_seconds,
        )
    except TimeoutError:
        # Any unfinished tasks were already cancelled by gather/wait_for.
        # We don't know which ones completed, so mark all as failed conservatively.
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning("discover_connections timed out after %dms", elapsed_ms)
        return DiscoverConnectionsResponse(
            connections_found=0,
            connections=[],
            sources_attempted=sources_attempted,
            sources_failed=sources_attempted,
            elapsed_ms=elapsed_ms,
            truncated=True,
        )

    # 4. Aggregate, classify failures, persist successes
    truncated = False
    connections: list[ConnectionRecord] = []
    persist_errors = 0

    for source, payloads_or_exc in results:
        if isinstance(payloads_or_exc, BaseException):
            sources_failed.append(source)
            continue
        payloads = payloads_or_exc
        if len(payloads) >= req.max_results_per_source:
            truncated = True
        for payload in payloads:
            signal_type = _signal_type_for(source, payload)
            confidence = _confidence_for(source, payload)
            structured = {
                "connected_to": str(
                    req.prospect_b_id if source != "career"
                    else (req.prospect_b_id if payload.get("connected_to") is None else payload["connected_to"])
                ),
                **payload,
            }
            try:
                signal_id = await _persist_signal(
                    req.prospect_a_id, account_id, source, signal_type, structured, confidence
                )
            except Exception:
                logger.exception("signal persist failed for source=%s", source)
                persist_errors += 1
                continue
            connections.append(
                ConnectionRecord(
                    signal_id=signal_id,
                    signal_type=signal_type,
                    structured_value=structured,
                    confidence=confidence,
                    source=source,  # type: ignore[arg-type]
                )
            )

    if persist_errors > 0 and not connections:
        # Everything we tried to write failed — Contract 1 says 502.
        raise HTTPException(
            status_code=502,
            detail={
                "error": "signal_persist_failed",
                "found_in_memory": persist_errors,
            },
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return DiscoverConnectionsResponse(
        connections_found=len(connections),
        connections=connections,
        sources_attempted=sources_attempted,
        sources_failed=sources_failed,
        elapsed_ms=elapsed_ms,
        truncated=truncated,
    )
