# CONTRACTS.md — Credence v3

> This document is the **interface agreement** between every module and every agent working on Credence. It is the single source of truth for the shape of the seams between frontend, backend, data layer, and demo mode.
>
> **The rule:** if implementation reveals a needed change to a contract, **discuss it explicitly before modifying this file**. Changing a contract mid-implementation breaks parallel subagent tasks. CONTRACTS.md is the input to parallel work, not its output.
>
> Companion documents:
> - `CLAUDE.md` — product spec, architecture decisions, data model, scoring math (read first)
> - `DEMO_CASES.md` — 5 real warm-path examples for demo mode (derived from career-overlap SQL; **not yet authored** as of this draft — pending Supabase credentials)

---

## How to Use This Document

- Each contract has a stable number (Contract 1–7). Reference contracts by number in code comments and PRs.
- Every contract section follows the same shape: **Owner module → Implementers → Consumers → Signature → Input → Output → Error behavior → Invariants → Test condition**.
- If you are implementing a contract, the **Test condition** is what your work must pass. If it does not, the implementation is incomplete.
- If a contract feels under-specified, that is a bug in the contract — raise it before writing code, not after.

---

## Contract Index

| # | Name | Owner | Status |
|---|---|---|---|
| 1 | `POST /signals/discover-connections` | `server/routes/signals.py` | TO BE IMPLEMENTED |
| 2 | `findWarmPaths()` | `src/lib/warmPaths.ts` | TO BE IMPLEMENTED |
| 3 | `EdgeKind` taxonomy + edge config registry | `src/lib/graph.ts` | EXISTS — needs extension to 12 kinds |
| 4 | `NormalizedSignal` shape | shared (frontend `src/types`, backend `server/lib`) | TO BE FORMALIZED |
| 5 | Demo mode contract | `src/lib/demoData.ts` + `src/store/graphStore.ts` | TO BE IMPLEMENTED |
| 6 | Score record schema + versioning | `server/lib/scoring.py` + Supabase | TO BE FORMALIZED |
| 7 | `person_connections` invariants + warm-path BFS query | Supabase + `server/lib/scoring.py` | SCHEMA EXISTS — query contract to be enforced |
| 10 | `GET /orgchart/uncertain-edges` | `server/credence/api.py` + `credence.orgchart.active_sampling` | SHIPPED (Phase D.3 backend) |
| 11 | `POST /orgchart/correction` — `component_attributions` field | `server/credence/api.py` + `org_chart_corrections` table | SHIPPED (Phase D.1 — additive) |

---

## Contract 1: `POST /signals/discover-connections`

**Owner module:** `server/routes/signals.py`
**Implementers:** backend agent (TBD)
**Consumers:** frontend `Discover.tsx` (on prospect select), `NodeInspector.tsx` (on "Find more connections" button), batch enrichment scripts

### Signature

```python
@router.post("/signals/discover-connections")
async def discover_connections(request: DiscoverConnectionsRequest) -> DiscoverConnectionsResponse: ...
```

### Input

```python
class DiscoverConnectionsRequest(BaseModel):
    prospect_a_id: str          # UUID; must exist in persons table
    prospect_b_id: str          # UUID; must exist in persons table; must != prospect_a_id
    sources: Optional[List[Literal["uspto", "scholar", "career"]]] = None
                                # if None, run all enabled extractors
    max_results_per_source: int = 25
                                # cap per extractor; protects against scrape bombs
    timeout_seconds: float = 5.0
                                # hard cap for the whole endpoint; partial results returned on timeout
```

### Output

```python
class ConnectionRecord(BaseModel):
    signal_id: str              # UUID of the row written to signals table
    signal_type: Literal[
        "patent_co_inventor",
        "academic_co_author",
        "career_overlap_same_team",
        "career_overlap_same_domain",
        "career_overlap_general",
        "conference_co_presenter",
        "standards_committee_peer",
    ]
    structured_value: dict      # shape depends on signal_type; see "structured_value shapes" below
    confidence: float           # [0.0, 1.0]
    source: Literal["uspto", "scholar", "career"]

class DiscoverConnectionsResponse(BaseModel):
    connections_found: int
    connections: List[ConnectionRecord]
    sources_attempted: List[str]    # e.g., ["uspto", "scholar"]
    sources_failed: List[str]       # subset of sources_attempted that errored or timed out
    elapsed_ms: int
    truncated: bool                 # true if any source hit max_results_per_source
```

#### `structured_value` shapes (by signal_type)

```python
# patent_co_inventor
{
    "connected_to": str,        # the other person's UUID
    "patent_number": str,
    "patent_title": str,
    "filing_date": str,         # ISO date
    "grant_date": Optional[str],
    "assignee": str,            # company name as it appears on the patent
    "uspto_url": str,
}

# academic_co_author
{
    "connected_to": str,
    "paper_title": str,
    "venue": str,               # conference or journal name
    "year": int,
    "citation_count": int,
    "semantic_scholar_id": str,
    "doi": Optional[str],
}

# career_overlap_*
{
    "connected_to": str,
    "company_id": str,
    "company_name": str,
    "overlap_start_year": int,
    "overlap_end_year": int,
    "overlap_years": int,
    "team_a": Optional[str],
    "team_b": Optional[str],
    "domain_a": str,
    "domain_b": str,
    "seniority_gap": int,
}
```

### Error behavior

- **Bad input** (missing/unknown prospect ID, A == B): `400` with `{"error": "...", "field": "..."}`.
- **Auth missing**: `401` with `{"error": "auth_required"}`. (Auth scheme TBD in a future contract; for now stub `verify_request()` always passes.)
- **External API failures (USPTO down, Semantic Scholar 5xx)**: do **not** raise. Mark the source as `failed`, return whatever other sources produced. Endpoint is **partial-results-tolerant**.
- **Timeout (`timeout_seconds` exceeded)**: cancel outstanding tasks via `asyncio.wait_for`, return whatever completed, `truncated=true`. The endpoint must always return within `timeout_seconds + 0.5s` (cleanup grace).
- **Rate-limit (429 from USPTO/Scholar)**: exponential backoff `1s → 2s → 4s`, max 3 retries per call. After exhaustion, mark source as failed and continue.
- **No connections found**: `200` with `connections_found: 0`. Empty result is success, not error.
- **Database write failure** (Supabase down): `502` with `{"error": "signal_persist_failed", "found_in_memory": [...]}` — caller can retry; results were discovered but not persisted.

### Invariants

- The endpoint **always writes to the `signals` table** when it finds connections; the response and the database must agree.
- Raw API blobs (full USPTO/Semantic Scholar JSON) are **never** stored in `structured_value`. They go to S3 at `raw_data_uri` (see Contract 4 / CLAUDE.md Decision 5). `structured_value` is capped at 4KB.
- The function **must not** call extractors sequentially. Use `asyncio.gather` so the wall-clock time is `max(extractor_times)`, not `sum(extractor_times)`.
- `confidence` per signal_type follows CLAUDE.md `STRENGTH_TABLE`. For `academic_co_author`, `0.90` if `author_count <= 5`, else `0.75`.

### Test condition

A test must:

1. POST `{prospect_a_id: <known_inventor_with_co_invention>, prospect_b_id: <known_co_inventor>}` → response has `connections_found >= 1`, includes a `patent_co_inventor` record with a real USPTO patent number.
2. POST with one prospect that has no patents/papers → response is `200` with `connections_found: 0`.
3. POST with USPTO mocked to time out → response returns within `timeout_seconds + 0.5s`, `sources_failed` includes `"uspto"`, other sources still produce results.
4. POST same input twice → second response is **idempotent**: existing signal rows are reused, not duplicated. (The signals table has a unique constraint on `(prospect_id, signal_type, structured_value->>'patent_number')` for patents and equivalent for papers.)
5. Verify Supabase `signals` table contains a row matching each `ConnectionRecord` returned.

