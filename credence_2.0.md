---
name: graph chat pivot (v2 — replaces Discover)
overview: "Pivot Credence into a LinkedIn-meets-Obsidian experience. The new Discover view is a full-page force-directed graph triangulating people across multiple node types (Person, Company, Role, City, Past employer, Partnership, School, Conference, Industry vertical). Three rails: left chat sidebar drives the graph via tool-calls; center is the canvas with edge-type filter pills + halo on selected + neighborhood fade; right is a node-aware inspector panel showing identity, sub-scores, and an evidence trail. Click a person -> still routes to `/prospect/:id` for the deep dive. Figma: https://www.figma.com/design/UTO07RPawGlBolyJrNUQb6"
todos:
  - id: deps_env
    content: Add react-force-graph-2d + openai SDK; mirror ZAI_API_KEY/ZAI_BASE_URL as VITE_-prefixed in .env.local; update .env.example
    status: pending
  - id: design_tokens
    content: "Add CSS variables to src/index.css for new node-type colors (person, company, role, city, school, conference, industry) and edge-type colors (reports, employer, location, evidence, scope, partnership, past-employer, education, vertical) + score-strong/plausible/weak. Mirror the Figma `Credence Tokens` collection."
    status: pending
  - id: mock_enrichment
    content: "Extend src/lib/mockStore.ts with seed data for past_employers, education, partnerships, conferences. New shapes derived (not separate tables): per-prospect `past_companies: string[]`, `education: {school, degree, year}[]`, `talks: {venue, year}[]`; per-company `partnerships: string[]`, `industry: string`. Bias toward 5–10 prospects so the graph is dense enough to demo."
    status: pending
  - id: graph_lib
    content: "Build src/lib/graph.ts: full type union (person | company | role | city | school | conference | industry) + edge kinds (works_at | colleague | located_in | reports_to | past_employer | partnership | education | scope_signal | vertical | evidence_cited). buildGraph() reads useProspects() + useScoresFor() + the new mock fields and emits {nodes, edges}. Inline COMPANY_META for HQ city/country and industry."
    status: pending
  - id: inspector_panel
    content: "Build src/components/NodeInspector.tsx: right rail (~380w) with per-node-type variants. Person: identity card (avatar/name/role/company), score breakdown (4 sub-scores incl. confidence), evidence trail rows (type pill, source, quote, timestamp, confidence dot). Company: firmographics (size, stage, industry), ICP-fit/hiring-velocity/tech-maturity/org-density, evidence (Greenhouse, SEC, press, LinkedIn density). Role: definition + holders count + avg tenure + scope-signal evidence. City: candidate count + company count + avg score + density signals."
    status: pending
  - id: chat_sidebar
    content: "Build src/components/GraphChat.tsx: left rail (~360w) with conversation history (user + assistant turns), hint chips (sample queries), pinned input bar at bottom. Renders tool-call traces inline above assistant text. Uses agent.ts."
    status: pending
  - id: agent_lib
    content: "Build src/lib/agent.ts: OpenAI SDK pointed at Z.AI (glm-5.1) + tool loop. Tools: focus_node(query) -> sets selectedId; filter(criteria: {company?, role?, city?, industry?, edgeKinds?, minScore?}) -> sets visible-set; explain(id) -> returns rich data bundle for the inspector + lets the model write prose; expand_node(id) -> adds neighborhood to visible-set."
    status: pending
  - id: top_bar_filters
    content: "Update src/components/TopBar.tsx: when on /discover, render edge-type filter pills (Reports, Employer, Location, Evidence, Scope, Partnership, Past empl., Education, Vertical) with color dots. Pills toggle visible edge kinds. Active state mirrors the Figma."
    status: pending
  - id: discover_replace
    content: "Replace src/pages/Discover.tsx: full-page layout with TopBar + 3-column body (chat sidebar | graph canvas with subheader | inspector panel). useState for selectedId, edgeKindsVisible, filters, messages. Composes <GraphChat />, <ForceGraph2D />, <NodeInspector />. Subheader shows live stats (nodes/edges/candidates/selected) + legend + zoom controls."
    status: pending
  - id: integration_polish
    content: "End-to-end wiring: chat tools mutate Discover.tsx state; node-click opens inspector with the right variant for that node type; halo + neighborhood-fade rendered via ForceGraph2D nodeCanvasObject; clicking a person inside the inspector still routes to /prospect/:id. Verify with `npm run lint` + `npm test` + manual run of `npm run dev`."
    status: pending
