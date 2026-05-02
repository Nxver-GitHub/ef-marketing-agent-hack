/**
 * graphStore — Zustand store for graph view state.
 *
 * Implements CONTRACTS.md Contract 5 (demo mode) and the parts of the graph
 * UI state that need to be shared between GraphChat (left rail), GraphCanvas
 * (center), and NodeInspector (right rail). Per CLAUDE.md §"How to Work on
 * This Codebase": "Any new global state for the graph must go here. Do not
 * create local component state for anything that needs to be shared between
 * GraphChat, GraphCanvas, and NodeInspector."
 *
 * The store DOES:
 *   - hold graph data (nodes + edges) once it has been loaded
 *   - hold selection / focus / filter state for the UI
 *   - latch demo-mode at app boot per Contract 5 (single read of
 *     `?demo` from the URL, cached, never re-read)
 *   - set `<html data-demo-mode="true">` once when demo mode is active so
 *     CSS hooks (banner, dimmed controls) can target it
 *
 * The store DOES NOT:
 *   - fetch data — Discover.tsx + db.ts hooks (live) or demoData.ts (demo)
 *     write into the store via setGraph()
 *   - compute warm paths — that lives in src/lib/warmPaths.ts (Contract 2)
 *   - compute scores — that lives in src/lib/scoreMath.ts and
 *     server/credence/score.py (Contract 6)
 *   - mutate backend state — demo mode must produce zero network writes
 *     (Contract 5 invariant)
 *
 * NOTE — dependency-free for now:
 *   The public API (`useGraphStore(selector)`, `useGraphStore.getState()`,
 *   `useGraphStore.setState({...})`) is byte-compatible with `zustand`'s
 *   `create()`. We use a tiny inline `useSyncExternalStore` implementation
 *   instead of importing zustand because (a) zustand isn't installed yet
 *   (Track A audit §A4.2; package-manager decision is open, item #14) and
 *   (b) Discover.tsx (Track L2) needs to import this store NOW. When the
 *   PM decision lands and `zustand` is added to package.json, swap the
 *   `vanillaCreate` definition for `import { create } from "zustand"` —
 *   no callsite changes.
 */

import { useSyncExternalStore } from "react"
import {
  ALL_EDGE_KINDS,
  EDGE_CONFIGS,
  type EdgeKind,
  type GraphEdge,
  type GraphNode,
  type NodeKind,
} from "../lib/graph"

// ─── Inline minimal zustand-compatible store factory ─────────────────────
// Roughly 25 lines vs pulling in the zustand package; produces a hook that
// (a) accepts a selector, (b) re-renders only on selector-output changes
// (referential equality, like zustand's default), (c) exposes static
// `getState` / `setState` for imperative use. Sufficient for our needs;
// not a complete zustand reimplementation.

type StoreSet<S> = (partial: Partial<S> | ((state: S) => Partial<S>)) => void
type StoreInit<S> = (set: StoreSet<S>) => S

interface StoreHook<S> {
  <T>(selector: (state: S) => T): T
  getState: () => S
  setState: StoreSet<S>
}

function create<S>(init: StoreInit<S>): StoreHook<S> {
  const listeners = new Set<() => void>()
  let state: S
  const setState: StoreSet<S> = (partial) => {
    const next =
      typeof partial === "function" ? (partial as (s: S) => Partial<S>)(state) : partial
    state = { ...state, ...next }
    for (const listener of listeners) listener()
  }
  state = init(setState)
  const subscribe = (listener: () => void): (() => void) => {
    listeners.add(listener)
    return () => {
      listeners.delete(listener)
    }
  }
  const getState = (): S => state
  const useStore = <T,>(selector: (s: S) => T): T =>
    useSyncExternalStore(
      subscribe,
      () => selector(state),
      () => selector(state),
    )
  ;(useStore as StoreHook<S>).getState = getState
  ;(useStore as StoreHook<S>).setState = setState
  return useStore as StoreHook<S>
}

// ─── Constants ─────────────────────────────────────────────────────────────

/**
 * Stable UUIDs for the 5 demo prospects per Contract 5 invariant. Real
 * Supabase rows use UUIDv4 (random), so these all-zeros prefixes can never
 * collide. demoData.ts must use these exact IDs.
 */
export const DEMO_PROSPECT_IDS = [
  "00000000-0000-0000-0000-000000000001",
  "00000000-0000-0000-0000-000000000002",
  "00000000-0000-0000-0000-000000000003",
  "00000000-0000-0000-0000-000000000004",
  "00000000-0000-0000-0000-000000000005",
] as const

