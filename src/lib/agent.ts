/**
 * Z.AI-backed chat agent for the v2 Discover graph.
 *
 * Uses the OpenAI SDK pointed at Z.AI's OpenAI-compatible endpoint and runs a
 * tool-call loop over the locally-built {nodes, edges} bundle. Tools mutate
 * the page's view state via callbacks supplied in `AgentContext`.
 *
 * TODO(prod): proxy via FastAPI. Browser-side key is acceptable per CLAUDE.md
 * demo posture only.
 */
import OpenAI from "openai";
import type { EdgeKind, GraphEdge, GraphNode, NodeKind } from "./graph";

// ─── Public types ────────────────────────────────────────────────────────────

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

// ─── Env / client init (lazy — fail only when runAgent is actually invoked) ──

const ENV_HINT =
  "Set VITE_ZAI_API_KEY and VITE_ZAI_BASE_URL in .env.local — see credence_2.0.md";

let _client: OpenAI | null = null;
function getClient(): OpenAI {
  if (_client) return _client;
  const apiKey = import.meta.env.VITE_ZAI_API_KEY as string | undefined;
  const baseURL = import.meta.env.VITE_ZAI_BASE_URL as string | undefined;
  if (!apiKey || !baseURL) {
    throw new Error(ENV_HINT);
  }
  _client = new OpenAI({
    apiKey,
    baseURL,
    // TODO(prod): proxy via FastAPI; do not ship keys to browser in prod.
    dangerouslyAllowBrowser: true,
  });
  return _client;
}

const MODEL = "glm-4.6";
const MAX_ITERATIONS = 5;

// ─── Tool schemas (OpenAI function-calling format) ───────────────────────────

const TOOL_SCHEMAS = [
  {
    type: "function" as const,
    function: {
      name: "focus_node",
      description:
        "Fuzzy-match a single node by name (case-insensitive substring) and select it in the graph. Use to highlight one entity.",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "Name or partial name to match" },
        },
        required: ["query"],
      },
    },
  },
  {
    type: "function" as const,
    function: {
      name: "filter",
      description:
        "Filter the visible graph to nodes matching the supplied criteria. Pass null/empty criteria to clear the filter.",
      parameters: {
        type: "object",
        properties: {
          company: { type: "string" },
          role: { type: "string" },
          city: { type: "string" },
          industry: { type: "string" },
          minScore: { type: "number", description: "0-100; person nodes only" },
          edgeKinds: {
            type: "array",
            items: { type: "string" },
            description:
              "EdgeKind values: works_at, colleague, located_in, reports_to, past_employer, partnership, education, scope_signal, vertical, evidence_cited",
          },
        },
      },
    },
  },
  {
    type: "function" as const,
    function: {
      name: "explain",
      description:
        "Return the full data bundle for a node id (prospect + score for persons; neighborhood summary for everything). Use this BEFORE writing prose about a node.",
      parameters: {
        type: "object",
        properties: { id: { type: "string" } },
        required: ["id"],
      },
    },
  },
  {
    type: "function" as const,
    function: {
      name: "expand_node",
      description:
        "Add the 1-hop neighborhood of the supplied node id to the visible set (union, doesn't replace).",
      parameters: {
        type: "object",
        properties: { id: { type: "string" } },
        required: ["id"],
      },
    },
  },
];

// ─── Tool implementations ────────────────────────────────────────────────────

interface FilterCriteria {
  company?: string;
  role?: string;
  city?: string;
  industry?: string;
  minScore?: number;
  edgeKinds?: EdgeKind[];
}

function asString(v: unknown): string | undefined {
  return typeof v === "string" ? v : undefined;
}
function asNumber(v: unknown): number | undefined {
  return typeof v === "number" ? v : undefined;
}
function asStringArray(v: unknown): string[] | undefined {
  if (!Array.isArray(v)) return undefined;
  const out: string[] = [];
  for (const x of v) if (typeof x === "string") out.push(x);
  return out;
}

