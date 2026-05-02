"""Parallel.ai conference co-appearance extractor.

Asks Parallel to identify conferences, workshops, and panels where both
`person_a` and `person_b` appeared in any role (presenter, panelist,
session chair, attendee, keynote). Output feeds Contract 1's
`conference_co_presenter` signal_type — the strongest interpretation
(co-presented) is preferred when the role evidence supports it; lesser
interpretations fall back to `conference_co_attendee` (a CLAUDE.md
STRENGTH_TABLE key) when available.

## Output contract — list of dicts feeding Contract 1's `structured_value`

```python
{
    "signal_type": "conference_co_presenter" | "conference_co_attendee",
    "event": str,              # conference / workshop name
    "year": Optional[int],
    "venue_city": Optional[str],
    "role_a": str,             # e.g., "presenter", "panelist", "attendee"
    "role_b": str,
    "session_title": Optional[str],
    "source_urls": list[str],  # citation provenance from Parallel
}
```

Per CLAUDE.md STRENGTH_TABLE:
- `conference_co_presenter` baseline 0.80
- `conference_co_attendee` baseline 0.20

The signals route reads `signal_type` from each dict and uses it directly
(`_signal_type_for("parallel", payload)` defers to the extractor when no
default exists for the source).

## Role-promotion rule

If both roles in `{presenter, panelist, session_chair, keynote}`, emit
`conference_co_presenter`. If either role is `attendee` (or unknown), emit
`conference_co_attendee`. When in doubt, the lower-strength tier is the
honest answer — better to under-claim and let the user upgrade than to
over-claim and break trust.
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

_PROMOTION_ROLES = frozenset(
    {"presenter", "panelist", "session_chair", "keynote", "speaker"}
)
_ATTENDEE_ROLES = frozenset({"attendee", "audience", "registrant"})


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
        "appearances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event": {"type": "string"},
                    "year": {"type": "integer"},
                    "venue_city": {"type": "string"},
                    "role_a": {"type": "string"},
                    "role_b": {"type": "string"},
                    "session_title": {"type": "string"},
                    "source_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["event"],
            },
        },
    },
    "required": ["appearances"],
}

_DESCRIPTION = (
    "Find conferences, workshops, panels, and standards-track meetings where "
    "BOTH person_a and person_b appeared on the program. Include each person's "
    "role (presenter, panelist, session_chair, keynote, attendee). Cite source "
    "URLs (event programs, archived agendas, conference proceedings). Limit to "
    "max_results events. Do not invent events — every entry must be backed by "
    "at least one source URL. If no co-appearance is documented, return an "
    "empty appearances list."
)


# ── Output classification ────────────────────────────────────────────────────


def _classify_signal_type(role_a: str, role_b: str) -> str:
    """Apply the role-promotion rule.

    Both presenters/panelists/etc → conference_co_presenter (strong, 0.80).
    Either side attendee/unknown   → conference_co_attendee (weak, 0.20).
    """
    a = role_a.strip().lower()
    b = role_b.strip().lower()
    if a in _PROMOTION_ROLES and b in _PROMOTION_ROLES:
        return "conference_co_presenter"
    if a in _ATTENDEE_ROLES or b in _ATTENDEE_ROLES:
        return "conference_co_attendee"
    # Unknown roles default to the lower-confidence interpretation.
    return "conference_co_attendee"


def _format_appearance(item: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Parallel `appearance` dict → Contract 1 `structured_value`.

    Returns None when the event name is missing AND no source URL exists —
    we refuse to emit a record we can't link back to anything.
    """
    event = item.get("event")
    if not isinstance(event, str):
        event = None
    sources = item.get("source_urls")
    if not isinstance(sources, list):
        sources = []
    sources = [s for s in sources if isinstance(s, str) and s]

    if not event and not sources:
        return None

    role_a_raw = item.get("role_a", "attendee")
    role_b_raw = item.get("role_b", "attendee")
    role_a = role_a_raw if isinstance(role_a_raw, str) else "attendee"
    role_b = role_b_raw if isinstance(role_b_raw, str) else "attendee"

    year = item.get("year")
    if not isinstance(year, int):
        year = None
    venue_city = item.get("venue_city")
    if not isinstance(venue_city, str):
        venue_city = None
    session_title = item.get("session_title")
    if not isinstance(session_title, str):
        session_title = None

    return {
        "signal_type": _classify_signal_type(role_a, role_b),
        "event": event or "(unknown event)",
        "year": year,
        "venue_city": venue_city,
        "role_a": role_a,
        "role_b": role_b,
        "session_title": session_title,
        "source_urls": sources,
    }


# ── Public entry point ──────────────────────────────────────────────────────


async def find_conference_co_appearances(
    person_a: PersonRef,
    person_b: PersonRef,
    *,
    max_results: int = 25,
    deadline_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
    client: Any | None = None,  # httpx.AsyncClient; loose typing for test injection
) -> list[dict[str, Any]]:
    """Return list of conference co-appearances between two persons.

    Returns `[]` on:
    - Submission failure
    - Deadline hit before terminal status
    - Task `failed` / `cancelled`
    - No `appearances` array in the structured output
    - Every appearance dropped by `_format_appearance` validation
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

    appearances = result.output.get("appearances")
    if not isinstance(appearances, list):
        return []

    out: list[dict[str, Any]] = []
    for item in appearances:
        if not isinstance(item, dict):
            continue
        record = _format_appearance(item)
        if record is not None:
            out.append(record)
        if len(out) >= max_results:
            break

    logger.info(
        "find_conference_co_appearances: a=%s b=%s → %d hits (run_id=%s, %d¢)",
        person_a.canonical_name,
        person_b.canonical_name,
        len(out),
        result.run_id,
        result.cost_cents,
    )
    return out
