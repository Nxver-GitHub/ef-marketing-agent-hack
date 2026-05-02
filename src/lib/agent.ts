/**
 * Anthropic Claude-backed chat agent for the v2 Discover graph.
 *
 * Thin client over `POST /chat` on the FastAPI backend (../server). The
 * server runs the Anthropic tool loop, executes tools against Supabase,
 * and returns the assistant turn(s) + tool_results.
 *
 * We mirror those results into the page's view state via the `AgentContext`
 * callbacks so the UI stays driven by the chat without changes to Discover.tsx.
 *
 * Env:
 *   VITE_API_URL — backend base, defaults to http://localhost:8000
 */
import type { GraphEdge, GraphNode } from "./graph";
import { getCredenceHeaders } from "./credenceHeaders";

// ─── Public types (unchanged — UI imports these) ─────────────────────────────

export type ChatMessage =
  | { role: "user"; content: string }
  | { role: "assistant"; content: string; toolCalls?: ToolCallTrace[] }
  | { role: "system"; content: string };

export type ToolCallTrace = { name: string; args: unknown; result?: unknown };

export type ToolName =
  | "focus_node"
  | "filter"
  | "explain"
  | "expand_node"
  | "find_warm_paths"
  | "get_org_context";

export interface AgentContext {
  nodes: GraphNode[];
  edges: GraphEdge[];
  setSelectedId: (id: string | null) => void;
  setVisibleNodeIds: (ids: Set<string> | null) => void;
  getProspectById?: (id: string) => unknown;
  getScoreById?: (id: string) => unknown;
}

export interface RunAgentResult {
  finalText: string;
  toolCalls: ToolCallTrace[];
  messages: ChatMessage[];
}

// ─── Backend client ──────────────────────────────────────────────────────────

const API_URL =
  (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://localhost:8000";

interface ServerToolResult {
  name: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown>;
}

interface ServerMessage {
  role: "user" | "assistant" | "system" | "tool";
  content: string | null;
  tool_calls?: { id: string; type: string; function: { name: string; arguments: string } }[];
  tool_call_id?: string;
  name?: string;
}

interface ChatResponse {
  messages: ServerMessage[];
  tool_results: ServerToolResult[];
}

// ─── Tool result -> UI mutation ──────────────────────────────────────────────

// Bridge server-side ids → graph-builder ids. The two systems disagree on
// prefix conventions:
//   server:   <uuid>           graph:  person:<uuid>
//   server:   co:nvidia        graph:  company:nvidia
//   server:   in:semiconductors graph: industry:semiconductors
function toGraphId(id: string, kind?: string): string {
  if (!id) return id;
  if (kind === "person") return id.startsWith("person:") ? id : `person:${id}`;
  if (id.startsWith("co:")) return `company:${id.slice(3)}`;
  if (id.startsWith("in:")) return `industry:${id.slice(3)}`;
  return id;
}

function applyToolResult(tr: ServerToolResult, ctx: AgentContext): void {
  const r = tr.result;

  switch (tr.name) {
    case "focus_node": {
      const arr = r.results as { id?: string; kind?: string }[] | undefined;
      const top = arr?.[0];
      if (top?.id) {
        const graphId = toGraphId(top.id, top.kind);
        ctx.setSelectedId(graphId);
        // Promote a focused person into the rendered subgraph so an
        // out-of-top-N person becomes visible. Person ids only — company /
        // industry hubs are derived from rendered prospects, so narrowing
        // to a single hub would erase the rest of the world.
        if (top.kind === "person") {
          ctx.setVisibleNodeIds(new Set([graphId]));
        }
      }
      break;
    }
    case "filter": {
      const arr = r.prospects as { id?: string }[] | undefined;
      if (arr) {
        const ids = new Set<string>();
        for (const p of arr) if (p.id) ids.add(toGraphId(p.id, "person"));
        if (ids.size > 0) ctx.setVisibleNodeIds(ids);
      }
      break;
    }
    case "expand_node": {
      const center = r.center as { id?: string; kind?: string } | undefined;
      const neighbors = r.neighbors as { id?: string; kind?: string }[] | undefined;
      if (neighbors) {
        const ids = new Set<string>();
        if (center?.id) ids.add(toGraphId(center.id, center.kind ?? "person"));
        for (const n of neighbors) {
          if (n.id) ids.add(toGraphId(n.id, n.kind ?? "person"));
        }
        if (ids.size > 0) ctx.setVisibleNodeIds(ids);
      }
      break;
    }
    case "explain": {
      const node = r.node as { id?: string; kind?: string } | undefined;
      if (node?.id) {
        const graphId = toGraphId(node.id, node.kind ?? "person");
        ctx.setSelectedId(graphId);
        if ((node.kind ?? "person") === "person") {
          ctx.setVisibleNodeIds(new Set([graphId]));
        }
      }
      break;
    }
    case "find_warm_paths": {
      // Surface the warm-path constellation on the canvas: highlight the
      // target + every connector found, so the rep visually sees who their
      // team can route through. We promote ALL path_nodes from each path,
      // not just the connector tip — the intermediate hops matter for
      // explaining how the warm chain works.
      const targetId = r.target_id as string | undefined;
      const paths = r.paths as
        | { path_names?: string[]; connector_id?: string; path_strength?: number }[]
        | undefined;
      if (!paths || paths.length === 0) break;
      // Need the actual UUIDs (path_names are display names). The
      // server returns connector_id at minimum; full UUID chains would
      // require schema enrichment in find_warm_paths' rendered shape —
      // for now we anchor on target + connector_ids, which is enough
      // to draw attention to the people the agent is talking about.
      const ids = new Set<string>()
      if (targetId) ids.add(toGraphId(targetId, "person"))
      for (const p of paths) {
        if (p.connector_id) ids.add(toGraphId(p.connector_id, "person"))
      }
      if (ids.size > 0) {
        // Anchor selection on the strongest connector so the inspector
        // opens with their card (paths are pre-sorted by strength desc).
        const top = paths[0]?.connector_id
        if (top) ctx.setSelectedId(toGraphId(top, "person"))
        ctx.setVisibleNodeIds(ids)
      }
      break;
    }
    case "get_org_context": {
      // Highlight the org neighborhood: target + managers + direct reports
      // + cluster peers. Selection anchors on the target so the side panel
      // shows their identity card.
      const person = r.person as { id?: string } | undefined;
      const managers = r.managers as { person_id?: string }[] | undefined;
      const reports = r.direct_reports as { person_id?: string }[] | undefined;
      const cluster = r.functional_cluster as
        | { peers?: { person_id?: string }[] }
        | undefined;
      const ids = new Set<string>()
      if (person?.id) {
        const pid = toGraphId(person.id, "person")
        ids.add(pid)
        ctx.setSelectedId(pid)
      }
      for (const m of managers ?? []) {
        if (m.person_id) ids.add(toGraphId(m.person_id, "person"))
      }
      for (const rpt of reports ?? []) {
        if (rpt.person_id) ids.add(toGraphId(rpt.person_id, "person"))
      }
      for (const peer of cluster?.peers ?? []) {
        if (peer.person_id) ids.add(toGraphId(peer.person_id, "person"))
      }
      if (ids.size > 0) ctx.setVisibleNodeIds(ids)
      break;
    }
  }
}

// ─── runAgent ────────────────────────────────────────────────────────────────

function lastAssistantText(messages: ServerMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === "assistant" && m.content && !m.tool_calls?.length) {
      return m.content;
    }
  }
  return "";
}

