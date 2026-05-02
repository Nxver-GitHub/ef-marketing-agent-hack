"""Unit tests for `credence.extractors.parallel_conference` — Wave 5 P3.e.

Pattern mirrors `test_patents.py` / `test_scholar.py`:
- `httpx.MockTransport` returns canned task-run JSON for `POST /tasks/runs`
  (submit) and `GET /tasks/runs/{run_id}` (poll).
- `asyncio.sleep` patched to no-op so the polling loop completes instantly.
- Each test asserts on the post-formatting list of dicts that the route
  consumes — not on raw HTTP shape.

Out of scope: live API behavior. See `test_parallel_live.py`.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from credence.extractors.parallel_conference import find_conference_co_appearances
from credence.extractors.patents import PersonRef

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the polling loop's `await asyncio.sleep(...)` a zero-cost no-op."""

    async def _instant(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "credence.extractors._parallel_client.asyncio.sleep", _instant
    )


def _person(name: str = "Lin Wei") -> PersonRef:
    return PersonRef(
        person_id=str(UUID("00000000-0000-0000-0000-aaaa00000001")),
        canonical_name=name,
    )


def _make_transport(
    *,
    submit_status: int = 200,
    submit_body: dict | None = None,
    poll_responses: list[dict] | None = None,
    submit_raises: bool = False,
) -> httpx.MockTransport:
    """Construct a MockTransport that walks through a scripted run lifecycle.

    `poll_responses` is a queue: index i is the body returned on the (i+1)th
    GET. After exhaustion, the last entry is returned indefinitely.
    """
    poll_idx = {"i": 0}
    poll_q = poll_responses or [
        {"run_id": "test-run-id", "status": "succeeded", "output": {"appearances": []}, "cost_cents": 0}
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if submit_raises and request.method == "POST":
            raise httpx.ConnectError("forced submit error")
        if request.method == "POST" and request.url.path.endswith("/tasks/runs"):
            return httpx.Response(
                submit_status,
                json=submit_body or {"run_id": "test-run-id", "status": "queued"},
            )
        if request.method == "GET" and "/tasks/runs/" in request.url.path:
            i = min(poll_idx["i"], len(poll_q) - 1)
            poll_idx["i"] += 1
            return httpx.Response(200, json=poll_q[i])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport)


