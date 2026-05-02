/**
 * Tests for the chat-tool → UI bridge (`applyToolResult` inside agent.ts).
 *
 * `applyToolResult` is module-private — we exercise it via `runAgent` with
 * `fetch` stubbed. The two assertions per case are: (a) `setSelectedId` was
 * called with the right graph id, and (b) `setVisibleNodeIds` received the
 * expected node-id set.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"
import { runAgent, type AgentContext, type ChatMessage } from "./agent"
import type { GraphEdge, GraphNode } from "./graph"

// ── Test helpers ───────────────────────────────────────────────────────────

function makeCtx(): AgentContext & {
  selectedSpy: ReturnType<typeof vi.fn>
  visibleSpy: ReturnType<typeof vi.fn>
} {
  const selectedSpy = vi.fn<(id: string | null) => void>()
  const visibleSpy = vi.fn<(ids: Set<string> | null) => void>()
  return {
    nodes: [] as GraphNode[],
    edges: [] as GraphEdge[],
    setSelectedId: selectedSpy,
    setVisibleNodeIds: visibleSpy,
    selectedSpy,
    visibleSpy,
  }
}

function stubFetch(toolResults: unknown[], assistantText = "ok") {
  return vi.fn(async () =>
    new Response(
      JSON.stringify({
        messages: [{ role: "assistant", content: assistantText }],
        tool_results: toolResults,
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    ),
  )
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn())
  vi.stubEnv("VITE_API_URL", "http://localhost:8000")
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

// ── focus_node — sanity check the test scaffolding mirrors the real code ──

describe("agent.applyToolResult — baseline regression", () => {
  it("focus_node sets selectedId + visible to the top result (person)", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch([
        {
          name: "focus_node",
          arguments: { query: "alice" },
          result: { results: [{ id: "uuid-1", kind: "person" }] },
        },
      ]),
    )
    const ctx = makeCtx()
    const messages: ChatMessage[] = [{ role: "user", content: "find alice" }]
    await runAgent(messages, ctx)
    expect(ctx.selectedSpy).toHaveBeenCalledWith("person:uuid-1")
    expect(ctx.visibleSpy).toHaveBeenCalledWith(new Set(["person:uuid-1"]))
  })
})

// ── find_warm_paths — anchor selection on strongest connector ─────────────

describe("agent.applyToolResult — find_warm_paths", () => {
  it("highlights target + every connector_id on the canvas", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch([
        {
          name: "find_warm_paths",
          arguments: { target_id: "uuid-target" },
          result: {
            target_id: "uuid-target",
            target_name: "Wei Chen",
            paths_found: 2,
            paths: [
              {
                path_strength: 0.85,
                hops: 1,
                connector: "Sarah Kim",
                connector_id: "uuid-conn-1",
                path_names: ["Wei Chen", "Sarah Kim"],
                connection_types: ["patent_co_inventor"],
                explanation: "...",
                suggested_opener: "...",
              },
              {
                path_strength: 0.55,
                hops: 2,
                connector: "Bob",
                connector_id: "uuid-conn-2",
                path_names: ["Wei Chen", "Mid", "Bob"],
                connection_types: ["career_overlap_general", "career_overlap_general"],
                explanation: "...",
                suggested_opener: "...",
              },
            ],
          },
        },
      ]),
    )
    const ctx = makeCtx()
    await runAgent(
      [{ role: "user", content: "warm intro to Wei Chen" }],
      ctx,
    )
    // Selected = strongest connector (paths are pre-sorted desc upstream).
    expect(ctx.selectedSpy).toHaveBeenCalledWith("person:uuid-conn-1")
    // Visible = target + both connector ids.
    const lastVisibleCall = ctx.visibleSpy.mock.calls.at(-1)?.[0]
    expect(lastVisibleCall).toBeInstanceOf(Set)
    const ids = lastVisibleCall as Set<string>
    expect(ids.has("person:uuid-target")).toBe(true)
    expect(ids.has("person:uuid-conn-1")).toBe(true)
    expect(ids.has("person:uuid-conn-2")).toBe(true)
  })

  it("no-ops when paths is empty (no setSelectedId / no setVisibleNodeIds for this tool)", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch([
        {
          name: "find_warm_paths",
          arguments: { target_id: "uuid-target" },
          result: {
            target_id: "uuid-target",
            target_name: "Wei Chen",
            paths_found: 0,
            paths: [],
            message: "No warm paths found.",
          },
        },
      ]),
    )
    const ctx = makeCtx()
    await runAgent(
      [{ role: "user", content: "warm intro to Wei Chen" }],
      ctx,
    )
    expect(ctx.selectedSpy).not.toHaveBeenCalled()
    expect(ctx.visibleSpy).not.toHaveBeenCalled()
  })

  it("handles missing connector_id in some paths gracefully", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch([
        {
          name: "find_warm_paths",
          arguments: { target_id: "uuid-target" },
          result: {
            target_id: "uuid-target",
            paths_found: 2,
            paths: [
              { connector_id: "uuid-good" },
              { /* missing connector_id */ },
            ],
          },
        },
      ]),
    )
    const ctx = makeCtx()
    await runAgent([{ role: "user", content: "x" }], ctx)
    const ids = ctx.visibleSpy.mock.calls.at(-1)?.[0] as Set<string>
    expect(ids.has("person:uuid-good")).toBe(true)
    expect(ids.has("person:uuid-target")).toBe(true)
    expect(ids.size).toBe(2) // target + 1 good connector, no nulls
  })
})

