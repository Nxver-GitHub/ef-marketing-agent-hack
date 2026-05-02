"""Unit tests for `credence.extractors.parallel_standards` — Wave 5 P3.e.

Same `httpx.MockTransport` + no-sleep pattern as `test_parallel_conference.py`.
Standards extractor differs in two interesting ways:
- Roles never demote signal_type (always `standards_committee_peer`)
- Role normalization: "Voting Member" / "VOTING_MEMBER" / etc. → canonical
  `voting_member`; unknown / missing → `member` (neutral baseline)
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from credence.extractors.parallel_standards import find_standards_committee_peers
from credence.extractors.patents import PersonRef


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "credence.extractors._parallel_client.asyncio.sleep", _instant
    )


def _person(name: str = "Wei Chen") -> PersonRef:
    return PersonRef(
        person_id=str(UUID("00000000-0000-0000-0000-bbbb00000001")),
        canonical_name=name,
    )


def _make_transport(
    *,
    poll_responses: list[dict] | None = None,
    submit_status: int = 200,
    submit_body: dict | None = None,
    submit_raises: bool = False,
) -> httpx.MockTransport:
    poll_idx = {"i": 0}
    poll_q = poll_responses or [
        {"run_id": "r-std", "status": "succeeded", "output": {"memberships": []}}
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if submit_raises and request.method == "POST":
            raise httpx.ConnectError("forced submit error")
        if request.method == "POST" and request.url.path.endswith("/tasks/runs"):
            return httpx.Response(
                submit_status,
                json=submit_body or {"run_id": "r-std", "status": "queued"},
            )
        if request.method == "GET" and "/tasks/runs/" in request.url.path:
            i = min(poll_idx["i"], len(poll_q) - 1)
            poll_idx["i"] += 1
            return httpx.Response(200, json=poll_q[i])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _client(t: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=t)


# ── Happy paths ─────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_happy_path_emits_standards_committee_peer() -> None:
    poll = [
        {
            "run_id": "r1",
            "status": "succeeded",
            "output": {
                "memberships": [
                    {
                        "body": "JEDEC",
                        "committee": "JC-42.4",
                        "committee_full_name": "DRAM Memory",
                        "overlap_years": "2018-2022",
                        "role_a": "chair",
                        "role_b": "voting_member",
                        "source_urls": ["https://jedec.org/jc-42-4-roster"],
                    }
                ]
            },
            "cost_cents": 18,
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_standards_committee_peers(_person(), _person("Bob"), client=c)
    assert len(out) == 1
    rec = out[0]
    assert rec["signal_type"] == "standards_committee_peer"
    assert rec["body"] == "JEDEC"
    assert rec["committee"] == "JC-42.4"
    assert rec["committee_full_name"] == "DRAM Memory"
    assert rec["overlap_years"] == "2018-2022"
    assert rec["role_a"] == "chair"
    assert rec["role_b"] == "voting_member"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("input_role", "expected"),
    [
        ("chair", "chair"),
        ("CHAIR", "chair"),
        ("Vice Chair", "vice_chair"),
        ("Voting Member", "voting_member"),
        ("VOTING_MEMBER", "voting_member"),
        ("voting-member", "voting_member"),
        ("observer", "observer"),
        ("editor", "editor"),
        ("rapporteur", "rapporteur"),
        ("Random Title", "member"),  # unknown → fallback
        ("", "member"),
        (None, "member"),  # missing → fallback
    ],
)
async def test_role_normalization(input_role: Any, expected: str) -> None:
    poll = [
        {
            "run_id": "rn",
            "status": "succeeded",
            "output": {
                "memberships": [
                    {
                        "body": "IEEE-SA",
                        "role_a": input_role,
                        "role_b": "member",
                        "source_urls": ["https://example.org/x"],
                    }
                ]
            },
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_standards_committee_peers(_person(), _person("X"), client=c)
    assert len(out) == 1
    assert out[0]["role_a"] == expected


@pytest.mark.unit
async def test_role_does_not_change_signal_type() -> None:
    """Even an attendee/observer combo emits standards_committee_peer."""
    poll = [
        {
            "run_id": "ro",
            "status": "succeeded",
            "output": {
                "memberships": [
                    {
                        "body": "IETF",
                        "role_a": "observer",
                        "role_b": "observer",
                        "source_urls": ["https://datatracker.ietf.org/x"],
                    }
                ]
            },
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_standards_committee_peers(_person(), _person("X"), client=c)
    assert out[0]["signal_type"] == "standards_committee_peer"


@pytest.mark.unit
async def test_multiple_memberships_returned() -> None:
    poll = [
        {
            "run_id": "rm",
            "status": "succeeded",
            "output": {
                "memberships": [
                    {"body": "JEDEC", "source_urls": ["u1"], "role_a": "chair"},
                    {"body": "IEEE-SA", "source_urls": ["u2"], "role_a": "voting_member"},
                    {"body": "SEMI", "source_urls": ["u3"], "role_a": "observer"},
                ]
            },
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_standards_committee_peers(_person(), _person("X"), client=c)
    assert [r["body"] for r in out] == ["JEDEC", "IEEE-SA", "SEMI"]
    # All same signal_type regardless of role
    assert all(r["signal_type"] == "standards_committee_peer" for r in out)


@pytest.mark.unit
async def test_max_results_caps_output() -> None:
    poll = [
        {
            "run_id": "rmax",
            "status": "succeeded",
            "output": {
                "memberships": [
                    {"body": f"Body{i}", "role_a": "member", "source_urls": ["u"]}
                    for i in range(10)
                ]
            },
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_standards_committee_peers(
            _person(), _person("X"), client=c, max_results=3
        )
    assert len(out) == 3


@pytest.mark.unit
async def test_drop_path_missing_body_and_urls() -> None:
    poll = [
        {
            "run_id": "rdrop",
            "status": "succeeded",
            "output": {
                "memberships": [
                    {},  # body missing AND no source urls → drop
                    {"body": "Real Body", "source_urls": ["u"]},  # kept
                ]
            },
        }
    ]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_standards_committee_peers(_person(), _person("X"), client=c)
    assert len(out) == 1
    assert out[0]["body"] == "Real Body"


# ── Polling + error paths ───────────────────────────────────────────────────


@pytest.mark.unit
async def test_failed_status_returns_empty() -> None:
    poll = [{"run_id": "rf", "status": "failed", "error": {"message": "no data"}}]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_standards_committee_peers(_person(), _person("X"), client=c)
    assert out == []


@pytest.mark.unit
async def test_submit_5xx_returns_empty() -> None:
    async with _client(_make_transport(submit_status=502)) as c:
        out = await find_standards_committee_peers(_person(), _person("X"), client=c)
    assert out == []


@pytest.mark.unit
async def test_submit_network_error_returns_empty() -> None:
    async with _client(_make_transport(submit_raises=True)) as c:
        out = await find_standards_committee_peers(_person(), _person("X"), client=c)
    assert out == []


@pytest.mark.unit
async def test_succeeded_missing_memberships_returns_empty() -> None:
    poll = [{"run_id": "rmm", "status": "succeeded", "output": {}}]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_standards_committee_peers(_person(), _person("X"), client=c)
    assert out == []


@pytest.mark.unit
async def test_deadline_with_perpetual_running_returns_empty() -> None:
    poll = [{"run_id": "rdl", "status": "running"}]
    async with _client(_make_transport(poll_responses=poll)) as c:
        out = await find_standards_committee_peers(
            _person(), _person("X"), client=c, deadline_seconds=0.05
        )
    assert out == []
