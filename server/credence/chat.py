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
from .search import (
    explain_company,
    explain_prospect,
    filter_prospects,
    find_warm_paths,
    focus_node,
    get_org_context,
    neighborhood,
)

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

You also have two warm-introduction tools beyond the original four:

find_warm_paths(target_id) — Use this whenever the user asks how to get
introduced to a person, who knows a person, or how warm a connection is.
Always call this before suggesting cold outreach. If paths are found, lead
your response with the strongest path's explanation and suggested opener.
If no paths are found, say so explicitly.

get_org_context(person_id) — Use this whenever the user asks about
reporting relationships, org chart position, scope of responsibility, or
budget ownership. When edge confidence is below 0.5, qualify the response:
"This is inferred from job posting language and may not reflect current
reality."

Combine tools when needed: if a user asks "who at my team knows the person
who manages NVIDIA's HBM program?", first use get_org_context to find who
manages HBM, then use find_warm_paths to find connections to that person.
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
    {
        "name": "find_warm_paths",
        "description": (
            "Find warm introduction paths between the user's team and a target prospect. "
            "Use when asked 'who knows this person?', 'how can I get introduced to X?', "
            "or 'find a warm path to [name]'. Returns ranked paths with explanation and "
            "suggested outreach opener for each."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "UUID of the target person node.",
                },
                "max_hops": {
                    "type": "integer",
                    "description": "Maximum path length (default 3, max 4).",
                    "default": 3,
                },
                "min_strength": {
                    "type": "number",
                    "description": "Minimum path strength threshold (default 0.30).",
                    "default": 0.30,
                },
                "connection_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Filter to specific connection types. Valid values "
                        "(must match WARM_CONNECTION_TYPES in search.py exactly): "
                        "patent_co_inventor, academic_co_author_multi, "
                        "academic_co_author_single, career_overlap_same_team, "
                        "career_overlap_same_domain, career_overlap_general, "
                        "conference_co_presenter, standards_committee_peer, "
                        "same_phd_advisor, co_board_member, co_investor. "
                        "Omit to include all warm types."
                    ),
                },
            },
            "required": ["target_id"],
        },
    },
    {
        "name": "get_org_context",
        "description": (
            "Get the org chart context for a person: their manager, direct reports, "
            "functional peers, and scope/budget estimates. Use when asked 'who does X report to?', "
            "'who are X's direct reports?', 'what does X own?', 'what is X's budget authority?', "
            "or 'where does X sit in the org?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person_id": {
                    "type": "string",
                    "description": "UUID of the person to get org context for.",
                },
                "include_peers": {
                    "type": "boolean",
                    "description": "Whether to include functional cluster peers (default true).",
                    "default": True,
                },
            },
            "required": ["person_id"],
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
        # COMPANY_ENRICHMENT_PLAN.md Step 5 — route company nodes to
        # `explain_company` instead of crashing on `UUID(args["id"])` for
        # `co:<slug>` handles. The dispatch is by id-prefix because the
        # GraphCanvas mixes UUID-resolved company nodes (v3) with legacy
        # slug handles (v0). UUIDs that turn out to be companies fall
        # through to the company path via `explain_company`'s internal
        # resolver — saves a per-request DB lookup to disambiguate.
        node_id = args["id"]
        if isinstance(node_id, str) and node_id.startswith("co:"):
            bundle = await explain_company(node_id)
            return {"node": bundle}
        try:
            uuid_obj = UUID(node_id)
        except (ValueError, TypeError):
            return {"node": None, "error": f"invalid node id {node_id!r}"}
        bundle = await explain_prospect(uuid_obj)
        if bundle is None:
            # Person lookup miss — try the company path before giving up.
            # Common when the GraphCanvas hands a UUID that the v3
            # resolver minted for a company node.
            bundle = await explain_company(node_id)
        return {"node": bundle}

    if name == "expand_node":
        return await neighborhood(UUID(args["id"]), hops=int(args.get("hops", 1)))

    if name == "find_warm_paths":
        return await find_warm_paths(
            target_person_id=args["target_id"],
            max_hops=min(4, max(1, int(args.get("max_hops", 3)))),
            min_strength=min(1.0, max(0.0, float(args.get("min_strength", 0.30)))),
            connection_types=args.get("connection_types"),
        )

    if name == "get_org_context":
        return await get_org_context(
            person_id=args["person_id"],
            include_peers=bool(args.get("include_peers", True)),
        )

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