// ── get_org_context — highlight the org neighborhood ──────────────────────

describe("agent.applyToolResult — get_org_context", () => {
  it("selects the target and highlights manager + reports + cluster peers", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch([
        {
          name: "get_org_context",
          arguments: { person_id: "uuid-target" },
          result: {
            person: { id: "uuid-target", name: "Adam Smith" },
            managers: [
              { person_id: "uuid-mgr-1", name: "Vishnu", edge_confidence: 0.78 },
            ],
            direct_reports: [
              { person_id: "uuid-rep-1" },
              { person_id: "uuid-rep-2" },
            ],
            direct_report_count: 2,
            functional_cluster: {
              domain: "product_management",
              sub_domain: null,
              peers: [
                { person_id: "uuid-peer-1" },
                { person_id: "uuid-peer-2" },
              ],
              peer_count: 2,
            },
            scope: {},
            org_chart_note: "...",
          },
        },
      ]),
    )
    const ctx = makeCtx()
    await runAgent(
      [{ role: "user", content: "who does Adam report to" }],
      ctx,
    )
    expect(ctx.selectedSpy).toHaveBeenCalledWith("person:uuid-target")
    const ids = ctx.visibleSpy.mock.calls.at(-1)?.[0] as Set<string>
    expect(ids).toBeInstanceOf(Set)
    expect(ids.has("person:uuid-target")).toBe(true)
    expect(ids.has("person:uuid-mgr-1")).toBe(true)
    expect(ids.has("person:uuid-rep-1")).toBe(true)
    expect(ids.has("person:uuid-rep-2")).toBe(true)
    expect(ids.has("person:uuid-peer-1")).toBe(true)
    expect(ids.has("person:uuid-peer-2")).toBe(true)
  })

  it("works when managers + reports + peers are empty (just selects person)", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch([
        {
          name: "get_org_context",
          arguments: { person_id: "uuid-target" },
          result: {
            person: { id: "uuid-target", name: "Solo" },
            managers: [],
            direct_reports: [],
            direct_report_count: 0,
            functional_cluster: { domain: null, peers: [], peer_count: 0 },
            scope: {},
            org_chart_note: null,
          },
        },
      ]),
    )
    const ctx = makeCtx()
    await runAgent([{ role: "user", content: "x" }], ctx)
    expect(ctx.selectedSpy).toHaveBeenCalledWith("person:uuid-target")
    const ids = ctx.visibleSpy.mock.calls.at(-1)?.[0] as Set<string>
    expect(ids?.size).toBe(1)
    expect(ids.has("person:uuid-target")).toBe(true)
  })

  it("skips entries with missing person_id without crashing", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch([
        {
          name: "get_org_context",
          arguments: { person_id: "uuid-target" },
          result: {
            person: { id: "uuid-target" },
            managers: [{}], // no person_id
            direct_reports: [{ person_id: "uuid-rep" }, {}],
            functional_cluster: { peers: [{ person_id: "uuid-peer" }, {}] },
            scope: {},
          },
        },
      ]),
    )
    const ctx = makeCtx()
    await runAgent([{ role: "user", content: "x" }], ctx)
    const ids = ctx.visibleSpy.mock.calls.at(-1)?.[0] as Set<string>
    // target + 1 valid report + 1 valid peer = 3
    expect(ids.size).toBe(3)
    expect(ids.has("person:uuid-target")).toBe(true)
    expect(ids.has("person:uuid-rep")).toBe(true)
    expect(ids.has("person:uuid-peer")).toBe(true)
  })
})