isProject: false
---

## Vision

```mermaid
flowchart LR
    subgraph leftRail [Chat Sidebar 360w]
        chat[GraphChat - sample queries, history, input]
    end
    subgraph canvas [Graph Canvas]
        subheader["Subheader: stats + legend + zoom"]
        subgraph graphArea [ForceGraph2D]
            person((Lin Wei))
            company([TSMC])
            city[/Hsinchu/]
            past([Intel past])
            sch[(NTU)]
            conf((SEMICON))
            ind{{Foundry vertical}}
            person --- company
            person -.- past
            person -.- sch
            person -.- conf
            company --- city
            company --- ind
        end
    end
    subgraph rightRail [Inspector 380w]
        ins[NodeInspector - identity, sub-scores, evidence trail]
    end
    chat -.tool calls.-> graphArea
    graphArea -.selectedId.-> ins
```

- **Three rails**: chat sidebar (left, ~360w) | graph canvas with subheader (center, fluid) | node-aware inspector (right, ~380w)
- **Node types** (9): `person`, `company`, `role`, `city`, `school`, `conference`, `industry`, `past_employer`, `partnership`
- **Edge kinds** (10): `works_at`, `colleague`, `reports_to`, `located_in`, `past_employer`, `partnership`, `education`, `scope_signal`, `vertical`, `evidence_cited`
- **Top-bar filter pills**: when route is `/discover`, render color-dotted toggles for each edge kind (Reports, Employer, Location, Evidence, Scope, Partnership, Past empl., Education, Vertical)
- **Visual affordances**: halo on selected node, neighborhood fade (non-neighbors at low alpha), person size driven by `overall_score`, person color driven by score band (strong / plausible / weak)
- **Click person -> `/prospect/:id`** still works (preserves existing deep-dive)

## Library + provider choices