const DEMO_MODE_ATTR = "data-demo-mode"

// ─── Demo-mode boot detection ─────────────────────────────────────────────

/**
 * Detect demo mode from the URL once. Per Contract 5: checked at app boot
 * and cached; subsequent SPA navigations do NOT re-check. Toggling demo
 * vs live requires a full page reload.
 *
 * Safe to call from non-browser environments (SSR, vitest jsdom): returns
 * false when `window` is undefined.
 */
function detectDemoModeOnce(): boolean {
  if (typeof window === "undefined") return false
  const params = new URLSearchParams(window.location.search)
  return params.has("demo")
}

const IS_DEMO_MODE = detectDemoModeOnce()

/**
 * Set the `<html data-demo-mode="true">` attribute so CSS can target demo
 * styling (banner, dimmed live-only controls). Idempotent — safe to call
 * multiple times. Per Contract 5 §"Activation".
 */
function setDemoModeAttribute(active: boolean): void {
  if (typeof document === "undefined") return
  if (active) {
    document.documentElement.setAttribute(DEMO_MODE_ATTR, "true")
  } else {
    document.documentElement.removeAttribute(DEMO_MODE_ATTR)
  }
}

// Apply the attribute exactly once at module load, mirroring the cached
// detection above. Subsequent flips do not happen because demo mode is
// boot-time-only.
setDemoModeAttribute(IS_DEMO_MODE)

/**
 * Public accessor for the cached demo-mode flag. Prefer this over reading
 * `window.location.search` ad-hoc — single source of truth.
 */
export function isDemoMode(): boolean {
  return IS_DEMO_MODE
}

// ─── Store shape ──────────────────────────────────────────────────────────

export type LoadStatus = "idle" | "loading" | "ready" | "error"

export interface GraphStoreState {
  // ── Mode (read-only after boot) ──
  readonly isDemoMode: boolean

  // ── Graph data ──
  nodes: GraphNode[]
  edges: GraphEdge[]
  loadStatus: LoadStatus
  loadError: string | null
  lastLoadedAt: number | null

  // ── Selection / focus ──
  /** The pinned node — drives NodeInspector + WarmPathPanel. */
  selectedNodeId: string | null
  /** Hover/preview node. NodeInspector falls back to this when no
   *  pinned selection exists. */
  focusedNodeId: string | null

  // ── Filters ──
  /** EdgeKinds currently visible. Empty set means hide all edges. */
  visibleEdgeKinds: ReadonlySet<EdgeKind>
  /** NodeKinds currently visible. Empty set means hide all nodes. */
  visibleNodeKinds: ReadonlySet<NodeKind>
  searchQuery: string

  // ── Actions ──
  setGraph: (data: { nodes: GraphNode[]; edges: GraphEdge[] }) => void
  setLoadStatus: (status: LoadStatus, error?: string | null) => void
  selectNode: (id: string | null) => void
  focusNode: (id: string | null) => void
  toggleEdgeKind: (kind: EdgeKind) => void
  setVisibleEdgeKinds: (kinds: ReadonlySet<EdgeKind>) => void
  toggleNodeKind: (kind: NodeKind) => void
  setVisibleNodeKinds: (kinds: ReadonlySet<NodeKind>) => void
  setSearchQuery: (q: string) => void
  reset: () => void
}

// ─── Initial state ────────────────────────────────────────────────────────

/**
 * Default-visible edge kinds — derived from Contract 3's `EDGE_CONFIGS`
 * registry in `graph.ts`. Single source of truth: changing visibility
 * defaults happens in one place (the `defaultVisible` field of an
 * `EdgeConfig` row). Adding a new EdgeKind without setting that field is
 * a TypeScript compile error, so this set automatically tracks the
 * canonical taxonomy without manual sync. Replaces the bootstrap
 * hardcoded list (post G.5).
 */
const DEFAULT_VISIBLE_EDGE_KINDS: ReadonlySet<EdgeKind> = new Set<EdgeKind>(
  ALL_EDGE_KINDS.filter((kind) => EDGE_CONFIGS[kind].defaultVisible),
)

const DEFAULT_VISIBLE_NODE_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>([
  "person",
  "company",
  "role",
  "city",
  "school",
  "conference",
  "industry",
])

