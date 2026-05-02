"""Comprehensive tests for `credence.chat` tool registration + dispatch.

Pure structural validation of TOOL_SCHEMAS plus dispatch wiring tests with
the underlying search functions stubbed. No Anthropic API call. No DB.

Coverage:
  - All 6 tools registered with required schema fields (name, description,
    input_schema with required + properties)
  - find_warm_paths schema lists every WARM_CONNECTION_TYPES value in its
    `connection_types.description` (so the LLM doesn't pass a value the
    server filters out)
  - get_org_context schema declares person_id required
  - `_dispatch` routes each tool name to the right callee
  - Defensive clamps on max_hops (∈ [1, 4]) and min_strength (∈ [0.0, 1.0])
  - Unknown tool → returns dict with `error` key (not raises)
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from credence import chat, search


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_six_tools_registered() -> None:
    names = [t["name"] for t in chat.TOOL_SCHEMAS]
    assert names == [
        "focus_node",
        "filter",
        "explain",
        "expand_node",
        "find_warm_paths",
        "get_org_context",
    ]


@pytest.mark.unit
@pytest.mark.parametrize("schema", chat.TOOL_SCHEMAS, ids=lambda s: s["name"])
def test_every_tool_schema_has_required_fields(schema: dict[str, Any]) -> None:
    """All Anthropic tool schemas require name + description + input_schema."""
    assert isinstance(schema["name"], str) and len(schema["name"]) > 0
    assert isinstance(schema["description"], str) and len(schema["description"]) > 0
    assert isinstance(schema["input_schema"], dict)
    assert schema["input_schema"]["type"] == "object"
    assert "properties" in schema["input_schema"]


@pytest.mark.unit
def test_find_warm_paths_schema_declares_target_id_required() -> None:
    schema = next(s for s in chat.TOOL_SCHEMAS if s["name"] == "find_warm_paths")
    assert "target_id" in schema["input_schema"]["required"]
    props = schema["input_schema"]["properties"]
    assert props["target_id"]["type"] == "string"
    assert props["max_hops"]["type"] == "integer"
    assert props["min_strength"]["type"] == "number"
    assert props["connection_types"]["type"] == "array"


@pytest.mark.unit
def test_find_warm_paths_schema_lists_every_warm_type_in_description() -> None:
    """Schema description must enumerate WARM_CONNECTION_TYPES exactly so
    the LLM doesn't pass a value the server's frozenset rejects."""
    schema = next(s for s in chat.TOOL_SCHEMAS if s["name"] == "find_warm_paths")
    desc = schema["input_schema"]["properties"]["connection_types"]["description"]
    for ctype in search.WARM_CONNECTION_TYPES:
        assert ctype in desc, f"warm type {ctype!r} missing from schema description"


@pytest.mark.unit
def test_get_org_context_schema_declares_person_id_required() -> None:
    schema = next(s for s in chat.TOOL_SCHEMAS if s["name"] == "get_org_context")
    assert "person_id" in schema["input_schema"]["required"]
    props = schema["input_schema"]["properties"]
    assert props["person_id"]["type"] == "string"
    assert props["include_peers"]["type"] == "boolean"


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch routing
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_find_warm_paths_dispatched_with_target_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = AsyncMock(return_value={"target_id": "abc", "paths_found": 0, "paths": []})
    monkeypatch.setattr(chat, "find_warm_paths", fake)
    result = await chat._dispatch("find_warm_paths", {"target_id": "abc"})
    fake.assert_awaited_once()
    call_kwargs = fake.await_args.kwargs
    assert call_kwargs["target_person_id"] == "abc"
    assert call_kwargs["max_hops"] == 3  # default
    assert call_kwargs["min_strength"] == 0.30  # default
    assert call_kwargs["connection_types"] is None
    assert result == {"target_id": "abc", "paths_found": 0, "paths": []}


@pytest.mark.unit
async def test_get_org_context_dispatched_with_person_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = AsyncMock(return_value={"person": {"id": "abc"}})
    monkeypatch.setattr(chat, "get_org_context", fake)
    result = await chat._dispatch("get_org_context", {"person_id": "abc"})
    fake.assert_awaited_once()
    assert fake.await_args.kwargs["person_id"] == "abc"
    assert fake.await_args.kwargs["include_peers"] is True  # default
    assert result == {"person": {"id": "abc"}}


@pytest.mark.unit
async def test_unknown_tool_returns_error_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await chat._dispatch("nonexistent_tool", {})
    assert "error" in out
    assert "unknown tool" in out["error"]


# ─────────────────────────────────────────────────────────────────────────────
# Defensive arg clamping (per dispatch hardening in chat.py)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw_max_hops, expected",
    [
        (-1, 1),     # below floor → clamp up
        (0, 1),      # below floor → clamp up
        (1, 1),      # in range
        (3, 3),      # default
        (4, 4),      # at ceiling
        (10, 4),     # above ceiling → clamp down
        (999, 4),    # ridiculous → clamp down
    ],
)
async def test_max_hops_clamped_to_one_through_four(
    monkeypatch: pytest.MonkeyPatch, raw_max_hops: int, expected: int
) -> None:
    fake = AsyncMock(return_value={})
    monkeypatch.setattr(chat, "find_warm_paths", fake)
    await chat._dispatch("find_warm_paths", {"target_id": "x", "max_hops": raw_max_hops})
    assert fake.await_args.kwargs["max_hops"] == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw_min_strength, expected",
    [
        (-0.5, 0.0),
        (0.0, 0.0),
        (0.30, 0.30),
        (0.99, 0.99),
        (1.0, 1.0),
        (1.5, 1.0),
        (999.9, 1.0),
    ],
)
async def test_min_strength_clamped_to_zero_through_one(
    monkeypatch: pytest.MonkeyPatch, raw_min_strength: float, expected: float
) -> None:
    fake = AsyncMock(return_value={})
    monkeypatch.setattr(chat, "find_warm_paths", fake)
    await chat._dispatch("find_warm_paths", {"target_id": "x", "min_strength": raw_min_strength})
    assert fake.await_args.kwargs["min_strength"] == pytest.approx(expected)


@pytest.mark.unit
async def test_max_hops_string_input_coerced_to_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM can pass `"3"` if it forgets the type hint; should still work."""
    fake = AsyncMock(return_value={})
    monkeypatch.setattr(chat, "find_warm_paths", fake)
    await chat._dispatch("find_warm_paths", {"target_id": "x", "max_hops": "3"})
    assert fake.await_args.kwargs["max_hops"] == 3