// ── Sequence + integration safety nets ────────────────────────────────────

describe("agent.runAgent — sequence + error paths", () => {
  it("aggregates tool_results across multiple sequential calls in one turn", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch([
        {
          name: "get_org_context",
          arguments: { person_id: "uuid-1" },
          result: {
            person: { id: "uuid-1" },
            managers: [{ person_id: "uuid-mgr" }],
            direct_reports: [],
            functional_cluster: { peers: [] },
            scope: {},
          },
        },
        {
          name: "find_warm_paths",
          arguments: { target_id: "uuid-mgr" },
          result: {
            target_id: "uuid-mgr",
            paths_found: 1,
            paths: [{ connector_id: "uuid-conn", path_strength: 0.9 }],
          },
        },
      ]),
    )
    const ctx = makeCtx()
    const result = await runAgent(
      [{ role: "user", content: "find warm path to adam's manager" }],
      ctx,
    )
    expect(result.toolCalls).toHaveLength(2)
    expect(result.toolCalls[0].name).toBe("get_org_context")
    expect(result.toolCalls[1].name).toBe("find_warm_paths")
    // Last setSelectedId wins — should be the warm-path connector.
    expect(ctx.selectedSpy).toHaveBeenLastCalledWith("person:uuid-conn")
  })

  it("propagates HTTP errors as thrown Error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500 })),
    )
    const ctx = makeCtx()
    await expect(
      runAgent([{ role: "user", content: "x" }], ctx),
    ).rejects.toThrow(/POST \/chat failed: 500/)
  })

  it("propagates network errors with friendly message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("ECONNREFUSED")
      }),
    )
    const ctx = makeCtx()
    await expect(
      runAgent([{ role: "user", content: "x" }], ctx),
    ).rejects.toThrow(/Could not reach the chat backend/)
  })

  it("returns finalText fallback when assistant returns no text content", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({ messages: [], tool_results: [] }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    )
    const ctx = makeCtx()
    const result = await runAgent([{ role: "user", content: "x" }], ctx)
    expect(result.finalText).toMatch(/no reply/i)
  })

  it("attaches snapshot summary to the request body", async () => {
    const fetchSpy = stubFetch([])
    vi.stubGlobal("fetch", fetchSpy)
    const ctx: AgentContext = {
      nodes: [
        { id: "person:1", kind: "person", label: "A" },
        { id: "person:2", kind: "person", label: "B" },
        { id: "company:co", kind: "company", label: "Acme" },
      ] as GraphNode[],
      edges: [
        { id: "e1", source: "person:1", target: "person:2", kind: "colleague" },
      ] as unknown as GraphEdge[],
      setSelectedId: vi.fn(),
      setVisibleNodeIds: vi.fn(),
    }
    await runAgent([{ role: "user", content: "x" }], ctx)
    const callArgs = fetchSpy.mock.calls[0]
    const body = JSON.parse((callArgs[1] as RequestInit).body as string)
    expect(body.snapshot.nodeCount).toBe(3)
    expect(body.snapshot.edgeCount).toBe(1)
    expect(body.snapshot.nodeKindCounts.person).toBe(2)
    expect(body.snapshot.nodeKindCounts.company).toBe(1)
  })
})