---

## Contract 2: `findWarmPaths()`

**Owner module:** `src/lib/warmPaths.ts`
**Implementers:** frontend agent (TBD)
**Consumers:** `NodeInspector.tsx` (WarmPathPanel section), GraphChat agent's `expand_node` tool

### Signature

```typescript
export function findWarmPaths(
  targetNodeId: string,
  sourceNodeIds: string[],
  graph: { nodes: GraphNode[]; edges: GraphEdge[] },
  options?: WarmPathOptions
): WarmPath[]

export interface WarmPathOptions {
  maxHops?: number              // default 3, range [1, 5]
  minStrength?: number          // default 0.30, range (0.0, 1.0)
  warmEdgeKinds?: EdgeKind[]    // default: all kinds with baseStrength >= 0.50 from Contract 3
  topK?: number                 // default 10; max paths returned after sorting
  dedupePolicy?: "node-set" | "edge-set"  // default "node-set"
}

export interface WarmPath {
  nodes: GraphNode[]            // ordered: [sourceNode, ...intermediate, targetNode]
  edges: GraphEdge[]            // edges.length === nodes.length - 1
  strength: number              // product of edge strengths in [0.0, 0.99]
  hopCount: number              // === edges.length
  explanation: string           // specific, human-readable; see "Explanation generation" below
  suggested_opener: string      // first sentence of an outreach email; see "Opener generation"
}
```

### Input

- `targetNodeId`: must exist in `graph.nodes`. If not, return `[]`.
- `sourceNodeIds`: zero or more node IDs that must exist in `graph.nodes`. Unknown IDs are silently dropped. If empty, return `[]`.
- `graph`: nodes and edges currently rendered in the UI. The function operates on the in-memory graph; it does **not** call Supabase.
- `options`: all fields optional. Defaults applied in pure-function fashion (no global config read).

### Output

- A sorted array of `WarmPath` objects, **descending by `strength`**, length ≤ `options.topK`.
- Each path's `nodes[0]` is one of `sourceNodeIds`; `nodes.at(-1)` is `targetNodeId`.
- Each path's `edges[i]` connects `nodes[i]` and `nodes[i+1]`; direction is irrelevant (edges are undirected per Contract 7).
- If no paths satisfy `minStrength` and `maxHops` constraints, return `[]` (empty array, never `null` or `undefined`).

### Error behavior

- **No throws.** Invalid input returns `[]` and logs `console.warn` with the specific reason.
- The function must be **deterministic**: same input → same output, including same path ordering for ties (sort stably by `strength desc`, then `hopCount asc`, then `nodes[0].id` as tiebreaker).
- The function must **not mutate** `graph`, `sourceNodeIds`, or `options`.

### Invariants

- **Strength = product, not sum.** A 1-hop patent_co_inventor path has strength `0.95`. A 2-hop path through `patent_co_inventor + academic_co_author` has strength `0.95 * 0.85 = 0.8075`. This naturally penalizes longer paths without an arbitrary cutoff.
- **Pruning.** During BFS, if `current_path_strength * max_remaining_edge_strength < minStrength`, prune the branch. (The bound uses the max strength of any allowed edge kind as the optimistic estimate.)
- **Edges are undirected.** An edge `(A, B)` can be traversed `A → B` or `B → A`. The strength is the same.
- **No node revisits within a path.** Standard BFS visited-set keyed on `node.id`.
- **Deduplication.** With `dedupePolicy: "node-set"` (default), two paths with the same set of node IDs (regardless of order) collapse to the higher-strength one.
- **Edge filter.** Only edges with `kind` in `options.warmEdgeKinds` are traversed. Edges of disallowed kinds are invisible to the algorithm.
- **Edge strength source.** Each edge carries a `strength` field already populated from Contract 3's `STRENGTH_TABLE`. `findWarmPaths` does **not** recompute strength; it reads.

### Explanation generation

A separate (non-exported) function `generateExplanation(path, graph): string`. Must produce a **specific** string referencing actual evidence, not a generic one. CLAUDE.md gives the required templates per first-edge kind. Reference template (verbatim from CLAUDE.md):

```
patent_co_inventor:
  "<NodeA.name> co-invented <patentTitle> with <NodeB.name>
   at <assignee> (<year>)"

academic_co_author:
  "<NodeA.name> co-authored \"<paperTitle>\" with <NodeB.name>
   at <venue> (<year>, <citationCount> citations)"

standards_committee:
  "<NodeA.name> and <NodeB.name> served on the <committee>
   together (<years>)"

conference_co_presenter:
  "<NodeA.name> and <NodeB.name> co-presented at <event> (<year>)"

default:
  "<NodeA.name> and <NodeB.name> have a <kind-as-words> connection"
```

When evidence fields are missing, use the documented fallback strings (`"a patent"`, `"a conference"`, `"year unknown"`) — never generate fictional data.

### Suggested opener generation

A non-exported function `generateSuggestedOpener(path): string`. The output is a complete first sentence ending in a period. The connector is `path.nodes[0]` (the person at "your company"), the target is `path.nodes.at(-1)` (the prospect). The opener references the actual connection from the first edge. Reference templates in CLAUDE.md "Suggested Opener Generation".

### Test condition

A test must verify:

1. Graph with a single `patent_co_inventor` edge between A and C → `findWarmPaths(C, [A], graph)` returns one path with `strength === 0.95`, `hopCount === 1`, `nodes` is `[A, C]`.
2. Graph with `A —[patent]— B —[paper]— C` → returns one path with `strength` ≈ `0.95 * 0.85 = 0.8075`, `hopCount === 2`.
3. With `minStrength: 0.90`, the same 2-hop graph returns `[]` (the path is below threshold).
4. With `maxHops: 1`, the 2-hop graph returns `[]`.
5. Empty graph or unknown `targetNodeId` returns `[]`.
6. `nodes` and `edges` arrays in input are not mutated (deep-equal check before/after).
7. Explanation is specific: contains the patent number or paper title, not a generic phrase.

---

## Contract 3: `EdgeKind` taxonomy + edge config registry

**Owner module:** `src/lib/graph.ts`
**Implementers:** frontend agent
**Consumers:** `TopBar.tsx` (filter pills), `GraphCanvas.tsx` (rendering), `NodeInspector.tsx` (display labels), `mockStore.ts` / `demoData.ts` (data construction), every component touching edges

### Single source of truth

```typescript
export type EdgeKind =
  // existing — already in graph.ts
  | "works_at"
  | "reports_to"
  | "located_in"
  | "evidence_cited"
  | "scope_signal"
  | "partnership"
  | "past_employer"
  | "vertical"
  // new — to be added in Track A or follow-up
  | "patent_co_inventor"
  | "academic_co_author"
  | "conference_co_presenter"
  | "standards_committee"

export interface EdgeConfig {
  kind: EdgeKind
  displayLabel: string          // shown in TopBar pills, NodeInspector
  cssVarName: string            // e.g., "--edge-patent-co-inventor"; defined in src/index.css
  defaultVisible: boolean       // initial filter state
  baseStrength: number          // [0.0, 1.0]; from CLAUDE.md STRENGTH_TABLE
  decayRate: number             // exp decay per year; from CLAUDE.md DECAY_RATES
  isWarmByDefault: boolean      // included in default warmEdgeKinds (Contract 2)
}

export const EDGE_CONFIGS: Record<EdgeKind, EdgeConfig> = { /* ... */ }

export const ALL_EDGE_KINDS: EdgeKind[] = Object.keys(EDGE_CONFIGS) as EdgeKind[]
```

### Invariants

