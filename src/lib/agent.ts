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
// Bridge server-side ids → graph-builder ids. The two systems disagree on
// prefix conventions:
//   server:   <uuid>           graph:  person:<uuid>
//   server:   co:nvidia        graph:  company:nvidia
//   server:   in:semiconductors graph: industry:semiconductors
// Translating here keeps the rest of the agent stupid about it.
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
      // result: { results: [{ id, kind, name, extras }, ...] }
      const arr = r.results as { id?: string; kind?: string }[] | undefined;
      const top = arr?.[0];
      if (top?.id) {
        const graphId = toGraphId(top.id, top.kind);
        ctx.setSelectedId(graphId);
        // Also promote the focused node into the rendered subgraph so an
        // out-of-top-N person becomes visible on the canvas. Person ids only:
        // company / industry hubs are derived from rendered prospects, so
        // narrowing to a single hub would erase the rest of the world.
        if (top.kind === "person") {
          ctx.setVisibleNodeIds(new Set([graphId]));
        }
      }
      break;
    }
    case "filter": {
      // result: { count, prospects: [{ id, ... }, ...] }
      const arr = r.prospects as { id?: string }[] | undefined;
      if (arr) {
        const ids = new Set<string>();
        for (const p of arr) if (p.id) ids.add(toGraphId(p.id, "person"));
        if (ids.size > 0) ctx.setVisibleNodeIds(ids);
      }
      break;
    }
    case "expand_node": {
      // result: { center: { id, kind, ... }, neighbors: [{ id, kind, ... }] }
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
      // result: { node: { id, kind, ... } }. The prose carries the detail,
      // but selecting the node opens the right-rail inspector with full
      // sub-scores + signals — strictly additive to the chat answer.
      const node = r.node as { id?: string; kind?: string } | undefined;
      if (node?.id) {
        const graphId = toGraphId(node.id, node.kind ?? "person");
        ctx.setSelectedId(graphId);
        // Promote into the rendered set (mirror of focus_node — see above).
        if ((node.kind ?? "person") === "person") {
          ctx.setVisibleNodeIds(new Set([graphId]));
        }
      }
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

// ─── Canned agent (no-backend mode for Vercel + snapshot demos) ────────────
// When VITE_USE_SNAPSHOT=true (or the live API is unreachable) we run a
// keyword-router that produces real ToolResults against ctx.nodes. The point
// is a believable demo, not full LLM coverage — enough scripted patterns to
// answer the queries the demo script actually walks through.
const USE_CANNED =
  (import.meta.env.VITE_USE_SNAPSHOT as string | undefined)?.toLowerCase() === "true";

function findCompanyId(ctx: AgentContext, q: string): string | null {
  const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, "");
  const nq = norm(q);
  let best: { id: string; score: number } | null = null;
  for (const n of ctx.nodes) {
    if (n.kind !== "company") continue;
    const nn = norm(n.name);
    if (!nn) continue;
    let score = 0;
    if (nn === nq) score = 100;
    else if (nn.startsWith(nq) || nq.startsWith(nn)) score = 80;
    else if (nn.includes(nq) || nq.includes(nn)) score = 60;
    if (score > 0 && (!best || score > best.score)) best = { id: n.id, score };
  }
  return best?.id ?? null;
}

function findIndustryId(ctx: AgentContext, q: string): string | null {
  const nq = q.toLowerCase().trim();
  for (const n of ctx.nodes) {
    if (n.kind === "industry" && n.name.toLowerCase() === nq) return n.id;
  }
  for (const n of ctx.nodes) {
    if (n.kind === "industry" && n.name.toLowerCase().includes(nq)) return n.id;
  }
  return null;
}

function topPersonIdsAtCompany(ctx: AgentContext, companyId: string, limit: number): string[] {
  const peers = ctx.edges
    .filter((e) => e.kind === "works_at" && (e.source === companyId || e.target === companyId))
    .map((e) => (e.source === companyId ? e.target : e.source))
    .filter((id) => {
      const n = ctx.nodes.find((nn) => nn.id === id);
      return n?.kind === "person";
    });
  // Sort by score (best-effort — node may carry .score).
  peers.sort((a, b) => {
    const na = ctx.nodes.find((n) => n.id === a) as { score?: number } | undefined;
    const nb = ctx.nodes.find((n) => n.id === b) as { score?: number } | undefined;
    return (nb?.score ?? -1) - (na?.score ?? -1);
  });
  return peers.slice(0, limit);
}