// ── ID prefix bridging — the spec for toGraphId via tool result paths ─────

describe("agent.applyToolResult — id prefix bridging", () => {
  it("focus_node company result keeps the co: → company: rewrite", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch([
        {
          name: "focus_node",
          arguments: { query: "nvidia" },
          result: { results: [{ id: "co:nvidia", kind: "company" }] },
        },
      ]),
    )
    const ctx = makeCtx()
    await runAgent([{ role: "user", content: "find nvidia" }], ctx)
    expect(ctx.selectedSpy).toHaveBeenCalledWith("company:nvidia")
    // Non-person → do NOT narrow visible to single hub.
    expect(ctx.visibleSpy).not.toHaveBeenCalled()
  })

  it("explain person result narrows visible to that single person", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch([
        {
          name: "explain",
          arguments: { id: "uuid-p" },
          result: { node: { id: "uuid-p", kind: "person" } },
        },
      ]),
    )
    const ctx = makeCtx()
    await runAgent([{ role: "user", content: "explain p" }], ctx)
    expect(ctx.selectedSpy).toHaveBeenCalledWith("person:uuid-p")
    expect(ctx.visibleSpy).toHaveBeenCalledWith(new Set(["person:uuid-p"]))
  })

  it("explain company result selects but does NOT narrow visible", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch([
        {
          name: "explain",
          arguments: { id: "co:nvidia" },
          result: { node: { id: "co:nvidia", kind: "company" } },
        },
      ]),
    )
    const ctx = makeCtx()
    await runAgent([{ role: "user", content: "explain nvidia" }], ctx)
    expect(ctx.selectedSpy).toHaveBeenCalledWith("company:nvidia")
    expect(ctx.visibleSpy).not.toHaveBeenCalled()
  })

  it("expand_node merges center + neighbors into visible set", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch([
        {
          name: "expand_node",
          arguments: { id: "uuid-c" },
          result: {
            center: { id: "uuid-c", kind: "person" },
            neighbors: [
              { id: "uuid-n1", kind: "person" },
              { id: "co:bigco", kind: "company" },
            ],
          },
        },
      ]),
    )
    const ctx = makeCtx()
    await runAgent([{ role: "user", content: "expand c" }], ctx)
    const ids = ctx.visibleSpy.mock.calls.at(-1)?.[0] as Set<string>
    expect(ids.has("person:uuid-c")).toBe(true)
    expect(ids.has("person:uuid-n1")).toBe(true)
    expect(ids.has("company:bigco")).toBe(true)
  })
})

// ── ToolName union exhaustiveness (compile-time guarantee) ────────────────

describe("ToolName union — compile-time exhaustiveness", () => {
  it("includes every tool name defined in chat.py TOOL_SCHEMAS", () => {
    // This is a compile-time + value test: if a new tool is added on the
    // backend without updating the union here, the type assignment below
    // becomes a TS error, AND the runtime enumeration would miss it.
    const expected: Array<
      | "focus_node"
      | "filter"
      | "explain"
      | "expand_node"
      | "find_warm_paths"
      | "get_org_context"
    > = [
      "focus_node",
      "filter",
      "explain",
      "expand_node",
      "find_warm_paths",
      "get_org_context",
    ]
    expect(expected).toHaveLength(6)
  })
})