- **`graph.ts` is the only file that defines `EdgeKind` values.** No string literals like `"patent_co_inventor"` may appear in any other source file. Components import `EDGE_CONFIGS[kind].displayLabel` etc.
- **Edge colors live exclusively in `src/index.css`** as CSS custom properties. `EDGE_CONFIGS[kind].cssVarName` is the lookup key. No hex codes for edge colors anywhere except `index.css`.
- **Adding a new edge kind requires four updates, in this order**:
  1. Add to `EdgeKind` union in `graph.ts`
  2. Add a row to `EDGE_CONFIGS` in `graph.ts`
  3. Add a CSS variable in `src/index.css`
  4. (No update needed to `TopBar.tsx`, `Discover.tsx`, etc. — they iterate `ALL_EDGE_KINDS`.)
- `baseStrength` and `decayRate` values must match CLAUDE.md `STRENGTH_TABLE` and `DECAY_RATES` exactly. If CLAUDE.md changes, update both atomically and bump a comment with the version.

### Error behavior

- A runtime check at module load time: if any `EdgeConfig.cssVarName` does not resolve in the active stylesheet, `console.error` once. (Optional but recommended; useful for catching misconfigurations early.)
- An exhaustive switch on `EdgeKind` somewhere (e.g., explanation generator) must use TypeScript's exhaustiveness check (`const _exhaustive: never = kind`) so adding a kind without updating switches is a compile error.

### Test condition

1. `ALL_EDGE_KINDS.length === Object.keys(EDGE_CONFIGS).length` (no kind without a config).
2. For each kind, `EDGE_CONFIGS[kind].kind === kind` (no copy-paste mismatch).
3. `grep -r '"patent_co_inventor"' src/ --include="*.ts" --include="*.tsx"` returns matches **only** in `src/lib/graph.ts` (and tests). Repeated for all 12 kinds.
4. `grep -E '#[0-9a-fA-F]{6}' src/lib/graph.ts src/components/` returns **no** matches in component files for edge styling. Hex codes only in `src/index.css`.
5. Adding a new kind to `EdgeKind` without adding a row to `EDGE_CONFIGS` is a TypeScript compile error.

---

## Contract 4: `NormalizedSignal` shape

**Owner module:** shared — TypeScript in `src/types/index.ts`, Python in `server/lib/signals.py` (or equivalent)
**Implementers:** frontend agent + backend agent (must agree)
**Consumers:** every extractor, every score computation, the signals table schema, `mockStore.ts`, `demoData.ts`

### Signature

```typescript
// TypeScript (frontend, mockStore, demoData)
export interface NormalizedSignal {
  id: string                    // UUID
  prospect_id: string           // FK → persons.id
  source: "uspto" | "scholar" | "career" | "linkedin" | "press" | "sec" | "manual"
  signal_type: SignalType       // see union below
  structured_value: Record<string, unknown>   // schema varies per signal_type; capped at 4KB
  raw_data_uri: string | null   // S3 URL of the raw API blob; null for derived/synthetic signals
  weight: number                // [0.0, 1.0] — relative importance for scoring; defaulted per signal_type
  confidence: number            // [0.0, 1.0] — how sure are we this signal is correct
  observed_at: string           // ISO datetime; when the signal was extracted
  weight_version_id: string     // FK → score_weights table; see Contract 6
}

export type SignalType =
  | "patent_co_inventor"
  | "academic_co_author"
  | "career_overlap_same_team"
  | "career_overlap_same_domain"
  | "career_overlap_general"
  | "conference_co_presenter"
  | "standards_committee_peer"
  | "career_history"
  | "education_history"
  | "executive_role"
  | "award"
  | "press_mention"
```

```python
# Python (backend) — must mirror exactly
class NormalizedSignal(BaseModel):
    id: str
    prospect_id: str
    source: Literal["uspto", "scholar", "career", "linkedin", "press", "sec", "manual"]
    signal_type: str            # validated against SIGNAL_TYPES set
    structured_value: dict
    raw_data_uri: Optional[str]
    weight: float
    confidence: float
    observed_at: datetime
    weight_version_id: str
```

### Invariants

- **`structured_value` is capped at 4KB serialized JSON.** Enforce at write time in the extractor. If the extracted data exceeds 4KB, push the full blob to S3 at `raw_data_uri` and put only the structured subset in `structured_value`. (CLAUDE.md Decision 5.)
- **No raw API responses in Postgres.** Period. The 4KB cap exists to make this physically obvious during code review.
- **`signal_type` ↔ `structured_value` shape mapping is fixed** per the table in Contract 1. A `patent_co_inventor` row must have `patent_number` in its `structured_value`. Validation lives in the extractor; tests cover each signal_type.
- **`confidence` is per-signal, not per-prospect.** A single prospect can have many signals with very different confidences.
- **`weight_version_id` is denormalized into every signal at write time.** This makes scores reproducible without joining; see Contract 6.
- **TypeScript and Python definitions must stay in sync.** A test compares the field names via a generated JSON schema dump; a CI job runs that test.

### Error behavior

- Writing a signal where `structured_value` exceeds 4KB serialized: extractor must raise `SignalTooLargeError`. Caller decides whether to truncate, store partial in S3, or skip.
- Reading a signal where `signal_type` is not in `SIGNAL_TYPES`: log warning, skip the row, do not crash the consumer.
- A `NormalizedSignal` with neither `raw_data_uri` nor a self-contained `structured_value` (e.g., empty dict) is invalid. Validation rejects on write.

### Test condition

1. JSON-schema parity test: TypeScript interface and Python BaseModel produce equivalent JSON schemas (field names, types, required-ness).
2. Extractor for each `signal_type` produces a `NormalizedSignal` whose `structured_value` matches the contract-1 shape table.
3. A patent extractor fed a USPTO response with a 30KB description correctly puts the description in S3 and only stores the title/number/dates in `structured_value`.
4. Reading a signal with `signal_type === "garbage_unknown"` does not crash any score computation; the signal is logged and skipped.

---

## Contract 5: Demo mode

**Owner module:** `src/lib/demoData.ts` + `src/store/graphStore.ts`
**Implementers:** frontend agent
**Consumers:** `Discover.tsx`, `NodeInspector.tsx`, the YC demo video

### Activation

```typescript
// In src/store/graphStore.ts (or an init module called by it)
export const isDemoMode = (): boolean =>
  new URLSearchParams(window.location.search).has("demo")
```

- `?demo=true` (or `?demo` with no value) activates demo mode.
- The query string is checked **once at app boot** and cached. Subsequent navigations within the SPA do not re-check (avoid mid-session state flips).
- A `data-demo-mode="true"` attribute is set on `<html>` for CSS hooks (e.g., dimming controls, banner styling).

### Data loading switch

```typescript
// In graphStore.ts initialization
if (isDemoMode()) {
  loadGraphFromDemoData()      // imports from src/lib/demoData.ts
} else {
  loadGraphFromSupabase()      // existing path
}
```

- Demo mode performs **zero** Supabase queries and **zero** FastAPI calls. Network panel must be empty for graph data fetches when `?demo=true`.
- All hardcoded demo data in `demoData.ts` must conform to the same TypeScript types used for live data — no shape drift, no debug fields.

### Required demo content

- **Exactly the 5 prospects from `DEMO_CASES.md`.** Real names, real employers, real evidence. Once `DEMO_CASES.md` exists, `demoData.ts` must reference it.
- **2 hidden-connection edges with real evidence**: one `conference_co_presenter` (✅ NVIDIA GTC 2022, Strier↔Fidler) and one `patent_co_inventor` (Ashton↔Clarke at Intel — pending USPTO ODP key registration to populate evidence). The `academic_co_author` edge originally specified in this contract is **intentionally omitted** as of 2026-04-30: live Scholar verification (LavenderPrairie msg 121) confirmed there is no co-authored paper between any pair in the demo cast (J. Absar's 58 unique co-authors include zero AMD employees; J. Newling's 23 papers contain zero Absar collaborations). Per CLAUDE.md "Common Mistakes" #6, we ship 2-of-3 with real evidence rather than fabricate the third.
- **No fictional data.** No `US Patent 99,999,999`. No `"A Generic Paper About Stuff"`.