function tokenizeRoleQuery(q: string): { roleKw: string | null; companyHint: string | null; limit: number } {
  const lc = q.toLowerCase();
  const roleKws = ["ceo", "cto", "coo", "cfo", "vp engineering", "vp of engineering", "head of engineering",
                   "director of engineering", "vp", "director", "engineer", "designer"];
  const roleKw = roleKws.find((kw) => lc.includes(kw)) ?? null;
  const at = / at ([a-z][a-z0-9 .&'-]*)/.exec(lc);
  const companyHint = at?.[1]?.trim() ?? null;
  const numMatch = /\b(\d{1,3})\b/.exec(lc);
  const limit = numMatch ? Math.min(40, Math.max(1, parseInt(numMatch[1]!, 10))) : 10;
  return { roleKw, companyHint, limit };
}

// Strip filler words from "show me X" → "X" so company/industry lookup
// doesn't try to resolve "me X". Same for "find a/the/an X", "give me X",
// "tell me about X", "what about X". Order matters — strip multi-token
// fillers before single-token articles.
function stripFiller(s: string): string {
  return s
    .replace(/^(?:give me|tell me about|what(?:'?s)? about|how about|let me see)\s+/i, "")
    .replace(/^(?:me|us|the|a|an|all|any)\s+/i, "")
    .trim();
}

function findPersonByName(ctx: AgentContext, q: string): string | null {
  const nq = q.toLowerCase().trim();
  if (nq.length < 3) return null;
  // Score: exact full-name > startsWith > includes — mirrors human intent.
  let best: { id: string; rank: number } | null = null;
  for (const n of ctx.nodes) {
    if (n.kind !== "person") continue;
    const nn = n.name.toLowerCase();
    let rank = 0;
    if (nn === nq) rank = 100;
    else if (nn.startsWith(nq)) rank = 80;
    else if (nn.includes(nq)) rank = 60;
    if (rank > 0 && (!best || rank > best.rank)) best = { id: n.id, rank };
  }
  return best?.id ?? null;
}

function topPeopleByScore(ctx: AgentContext, limit: number, filter?: (n: GraphNode) => boolean): GraphNode[] {
  const people = ctx.nodes.filter(
    (n): n is GraphNode => n.kind === "person" && (!filter || filter(n)),
  );
  people.sort((a, b) => ((b as { score?: number }).score ?? 0) - ((a as { score?: number }).score ?? 0));
  return people.slice(0, limit);
}

function cannedTurn(userText: string, ctx: AgentContext): { reply: string; toolResults: ServerToolResult[] } {
  const t = userText.trim();
  if (!t) return { reply: "Ask me about a company, an industry, or a role and I'll surface matches on the graph.", toolResults: [] };
  const lc = t.toLowerCase();

  // ── Pattern 0: "top N (people|leaders|prospects)" — score-sorted feed ────
  // Catches "show top 10", "highest-scoring people", "best 5 prospects",
  // "who are the top candidates" etc. before the more specific patterns.
  if (
    /\b(top|best|highest[- ]?scoring|highest[- ]?trust|highest)\b/i.test(lc) &&
    /\b(people|leaders|prospects|candidates|matches|hits)\b/i.test(lc)
  ) {
    const numMatch = /\b(\d{1,3})\b/.exec(lc);
    const limit = numMatch ? Math.min(40, Math.max(1, parseInt(numMatch[1]!, 10))) : 10;
    const top = topPeopleByScore(ctx, limit);
    if (top.length > 0) {
      return {
        reply: `Top **${top.length}** prospects by overall score. Highlighting them on the graph.`,
        toolResults: [
          {
            name: "filter",
            arguments: { sort: "overall_score", limit },
            result: {
              count: top.length,
              prospects: top.map((n) => ({
                id: n.id.replace(/^person:/, ""),
                name: n.name,
                role: (n as { role?: string }).role,
                overall_score: (n as { score?: number }).score,
              })),
            },
          },
        ],
      };
    }
  }

  // ── Pattern 1: "show ___" or "focus on ___" or "find ___" — entity focus
  const focusMatch = /^(?:show|focus(?: on)?|find|open|highlight|jump to|go to|search)\s+(.+)/i.exec(t);
  const target = focusMatch?.[1]?.trim() ? stripFiller(focusMatch[1].trim()) : null;

  if (target) {
    // Industry first (semiconductors, defense, etc.)
    const ind = findIndustryId(ctx, target);
    if (ind) {
      const node = ctx.nodes.find((n) => n.id === ind);
      return {
        reply: `Highlighting the **${node?.name ?? target}** cluster — companies in that vertical and the people working there.`,
        toolResults: [
          {
            name: "focus_node",
            arguments: { query: target, kind: "industry" },
            result: { results: [{ id: ind, kind: "industry", name: node?.name ?? target }] },
          },
        ],
      };
    }
    // Company match (NVIDIA, Intel, Micron, ASML, etc.)
    const co = findCompanyId(ctx, target);
    if (co) {
      const node = ctx.nodes.find((n) => n.id === co);
      const peers = topPersonIdsAtCompany(ctx, co, 20);
      return {
        reply: `Focused on **${node?.name ?? target}** — surfacing the top ${peers.length} people on the graph by overall score.`,
        toolResults: [
          {
            name: "focus_node",
            arguments: { query: target, kind: "company" },
            result: { results: [{ id: co, kind: "company", name: node?.name ?? target }] },
          },
        ],
      };
    }
    // Person name fallback — "show Marc Hamilton" / "find Sarah Chen"
    const person = findPersonByName(ctx, target);
    if (person) {
      const n = ctx.nodes.find((nn) => nn.id === person);
      const role = (n as { role?: string } | undefined)?.role ?? "";
      const score = (n as { score?: number } | undefined)?.score ?? 0;
      return {
        reply: `Opening **${n?.name ?? target}**${role ? ` — ${role}` : ""}${score > 0 ? ` (score ${Math.round(score)})` : ""}. Right rail has the full evidence trail.`,
        toolResults: [
          {
            name: "focus_node",
            arguments: { query: target, kind: "person" },
            result: { results: [{ id: person.replace(/^person:/, ""), kind: "person", name: n?.name ?? target }] },
          },
        ],
      };
    }
  }

  // ── Pattern 2: "VPs at Nvidia", "directors of engineering at Intel", "ceos in semiconductors"
  if (/\b(vp|cto|ceo|coo|cfo|director|head of|vice president|engineer|architect|principal|founder)\b/i.test(lc)) {
    const { roleKw, companyHint, limit } = tokenizeRoleQuery(lc);
    let restrictCompany: string | null = null;
    if (companyHint) restrictCompany = findCompanyId(ctx, companyHint);
    const personIds: string[] = [];
    for (const n of ctx.nodes) {
      if (n.kind !== "person") continue;
      const role = (n as { role?: string }).role ?? "";
      if (roleKw && !role.toLowerCase().includes(roleKw.replace(" of engineering", "").replace("vp", "vp "))) {
        if (!role.toLowerCase().includes(roleKw)) continue;
      }
      if (restrictCompany) {
        const at = ctx.edges.find(
          (e) => e.kind === "works_at" && (
            (e.source === n.id && e.target === restrictCompany) ||
            (e.target === n.id && e.source === restrictCompany)
          ),
        );
        if (!at) continue;
      }
      personIds.push(n.id);
      if (personIds.length >= limit) break;
    }
    if (personIds.length > 0) {
      const restrictName = restrictCompany
        ? (ctx.nodes.find((n) => n.id === restrictCompany)?.name ?? "the company")
        : "all companies";
      return {
        reply: `Found **${personIds.length}** matching ${roleKw ?? "candidates"} at ${restrictName}. Highlighting them on the graph.`,
        toolResults: [
          {
            name: "filter",
            arguments: { role: roleKw, company: companyHint, limit },
            result: {
              count: personIds.length,
              prospects: personIds.map((id) => ({ id: id.replace(/^person:/, "") })),
            },
          },
        ],
      };
    }
  }

  // ── Pattern 3: "explain ___" — open inspector on best entity match
  const explainMatch = /^(?:explain|why|tell me about|describe|who is)\s+(.+)/i.exec(t);
  if (explainMatch) {
    const q = stripFiller(explainMatch[1].trim());
    const co = findCompanyId(ctx, q);
    if (co) {
      const node = ctx.nodes.find((n) => n.id === co);
      return {
        reply: `Opening the inspector for **${node?.name ?? q}**. Right rail shows the firmographics + ICP fit.`,
        toolResults: [
          {
            name: "explain",
            arguments: { query: q },
            result: { node: { id: co, kind: "company" } },
          },
        ],
      };
    }
    const person = findPersonByName(ctx, q);
    if (person) {
      const n = ctx.nodes.find((nn) => nn.id === person);
      return {
        reply: `Opening the inspector for **${n?.name ?? q}**. Right rail shows the score breakdown + signal evidence.`,
        toolResults: [
          {
            name: "explain",
            arguments: { query: q, kind: "person" },
            result: { node: { id: person.replace(/^person:/, ""), kind: "person", name: n?.name ?? q } },
          },
        ],
      };
    }
  }

  // ── Pattern 4: bare entity names ("Nvidia", "Intel", "Marc Hamilton") ──
  // Lots of users don't bother with verbs. If the entire query resolves to
  // a known company/industry/person, treat it as an implicit focus.
  if (lc.length > 2 && lc.length < 60 && !/[?!.]\s/.test(t)) {
    const ind = findIndustryId(ctx, lc);
    if (ind) {
      const node = ctx.nodes.find((n) => n.id === ind);
      return {
        reply: `Highlighting the **${node?.name ?? lc}** cluster.`,
        toolResults: [
          {
            name: "focus_node",
            arguments: { query: lc, kind: "industry" },
            result: { results: [{ id: ind, kind: "industry", name: node?.name ?? lc }] },
          },
        ],
      };
    }
    const co = findCompanyId(ctx, lc);
    if (co) {
      const node = ctx.nodes.find((n) => n.id === co);
      return {
        reply: `Focused on **${node?.name ?? lc}**.`,
        toolResults: [
          {
            name: "focus_node",
            arguments: { query: lc, kind: "company" },
            result: { results: [{ id: co, kind: "company", name: node?.name ?? lc }] },
          },
        ],
      };
    }
    const person = findPersonByName(ctx, lc);
    if (person) {
      const n = ctx.nodes.find((nn) => nn.id === person);
      const role = (n as { role?: string } | undefined)?.role ?? "";
      return {
        reply: `Opening **${n?.name ?? lc}**${role ? ` — ${role}` : ""}.`,
        toolResults: [
          {
            name: "focus_node",
            arguments: { query: lc, kind: "person" },
            result: { results: [{ id: person.replace(/^person:/, ""), kind: "person", name: n?.name ?? lc }] },
          },
        ],
      };
    }
  }

  // Default — answer from the snapshot stats so the assistant never returns silence.
  const counts: Record<string, number> = {};
  for (const n of ctx.nodes) counts[n.kind] = (counts[n.kind] ?? 0) + 1;
  const summary = Object.entries(counts)
    .map(([k, v]) => `${v} ${k}${v === 1 ? "" : "s"}`)
    .join(" · ");
  return {
    reply:
      `I'm in offline-demo mode. Try one of:\n` +
      `- *show Nvidia* — focus a company\n` +
      `- *VPs of engineering at Intel* — role + company filter\n` +
      `- *top 10 prospects* — score-sorted list\n` +
      `- *explain Lockheed Martin* — open inspector\n\n` +
      `Currently rendered: ${summary}.`,
    toolResults: [],
  };
}

async function runCannedAgent(
  messages: ChatMessage[],
  ctx: AgentContext,
): Promise<RunAgentResult> {
  const lastUser = [...messages].reverse().find((m) => m.role === "user");
  const { reply, toolResults } = cannedTurn(lastUser?.content ?? "", ctx);
  const toolCalls: ToolCallTrace[] = toolResults.map((tr) => {
    applyToolResult(tr, ctx);
    return { name: tr.name, args: tr.arguments, result: tr.result };
  });
  const updated: ChatMessage[] = [
    ...messages,
    { role: "assistant", content: reply, toolCalls },
  ];
  return { finalText: reply, toolCalls, messages: updated };
}

export async function runAgent(
  messages: ChatMessage[],
  ctx: AgentContext,
): Promise<RunAgentResult> {
  if (USE_CANNED) {
    // Same fake-thinking delay range as the canned-prompt path so ad-hoc
    // free-text queries also feel like they're round-tripping a model.
    await new Promise((r) => setTimeout(r, 2800 + Math.random() * 1500));
    return runCannedAgent(messages, ctx);
  }

  let resp: Response;
  try {
    resp = await fetch(`${API_URL}/chat`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        messages: toServerMessages(messages),
        snapshot: buildSnapshot(ctx),
      }),
    });
  } catch {
    return runCannedAgent(messages, ctx);
  }

  if (!resp.ok) {
    // Hard failure from the backend (5xx, schema mismatch) — also fall back.
    if (resp.status >= 500 || resp.status === 404) return runCannedAgent(messages, ctx);
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