- **Graph**: `react-force-graph-2d` (canvas, true Obsidian-style physics; `nodeCanvasObject` for per-kind rendering). New dep.
- **AI**: Z.AI's GLM via OpenAI-compatible endpoint (per [docs.z.ai](https://docs.z.ai/guides/overview/quick-start)). `openai` npm SDK pointed at `baseURL: VITE_ZAI_BASE_URL` (`https://api.z.ai/api/paas/v4`), model `glm-5.1`. Standard OpenAI function-calling. `VITE_ZAI_API_KEY` required, hard-fail on init if missing. Browser-side `dangerouslyAllowBrowser: true` (TODO comment to proxy via FastAPI later). Non-streaming for v1.
- **Env wiring**: `.env.local` already has `ZAI_API_KEY` and `ZAI_BASE_URL` (unprefixed — Vite won't expose them). Mirror as `VITE_ZAI_API_KEY` and `VITE_ZAI_BASE_URL`.
- React Flow stays untouched on `/prospect/:id`.

## Data model

**Schema unchanged** — derived graph at render time. Mock-store enriched with three new per-prospect fields and one per-company-derived field.

[src/lib/mockStore.ts](src/lib/mockStore.ts) Prospect type extension (mock-only; Supabase rows ignore unknown fields):

```ts
type Prospect = {
  // ...existing
  past_companies?: string[];                          // ["Intel", "GlobalFoundries"]
  education?: { school: string; degree: string; year: number }[];
  talks?:    { venue: string; year: number }[];       // conferences
};

const COMPANY_META: Record<string, {
  city: string; country: string;
  industry: string;                                   // "Foundry", "EUV Lithography", "GPU Compute"
  partnerships: string[];                             // ["ASML", "Apple"]
}> = { TSMC: { city: "Hsinchu", country: "Taiwan", industry: "Foundry", partnerships: ["ASML", "Apple", "NVIDIA"] }, /* ... */ };
```

[src/lib/graph.ts](src/lib/graph.ts) types:

```ts
export type NodeKind = "person" | "company" | "role" | "city" | "school" | "conference" | "industry" | "past_employer" | "partnership";
export type EdgeKind = "works_at" | "colleague" | "reports_to" | "located_in" | "past_employer" | "partnership" | "education" | "scope_signal" | "vertical" | "evidence_cited";

export type GraphNode =
  | { id: string; kind: "person";        name: string; role: string; companyId: string; score?: number; confidence?: number; raw: Prospect }
  | { id: string; kind: "company";       name: string; cityId?: string; industryId?: string; partnerships: string[] }
  | { id: string; kind: "role";          title: string; holders: number; avgTenure: number }
  | { id: string; kind: "city";          name: string; country: string; candidates: number }
  | { id: string; kind: "school";        name: string }
  | { id: string; kind: "conference";    name: string }
  | { id: string; kind: "industry";      name: string }
  | { id: string; kind: "past_employer"; name: string }
  | { id: string; kind: "partnership";   name: string };

export type GraphEdge = { id: string; source: string; target: string; kind: EdgeKind; weight?: number };

export function buildGraph(args: {
  prospects: Prospect[];
  scores: Record<string, Score>;
  signals?: Record<string, Signal[]>;
}): { nodes: GraphNode[]; edges: GraphEdge[] };
```

Edge derivation rules:

- `works_at`: person -> their `company`
- `colleague`: any two persons sharing the same `companyId` (capped per-company to avoid quadratic blowup at scale)
- `located_in`: company -> city
- `vertical`: company -> industry
- `partnership`: company -> partnership node (deduped string)
- `past_employer`: person -> past_employer node (one node per unique string)
- `education`: person -> school
- `scope_signal`: person -> role node (when title fuzzy-matches a canonical role)
- `evidence_cited`: person -> signal-source node (one node per `signal.source` per person, weight = `signal.confidence`)
- `reports_to`: person -> person (inferred via existing `seniorityRank` from `ProspectDetail.tsx`; only when same company)

## Design tokens (src/index.css)

Add CSS variables under `:root` (and dark twin where the existing theme has one). Names mirror the Figma `Credence Tokens` collection. Placeholder hex values to be replaced once Figma values are in hand.

```css
:root {
  /* node colors — semantic, paired w/ kind */
  --node-person:        220 90% 56%;   /* TODO: Figma */
  --node-company:       260 70% 55%;
  --node-role:          145 55% 45%;
  --node-city:          30  85% 55%;
  --node-school:        200 60% 45%;
  --node-conference:    340 70% 55%;
  --node-industry:      50  85% 50%;
  --node-past-employer: 270 30% 55%;
  --node-partnership:   180 55% 45%;

  /* edge colors — match filter pills */
  --edge-reports:       0   0%  35%;
  --edge-employer:      220 90% 56%;
  --edge-location:      30  85% 55%;
  --edge-evidence:      280 70% 55%;
  --edge-scope:         145 55% 45%;
  --edge-partnership:   180 55% 45%;
  --edge-past-employer: 270 30% 55%;
  --edge-education:     200 60% 45%;
  --edge-vertical:      50  85% 50%;

  /* score bands — for halo + person fill */
  --score-strong:    142 70% 45%;
  --score-plausible: 45  90% 50%;
  --score-weak:      0   70% 55%;
}
```

## File layout

**New (4 files):**

- [src/lib/graph.ts](src/lib/graph.ts) — full type union, `COMPANY_META`, `buildGraph()`, edge-derivation helpers
- [src/lib/agent.ts](src/lib/agent.ts) — OpenAI SDK at Z.AI base URL, 4 tool schemas, tool loop
- [src/components/NodeInspector.tsx](src/components/NodeInspector.tsx) — right rail, per-node-kind variants
- [src/components/GraphChat.tsx](src/components/GraphChat.tsx) — left rail, hint chips + history + input

**Modified (7 files):**

- [package.json](package.json) — add `react-force-graph-2d`, `openai`
- [.env.local](.env.local) — add `VITE_ZAI_API_KEY` + `VITE_ZAI_BASE_URL` mirrors
- [.env.example](.env.example) — same
- [src/index.css](src/index.css) — node + edge + score CSS variables
- [src/lib/mockStore.ts](src/lib/mockStore.ts) — seed `past_companies`, `education`, `talks` on each prospect; bump seed to 8–10 prospects across TSMC/ASML/Intel/NVIDIA/Infineon/AMD/Samsung; add `COMPANY_META` partnerships
- [src/components/TopBar.tsx](src/components/TopBar.tsx) — when `useLocation().pathname === "/discover"`, render edge-kind filter pills with color dots; pills toggle a `Set<EdgeKind>` lifted via context or a small Zustand slice (deferred — for v1, use a top-level state in `Discover.tsx` and read it via a `EdgeFilterContext` provider mounted in `Discover.tsx` so the TopBar pills are bound to the same state)
- [src/pages/Discover.tsx](src/pages/Discover.tsx) — **full replace** of the current table view. Owns: `selectedId`, `edgeKindsVisible: Set<EdgeKind>`, `filters`, `messages`, `hoverNeighborIds`. Composes the three rails.

**Untouched:** `/`, `/validate`, `/settings`, `/prospect/:id`, scoring logic, `db.ts`, Supabase schema.

## Tool catalog (agent.ts)

OpenAI function-calling style; 4 tools.

- `focus_node({ query: string })` — fuzzy-match across all node names; returns `{ id, kind }`. Caller sets `selectedId`.
- `filter({ company?, role?, city?, industry?, edgeKinds?: EdgeKind[], minScore? })` — returns `{ visibleNodeIds, visibleEdgeIds }`. Caller sets `filters` + `edgeKindsVisible`.
- `explain({ id })` — returns the data bundle for the node (shape varies per kind; mirrors NodeInspector variants). Model writes prose using the bundle in its final reply.
- `expand_node({ id, hops?: 1 | 2 })` — BFS from the node up to N hops; returns `{ visibleNodeIds, visibleEdgeIds }` to merge into current visible-set.

System prompt (in `agent.ts`):

> You're an analyst exploring a graph of people, companies, and contextual nodes
> (cities, schools, conferences, industries, past employers, partnerships).
> Use the four tools to help the user navigate. Prefer `filter` over enumerating
> nodes in prose. Always call `explain` before describing a node in detail.
> Available node kinds: ... Available edge kinds: ...

## Chat tool loop

```mermaid
sequenceDiagram
    participant User
    participant Page as Discover.tsx
    participant Agent as agent.ts
    User->>Page: "Show me everyone at TSMC who reports to a VP"
    Page->>Agent: runAgent(messages, tools, snapshot)
    Agent->>Agent: glm-5.1 returns tool_call: filter({company: "TSMC", edgeKinds: ["reports_to"]})
    Agent-->>Page: applies visible-set + edgeKinds
    Page->>Agent: continue loop with tool_result
    Agent->>Agent: tool_call: explain({id: "p_lin_wei"})
    Agent-->>Page: returns bundle; final reply
    Agent-->>Page: "Filtered to 1 person at TSMC. Lin Wei reports to ..."
```

Tool executors are pure functions over the locally-built `{ nodes, edges }` snapshot; they return result objects that `Discover.tsx` uses to update its `useState`. No external store.

## Layout (3 rails)

```tsx
// src/pages/Discover.tsx
<div className="h-screen flex flex-col">
  <TopBar />                                   {/* edge-kind filter pills appear here */}
  <div className="flex-1 grid grid-cols-[360px_1fr_380px] min-h-0">
    <GraphChat                                  /* left */
      messages={messages}
      onSend={handleSend}
    />
    <div className="relative flex flex-col min-h-0 border-x border-border">
      <Subheader stats={...} />                {/* nodes/edges/candidates/selected + legend */}
      <ForceGraph2D                             /* center */
        nodeCanvasObject={paintByKind}
        linkColor={l => edgeColor(l.kind)}
        onNodeClick={n => setSelectedId(n.id)}
        // halo + neighborhood fade computed from selectedId + hoverNeighborIds
      />
    </div>
    <NodeInspector                              /* right */
      node={selectedNode}
      onOpenProfile={id => navigate(`/prospect/${id}`)}
    />
  </div>
</div>
```

## What's deferred

- Streaming chat + dedicated `ToolCallChip` component (currently: non-streaming + plain-text traces above the assistant turn)
- Tool: `find_path`
- External graph state store (extract once `Discover.tsx` state grows past ~6 fields)
- Keyboard shortcuts (`/` focuses chat; `Esc` clears selection)
- Supabase-persisted curated edges (intros, "I know X")
- Real Figma color values (current tokens are HSL placeholders; replace once tokens are confirmed)

## Open assumptions

- Browser-side Z.AI key acceptable for demo (per `CLAUDE.md`); TODO to proxy via FastAPI.
- Z.AI's `glm-5.1` honors OpenAI `tools` / `tool_choice`; if not, fall back to a tagged-text intent parser in `agent.ts`. Verify on first run.
- Inspector rail size (~380w) and chat rail size (~360w) match Figma. Tweak after eyeballing alongside the design.
- Edge-derivation rules (esp. `reports_to` from `seniorityRank`) are heuristic; good enough for the demo, replace with explicit edge data later.