### UI requirements

- A subtle **"DEMO MODE" banner** in the corner (top-right, `position: fixed`), visible on every page when active.
- A **Demo Script floating button** (bottom-right) that opens a panel with the talking points for the YC video. Panel content comes from `DEMO_CASES.md`'s talking-points section.
- Clicking any of the 5 demo prospects must show `WarmPathPanel` immediately, with the correct path populated.

### Error behavior

- Demo mode must not produce any console errors or warnings. If it does, the demo fails publicly. CI runs the app with `?demo=true` and asserts an empty console.
- If `demoData.ts` imports something that does not exist (e.g., a missing helper), the build fails. Demo mode is build-time validated.
- Toggling between demo and live mode requires a full page reload. The store does not support hot-swapping data sources.

### Invariants

- The 5 demo prospects' UUIDs are **stable constants** (e.g., `00000000-0000-0000-0000-000000000001` … `005`). They never collide with real Supabase UUIDs (which are version-4 random).
- Demo mode does **not** mutate any real backend state. No POST to `/signals/*`. No write to Supabase.
- The `WarmPathPanel` interaction in demo mode uses the same `findWarmPaths()` (Contract 2) implementation as live mode. Demo mode is a data swap, not a code swap.

### Test condition

1. Visit `/discover?demo=true` with network throttled to offline → graph loads, all 5 prospects render, no errors.
2. Click any of the 5 demo prospects → `WarmPathPanel` appears within 500ms with at least one path.
3. The "DEMO MODE" banner is present in the DOM with `?demo=true` and absent without.
4. Network panel during demo mode shows 0 requests to Supabase and 0 to FastAPI's `/signals/*`.
5. The 3 hardcoded edges in `demoData.ts` reference patent numbers, paper titles, and conference names that match `DEMO_CASES.md` byte-for-byte.

---

## Contract 6: Score record schema + versioning

**Owner module:** `server/lib/scoring.py` + Supabase tables `score_weights`, `score_records`
**Implementers:** backend agent
**Consumers:** scoring pipeline, `/settings` UI (live weight editing), `NodeInspector.tsx` (score display + "computed with previous weights" banner)

### Tables

```sql
-- Versioned weight snapshots; one row per "saved" weight configuration
create table score_weights (
  id              uuid primary key default gen_random_uuid(),
  authenticity_w  numeric not null check (authenticity_w >= 0 and authenticity_w <= 1),
  authority_w     numeric not null check (authority_w     >= 0 and authority_w     <= 1),
  warmth_w        numeric not null check (warmth_w        >= 0 and warmth_w        <= 1),
  sub_weights     jsonb   not null,            -- per-component weights (patent, paper, etc.)
  created_at      timestamptz not null default now(),
  created_by      text    not null,            -- user email or "system"
  is_active       boolean not null default false,
  constraint sum_to_one check (
    abs(authenticity_w + authority_w + warmth_w - 1.0) < 0.001
  )
);

-- Materialized per-prospect scores
create table score_records (
  id                  uuid primary key default gen_random_uuid(),
  prospect_id         uuid not null references persons(id) on delete cascade,
  weight_version_id   uuid not null references score_weights(id),
  authenticity_score  numeric not null check (authenticity_score between 0 and 100),
  authority_score     numeric not null check (authority_score     between 0 and 100),
  warmth_score        numeric not null check (warmth_score        between 0 and 100),
  overall_score       numeric not null check (overall_score       between 0 and 100),
  falsification_note  text    not null,        -- single most plausible reason this is wrong
  computed_at         timestamptz not null default now(),
  unique (prospect_id, weight_version_id)
);

create index idx_score_records_prospect on score_records (prospect_id, computed_at desc);
```

### Behavior

- Exactly **one** row in `score_weights` has `is_active = true` at any time. Editing weights in `/settings` inserts a new row and atomically flips `is_active`.
- Scores are **never invalidated on weight change.** Existing `score_records` are preserved (so we can audit historical scores). When weights change, the active version's `id` updates everywhere new computations land.
- **Lazy recompute on read.** When a UI request asks for prospect P's score:
  1. Look up the active `weight_version_id`.
  2. If a `score_records` row exists for `(P, active_version)`, return it.
  3. Otherwise, compute on demand, insert into `score_records`, return.
- A background job sweeps prospects in priority order (recently viewed first) to backfill the new version. Until backfill is complete, the UI may show stale scores.

### UI banner contract

- When `score_records.weight_version_id` of the displayed score does not match the current active version, `NodeInspector.tsx` shows a small banner:
  > "Score computed with previous weights. Updating in the background — refresh in {N} minutes."
- `N` is computed from a heuristic: `(prospects_remaining_in_backfill / backfill_rate_per_min)`, clamped `[1, 30]`.

### Error behavior

- Reading scores during backfill is always safe; falls back to the most recent `score_records` row if the active version is missing. The banner explains.
- Inserting a new `score_weights` with `sum_to_one` violation: 400 error, no row written.
- Concurrent `/settings` saves: last-writer-wins via the `is_active` flip; old rows are preserved as audit trail.

### Invariants

- `overall_score = authenticity_score * w.auth + authority_score * w.authority + warmth_score * w.warmth`, where `w` is from the row identified by `weight_version_id`. This must be exact within floating-point tolerance (`abs(diff) < 0.01`).
- `falsification_note` is **never empty**. CLAUDE.md spec: every score has a one-sentence "single most plausible reason this score is wrong". Generators must produce one even when confidence is high (the generic version is e.g., "No employment end date confirmation exists").
- `score_records` is append-only in normal operation. Hard deletes are forbidden in app code (use `on delete cascade` only on prospect deletion).

### Test condition

1. Insert a weights row → assert `is_active` is set on exactly one row across the table.
2. Insert a new weights row via `/settings` → previous row's `is_active` flips to `false` atomically (transactional test).
3. Compute a score for prospect P with weights `(0.4, 0.4, 0.2)` and components `(80, 90, 70)` → `overall_score === 80*0.4 + 90*0.4 + 70*0.2 === 82.0`.
4. Change weights → request P's score → row lazy-inserts with new `weight_version_id`; previous row still exists.
5. UI banner appears when displayed score's `weight_version_id` ≠ active version.

---

## Contract 7: `person_connections` invariants + warm-path BFS query

**Owner module:** Supabase schema + `server/lib/scoring.py` (BFS query helpers, when needed server-side)
**Implementers:** backend agent (schema), backend + frontend (consumer queries)
**Consumers:** `findWarmPaths()` (Contract 2) when graph is built from server data, batch scoring jobs, `/signals/discover-connections` writes

### Schema

```sql
create table person_connections (
  id                    uuid primary key default gen_random_uuid(),
  person_a_id           uuid not null references persons(id) on delete cascade,
  person_b_id           uuid not null references persons(id) on delete cascade,
  connection_type       text not null,   -- one of CONNECTION_TYPES (Contract 4 SignalType subset)
  base_strength         numeric not null check (base_strength between 0 and 1),
  recency_factor        numeric not null check (recency_factor between 0 and 1),
  frequency_factor      numeric not null,
  corroboration_factor  numeric not null,
  computed_strength     numeric not null check (computed_strength between 0 and 0.99),
  evidence_ids          uuid[] not null default '{}',  -- FKs into connection_evidence
  last_active_year      int    not null,
  corroboration_count   int    not null default 1,
  source_type_count     int    not null default 1,
  computed_at           timestamptz not null default now(),
  constraint a_lt_b check (person_a_id < person_b_id),
  unique (person_a_id, person_b_id, connection_type)
);

create index idx_person_connections_a on person_connections (person_a_id, computed_strength desc);
create index idx_person_connections_b on person_connections (person_b_id, computed_strength desc);
create index idx_person_connections_type on person_connections (connection_type);
```

### Invariants