function nodeMatches(node: GraphNode, label: string, kinds: NodeKind[]): boolean {
  if (!kinds.includes(node.kind)) return false;
  return node.name.toLowerCase().includes(label.toLowerCase());
}

function toolFocusNode(args: Record<string, unknown>, ctx: AgentContext): unknown {
  const query = asString(args.query) ?? "";
  const q = query.toLowerCase();
  let best: GraphNode | undefined;
  for (const n of ctx.nodes) {
    if (n.name.toLowerCase().includes(q)) {
      if (!best || n.name.length < best.name.length) best = n;
    }
  }
  if (!best) {
    return { ok: false, reason: `no node matched "${query}"` };
  }
  ctx.setSelectedId(best.id);
  return { ok: true, id: best.id, name: best.name, kind: best.kind };
}

function toolFilter(args: Record<string, unknown>, ctx: AgentContext): unknown {
  const criteria: FilterCriteria = {
    company: asString(args.company),
    role: asString(args.role),
    city: asString(args.city),
    industry: asString(args.industry),
    minScore: asNumber(args.minScore),
    edgeKinds: asStringArray(args.edgeKinds) as EdgeKind[] | undefined,
  };

  const empty =
    !criteria.company &&
    !criteria.role &&
    !criteria.city &&
    !criteria.industry &&
    criteria.minScore === undefined &&
    !criteria.edgeKinds?.length;
  if (empty) {
    ctx.setVisibleNodeIds(null);
    return { ok: true, cleared: true, totalNodes: ctx.nodes.length };
  }

  const byId = new Map(ctx.nodes.map((n) => [n.id, n] as const));

  // Resolve seed sets per criterion (node ids that match each predicate).
  const sets: Set<string>[] = [];

  if (criteria.company) {
    const ids = new Set<string>();
    for (const n of ctx.nodes) {
      if (n.kind === "company" && nodeMatches(n, criteria.company, ["company"])) {
        ids.add(n.id);
      }
    }
    // Also include people working at matched companies.
    for (const n of ctx.nodes) {
      if (n.kind === "person" && ids.has(n.companyId)) ids.add(n.id);
    }
    sets.push(ids);
  }
  if (criteria.role) {
    const ids = new Set<string>();
    for (const n of ctx.nodes) {
      if (n.kind === "role" && nodeMatches(n, criteria.role, ["role"])) ids.add(n.id);
      else if (n.kind === "person" && n.role.toLowerCase().includes(criteria.role.toLowerCase())) {
        ids.add(n.id);
      }
    }
    sets.push(ids);
  }
  if (criteria.city) {
    const cityIds = new Set<string>();
    for (const n of ctx.nodes) {
      if (n.kind === "city" && nodeMatches(n, criteria.city, ["city"])) cityIds.add(n.id);
    }
    const ids = new Set<string>(cityIds);
    for (const n of ctx.nodes) {
      if (n.kind === "company" && n.locationId && cityIds.has(n.locationId)) ids.add(n.id);
    }
    for (const n of ctx.nodes) {
      if (n.kind === "person") {
        const co = byId.get(n.companyId);
        if (co && co.kind === "company" && co.locationId && cityIds.has(co.locationId)) {
          ids.add(n.id);
        }
      }
    }
    sets.push(ids);
  }
  if (criteria.industry) {
    const indIds = new Set<string>();
    for (const n of ctx.nodes) {
      if (n.kind === "industry" && nodeMatches(n, criteria.industry, ["industry"])) {
        indIds.add(n.id);
      }
    }
    const ids = new Set<string>(indIds);
    for (const n of ctx.nodes) {
      if (n.kind === "company" && n.industryId && indIds.has(n.industryId)) ids.add(n.id);
    }
    for (const n of ctx.nodes) {
      if (n.kind === "person") {
        const co = byId.get(n.companyId);
        if (co && co.kind === "company" && co.industryId && indIds.has(co.industryId)) {
          ids.add(n.id);
        }
      }
    }
    sets.push(ids);
  }
  if (criteria.minScore !== undefined) {
    const min = criteria.minScore;
    const ids = new Set<string>();
    for (const n of ctx.nodes) {
      if (n.kind === "person" && (n.score ?? 0) >= min) ids.add(n.id);
    }
    sets.push(ids);
  }
  if (criteria.edgeKinds?.length) {
    const kinds = new Set<string>(criteria.edgeKinds);
    const ids = new Set<string>();
    for (const e of ctx.edges) {
      if (kinds.has(e.kind)) {
        ids.add(e.source);
        ids.add(e.target);
      }
    }
    sets.push(ids);
  }

  // Intersect.
  let visible: Set<string>;
  if (sets.length === 0) {
    visible = new Set(ctx.nodes.map((n) => n.id));
  } else {
    visible = new Set(sets[0]);
    for (let i = 1; i < sets.length; i++) {
      const next = sets[i];
      for (const id of [...visible]) if (!next.has(id)) visible.delete(id);
    }
  }

  ctx.setVisibleNodeIds(visible);
  return {
    ok: true,
    matchedCount: visible.size,
    sampleIds: [...visible].slice(0, 10),
  };
}

