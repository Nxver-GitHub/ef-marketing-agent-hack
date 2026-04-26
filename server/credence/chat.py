"""Z.AI proxy + tool dispatcher.

Single endpoint: POST /chat. Body:
  { messages: ChatMessage[], snapshot?: {...} }

Loops the OpenAI tool-call protocol until the model returns a final text reply.
Tools execute server-side against Postgres so the browser never sees the API
key and the agent can reason over the full 10k-prospect graph.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from openai import AsyncOpenAI

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

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "focus_node",
            "description": "Fuzzy-match a node by free text. Returns top candidates across people, companies, industries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "filter",
            "description": "Return prospects matching the criteria, sorted by overall_score.",
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "explain",
            "description": "Rich bundle for one prospect: identity, sub-scores, top signals.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "prospect UUID"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "expand_node",
            "description": "1-hop neighbors of a person via colleagues / past employers / schools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "prospect UUID"},
                    "hops": {"type": "integer", "default": 1},
                },
                "required": ["id"],
            },
        },
    },
]


def _client() -> AsyncOpenAI:
    s = get_settings()
    return AsyncOpenAI(api_key=s.zai_api_key, base_url=s.zai_base_url)


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


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce ChatMessage models / dicts into OpenAI's expected shape.

    The frontend sends `{role, content, tool_calls?, tool_call_id?, name?}`.
    OpenAI accepts that as-is — we just drop None fields.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        d = {k: v for k, v in dict(m).items() if v is not None}
        out.append(d)
    return out


async def run_chat(
    user_messages: list[dict[str, Any]],
    snapshot: dict[str, Any] | None = None,
    max_iters: int = 6,
) -> dict[str, Any]:
    """Run the tool loop until the model emits a final assistant message.

    Returns: { messages: [...all turns including tool roles...], tool_results: [...] }
    """
    client = _client()
    s = get_settings()

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if snapshot:
        messages.append(
            {
                "role": "system",
                "content": f"Current canvas snapshot: {json.dumps(snapshot)[:2000]}",
            }
        )
    messages.extend(_to_openai_messages(user_messages))

    tool_results: list[dict[str, Any]] = []

    for _ in range(max_iters):
        resp = await client.chat.completions.create(
            model=s.zai_model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0.2,
        )
        choice = resp.choices[0]
        msg = choice.message

        # Append the assistant turn (might be a tool-call request or final text)
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_msg)

        if not msg.tool_calls:
            break

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = await _dispatch(tc.function.name, args)
            except Exception as e:
                log.warning("tool %s failed: %s", tc.function.name, e)
                result = {"error": str(e)}

            tool_results.append({"name": tc.function.name, "arguments": args, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": json.dumps(result, default=str),
                }
            )

    return {"messages": messages, "tool_results": tool_results}