function toServerMessages(messages: ChatMessage[]): { role: string; content: string }[] {
  return messages.map((m) => ({ role: m.role, content: m.content ?? "" }));
}

function buildSnapshot(ctx: AgentContext): Record<string, unknown> {
  // Tight summary so the system prompt stays under the model's context budget.
  const counts: Record<string, number> = {};
  for (const n of ctx.nodes) counts[n.kind] = (counts[n.kind] ?? 0) + 1;
  return {
    nodeCount: ctx.nodes.length,
    edgeCount: ctx.edges.length,
    nodeKindCounts: counts,
  };
}

export async function runAgent(
  messages: ChatMessage[],
  ctx: AgentContext,
): Promise<RunAgentResult> {
  let resp: Response;
  try {
    resp = await fetch(`${API_URL}/chat`, {
      method: "POST",
      // `getCredenceHeaders()` attaches `X-Credence-Demo: true` in demo mode
      // (Wave 6 M5) and will attach `Authorization: Bearer <jwt>` once M3
      // wires authenticated live mode.
      headers: { "content-type": "application/json", ...getCredenceHeaders() },
      body: JSON.stringify({
        messages: toServerMessages(messages),
        snapshot: buildSnapshot(ctx),
      }),
    });
  } catch (err) {
    throw new Error(
      `Could not reach the chat backend at ${API_URL}. ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`POST /chat failed: ${resp.status} ${text}`);
  }

  const data = (await resp.json()) as ChatResponse;

  const toolCalls: ToolCallTrace[] = data.tool_results.map((tr) => {
    applyToolResult(tr, ctx);
    return { name: tr.name, args: tr.arguments, result: tr.result };
  });

  const finalText =
    lastAssistantText(data.messages) ||
    "(no reply — the model returned only tool calls)";

  const updated: ChatMessage[] = [
    ...messages,
    { role: "assistant", content: finalText, toolCalls },
  ];
  return { finalText, toolCalls, messages: updated };
}