# ── Happy paths ─────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_happy_path_both_presenters_emits_co_presenter() -> None:
    poll = [
        {
            "run_id": "r1",
            "status": "succeeded",
            "output": {
                "appearances": [
                    {
                        "event": "NeurIPS 2023",
                        "year": 2023,
                        "venue_city": "New Orleans",
                        "role_a": "presenter",
                        "role_b": "panelist",
                        "session_title": "Accelerator Design Track",
                        "source_urls": ["https://neurips.cc/2023/programme"],
                    }
                ]
            },
            "cost_cents": 12,
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_conference_co_appearances(_person("Lin Wei"), _person("Bob"), client=c)
    assert len(out) == 1
    rec = out[0]
    assert rec["signal_type"] == "conference_co_presenter"
    assert rec["event"] == "NeurIPS 2023"
    assert rec["year"] == 2023
    assert rec["role_a"] == "presenter"
    assert rec["role_b"] == "panelist"
    assert rec["source_urls"] == ["https://neurips.cc/2023/programme"]


@pytest.mark.unit
async def test_attendee_role_demotes_to_co_attendee() -> None:
    poll = [
        {
            "run_id": "r2",
            "status": "succeeded",
            "output": {
                "appearances": [
                    {
                        "event": "IEDM 2022",
                        "role_a": "attendee",
                        "role_b": "presenter",
                        "source_urls": ["https://ieee.org/iedm"],
                    }
                ]
            },
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_conference_co_appearances(_person(), _person("X"), client=c)
    assert out[0]["signal_type"] == "conference_co_attendee"


@pytest.mark.unit
async def test_unknown_roles_default_to_attendee() -> None:
    poll = [
        {
            "run_id": "r3",
            "status": "succeeded",
            "output": {
                "appearances": [
                    {
                        "event": "SPIE 2021",
                        "source_urls": ["https://spie.org/x"],
                    }
                ]
            },
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_conference_co_appearances(_person(), _person("X"), client=c)
    assert len(out) == 1
    assert out[0]["role_a"] == "attendee"
    assert out[0]["role_b"] == "attendee"
    assert out[0]["signal_type"] == "conference_co_attendee"


@pytest.mark.unit
async def test_multiple_appearances_returned_in_order() -> None:
    poll = [
        {
            "run_id": "r4",
            "status": "succeeded",
            "output": {
                "appearances": [
                    {"event": "A", "role_a": "presenter", "role_b": "presenter", "source_urls": ["u1"]},
                    {"event": "B", "role_a": "attendee", "role_b": "attendee", "source_urls": ["u2"]},
                    {"event": "C", "role_a": "keynote", "role_b": "session_chair", "source_urls": ["u3"]},
                ]
            },
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_conference_co_appearances(_person(), _person("X"), client=c)
    assert [r["event"] for r in out] == ["A", "B", "C"]
    assert [r["signal_type"] for r in out] == [
        "conference_co_presenter",
        "conference_co_attendee",
        "conference_co_presenter",
    ]


@pytest.mark.unit
async def test_max_results_caps_output() -> None:
    poll = [
        {
            "run_id": "r5",
            "status": "succeeded",
            "output": {
                "appearances": [
                    {"event": f"E{i}", "role_a": "presenter", "role_b": "presenter", "source_urls": ["u"]}
                    for i in range(10)
                ]
            },
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_conference_co_appearances(
            _person(), _person("X"), client=c, max_results=3
        )
    assert len(out) == 3


@pytest.mark.unit
async def test_drop_path_missing_event_and_urls() -> None:
    poll = [
        {
            "run_id": "r6",
            "status": "succeeded",
            "output": {
                "appearances": [
                    {},  # nothing → drop
                    {"event": "Real Event", "source_urls": ["u"]},  # kept
                ]
            },
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_conference_co_appearances(_person(), _person("X"), client=c)
    assert len(out) == 1
    assert out[0]["event"] == "Real Event"


# ── Polling lifecycle ───────────────────────────────────────────────────────


@pytest.mark.unit
async def test_queued_then_running_then_succeeded_terminates_correctly() -> None:
    poll = [
        {"run_id": "r7", "status": "queued"},
        {"run_id": "r7", "status": "running"},
        {
            "run_id": "r7",
            "status": "succeeded",
            "output": {
                "appearances": [
                    {"event": "E", "role_a": "presenter", "role_b": "presenter", "source_urls": ["u"]}
                ]
            },
        },
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_conference_co_appearances(_person(), _person("X"), client=c)
    assert len(out) == 1
    assert out[0]["event"] == "E"


@pytest.mark.unit
async def test_failed_status_returns_empty() -> None:
    poll = [
        {
            "run_id": "rf",
            "status": "failed",
            "error": {"message": "model refused to answer"},
            "cost_cents": 5,
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_conference_co_appearances(_person(), _person("X"), client=c)
    assert out == []


@pytest.mark.unit
async def test_cancelled_status_returns_empty() -> None:
    poll = [{"run_id": "rc", "status": "cancelled"}]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_conference_co_appearances(_person(), _person("X"), client=c)
    assert out == []


@pytest.mark.unit
async def test_deadline_with_perpetual_running_returns_empty() -> None:
    poll = [{"run_id": "rd", "status": "running"}]  # never resolves
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_conference_co_appearances(
            _person(), _person("X"), client=c, deadline_seconds=0.05
        )
    # Without sleep delays, the loop will spin until time.monotonic catches up;
    # the no-sleep fixture means time still advances over many iterations.
    assert out == []


# ── Error handling ──────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_submit_5xx_returns_empty() -> None:
    transport = _make_transport(submit_status=500, submit_body={"error": "boom"})
    async with _client(transport) as c:
        out = await find_conference_co_appearances(_person(), _person("X"), client=c)
    assert out == []


@pytest.mark.unit
async def test_submit_network_error_returns_empty() -> None:
    transport = _make_transport(submit_raises=True)
    async with _client(transport) as c:
        out = await find_conference_co_appearances(_person(), _person("X"), client=c)
    assert out == []


@pytest.mark.unit
async def test_submit_missing_run_id_returns_empty() -> None:
    transport = _make_transport(submit_body={"status": "queued"})  # no run_id
    async with _client(transport) as c:
        out = await find_conference_co_appearances(_person(), _person("X"), client=c)
    assert out == []


@pytest.mark.unit
async def test_succeeded_with_missing_appearances_array_returns_empty() -> None:
    poll = [{"run_id": "rx", "status": "succeeded", "output": {}}]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_conference_co_appearances(_person(), _person("X"), client=c)
    assert out == []


@pytest.mark.unit
async def test_succeeded_with_non_list_appearances_returns_empty() -> None:
    poll = [{"run_id": "ry", "status": "succeeded", "output": {"appearances": "not a list"}}]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_conference_co_appearances(_person(), _person("X"), client=c)
    assert out == []
