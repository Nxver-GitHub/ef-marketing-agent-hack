"""Parallel.ai standards-committee co-membership extractor.

Asks Parallel to identify standards bodies (JEDEC, IEEE-SA, SEMI, IETF, W3C,
3GPP, etc.) where both `person_a` and `person_b` served on the same
committee or working group during overlapping time windows. Output feeds
Contract 1's `standards_committee_peer` signal_type — base strength 0.82
per CLAUDE.md STRENGTH_TABLE.

## Output contract — list of dicts feeding Contract 1's `structured_value`

```python
{
    "signal_type": "standards_committee_peer",
    "body": str,                     # standards body, e.g., "JEDEC"
    "committee": str,                # committee/WG identifier, e.g., "JC-42.4"
    "committee_full_name": Optional[str],
    "overlap_years": Optional[str],  # e.g., "2018-2022"
    "role_a": str,                   # "voting_member" | "chair" | "vice_chair" | "observer" | "member"
    "role_b": str,
    "source_urls": list[str],
}
```

Per CLAUDE.md STRENGTH_TABLE, base strength is `0.82` for any documented
co-membership; the role_a/role_b fields surface the seniority of the
participation but do not change the signal_type (unlike conference, where
attendee vs presenter shifts the tier). Standards co-membership is
inherently strong — it implies sustained collaboration over years.

## Sandbox / live-API status

Built against the documented Parallel.ai task-runs schema (same as
parallel_conference.py). Live integration test deferred to whoever has a
`PARALLEL_API_KEY` and runs `pytest -m integration`. Unit tests use
`httpx.MockTransport` returning canned task-run JSON.
"""
from __future__ import annotations

import logging
from typing import Any

from ._parallel_client import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
    run_parallel_task,
)
from .patents import PersonRef

logger = logging.getLogger(__name__)


# ── Schemas submitted to Parallel ────────────────────────────────────────────

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "person_a": {
            "type": "object",
            "properties": {
                "canonical_name": {"type": "string"},
                "current_company": {"type": "string"},
                "linkedin_url": {"type": "string"},
            },
            "required": ["canonical_name"],
        },
        "person_b": {
            "type": "object",
            "properties": {
                "canonical_name": {"type": "string"},
                "current_company": {"type": "string"},
                "linkedin_url": {"type": "string"},
            },
            "required": ["canonical_name"],
        },
        "max_results": {"type": "integer"},
    },
    "required": ["person_a", "person_b"],
}

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "memberships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "body": {"type": "string"},
                    "committee": {"type": "string"},
                    "committee_full_name": {"type": "string"},
                    "overlap_years": {"type": "string"},
                    "role_a": {"type": "string"},
                    "role_b": {"type": "string"},
                    "source_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["body"],
            },
        },
    },
    "required": ["memberships"],
}

_DESCRIPTION = (
    "Find standards bodies and working groups (JEDEC, IEEE-SA, SEMI, IETF, "
    "W3C, 3GPP, ITU, ETSI, OASIS, IPC, etc.) where BOTH person_a and "
    "person_b served on the SAME committee or working group during "
    "overlapping time windows. Identify the body, the specific committee or "
    "WG identifier (e.g., \"JC-42.4\", \"802.11\", \"JTC1/SC42\"), the years "
    "of overlap, and each person's role (voting_member, chair, vice_chair, "
    "observer, member). Cite source URLs (committee rosters, attendee lists, "
    "minutes, archived web pages). Limit to max_results entries. Every entry "
    "must be backed by at least one source URL — do not invent committees. "
    "If no co-membership is documented, return an empty memberships list."
)


# Recognized roles, ranked by seniority (informational only — does not change
# signal_type, since standards co-membership is uniformly strong evidence).
_KNOWN_ROLES = frozenset(
    {
        "chair",
        "vice_chair",
        "vice-chair",
        "secretary",
        "voting_member",
        "voting-member",
        "member",
        "observer",
        "rapporteur",
        "convener",
        "editor",
    }
)


# ── Output shaping ──────────────────────────────────────────────────────────


def _normalize_role(value: Any) -> str:
    """Default to 'member' when unspecified or unrecognized.

    Canonicalizes whitespace and hyphens to underscores so "Voting Member"
    and "voting-member" both land as `voting_member`. We don't punt to
    'observer' — that's a real (lower-status) role and misclassifying
    members as observers under-represents the connection. 'member' is
    the neutral baseline.
    """
    if not isinstance(value, str):
        return "member"
    cleaned = value.strip().lower().replace(" ", "_").replace("-", "_")
    return cleaned if cleaned in _KNOWN_ROLES else "member"


def _format_membership(item: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Parallel `memberships` dict → Contract 1 `structured_value`.

    Returns None when the body name is missing AND no source URL exists —
    we refuse to emit a record we can't cite. Other fields tolerate absence
    and propagate as None / empty.
    """
    body = item.get("body")
    if not isinstance(body, str):
        body = None
    sources = item.get("source_urls")
    if not isinstance(sources, list):
        sources = []
    sources = [s for s in sources if isinstance(s, str) and s]

    if not body and not sources:
        return None

    committee = item.get("committee")
    if not isinstance(committee, str):
        committee = ""
    committee_full_name = item.get("committee_full_name")
    if not isinstance(committee_full_name, str):
        committee_full_name = None
    overlap_years = item.get("overlap_years")
    if not isinstance(overlap_years, str):
        overlap_years = None

    return {
        "signal_type": "standards_committee_peer",
        "body": body or "(unknown body)",
        "committee": committee,
        "committee_full_name": committee_full_name,
        "overlap_years": overlap_years,
        "role_a": _normalize_role(item.get("role_a")),
        "role_b": _normalize_role(item.get("role_b")),
        "source_urls": sources,
    }


# ── Public entry point ──────────────────────────────────────────────────────


async def find_standards_committee_peers(
    person_a: PersonRef,
    person_b: PersonRef,
    *,
    max_results: int = 25,
    deadline_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
    client: Any | None = None,  # httpx.AsyncClient; loose typing for test injection
) -> list[dict[str, Any]]:
    """Return list of standards-committee co-memberships between two persons.

    Returns `[]` on:
    - Submission failure
    - Deadline hit before terminal status
    - Task `failed` / `cancelled`
    - No `memberships` array in the structured output
    - Every membership dropped by `_format_membership` validation
    """
    input_payload = {
        "person_a": {
            "canonical_name": person_a.canonical_name,
            **({"linkedin_url": person_a.linkedin_url} if person_a.linkedin_url else {}),
        },
        "person_b": {
            "canonical_name": person_b.canonical_name,
            **({"linkedin_url": person_b.linkedin_url} if person_b.linkedin_url else {}),
        },
        "max_results": max_results,
    }

    result = await run_parallel_task(
        description=_DESCRIPTION,
        input_payload=input_payload,
        input_schema=_INPUT_SCHEMA,
        output_schema=_OUTPUT_SCHEMA,
        deadline_seconds=deadline_seconds,
        client=client,
    )

    if result is None or result.status != "succeeded" or result.output is None:
        return []

    memberships = result.output.get("memberships")
    if not isinstance(memberships, list):
        return []

    out: list[dict[str, Any]] = []
    for item in memberships:
        if not isinstance(item, dict):
            continue
        record = _format_membership(item)
        if record is not None:
            out.append(record)
        if len(out) >= max_results:
            break

    logger.info(
        "find_standards_committee_peers: a=%s b=%s → %d hits (run_id=%s, %d¢)",
        person_a.canonical_name,
        person_b.canonical_name,
        len(out),
        result.run_id,
        result.cost_cents,
    )
    return out