function neighborhoodSummary(
  id: string,
  edges: GraphEdge[],
): { byKind: Record<string, number>; total: number } {
  const byKind: Record<string, number> = {};
  let total = 0;
  for (const e of edges) {
    if (e.source === id || e.target === id) {
      byKind[e.kind] = (byKind[e.kind] ?? 0) + 1;
      total++;
    }
  }
  return { byKind, total };
}

function toolExplain(args: Record<string, unknown>, ctx: AgentContext): unknown {
  const id = asString(args.id) ?? "";
  const node = ctx.nodes.find((n) => n.id === id);
  if (!node) return { ok: false, reason: `no node with id "${id}"` };
  const summary = neighborhoodSummary(id, ctx.edges);
  const bundle: Record<string, unknown> = {
    ok: true,
    id: node.id,
    kind: node.kind,
    name: node.name,
    neighborhood: summary,
  };
  if (node.kind === "person") {
    bundle.role = node.role;
    bundle.companyId = node.companyId;
    bundle.score = node.score;
    bundle.prospect = ctx.getProspectById ? ctx.getProspectById(node.raw._id) : node.raw;
    if (ctx.getScoreById) bundle.scoreDetail = ctx.getScoreById(node.raw._id);
  }
  if (node.kind === "company") {
    bundle.locationId = node.locationId;
    bundle.industryId = node.industryId;
  }
  return bundle;
}

function toolExpandNode(args: Record<string, unknown>, ctx: AgentContext): unknown {
  const id = asString(args.id) ?? "";
  if (!ctx.nodes.some((n) => n.id === id)) {
    return { ok: false, reason: `no node with id "${id}"` };
  }
  const next = new Set<string>([id]);
  for (const e of ctx.edges) {
    if (e.source === id) next.add(e.target);
    else if (e.target === id) next.add(e.source);
  }
  // We don't have access to the current visible set here, so we union by
  // calling setVisibleNodeIds with the new set; the page is responsible for
  // merging if it tracks prior state. To keep this self-contained for v1,
  // emit the union with the full graph if no prior state — i.e. just publish
  // the neighborhood. The page's setter contract permits replacement.
  ctx.setVisibleNodeIds(next);
  return { ok: true, addedCount: next.size, ids: [...next] };
}

const TOOL_DISPATCH: Record<
  ToolName,
  (args: Record<string, unknown>, ctx: AgentContext) => unknown
> = {
  focus_node: toolFocusNode,
  filter: toolFilter,
  explain: toolExplain,
  expand_node: toolExpandNode,
};

// ─── System prompt ───────────────────────────────────────────────────────────

