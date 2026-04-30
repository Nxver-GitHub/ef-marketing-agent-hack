"""Anthropic Claude proxy + tool dispatcher.

Single endpoint: POST /chat. Body:
  { messages: ChatMessage[], snapshot?: {...} }

Loops Claude's tool-use protocol until the model returns end_turn. Tools
execute server-side against Postgres so the browser never sees the API key
and the agent can reason over the full 10k-prospect graph.

Returns a frontend-compatible payload:
  { messages: [...assistant turns...], tool_results: [{name, arguments, result}] }
The frontend only inspects the trailing assistant message's `content` and
the `tool_results` array — wire shape is stable across the provider swap.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from anthropic import AsyncAnthropic

from .config import get_settings
from .search import explain_prospect, filter_prospects, focus_node, neighborhood

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an analyst helping the user navigate a graph of \
people, companies, cities, schools, conferences, industries, past employers, \
and partnerships. The graph is built from a Supabase database of 10k+ \
semiconductor-industry prospects with trust-and-fit scores.

Use the four tools to drive the UI:
- focus_node(query): find a node by free-text. Caller will set selectedId.
- filter(...): narrow the visible set. Prefer this over enumerating in prose.
- explain(id): return identity + sub-scores + evidence for one node.
- expand_node(id): return 1-hop neighbors (colleagues, school, past employers).

Style:
- Always call `explain` before describing a person in detail.
- Keep replies under three short paragraphs unless the user asks for more.
- Cite signals by (source, signal_type) when available.
"""

# Anthropic tool schema: flat {name, description, input_schema}.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "focus_node",
        "description": "Fuzzy-match a node by free text. Returns top candidates across people, companies, industries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "filter",
        "description": "Return prospects matching the criteria, sorted by overall_score.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string"},
                "role": {"type": "string"},
                "industry": {"type": "string"},
                "name_contains": {"type": "string"},
                "min_score": {"type": "number"},
                "has_past_employer": {"type": "string"},
                "has_school": {"type": "string"},
                "limit": {"type": "integer", "default": 30},
            },
        },
    },
    {
        "name": "explain",
        "description": "Rich bundle for one prospect: identity, sub-scores, top signals.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "prospect UUID"}},
            "required": ["id"],
        },
    },
    {
        "name": "expand_node",
        "description": "1-hop neighbors of a person via colleagues / past employers / schools.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "prospect UUID"},
                "hops": {"type": "integer", "default": 1},
            },
            "required": ["id"],
        },
    },
]


def _client() -> AsyncAnthropic:
    s = get_settings()
    return AsyncAnthropic(api_key=s.anthropic_api_key)


async def _dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool call. Returns a JSON-serializable dict."""
    if name == "focus_node":
        results = await focus_node(args["query"], limit=int(args.get("limit", 5)))
        return {"results": results}

    if name == "filter":
        results = await filter_prospects(
            company=args.get("company"),
            role=args.get("role"),
            industry=args.get("industry"),
            name_contains=args.get("name_contains"),
            min_score=args.get("min_score"),
            has_past_employer=args.get("has_past_employer"),
            has_school=args.get("has_school"),
            limit=int(args.get("limit", 30)),
        )
        return {"count": len(results), "prospects": results}

    if name == "explain":
        bundle = await explain_prospect(UUID(args["id"]))
        return {"node": bundle}

    if name == "expand_node":
        return await neighborhood(UUID(args["id"]), hops=int(args.get("hops", 1)))

    return {"error": f"unknown tool {name!r}"}


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce frontend ChatMessages into Anthropic's user/assistant format.

    The frontend sends `{role, content}`. System messages are stripped here
    — the prompt is passed via the top-level `system=` parameter on the API
    call. Empty turns are dropped.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role not in ("user", "assistant") or not content:
            continue
        out.append({"role": role, "content": content})
    return out


async def run_chat(
    user_messages: list[dict[str, Any]],
    snapshot: dict[str, Any] | None = None,
    max_iters: int = 6,
) -> dict[str, Any]:
    """Run the tool loop until Claude emits stop_reason=end_turn.

    Returns frontend-compatible payload:
      { messages: [...assistant turns...], tool_results: [{name, arguments, result}] }
    """
    client = _client()
    s = get_settings()

    system = SYSTEM_PROMPT
    if snapshot:
        system += f"\n\nCurrent canvas snapshot: {json.dumps(snapshot)[:2000]}"

    # Internal Anthropic-format conversation. The system prompt rides outside.
    convo: list[dict[str, Any]] = _to_anthropic_messages(user_messages)

    tool_results: list[dict[str, Any]] = []
    out_messages: list[dict[str, Any]] = []

    for _ in range(max_iters):
        resp = await client.messages.create(
            model=s.anthropic_model,
            system=system,
            messages=convo,
            tools=TOOL_SCHEMAS,
            max_tokens=2048,
            temperature=0.2,
        )

        # Mirror the assistant turn back into the conversation verbatim
        # (Anthropic requires the original content blocks alongside the
        # matching tool_result blocks on the next user turn).
        assistant_blocks = [block.model_dump() for block in resp.content]
        convo.append({"role": "assistant", "content": assistant_blocks})

        text_parts = [b["text"] for b in assistant_blocks if b.get("type") == "text"]
        tool_uses = [b for b in assistant_blocks if b.get("type") == "tool_use"]

        if resp.stop_reason != "tool_use":
            out_messages.append({"role": "assistant", "content": "".join(text_parts)})
            break

        result_blocks: list[dict[str, Any]] = []
        for tu in tool_uses:
            name = tu["name"]
            args = tu.get("input") or {}
            try:
                result = await _dispatch(name, args)
            except Exception as e:
                log.warning("tool %s failed: %s", name, e)
                result = {"error": str(e)}
            tool_results.append({"name": name, "arguments": args, "result": result})

            result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": json.dumps(result, default=str),
                }
            )

        convo.append({"role": "user", "content": result_blocks})

    if not out_messages:
        out_messages.append(
            {"role": "assistant", "content": "(stopped after max tool iterations)"}
        )

    return {"messages": out_messages, "tool_results": tool_results}