- **`person_a_id < person_b_id` always** (UUID lexicographic comparison; enforced by `a_lt_b` CHECK constraint). This prevents duplicate `(A,B)` and `(B,A)` rows. CLAUDE.md Decision 1.
- **Edges are bidirectional.** Consumers querying for connections of a node must query both columns:
  ```sql
  select * from person_connections
  where person_a_id = $1 or person_b_id = $1;
  ```
- **`computed_strength` is the indexed read column.** BFS and ranking always read this. **Never** compute strength on the fly at query time. CLAUDE.md Decision 7.
- **`computed_strength` formula** (recomputed offline by the strength job, not at read time):
  ```
  computed_strength = min(0.99,
      base_strength
      * exp(-DECAY_RATE[type] * (current_year - last_active_year))
      * (1.0 + log(max(1, corroboration_count)) * 0.15)
      * (1.0 + source_type_count * 0.10)
  )
  ```
- **`base_strength` per connection_type** matches CLAUDE.md `STRENGTH_TABLE` exactly. Same for `DECAY_RATES`.
- **`evidence_ids`** points into a `connection_evidence` table; each id corresponds to the specific signal (patent number, paper id, employment overlap) that supports the connection.
- **Adding a new connection type** requires: (a) extending `SIGNAL_TYPES` in Contract 4, (b) extending `EdgeKind` in Contract 3, (c) adding the type's row to `STRENGTH_TABLE` and `DECAY_RATES` in CLAUDE.md, (d) updating any explicit `connection_type` validation lists.

### Read-side query contract (for warm-path BFS)

```sql
-- Standard "expand from node N, top K strongest neighbors" query
select
  case when person_a_id = $1 then person_b_id else person_a_id end as neighbor_id,
  connection_type,
  computed_strength,
  evidence_ids
from person_connections
where (person_a_id = $1 or person_b_id = $1)
  and computed_strength >= $2   -- min_strength filter
order by computed_strength desc
limit $3;
```

- Target wall-clock: **< 50 ms at 2M persons** with the indexes above. (CLAUDE.md Decision 7 sets this as the scaling target. Currently at 20k, well under target.)
- A consumer must **not** wrap this in a recursive CTE that joins back to itself for multi-hop. Multi-hop expansion is the **client's** (Contract 2's `findWarmPaths`) job. The server returns one-hop neighbors; the client BFS-walks.

### Error behavior

- Inserting a connection where `person_a_id >= person_b_id`: rejected by CHECK constraint. Writers (extractors, `/signals/discover-connections`) must `LEAST/GREATEST` the IDs before insert.
- Duplicate `(person_a, person_b, connection_type)`: rejected by `unique` constraint. Writers must use `ON CONFLICT DO UPDATE` to merge new evidence:
  ```sql
  insert into person_connections (...) values (...)
  on conflict (person_a_id, person_b_id, connection_type)
  do update set
      evidence_ids = person_connections.evidence_ids || excluded.evidence_ids,
      corroboration_count = person_connections.corroboration_count + 1,
      last_active_year = greatest(person_connections.last_active_year, excluded.last_active_year),
      computed_at = now();
  ```