@pytest.mark.unit
async def test_min_strength_string_input_coerced_to_float(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = AsyncMock(return_value={})
    monkeypatch.setattr(chat, "find_warm_paths", fake)
    await chat._dispatch("find_warm_paths", {"target_id": "x", "min_strength": "0.5"})
    assert fake.await_args.kwargs["min_strength"] == pytest.approx(0.5)


@pytest.mark.unit
async def test_include_peers_string_truthy_input_coerced_to_bool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = AsyncMock(return_value={})
    monkeypatch.setattr(chat, "get_org_context", fake)
    await chat._dispatch("get_org_context", {"person_id": "x", "include_peers": "yes"})
    # bool("yes") = True; the wrapper preserves truthiness without surprises.
    assert fake.await_args.kwargs["include_peers"] is True


# ─────────────────────────────────────────────────────────────────────────────
# System-prompt augmentation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_system_prompt_mentions_both_new_tools_by_name() -> None:
    """The agent needs to know the tools exist or it won't call them."""
    assert "find_warm_paths" in chat.SYSTEM_PROMPT
    assert "get_org_context" in chat.SYSTEM_PROMPT


@pytest.mark.unit
def test_system_prompt_mentions_low_confidence_qualification() -> None:
    """We instruct the agent to qualify low-confidence org edges."""
    assert "0.5" in chat.SYSTEM_PROMPT or "below 0.5" in chat.SYSTEM_PROMPT