const INITIAL_STATE: Omit<
  GraphStoreState,
  | "setGraph"
  | "setLoadStatus"
  | "selectNode"
  | "focusNode"
  | "toggleEdgeKind"
  | "setVisibleEdgeKinds"
  | "toggleNodeKind"
  | "setVisibleNodeKinds"
  | "setSearchQuery"
  | "reset"
> = {
  isDemoMode: IS_DEMO_MODE,
  nodes: [],
  edges: [],
  loadStatus: "idle",
  loadError: null,
  lastLoadedAt: null,
  selectedNodeId: null,
  focusedNodeId: null,
  visibleEdgeKinds: DEFAULT_VISIBLE_EDGE_KINDS,
  visibleNodeKinds: DEFAULT_VISIBLE_NODE_KINDS,
  searchQuery: "",
}

// ─── Store ────────────────────────────────────────────────────────────────

export const useGraphStore = create<GraphStoreState>((set) => ({
  ...INITIAL_STATE,

  setGraph: ({ nodes, edges }) =>
    set({
      nodes,
      edges,
      loadStatus: "ready",
      loadError: null,
      lastLoadedAt: Date.now(),
    }),

  setLoadStatus: (status, error = null) =>
    set({
      loadStatus: status,
      loadError: status === "error" ? (error ?? "Unknown error") : null,
    }),

  selectNode: (id) => set({ selectedNodeId: id }),
  focusNode: (id) => set({ focusedNodeId: id }),

  toggleEdgeKind: (kind) =>
    set((s) => {
      const next = new Set(s.visibleEdgeKinds)
      if (next.has(kind)) next.delete(kind)
      else next.add(kind)
      return { visibleEdgeKinds: next }
    }),

  setVisibleEdgeKinds: (kinds) => set({ visibleEdgeKinds: new Set(kinds) }),

  toggleNodeKind: (kind) =>
    set((s) => {
      const next = new Set(s.visibleNodeKinds)
      if (next.has(kind)) next.delete(kind)
      else next.add(kind)
      return { visibleNodeKinds: next }
    }),

  setVisibleNodeKinds: (kinds) => set({ visibleNodeKinds: new Set(kinds) }),

  setSearchQuery: (q) => set({ searchQuery: q }),

  reset: () => set(INITIAL_STATE),
}))

// ─── Demo-mode data injection ────────────────────────────────────────────

/**
 * Load demo-mode graph data into the store. Per Contract 5
 * §"Data loading switch", graphStore must populate from `demoData.ts`
 * when `?demo=true` is active. We use dependency injection (caller passes
 * the data) rather than `import { DEMO_GRAPH_NODES } from "../lib/demoData"`
 * to keep `graphStore.ts` independent of `demoData.ts` (which itself
 * imports `DEMO_PROSPECT_IDS` from this file — direct import would cycle).
 *
 * Callers (typically `App.tsx` at boot) invoke:
 *
 *   import { initDemoMode } from "@/store/graphStore"
 *   import { DEMO_GRAPH_NODES, DEMO_EDGES } from "@/lib/demoData"
 *   initDemoMode([...DEMO_GRAPH_NODES], [...DEMO_EDGES])
 *
 * No-op when not in demo mode, so safe to call unconditionally at boot.
 */
export function initDemoMode(nodes: GraphNode[], edges: GraphEdge[]): void {
  if (!IS_DEMO_MODE) return
  useGraphStore.getState().setGraph({ nodes, edges })
}

// ─── Selectors (call sites use these via `useGraphStore(selector)`) ──────

/** Resolve the pinned node, or fall back to the focus/hover node. */
export const selectActiveNode = (s: GraphStoreState): GraphNode | null => {
  const id = s.selectedNodeId ?? s.focusedNodeId
  if (!id) return null
  return s.nodes.find((n) => n.id === id) ?? null
}

/** Edges whose kind is currently visible. */
export const selectVisibleEdges = (s: GraphStoreState): GraphEdge[] =>
  s.edges.filter((e) => s.visibleEdgeKinds.has(e.kind))

/** Nodes whose kind is currently visible AND that match the search query.
 *  Search is a case-insensitive substring match against `name` for typed
 *  nodes that have one. */
export const selectVisibleNodes = (s: GraphStoreState): GraphNode[] => {
  const q = s.searchQuery.trim().toLowerCase()
  return s.nodes.filter((n) => {
    if (!s.visibleNodeKinds.has(n.kind)) return false
    if (!q) return true
    const name = "name" in n ? n.name : ""
    return name.toLowerCase().includes(q)
  })
}
