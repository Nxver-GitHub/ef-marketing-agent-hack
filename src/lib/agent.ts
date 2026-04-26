/**
 * Z.AI-backed chat agent for the v2 Discover graph.
 *
 * Thin client over `POST /chat` on the FastAPI backend (../server). The
 * server runs the OpenAI tool loop against Z.AI, executes tools against
 * Supabase, and returns the assistant turn(s) + tool_results.
 *
 * We mirror those results into the page's view state via the `AgentContext`
 * callbacks so the UI stays driven by the chat without changes to Discover.tsx.
 *
 * Env:
 *   VITE_API_URL — backend base, defaults to http://localhost:8000
 */
import type { GraphEdge, GraphNode } from "./graph";

// ─── Public types (unchanged — UI imports these) ─────────────────────────────

export type ChatMessage =
  | { role: "user"; content: string }
  | { role: "assistant"; content: string; toolCalls?: ToolCallTrace[] }
  | { role: "system"; content: string };

export type ToolCallTrace = { name: string; args: unknown; result?: unknown };

export type ToolName = "focus_node" | "filter" | "explain" | "expand_node";

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

/**
 * Mirror server tool results into the page's view state. Skips silently when
 * the result shape isn't recognized (defensive — server can change).
 */
function applyToolResult(tr: ServerToolResult, ctx: AgentContext): void {
  const r = tr.result;

  switch (tr.name) {
    case "focus_node": {
      // result: { results: [{ id, kind, name, extras }, ...] }
      const arr = r.results as { id?: string; kind?: string }[] | undefined;
      const top = arr?.[0];
      if (top?.id) {
        // Server may return synthetic ids (e.g. "co:nvidia"); only select if
        // the id exists in the current graph snapshot.
        if (ctx.nodes.some((n) => n.id === top.id)) {
          ctx.setSelectedId(top.id);
        }
      }
      break;
    }
    case "filter": {
      // result: { count, prospects: [{ id, ... }, ...] }
      const arr = r.prospects as { id?: string }[] | undefined;
      if (arr) {
        const ids = new Set<string>();
        for (const p of arr) if (p.id) ids.add(p.id);
        if (ids.size > 0) ctx.setVisibleNodeIds(ids);
      }
      break;
    }
    case "expand_node": {
      // result: { center: { id, ... }, neighbors: [{ id, ... }] }
      const center = r.center as { id?: string } | undefined;
      const neighbors = r.neighbors as { id?: string }[] | undefined;
      if (neighbors) {
        const ids = new Set<string>();
        if (center?.id) ids.add(center.id);
        for (const n of neighbors) if (n.id) ids.add(n.id);
        if (ids.size > 0) ctx.setVisibleNodeIds(ids);
      }
      break;
    }
    case "explain":
      // explain doesn't mutate UI directly — the prose answer carries the info.
      break;
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
  const resp = await fetch(`${API_URL}/chat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      messages: toServerMessages(messages),
      snapshot: buildSnapshot(ctx),
    }),
  });

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