- The `computed_strength` re-compute is **not** done in the trigger. A separate job processes "stale" rows (rows where `computed_at` is older than the strength job's last successful run). This avoids write amplification.

### Test condition

1. Insert `(A, B, "patent_co_inventor")` where `A > B` (UUID compare): rejected, error message references `a_lt_b`.
2. Insert two rows for the same `(A, B, "patent_co_inventor")`: second insert merges via `ON CONFLICT`, `corroboration_count` becomes 2.
3. Query "neighbors of A with `computed_strength >= 0.5`": returns rows where A appears in either column, sorted desc.
4. Verify no consumer code computes `min(0.99, base * exp(...) * ...)` at query time. (`grep` for `exp(` in `server/`, `src/lib/`: only the strength-job module should match.)
5. EXPLAIN on the neighbor query at 1M-row scale uses both `idx_person_connections_a` and `idx_person_connections_b`, no full scan.

---

## Contract 8: Per-prospect enrichment (Wave 5)

**Owner module:** `server/credence/enrichment/{apollo,pdl,…}.py` + `server/credence/routes/enrich.py`
**Implementers:** LavenderPrairie (Apollo, Phase 1), DarkBeaver (PDL, Phase 2), and any future per-vendor module
**Consumers:** prospect detail page (refresh-contact), batch enrichment scripts, Discover.tsx (lazy fill on inspector open)

Contract 8 covers the **enrichment-on-a-single-prospect** pattern, which is semantically distinct from Contract 1's pair-discovery. Where Contract 1 answers *"what documented relationships exist between these two people?"*, Contract 8 answers *"what additional facts about this one person can we buy / scrape / compute?"*.

### Why a separate contract

- Different fanout shape: per-prospect, not per-pair
- Different cost model: per-row charged by the vendor, not per-search
- Different idempotency key: `(prospect_id, vendor)` rather than `(prospect_a, prospect_b, signal_type)`
- Different freshness contract: enrichment is "as of now"; connection signals are "as of when the underlying record was filed"

### Endpoint

```python
@router.post("/enrich/{prospect_id}")
async def enrich_prospect(prospect_id: UUID, req: EnrichRequest) -> EnrichResponse: ...
```

### Input

```python
class EnrichRequest(BaseModel):
    vendors: Optional[List[Literal["apollo", "pdl", "parallel", "firecrawl"]]] = None
                                # if None, run every enabled vendor for this prospect_id
    max_cost_cents: int = 100   # hard cap; vendors that would push cumulative spend over this
                                # are skipped, returned in `vendors_skipped_for_cost`
    timeout_seconds: float = 10.0
                                # whole-endpoint timeout; per-vendor calls share the budget
    refresh: bool = False       # if False, return cached values when last_enriched_at is fresh
                                # (< 24h old); if True, force a re-fetch from the vendor
```

### Output

```python
class EnrichmentRecord(BaseModel):
    vendor: Literal["apollo", "pdl", "parallel", "firecrawl"]
    fields: dict[str, Any]      # vendor-specific shape; see "fields shapes" below
    confidence: float           # vendor's reported confidence, [0.0, 1.0]
    cost_cents: int             # actual cost charged for this call
    fetched_at: datetime
    cached: bool                # true if returned from cache (no vendor hit on this call)

class EnrichResponse(BaseModel):
    prospect_id: UUID
    records: list[EnrichmentRecord]
    vendors_attempted: list[str]
    vendors_failed: list[str]
    vendors_skipped_for_cost: list[str]
    total_cost_cents: int
    elapsed_ms: int
```

### Fields shapes (per vendor)

```python
# apollo — email + employment snapshot (no phone per user direction)
{
    "email": Optional[str],            # verified deliverable email
    "email_status": Literal["verified", "guessed", "no_match"],
    "current_title": Optional[str],
    "current_company_name": Optional[str],
    "current_company_domain": Optional[str],
    "linkedin_url": Optional[str],
    "city": Optional[str],
    "country": Optional[str],
    "apollo_person_id": str,           # for back-reference / idempotency
}
# Phone numbers are intentionally NOT requested — warm-intro flow ends in
# an email send. Re-add `phone: Optional[str]` if a future workflow needs them.

# pdl — employment time-series + skills
{
    "linkedin_url": Optional[str],
    "skills": list[str],
    "employment_periods": list[{       # FK target: persons.id; written by writer, not extractor
        "company_name": str,
        "title": str,
        "functional_domain": Optional[str],
        "start_date": Optional[str],   # ISO YYYY-MM-DD or YYYY-MM
        "end_date": Optional[str],     # null when is_current
        "is_current": bool,
    }],
    "pdl_person_id": str,
}

# parallel — flexible AI research output
{
    "task_id": str,
    "task_type": Literal["conference_appearance", "standards_membership", "press_mention", …],
    "structured_value": dict[str, Any],   # task-specific
    "source_urls": list[str],             # citation provenance
}

# firecrawl — single-URL structured extraction
{
    "source_url": str,
    "structured_value": dict[str, Any],
    "extracted_at": datetime,
}
```

### Persistence

Each enrichment call writes:

1. The relevant fields onto `prospects` (cheap columns: `email`, `email_status`, `current_title`) or `persons` (v3 canonical record). Apollo's email lands on `prospects.email`. PDL's `employment_periods` get materialized into the v3 table.
2. **`enrichment_cost_log` row** for every vendor invocation (see schema migration `20260430_v3_enrichment.sql`):
   ```sql
   INSERT INTO enrichment_cost_log (prospect_id, vendor, cost_cents, called_at, cache_hit, success)
   VALUES (...);
   ```
3. **`prospects.last_enriched_at = NOW()`** after a successful run, used by the cache-freshness check.

### Error behavior

- **Bad input** (unknown prospect_id): 400 with `{"error": "prospect_not_found"}`
- **Vendor failure** (HTTP 5xx, network error, timeout): swallow; mark vendor in `vendors_failed`. Endpoint returns 200 with whatever vendors did succeed (mirrors Contract 1's partial-results posture).
- **Cost ceiling hit**: skip remaining vendors, mark them in `vendors_skipped_for_cost`, return successfully with what we got.
- **Authentication failure** at the vendor (401/403): mark `vendors_failed`, log a warning. Repeated 401s should bubble to ops via the cost log (a `success=FALSE` count alert).
- **All vendors fail and we wrote nothing to DB**: 502 with `{"error": "all_vendors_failed", "vendors_failed": [...]}`. The caller can retry; nothing was persisted.

### Invariants

- **Every vendor call writes an `enrichment_cost_log` row**, even cache hits (`cache_hit=TRUE, cost_cents=0`). This makes operational dashboards trivial — a single table → all spend.
- **No raw API responses in `prospects` or `persons`**. Same Decision-5 rule as Contract 1 — `raw_data_uri` to S3 if we ever need archival; otherwise the structured fields only.
- **Idempotency on `(prospect_id, vendor, called_at::date)`**: re-POST within the same calendar day reads from cache (returns `cached=true`) rather than re-charging. Override with `refresh=true`.
- **Cost ceiling is enforced before issuing the API call** — we don't pay for a request and then refuse to use the data.
- **GDPR data-subject-erasure compatibility**: `prospects` row deletion CASCADEs into `enrichment_cost_log` rows for that prospect (drops audit trail too). EU residents get a coarser erasure than US — acceptable trade-off documented in legal section.

### Test condition

A test must verify:

1. `POST /enrich/{p}` with `vendors=["apollo"]` and a stubbed Apollo client returning a known shape → 200 with one EnrichmentRecord, vendor=apollo, fields populated correctly.
2. Two consecutive calls within 24h with `refresh=false` → second call returns `cached=true`, `cost_cents=0`, no vendor invocation.
3. Apollo + PDL + Parallel run in parallel; one raises → `vendors_failed` includes only that vendor; others succeed.
4. `max_cost_cents=10` with 3 vendors that would each charge 5¢ → first two run, third skipped, response carries `vendors_skipped_for_cost=["parallel"]` (or whichever sorts last).
5. Unknown prospect_id → 400 with `prospect_not_found`.
6. After 100 successful enrichments, `enrichment_cost_log` has 100 rows; sum of `cost_cents` matches the response totals.

---

## Contract 9: Multitenancy (accounts + RLS + session context) — Wave 6

**Owner module:** `supabase/migrations/20260430_v3_multitenant.sql` (schema), `server/credence/auth.py` (session resolution), `src/contexts/AccountContext.tsx` (frontend)
**Implementers:** DarkBeaver (M1 schema), SunnyRidge (M2 middleware), LavenderPrairie (M3 frontend), SwiftElk (M4 settings + M5 demo)
**Consumers:** every read or write to a domain table; every API route; the entire frontend

Contract 9 is the cross-cutting tenancy boundary. Where Contracts 1-8 specify per-feature interfaces, Contract 9 specifies the **isolation invariant** that wraps all of them: every row in every domain table belongs to exactly one tenant, and every read or write must scope to a session-bound `account_id`.

### v1 scope (explicitly minimal)

- One **account** per real company. No nested orgs / workspaces / teams in v1.
- One **user** per account in v1. Multi-user with viewer/editor/admin roles is v2.
- **Supabase Auth** as the IdP. Already in the stack — reusing rather than introducing a separate auth provider.
- **Service-role bypass** for backend extractors and cost tracking (those run as system, not as a user).
- **Demo mode** = a pseudo-tenant with id `00000000-0000-0000-0000-000000000fff`. RLS allows public reads; writes are forbidden.

### Schema invariants

```sql
-- Top-level tenant entity. One row per real customer.
create table accounts (
  id              uuid primary key default gen_random_uuid(),
  display_name    text not null,
  slug            text not null unique,        -- url-safe identifier
  plan_tier       text not null default 'free', -- free | pro | enterprise
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- User → account membership. Single row per user in v1; multi-row in v2.
create table account_users (
  account_id      uuid not null references accounts(id) on delete cascade,
  user_id         uuid not null references auth.users(id) on delete cascade,
  role            text not null default 'owner',
  created_at      timestamptz not null default now(),
  primary key (account_id, user_id)
);

-- Per-account budgets / settings. Wave 5 enrichment caps land here.
create table account_settings (
  account_id              uuid primary key references accounts(id) on delete cascade,
  apollo_monthly_cents    integer not null default 0,
  pdl_monthly_cents       integer not null default 0,
  parallel_monthly_cents  integer not null default 0,
  firecrawl_monthly_cents integer not null default 0,
  -- ...future per-tenant feature flags here...
  updated_at              timestamptz not null default now()
);
```

**Every domain table gains `account_id uuid not null references accounts(id) on delete cascade`.** Affected tables (M1's checklist):

1. `prospects`
2. `signals`
3. `scores`
4. `signal_weights`
5. `scoring_runs`
6. `persons`
7. `companies`
8. `employment_periods`
9. `education_periods`
10. `patents`
11. `patent_inventors`
12. `person_connections`
13. `connection_evidence`
14. `enrichment_cost_log`

### RLS policies — Supabase-native auth.uid() pattern

Every domain table has RLS enabled with a policy referencing the user's account memberships via `auth.uid()`:

```sql
alter table prospects enable row level security;

create policy prospects_tenant_isolation on prospects
  for all to authenticated
  using (account_id in (
    select au.account_id from public.account_users au
    where au.user_id = auth.uid()
  ))
  with check (account_id in (
    select au.account_id from public.account_users au
    where au.user_id = auth.uid()
  ));
```

This works directly with PostgREST's standard JWT-role assumption — when the frontend issues a query with the user's Supabase Auth JWT, PostgREST runs as the `authenticated` role, `auth.uid()` resolves to the user's UUID, and the subquery yields the user's account memberships. Returns empty for unauthenticated `anon` traffic.

**Service-role bypass:** backend admin tasks (extractors, cost-tracking, scheduled enrichment) connect via `DATABASE_URL` with the postgres-equivalent role, which bypasses RLS by default. Those code paths must explicitly filter by `account_id` themselves — RLS is the safety net for user-driven traffic, not the only filter.

**Sequencing constraint:** RLS is **deferred** until M3 (frontend AccountProvider + Login) is shipped. Enabling RLS before authenticated reads exist would break the v2 anon-key frontend (anon role gets filtered to empty). The schema migration `20260430_v3_multitenant.sql` is safe to apply immediately; the RLS migration `20260430_v3_multitenant_rls.sql` lands after M3.

### Session-context invariant

For any HTTP request to the backend FastAPI service:

1. Auth middleware extracts the JWT from the `Authorization: Bearer <token>` header.
2. `server/credence/auth.py:resolve_session(jwt)` → `Session(user_id, account_id, is_demo, is_service)`.
3. Session is attached to `request.state.session` and exposed via the `get_session` FastAPI dependency.
4. Route handlers READ `session.account_id` and apply application-level filters (since backend connections bypass RLS).

**The invariant: no backend write happens without an explicit `account_id` filter on the query.** Backend reads / writes via DATABASE_URL run with bypass-RLS privileges — the application layer is responsible for tenant scoping.

For frontend reads (Supabase REST via PostgREST): RLS does the filtering automatically. The frontend never sees rows from accounts the user doesn't belong to.

Validation:
- A test that hits a route without auth → expect 401.
- A test that hits a route with a valid token from account A but tries to write into account B → 403 (application-level check on `session.account_id` vs target's `account_id`).
- A frontend Supabase query against another tenant's prospect_id → returns empty (not 403; row is invisible).

### Demo mode reconciliation (M5)

`?demo=true` short-circuits Supabase Auth:

1. Frontend `AccountProvider` detects `isDemoMode()` (graphStore) → skips login, sets account context to `{id: "00000000-0000-0000-0000-000000000fff", display_name: "Demo Account"}`.
2. All Supabase reads in demo mode use the **anon key with a pre-signed JWT** that resolves to the demo account.
3. The demo account has its own RLS policy: `using (account_id = '00000000-...-000fff'::uuid)` — public-readable, write-forbidden.
4. `loadGraphFromDemoData()` (already exists in graphStore) sources from `demoData.ts` regardless; the auth path is just consistent.

### Wave 5 integration

- `enrichment_cost_log.account_id` → query "what did account X spend on Apollo this month?" is one indexed read.
- Apollo / PDL / Parallel route checks `account_settings.<vendor>_monthly_cents` before issuing a vendor call. Sum YTD spend, compare, decline if over.
- Vendor budget enforcement happens server-side (route layer); frontend never sees keys.

### Test conditions

A complete M1+M2+M3 implementation must pass:

1. **Migration safety:** running M1 against a populated v2 database doesn't lose data — every existing prospect/signal/score row gets a default tenant assignment (M6 decision).
2. **Cross-tenant invisibility:** create accounts A and B. Insert prospect into A. Query as user-of-B. Result is empty.
3. **Service-role bypass:** scheduled extractor connects with service role, INSERTs into account-A's signals table while `app.account_id` is unset. Insert succeeds.
4. **Auth required:** `POST /signals/discover-connections` without a Bearer token → 401.
5. **Demo mode:** `GET /discover?demo=true` loads without a Supabase token, account context is the demo pseudo-tenant, no real-tenant data leaks.
6. **Budget enforcement:** account A has `apollo_monthly_cents = 50`. Monthly spend is already 48. Next call: declines with `vendors_skipped_for_cost`.

### Open architecture questions (gated on user)

- **Multi-user per account:** v1 is single-user; the schema (`account_users` join table) anticipates v2 but doesn't yet enforce role-based perms. v2 work.
- **Account ownership transfer:** not in scope for v1.
- **Admin / impersonation flow:** not in scope for v1.
- **Cross-tenant data sharing** (e.g., shared "your team" lists): not in scope for v1.

---

## Contract 10: `GET /orgchart/uncertain-edges`

**Owner module:** `server/credence/api.py` (route) + `credence.orgchart.active_sampling` (selection)
**Implementers:** DarkBeaver (backend)
**Consumers:** SunnyRidge — active-sampling UI in `NodeInspector.tsx`

### Signature

```
GET /orgchart/uncertain-edges?account_id=<uuid>&limit=20&confidence_ceiling=0.55
```

### Input (query params)

- `account_id` — required, UUID. Tenant scope.
- `limit` — optional, int, default `20`, range `[1, 50]`.
- `confidence_ceiling` — optional, float, default `0.55`, range `[0.0, 1.0]`. Implicit edges with `confidence ≤ ceiling` are eligible.
- Auth: same Session middleware as `POST /orgchart/correction`.

### Output

Pydantic `UncertainEdgesResponse`:

```json
{
  "count": 5,
  "account_id": "00000000-0000-0000-0000-000000000001",
  "limit": 20,
  "confidence_ceiling": 0.55,
  "edges": [
    {
      "edge_id": "...",
      "account_id": "...",
      "manager": {"id": "...", "name": "...", "title": "...", "company_id": "..."},
      "report":  {"id": "...", "name": "...", "title": "..."},
      "confidence": 0.50,
      "path_confidence": 0.41,
      "inference_method": "implicit_scoring",
      "dominant_signal": "domain_match",
      "score_components": {"seniority_gap": 0.18, "domain_match": 0.25},
      "manager_span": 8,
      "uncertainty_score": 1.599
    }
  ]
}
```

### Selection model

- Filters: `is_current=TRUE`, `inference_method='implicit_scoring'`, `confidence ≤ confidence_ceiling`.
- Ranking: `(1 - confidence) * (1 + log1p(manager_span))` — combines local uncertainty with downstream blast radius.
- Module: `credence.orgchart.active_sampling.select_uncertain_edges`.

---

## Contract 11: `POST /orgchart/correction` — `component_attributions` field

**Owner module:** `server/credence/api.py` + migration `20260501_v3_orgchart_correction_attributions.sql`
**Implementers:** SwiftElk (Phase D.1)
**Consumers:** Phase D.2 optimizer (DarkBeaver/SwiftElk lane)

Existing endpoint, additive optional field. Backward-compatible.

### New optional body field

- `component_attributions: dict[str, float] | None` (default `None`).
- Keys must be drawn from the seven implicit-scoring components — same keyspace as `org_reporting_edges.score_components`:
  `seniority_gap`, `domain_match`, `subdomain_match`, `manager_title`, `span_capacity`, `patent_cluster`, `geographic_scope`.
- Values: float in `[0.0, 1.0]`.
- Empty dict `{}` is accepted (operator submitted no attribution); `None` and field-omitted both default to no attribution.
- Validation: server raises `400` with a specific message on unknown keys, non-numeric values, or out-of-range floats.

### Persistence

- DB column: `org_chart_corrections.component_attributions JSONB`.
- Migration: `20260501_v3_orgchart_correction_attributions.sql`.

### Used by

Phase D.2 optimizer reads attributions to nudge specific scoring components rather than applying a uniform multiplier when corrections accumulate.

---

## Cross-cutting Constants — Source of Truth

These tables exist in CLAUDE.md and must be mirrored exactly in code. **Do not duplicate values** — import from the canonical location:

| Constant | Canonical location | Used by |
|---|---|---|
| `STRENGTH_TABLE` (base strength per connection type) | `src/lib/graph.ts` `EDGE_CONFIGS[k].baseStrength` | Contract 2, 3, 7; extractors |
| `DECAY_RATES` (per connection type, per year) | `src/lib/graph.ts` `EDGE_CONFIGS[k].decayRate` | Contract 7's strength job |
| Seniority taxonomy (CEO=100 … Engineer=35) | `server/lib/taxonomy.py` `SENIORITY_SCORES` | Contract 1 (career_overlap signals), org chart |
| Functional domain keys | `server/lib/taxonomy.py` `FUNCTIONAL_DOMAINS` | Contract 1, org chart |
| Reporting/scope NLP regexes | `server/lib/extractors/text_patterns.py` | Org chart pipeline |

Backend taxonomy modules (`server/lib/taxonomy.py`, `text_patterns.py`) **do not yet exist**; they are part of the org-chart track, not Track C. Listed here for forward-compatibility.

---

## Org-chart pipeline — v3.1 build in progress (status: 2026-05-01)

The schema migration `20260501_v3_orgchart_schema.sql` (drafted by
SwiftElk in msg 135) is **NOT YET applied** to live Supabase pending LP
review — supersedes the earlier note that schemas were live. The 6 tables
remain absent in the live DB; the population code below ships against
unit-test shims and goes live the moment LP applies the migration.

### What v3.1 ships (in flight, not yet 100%)

The tables `org_reporting_edges`, `org_functional_clusters`,
`org_cluster_members`, `person_scope_estimates`, `org_chart_corrections`,
`org_signal_performance` are written by `server/credence/orgchart/`:

| Module | Owner | Status |
|---|---|---|
| `clustering.py` (Stage 1.1) | SwiftElk | ✅ shipped (msg 135) — populates `org_functional_clusters` + `org_cluster_members` from `employment_periods` joined with `persons`. IC-track flag set per CLAUDE.md L211. |
| `hierarchy.py` (Stage 1.2) | DarkBeaver | ✅ shipped (msg 141 area) — explicit + implicit edge inference, span limits, IC-vs-management preservation. 30 unit specs green. |
| `scope.py` (Stage 1.3) | DarkBeaver | ⏸ open — `person_scope_estimates` derived from reporting tree + patent assignees |
| `corrections.py` + route | SwiftElk | ✅ shipped (msg 146) — `POST /orgchart/correction` + `record_correction()` primitive, 4-value keyspace, 15 unit specs |
| `performance.py` (Stage 2.2) | SwiftElk | (in progress — depends on corrections, schema apply, expected msg ~147) |
| `optimizer.py` (Stage 3.1) | LavenderPrairie | ⏸ open — Bayesian weight tuning over `org_signal_performance` |
| `validation.py` (Stage 3.2) | DarkBeaver | ⏸ open — span limits + cycle detection + IC misclassification flagging |
| Confidence propagation (Stage 3.3) | DarkBeaver | ⏸ open — extends `hierarchy.py` to fill `path_confidence` |

### Authoritative spec

CLAUDE.md L182-251 + V3_PT2.md (Plan A) — cluster by functional_domain
**before** hierarchy assignment, prefer explicit signals over implicit,
span-of-control caps per seniority tier, IC-track parity at same seniority.

### Frontend consumers

- `NodeInspector.tsx` will surface a "Report wrong relationship" button
  per V3_PT2.md L184-208 — UI half (A4) open for SR. Backend route
  + 4-value keyspace already live per A4 backend ship.
- `OrgChartPanel` (component) is **still deferred** — V3_PT2.md
  describes the data model + correction capture; the actual graph
  visualization extends `GraphCanvas.tsx`'s force-graph for tree
  rendering. Out of v3.1 ship.

---

## Hidden-connections expansion — v3.1 build in progress (status: 2026-05-01)

V3_PT2.md (Plan B) adds 4 new EdgeKinds (`same_mba_cohort`,
`same_phd_program`, `executive_education`, `same_undergrad_cohort`)
plus 3 schema tables (`institutions`, `education_overlaps`,
`conference_attendances`) and 3 new extractors. Status:

| Item | Owner | Status |
|---|---|---|
| Schema migration `20260501_v3_education_conference.sql` | SwiftElk | 📝 drafted (msg 138), **needs LP apply** |
| 4 new EdgeKinds in `graph.ts` + EDGE_CONFIGS + CSS | SunnyRidge | ✅ shipped |
| Education extractor (`extractors/education.py`) | SwiftElk | ✅ shipped (msg 140) — PDL + school normalization + cohort strength, 23 specs |
| Conference-program extractor (`extractors/conference.py`) | SwiftElk | ✅ shipped (msg 144) — Firecrawl + regex parsing, 16 specs |
| Standards-roster extractor (`extractors/standards.py`) | SwiftElk | ✅ shipped (msg 142) — 6 bodies via Firecrawl, regex parsing, 15 specs |
| `signals.py` API extension (3 new sources) | SwiftElk | ✅ shipped (msg 138) — `education`, `conference`, `standards` source names |
| Warm-path explanation+opener templates (`warmPaths.ts`) | SunnyRidge | ⏸ open |
| `DEMO_CASES.md` update if real pair surfaces | open | ⏸ depends on B3/B4 live runs |

### Confidence map (added by Plan B6)

The `_confidence_for(source, payload)` route helper now honors:
- `education`: `payload["confidence"]` (cohort_strength) or STRENGTH_TABLE
  per signal_type fallback
- `conference`: 0.80 / 0.20 (presenter / attendee)
- `standards`: 0.82 (committee_peer)

These mirror CLAUDE.md STRENGTH_TABLE base values. The new edge kinds use
`baseStrength` from `EDGE_CONFIGS` per V3_PT2.md L391-422.

---

## Open Questions / Judgment Calls (from Track C drafting)

The following points required judgment because CLAUDE.md was either silent or partially specified. Flagging for SwiftElk to confirm or override:

1. **Contract 1, idempotency**: CLAUDE.md does not specify whether `POST /signals/discover-connections` is idempotent. I asserted it must be (re-POSTing the same pair reuses existing rows via a unique constraint). If SwiftElk wants append-on-each-call semantics instead, the unique constraint goes away.
2. **Contract 1, response shape**: CLAUDE.md's example returns `{"connections_found": N, "connections": [...]}`. I extended this with `sources_attempted`, `sources_failed`, `elapsed_ms`, `truncated` to make partial-results behavior explicit. These are additions; nothing in CLAUDE.md is removed.
3. **Contract 2, default `warmEdgeKinds`**: CLAUDE.md does not specify a default. I picked "all kinds with `baseStrength >= 0.50`" — this excludes `alumni_network` (0.25) and `conference_co_attendee` (0.20) but includes everything that genuinely indicates a warm relationship.
4. **Contract 2, `topK` default 10**: CLAUDE.md says "return top 10 paths" — codified as the default with override capability.
5. **Contract 4, `weight_version_id` on every signal**: I denormalized this. CLAUDE.md Decision 6 says scores are versioned but doesn't explicitly say signals also carry the version. I argue they must, because score recomputation joins `signals × weight_version` and partial recompute needs to know which signals were used under which weights. If SwiftElk disagrees, drop this field and adjust Contract 6.
6. **Contract 5, demo prospect UUIDs**: CLAUDE.md doesn't specify. I picked the deterministic-but-clearly-fake `00000000-0000-0000-0000-00000000000{N}` pattern so demo data can never be confused with real Supabase UUIDs.
7. **Contract 6, the "previous weights" banner heuristic**: CLAUDE.md says "Score computed with previous weights, updated X minutes ago" but doesn't specify how X is computed. Picked a simple heuristic (remaining ÷ rate, clamped). Open to refinement.
8. **Contract 7, schema** beyond what CLAUDE.md specifies: CLAUDE.md describes the columns inline. I added explicit indexes (`idx_person_connections_a/b/type`), the `unique (a, b, connection_type)` constraint, and the `ON CONFLICT DO UPDATE` merge semantics. These are implied by CLAUDE.md's "never compute on the fly" and "merge corroboration" guidance, but were not formal SQL.

None of these conflict with CLAUDE.md. They formalize what CLAUDE.md leaves under-specified.

---

## What Comes Next

After this contract is reviewed and (where needed) corrected:

- **Track A** (DarkBeaver, v2 audit): can verify the existing repo's `graph.ts`, mockStore, FastAPI scaffolding against Contracts 3, 4, and 1 respectively.
- **Track B** (LavenderPrairie, v3 gap analysis): can produce a precise diff between Contracts 1–7 and the repo's current state, file-by-file.
- **Future tracks** (warm-path engine, signals endpoint, demo mode): each implements a single contract end-to-end with clear test conditions.

This document is the input to those tracks. It will not be modified during their execution. If a track surfaces a contract bug, the track pauses, the bug is filed back to whoever owns this document (currently SwiftElk-as-orchestrator), and the contract is amended in a separate, explicit step.

---

*CONTRACTS.md — Credence v3 — drafted by SunnyRidge (Track C)*