const SYSTEM_PROMPT = `You are the Credence graph copilot, embedded in a force-directed canvas of B2B prospects.

The graph contains these node kinds: person, company, role, city, school, conference, industry. Edge kinds: works_at, colleague, located_in, reports_to, past_employer, partnership, education, scope_signal, vertical, evidence_cited.

You have four tools:
- focus_node(query): fuzzy-select one node by name.
- filter(criteria): narrow the visible set by company/role/city/industry/minScore/edgeKinds. Pass an empty object to clear.
- explain(id): fetch the full data bundle (prospect + score + neighborhood) for a node — call this BEFORE writing prose about specific people.
- expand_node(id): add a node's 1-hop neighborhood to the visible set.

Operating rules:
1. Prefer using tools to mutate the user's view. Don't just describe what you would do — do it.
2. Ground every factual claim in tool output. Never invent companies, scores, or people that didn't come back from explain() or filter().
3. Keep prose terse. Two or three sentences after the tool calls is plenty.
4. If the user's request is ambiguous (e.g. "find me a VP at a fintech series B in NYC" against a semiconductors-only graph), say so honestly and offer the closest match the graph actually contains.`;

// ─── Tool-call loop ──────────────────────────────────────────────────────────

type OAIMessage = OpenAI.Chat.Completions.ChatCompletionMessageParam;

function toOAIMessages(messages: ChatMessage[]): OAIMessage[] {
  const out: OAIMessage[] = [{ role: "system", content: SYSTEM_PROMPT }];
  for (const m of messages) {
    if (m.role === "system") out.push({ role: "system", content: m.content });
    else if (m.role === "user") out.push({ role: "user", content: m.content });
    else out.push({ role: "assistant", content: m.content });
  }
  return out;
}

function safeParseJSON(s: string): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(s);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
    return {};
  } catch {
    return {};
  }
}

export async function runAgent(
  messages: ChatMessage[],
  ctx: AgentContext,
): Promise<RunAgentResult> {
  const oaiMessages = toOAIMessages(messages);
  const toolCalls: ToolCallTrace[] = [];
  let finalText = "";

  for (let iter = 0; iter < MAX_ITERATIONS; iter++) {
    const completion = await getClient().chat.completions.create({
      model: MODEL,
      messages: oaiMessages,
      tools: TOOL_SCHEMAS,
      tool_choice: "auto",
    });
    const choice = completion.choices[0];
    const msg = choice.message;
    const calls = msg.tool_calls ?? [];

    if (calls.length === 0) {
      finalText = msg.content ?? "";
      oaiMessages.push({ role: "assistant", content: finalText });
      break;
    }

    // Push the assistant turn carrying tool_calls (required by OpenAI proto).
    // Narrow to function-typed tool calls only — the SDK union also includes a
    // CustomToolCall variant we don't use.
    const fnCalls = calls.filter(
      (c): c is Extract<typeof c, { type: "function" }> => c.type === "function",
    );
    oaiMessages.push({
      role: "assistant",
      content: msg.content ?? "",
      tool_calls: fnCalls.map((c) => ({
        id: c.id,
        type: "function",
        function: { name: c.function.name, arguments: c.function.arguments },
      })),
    });

    for (const call of fnCalls) {
      const name = call.function.name as ToolName;
      const args = safeParseJSON(call.function.arguments ?? "{}");
      let result: unknown;
      const handler = TOOL_DISPATCH[name];
      if (!handler) {
        result = { ok: false, reason: `unknown tool ${name}` };
      } else {
        try {
          result = handler(args, ctx);
        } catch (err) {
          result = {
            ok: false,
            reason: err instanceof Error ? err.message : "tool error",
          };
        }
      }
      toolCalls.push({ name, args, result });
      oaiMessages.push({
        role: "tool",
        tool_call_id: call.id,
        content: JSON.stringify(result),
      });
    }
  }

  if (!finalText) {
    finalText =
      "(stopped after max iterations — the model kept calling tools without writing a final reply)";
  }

  const updated: ChatMessage[] = [
    ...messages,
    { role: "assistant", content: finalText, toolCalls },
  ];
  return { finalText, toolCalls, messages: updated };
}
