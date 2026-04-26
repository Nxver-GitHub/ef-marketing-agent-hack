/**
 * Discover (v2) — three-column graph view.
 *
 * Layout:
 *   TopBar (with edge-filter pills)
 *   ┌──────────────────────────────────────────────────────────────┐
 *   │ GraphChat │ Subheader + Force-graph canvas │ NodeInspector  │
 *   └──────────────────────────────────────────────────────────────┘
 *
 * We bypass PageShell here to get full-bleed; PageShell adds max-width +
 * padding which breaks the graph canvas filling the body row. The TopBar
 * is rendered directly so the route still uses the shared chrome.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useIsFetching } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import ForceGraph2D, {
  type ForceGraphMethods,
  type LinkObject,
  type NodeObject,
} from "react-force-graph-2d";
import { TopBar } from "@/components/TopBar";
import { GraphChat } from "@/components/GraphChat";
import { NodeInspector } from "@/components/NodeInspector";
import { useProspects, useScoresFor, useSignalsForMany } from "@/lib/db";
import { useDocumentTitle } from "@/lib/useDocumentTitle";
import {
  buildGraph,
  type EdgeKind,
  type GraphNode,
  type NodeKind,
  type ThemeTokens,
} from "@/lib/graph";
import type { AgentContext } from "@/lib/agent";

// ─── CSS-var color helpers ───────────────────────────────────────────────────

const slugifyEdge = (kind: EdgeKind): string => {
  // index.css maps EdgeKind -> --edge-<slug>; mapping is the post-fix part.
  switch (kind) {
    case "reports_to":
      return "reports";
    case "works_at":
      return "employer";
    case "located_in":
      return "location";
    case "evidence_cited":
      return "evidence";
    case "scope_signal":
      return "scope";
    case "partnership":
      return "partnership";
    case "past_employer":
      return "past-empl";
    case "education":
      return "education";
    case "vertical":
      return "vertical";
    case "colleague":
      // Not defined as its own token — borrow employer.
      return "employer";
  }
};

function hslFromVar(varName: string): string {
  if (typeof window === "undefined") return "hsl(0 0% 50%)";
  const raw = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
  return raw ? `hsl(${raw})` : "hsl(0 0% 50%)";
}

const NODE_VAR: Record<NodeKind, string> = {
  person: "--node-person",
  company: "--node-company",
  role: "--node-role",
  city: "--node-city",
  school: "--node-school",
  conference: "--node-conference",
  industry: "--node-industry",
};

// ─── Force-graph link/node typing helpers ────────────────────────────────────

type FGNode = NodeObject<GraphNode>;
type FGLink = LinkObject<
  GraphNode,
  { kind: EdgeKind; id: string; color?: string; width?: number }
>;

// All edge kinds — used for the edge-color theme map.
const ALL_EDGE_KINDS: EdgeKind[] = [
  "works_at",
  "colleague",
  "located_in",
  "reports_to",
  "past_employer",
  "partnership",
  "education",
  "scope_signal",
  "vertical",
  "evidence_cited",
];

// react-force-graph mutates source/target to NodeObjects after init; this
// narrows safely whether we're pre- or post-init.
function linkEndpointId(end: string | number | FGNode | undefined): string | undefined {
  if (end === undefined) return undefined;
  if (typeof end === "string") return end;
  if (typeof end === "number") return String(end);
  return end.id as string | undefined;
}

// ─── Small per-node-kind shape painter ───────────────────────────────────────

function paintShape(
  ctx: CanvasRenderingContext2D,
  kind: NodeKind,
  x: number,
  y: number,
  r: number,
  fill: string,
): void {
  ctx.fillStyle = fill;
  ctx.strokeStyle = fill;
  ctx.lineWidth = 1;
  switch (kind) {
    case "person": {
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
      return;
    }
    case "company": {
      const s = r * 1.8;
      const rad = r * 0.35;
      roundRect(ctx, x - s / 2, y - s / 2, s, s, rad);
      ctx.fill();
      return;
    }
    case "role": {
      // Hexagon
      ctx.beginPath();
      for (let i = 0; i < 6; i++) {
        const a = (Math.PI / 3) * i + Math.PI / 6;
        const px = x + Math.cos(a) * r;
        const py = y + Math.sin(a) * r;
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      }
      ctx.closePath();
      ctx.fill();
      return;
    }
    case "city": {
      // Pill
      const w = r * 2.6;
      const h = r * 1.4;
      roundRect(ctx, x - w / 2, y - h / 2, w, h, h / 2);
      ctx.fill();
      return;
    }
    case "school": {
      // Diamond
      ctx.beginPath();
      ctx.moveTo(x, y - r);
      ctx.lineTo(x + r, y);
      ctx.lineTo(x, y + r);
      ctx.lineTo(x - r, y);
      ctx.closePath();
      ctx.fill();
      return;
    }
    case "conference": {
      // Triangle
      ctx.beginPath();
      ctx.moveTo(x, y - r);
      ctx.lineTo(x + r * 0.9, y + r * 0.7);
      ctx.lineTo(x - r * 0.9, y + r * 0.7);
      ctx.closePath();
      ctx.fill();
      return;
    }
    case "industry": {
      // Small square
      const s = r * 1.6;
      ctx.fillRect(x - s / 2, y - s / 2, s, s);
      return;
    }
  }
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.lineTo(x + w - rr, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + rr);
  ctx.lineTo(x + w, y + h - rr);
  ctx.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
  ctx.lineTo(x + rr, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - rr);
  ctx.lineTo(x, y + rr);
  ctx.quadraticCurveTo(x, y, x + rr, y);
  ctx.closePath();
}

// ─── Subheader bits ──────────────────────────────────────────────────────────

const NODE_KINDS_LEGEND: ReadonlyArray<{ kind: NodeKind; label: string }> = [
  { kind: "person", label: "Person" },
  { kind: "company", label: "Company" },
  { kind: "role", label: "Role" },
  { kind: "city", label: "City" },
  { kind: "school", label: "School" },
  { kind: "conference", label: "Conf." },
  { kind: "industry", label: "Industry" },
];

const DEFAULT_EDGE_KINDS: EdgeKind[] = [
  "reports_to",
  "works_at",
  "located_in",
  "evidence_cited",
  "scope_signal",
  "partnership",
  "past_employer",
];

// Cap how many prospects we let into the force-directed canvas at the
// "wide" (no focus) view. We're after an Obsidian-style web — readable at
// first glance, with focusable clusters, not a 500-node hairball. Once the
// user clicks a node, we drill into a focal subgraph that's bounded
// separately by FOCAL_PEOPLE_CAP. Chat copilot operates on the full
// 10k-prospect graph via AgentContext.
const MAX_PROSPECTS_RENDERED = 120;
// When drilled into a focal node, we never expand more than this many
// person-neighbors. (e.g., clicking TSMC with 1500 employees still reads
// as a clean cluster, top-scored.)
const FOCAL_PEOPLE_CAP = 20;
// Singleton root id (mirrors graph.ts). We pin this in every focal
// subgraph so the user has a click-to-home anchor without needing a back
// button.
const TECH_ROOT_ID = "industry:technology";

// ─── Component ───────────────────────────────────────────────────────────────

// Edge-pill order — duplicated from TopBar so the legend renders the same
// kinds in the same order. Kept local to avoid a one-import dep on TopBar.
const EDGE_LEGEND: ReadonlyArray<{ kind: EdgeKind; label: string }> = [
  { kind: "reports_to", label: "Reports" },
  { kind: "works_at", label: "Employer" },
  { kind: "located_in", label: "Location" },
  { kind: "evidence_cited", label: "Evidence" },
  { kind: "scope_signal", label: "Scope" },
  { kind: "partnership", label: "Partnership" },
  { kind: "past_employer", label: "Past empl." },
  { kind: "education", label: "Education" },
  { kind: "vertical", label: "Vertical" },
];

// Encode/decode helpers for URL-sync. Unknown values are dropped so a hand-
// edited URL can't crash the page.
function encodeEdgeKinds(active: Set<EdgeKind>): string {
  return Array.from(active).join(",");
}
function decodeEdgeKinds(raw: string | null): Set<EdgeKind> | null {
  if (raw === null) return null;
  const valid = new Set(ALL_EDGE_KINDS);
  const out = new Set<EdgeKind>();
  for (const piece of raw.split(",")) {
    if (valid.has(piece as EdgeKind)) out.add(piece as EdgeKind);
  }
  return out;
}

const Discover = () => {
  useDocumentTitle("Discover");
  const navigate = useNavigate();
  const allProspects = useProspects();
  const allProspectIds = useMemo(() => allProspects.map((p) => p._id), [allProspects]);
  const scores = useScoresFor(allProspectIds);

  // ─── URL-synced view state — hoisted so `prospects` below can react to
  //     chat-driven selection / filtering. (Originally lived further down.) ──
  const [searchParams, setSearchParams] = useSearchParams();
  const initialEdgeKinds = useMemo(
    () =>
      decodeEdgeKinds(searchParams.get("edges")) ??
      new Set<EdgeKind>(DEFAULT_EDGE_KINDS),
    // Read once on mount; subsequent state mutations push back to the URL
    // via the effect below. Including searchParams here would create a loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  // `focusId` is the current "you are here" — clicking any node makes it the
  // new focus and the graph re-flows around it. There's no back button by
  // design (Obsidian-style endless traversal), but the Technology root is
  // always present in every focal subgraph as a click-to-home anchor.
  const [focusId, setFocusId] = useState<string | null>(
    () => searchParams.get("focus") ?? searchParams.get("selected"),
  );
  // selectedId mirrors focusId for inspector wiring. Kept as its own piece
  // of state in case we later want "selection without re-focusing" (e.g.
  // hover-preview). For now, click = focus = select.
  const [selectedId, setSelectedId] = useState<string | null>(focusId);
  const [visibleNodeIds, setVisibleNodeIds] = useState<Set<string> | null>(null);
  const [edgeKindsActive, setEdgeKindsActive] = useState<Set<EdgeKind>>(initialEdgeKinds);

  // Render top-N by overall_score by default. When the chat copilot has
  // narrowed the world via `filter` / `focus_node` / `explain` /
  // `expand_node` (signalled by visibleNodeIds being non-null), render
  // exactly those prospects instead — regardless of where they sit in the
  // score distribution. Without this, "MIT engineers" with overall_score=0
  // returned 6 hits but the canvas kept painting the unrelated top-120
  // because none of the 6 were in the rendered slice.
  //
  // We deliberately do NOT include `selectedId` here: that's set on every
  // canvas click too, and including it would collapse the graph to a single
  // node every time the user clicks anything. Chat-driven selection is
  // bridged separately by also writing into visibleNodeIds (see
  // applyToolResult in src/lib/agent.ts).
  const chatPromotedIds = useMemo<Set<string> | null>(() => {
    if (!visibleNodeIds || visibleNodeIds.size === 0) return null;
    const out = new Set<string>();
    for (const id of visibleNodeIds) {
      out.add(id.startsWith("person:") ? id.slice(7) : id);
    }
    return out;
  }, [visibleNodeIds]);

  const prospects = useMemo(() => {
    if (chatPromotedIds) {
      const matched = allProspects.filter((p) => chatPromotedIds.has(p._id));
      if (matched.length > 0) return matched;
    }
    if (allProspects.length <= MAX_PROSPECTS_RENDERED) return allProspects;
    const ranked = [...allProspects].sort((a, b) => {
      const sa = scores[a._id]?.overall_score ?? -1;
      const sb = scores[b._id]?.overall_score ?? -1;
      return sb - sa;
    });
    return ranked.slice(0, MAX_PROSPECTS_RENDERED);
  }, [allProspects, scores, chatPromotedIds]);

  const prospectIds = useMemo(() => prospects.map((p) => p._id), [prospects]);
  const signalsById = useSignalsForMany(prospectIds);

  // Per-kind cached colors. Recomputed once per mount; theme switch would
  // need to bust this — fine for v1 since the theme toggle isn't on Discover.
  const nodeColorByKind = useMemo<Record<NodeKind, string>>(() => {
    const out = {} as Record<NodeKind, string>;
    (Object.keys(NODE_VAR) as NodeKind[]).forEach((k) => {
      out[k] = hslFromVar(NODE_VAR[k]);
    });
    return out;
  }, []);

  // Edge color cache — same caveat as above.
  const edgeColorByKind = useMemo<Record<EdgeKind, string>>(() => {
    const out = {} as Record<EdgeKind, string>;
    for (const k of ALL_EDGE_KINDS) out[k] = hslFromVar(`--edge-${slugifyEdge(k)}`);
    return out;
  }, []);

  const theme = useMemo<ThemeTokens>(
    () => ({ nodeColors: nodeColorByKind, edgeColors: edgeColorByKind }),
    [nodeColorByKind, edgeColorByKind],
  );

  // Theme-aware label palette. Uses --foreground / --muted-foreground so
  // the labels flip light automatically in dark mode. The "stroke" colour is
  // the canvas background, drawn behind the fill text to give every label a
  // 1px halo and keep it readable against busy edges.
  const labelStrong = useMemo(() => hslFromVar("--foreground"), []);
  const labelMuted = useMemo(() => hslFromVar("--muted-foreground"), []);
  const labelHalo = useMemo(() => hslFromVar("--background"), []);

  // Build graph (memoized). Pre-bakes color on every node + edge.
  const { nodes, edges } = useMemo(
    () => buildGraph({ prospects, scores, signalsById, theme }),
    [prospects, scores, signalsById, theme],
  );

  // Push state -> URL (replace, not push — the user said no back-button feel,
  // and we don't want focus changes piling into the browser history stack).
  useEffect(() => {
    const next = new URLSearchParams(searchParams);
    if (focusId) next.set("focus", focusId);
    else next.delete("focus");
    next.delete("selected"); // legacy — focus is the source of truth now
    next.set("edges", encodeEdgeKinds(edgeKindsActive));
    if (next.toString() !== searchParams.toString()) {
      setSearchParams(next, { replace: true });
    }
  }, [focusId, edgeKindsActive, searchParams, setSearchParams]);

  // Derived: neighbor map for halo / fade.
  const neighborByNode = useMemo(() => {
    const map = new Map<string, Set<string>>();
    for (const e of edges) {
      if (!map.has(e.source)) map.set(e.source, new Set());
      if (!map.has(e.target)) map.set(e.target, new Set());
      map.get(e.source)!.add(e.target);
      map.get(e.target)!.add(e.source);
    }
    return map;
  }, [edges]);

  // Resolve selected node (may be null/stale).
  const selectedNode = useMemo<GraphNode | null>(
    () => (selectedId ? (nodes.find((n) => n.id === selectedId) ?? null) : null),
    [nodes, selectedId],
  );

  // ─── Focal subgraph — the local universe around `focusId` ──────────────────
  // When focusId is null we're in "wide view": show the whole rendered set.
  // When it's set we keep the focal node + its 1-hop neighborhood + the
  // Technology root (always available as a click-to-home anchor). Person
  // neighbors are score-capped to keep big-company drill-downs legible.
  const nodeById = useMemo(() => {
    const m = new Map<string, GraphNode>();
    for (const n of nodes) m.set(n.id, n);
    return m;
  }, [nodes]);

  const focalNodes = useMemo<GraphNode[]>(() => {
    if (!focusId) return nodes;
    const focal = nodeById.get(focusId);
    if (!focal) return nodes; // stale focus id (e.g. from URL) — show all
    const visible = new Set<string>([focusId]);
    if (nodeById.has(TECH_ROOT_ID)) visible.add(TECH_ROOT_ID);
    const neighbors = neighborByNode.get(focusId);
    if (neighbors) {
      // Split neighbors by kind so we can apply person-only capping.
      const persons: string[] = [];
      const others: string[] = [];
      for (const id of neighbors) {
        const n = nodeById.get(id);
        if (!n) continue;
        if (n.kind === "person") persons.push(id);
        else others.push(id);
      }
      // Sort persons by score desc, take the top N. Roles, companies,
      // industries, cities, schools, conferences are all kept.
      persons.sort((a, b) => {
        const sa = (nodeById.get(a) as Extract<GraphNode, { kind: "person" }> | undefined)?.score ?? -1;
        const sb = (nodeById.get(b) as Extract<GraphNode, { kind: "person" }> | undefined)?.score ?? -1;
        return sb - sa;
      });
      for (const id of persons.slice(0, FOCAL_PEOPLE_CAP)) visible.add(id);
      for (const id of others) visible.add(id);
    }
    const out: GraphNode[] = [];
    for (const n of nodes) if (visible.has(n.id)) out.push(n);
    return out;
  }, [focusId, nodes, nodeById, neighborByNode]);

  const focalEdges = useMemo(() => {
    if (!focusId) return edges;
    const visible = new Set(focalNodes.map((n) => n.id));
    return edges.filter((e) => visible.has(e.source) && visible.has(e.target));
  }, [focusId, edges, focalNodes]);

  // Build the graphData ForceGraph2D consumes (nodes + links). When the
  // focal subgraph changes (focusId click), graphData identity changes and
  // ForceGraph2D re-runs the simulation from scratch — that's the
  // "rearranging around the new universe" feel. `color` is pre-baked by
  // buildGraph(); `width` is mutated in place on selection (see effect
  // below) so the simulation doesn't reset on every selection click.
  const graphData = useMemo(
    () => ({
      nodes: focalNodes as FGNode[],
      links: focalEdges.map<FGLink>((e) => ({
        source: e.source,
        target: e.target,
        kind: e.kind,
        id: e.id,
        color: e.color,
        width: 0.6,
      })),
    }),
    [focalNodes, focalEdges],
  );

  // Mutate link widths in place when selection changes. Avoids re-creating
  // graphData (which would reset the ForceGraph simulation).
  useEffect(() => {
    for (const link of graphData.links) {
      const sId = linkEndpointId(link.source);
      const tId = linkEndpointId(link.target);
      const focal =
        selectedId !== null && (sId === selectedId || tId === selectedId);
      link.width = focal ? 1.6 : 0.6;
    }
  }, [selectedId, graphData]);

  // ─── Canvas sizing — ResizeObserver against the canvas column ──────────────
  const canvasWrapRef = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<ForceGraphMethods<FGNode, FGLink> | undefined>(undefined);
  const [canvasSize, setCanvasSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });

  useEffect(() => {
    const el = canvasWrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        setCanvasSize({ w: Math.max(0, Math.floor(width)), h: Math.max(0, Math.floor(height)) });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // ─── Engine-running flag — drives the warmup skeleton overlay (U4) ─────────
  const [engineRunning, setEngineRunning] = useState(true);
  useEffect(() => {
    if (focalNodes.length) setEngineRunning(true);
  }, [focalNodes, focalEdges]);
  const onEngineStop = useCallback(() => {
    setEngineRunning(false);
    const fg = fgRef.current;
    if (!fg) return;
    // If the user has drilled into a focal node, center the camera on it
    // (now that the simulation has settled around the new local universe).
    // Otherwise, frame the whole view.
    if (focusId) {
      const focal = focalNodes.find((n) => n.id === focusId) as
        | (GraphNode & { x?: number; y?: number })
        | undefined;
      if (focal && typeof focal.x === "number" && typeof focal.y === "number") {
        fg.centerAt(focal.x, focal.y, 600);
        fg.zoom(2.0, 600);
        return;
      }
    }
    fg.zoomToFit(400, 60);
  }, [focusId, focalNodes]);

  // ─── Agent context ─────────────────────────────────────────────────────────
  // Built off the FULL prospect set, not the rendered slice — so the chat
  // copilot keeps recall over all 10k+ candidates even when the canvas only
  // shows the top 500. focus_node / filter / explain etc. all operate on
  // this fuller graph.
  const fullGraph = useMemo(
    () => buildGraph({ prospects: allProspects, scores, signalsById, theme }),
    [allProspects, scores, signalsById, theme],
  );
  const ctx: AgentContext = useMemo(
    () => ({
      nodes: fullGraph.nodes,
      edges: fullGraph.edges,
      setSelectedId,
      setVisibleNodeIds,
      getProspectById: (id) =>
        allProspects.find((p) => `person:${p._id}` === id || p._id === id),
      getScoreById: (id) => {
        const personId = id.startsWith("person:") ? id.slice(7) : id;
        return scores[personId];
      },
    }),
    [fullGraph, allProspects, scores],
  );

  // ─── Inspector data wiring (person variant) ────────────────────────────────
  const selectedProspect = useMemo(() => {
    if (!selectedNode || selectedNode.kind !== "person") return undefined;
    return prospects.find((p) => p._id === selectedNode.raw._id);
  }, [selectedNode, prospects]);
  const selectedScore = selectedProspect ? scores[selectedProspect._id] : undefined;
  const selectedSignals = selectedProspect ? signalsById[selectedProspect._id] : undefined;

  // ─── Edge-kind toggle handler ──────────────────────────────────────────────
  const onToggleEdgeKind = useCallback(
    (kind: EdgeKind) =>
      setEdgeKindsActive((prev) => {
        const next = new Set(prev);
        if (next.has(kind)) next.delete(kind);
        else next.add(kind);
        return next;
      }),
    [],
  );

  // ─── Empty-state gate ──────────────────────────────────────────────────────
  const isEmpty = nodes.length === 0;
  // Bulk-table fetches against Supabase paginate up to ~10s for the full
  // 10k+ prospects pull. Until either prospects or scores resolves, show
  // "Loading…" rather than the misleading "validate one to get started"
  // empty state. `useIsFetching` returns the count of in-flight queries
  // matching the key — non-zero = still streaming pages.
  const fetchingProspects = useIsFetching({ queryKey: ["prospects"] });
  const fetchingScores = useIsFetching({ queryKey: ["scores"] });
  const fetchingSignals = useIsFetching({ queryKey: ["signals", "all"] });
  const isLoadingData =
    fetchingProspects > 0 || fetchingScores > 0 || fetchingSignals > 0;

  // ─── Stable callbacks for ForceGraph2D (avoid per-render identity churn) ───
  const linkVisibility = useCallback(
    (link: FGLink) => edgeKindsActive.has(link.kind),
    [edgeKindsActive],
  );

  const nodeVisibility = useCallback(
    (node: FGNode) => {
      if (visibleNodeIds === null) return true;
      if (visibleNodeIds.has(node.id as string)) return true;
      if (selectedId && node.id === selectedId) return true;
      return false;
    },
    [visibleNodeIds, selectedId],
  );

  // Endless-traversal click handler. Clicking a node makes it the new
  // focus — the graph data swaps to its 1-hop universe, ForceGraph re-runs
  // the layout, and onEngineStop centers/zooms the camera once the new
  // arrangement settles. No back button by design: the user navigates
  // forward by clicking, and the Technology root is always reachable as
  // a "click home" anchor.
  const onNodeClickStable = useCallback((node: FGNode) => {
    const id = node.id as string;
    setFocusId(id);
    setSelectedId(id);
    setEngineRunning(true);
  }, []);
  // Background click is a no-op — preserves the current focal universe.
  // Resetting to the wide view is intentional here: the user clicks the
  // Technology root (always present in the focal subgraph) to "go home".
  const onBackgroundClickStable = useCallback(() => undefined, []);

  const nodeCanvasObject = useCallback(
    (node: FGNode, ctx2d: CanvasRenderingContext2D, globalScale: number) => {
      const gn = node as unknown as GraphNode;
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      // Node radius — slight rank by kind so the eye lands on the bigger
      // hubs (Technology root, industries, cities) before the leaves.
      const isRoot = gn.id === "industry:technology";
      const isSelected = gn.id === selectedId;
      const baseR = isRoot
        ? 9
        : gn.kind === "industry" || gn.kind === "city"
          ? 6
          : gn.kind === "company" || gn.kind === "role"
            ? 5
            : gn.kind === "school" || gn.kind === "conference"
              ? 4
              : 3.5; // person
      const r = isSelected ? baseR + 1.5 : baseR;

      // Obsidian-style neighborhood fade: when something's selected, dim
      // every non-neighbor so the focal cluster reads cleanly.
      const isNeighbor = selectedId
        ? (neighborByNode.get(selectedId)?.has(gn.id) ?? false)
        : false;
      let alpha = 1;
      if (selectedId && visibleNodeIds === null) {
        if (!isSelected && !isNeighbor) alpha = 0.18;
      }

      ctx2d.save();
      ctx2d.globalAlpha = alpha;

      // Selected halo
      if (isSelected) {
        ctx2d.beginPath();
        ctx2d.arc(x, y, r + 4, 0, Math.PI * 2);
        ctx2d.strokeStyle = nodeColorByKind[gn.kind];
        ctx2d.lineWidth = 1.5;
        ctx2d.globalAlpha = alpha * 0.6;
        ctx2d.stroke();
        ctx2d.globalAlpha = alpha;
      }

      paintShape(ctx2d, gn.kind, x, y, r, nodeColorByKind[gn.kind]);

      // Label policy — Obsidian-style. Labels only render when:
      //  • the node is the technology root (anchor reference)
      //  • OR it's an industry / city — always visible (level-1 anchors)
      //  • OR something is selected and this node is the selection or a
      //    direct neighbor
      //  • OR the user has zoomed in past 1.4x (then everyone is labeled)
      const isAnchor =
        isRoot || gn.kind === "industry" || gn.kind === "city";
      const inFocus = isSelected || isNeighbor;
      const zoomedIn = globalScale >= 1.4;
      if (isAnchor || inFocus || zoomedIn) {
        const labelPx = isRoot
          ? 13
          : gn.kind === "industry" || gn.kind === "city"
            ? 10
            : gn.kind === "company" || gn.kind === "role"
              ? 9
              : 8;
        // Use Inter (self-hosted via @fontsource) so canvas labels match
        // the rest of the UI, with weight bumped on selected/root for hub
        // emphasis and slight negative tracking for an editorial feel.
        const weight = isRoot ? 600 : isSelected ? 600 : isAnchor ? 500 : 400;
        ctx2d.font = `${weight} ${labelPx / globalScale}px Inter, ui-sans-serif, system-ui, sans-serif`;
        // Modern Chrome / Safari respect this; older engines just ignore.
        // -1.5% tightens long labels (industry / role names) without
        // overcrowding short ones.
        (ctx2d as CanvasRenderingContext2D & { letterSpacing?: string }).letterSpacing = "-0.015em";
        ctx2d.textAlign = "center";
        ctx2d.textBaseline = "top";
        const fill = isRoot || isSelected || isAnchor ? labelStrong : labelMuted;
        // Halo: stroke the text in the bg color before filling, so labels
        // stay readable against any cluster of edges crossing them.
        ctx2d.lineWidth = 4 / globalScale;
        ctx2d.strokeStyle = labelHalo;
        ctx2d.lineJoin = "round";
        ctx2d.miterLimit = 2;
        ctx2d.strokeText(gn.name, x, y + r + 2);
        ctx2d.fillStyle = fill;
        ctx2d.fillText(gn.name, x, y + r + 2);
      }

      ctx2d.restore();
    },
    [
      selectedId,
      visibleNodeIds,
      neighborByNode,
      nodeColorByKind,
      labelStrong,
      labelMuted,
      labelHalo,
    ],
  );

  // ─── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="h-screen flex flex-col bg-background text-foreground">
      {/* Off-screen heading for screen readers + document outline (U1). */}
      <h1 className="sr-only">Discover — prospect graph</h1>
      <TopBar />
      {/* Spacer for fixed TopBar (h-12) */}
      <div className="h-12 shrink-0" />
      <div className="flex flex-1 min-h-0">
        {/* Left: chat */}
        <GraphChat ctx={ctx} />

        {/* Center: subheader + canvas */}
        <div className="flex-1 min-w-0 flex flex-col">
          {/* Subheader: stats row */}
          <div className="flex items-center justify-between gap-4 border-b border-border px-5 py-3">
            <div className="flex items-center gap-5 text-[11px] text-muted-foreground text-mono">
              <span>
                <span className="text-foreground">{nodes.length}</span> nodes
              </span>
              <span>
                <span className="text-foreground">{edges.length}</span> edges
              </span>
              <span>
                <span className="text-foreground">
                  {nodes.filter((n) => n.kind === "person").length}
                </span>{" "}
                {allProspects.length > MAX_PROSPECTS_RENDERED
                  ? `of ${allProspects.length} candidates (top by score)`
                  : "candidates"}
              </span>
              <span>
                Selected:{" "}
                <span className="text-foreground">
                  {selectedNode?.name ?? "—"}
                </span>
              </span>
            </div>
            <div className="flex items-center gap-1 text-mono text-[11px] text-muted-foreground">
              <ZoomBtn onClick={() => fgRef.current?.zoom((fgRef.current?.zoom() ?? 1) * 0.8, 200)}>
                −
              </ZoomBtn>
              <ZoomBtn onClick={() => fgRef.current?.zoom(1, 200)}>100%</ZoomBtn>
              <ZoomBtn onClick={() => fgRef.current?.zoom((fgRef.current?.zoom() ?? 1) * 1.25, 200)}>
                +
              </ZoomBtn>
              <ZoomBtn onClick={() => fgRef.current?.zoomToFit(400, 60)}>↻</ZoomBtn>
            </div>
          </div>

          {/* Subheader: legend (nodes + edges) — U5 */}
          <div className="flex flex-col gap-1.5 border-b border-border px-5 py-2">
            <div className="flex items-center gap-3 flex-wrap">
              <span className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground w-12 shrink-0">
                Nodes
              </span>
              {NODE_KINDS_LEGEND.map((l) => (
                <span
                  key={l.kind}
                  className="flex items-center gap-1.5 text-[11px] text-muted-foreground"
                >
                  <span
                    className="inline-block h-2.5 w-2.5 rounded-full"
                    style={{ background: nodeColorByKind[l.kind] }}
                  />
                  {l.label}
                </span>
              ))}
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground w-12 shrink-0">
                Edges
              </span>
              {EDGE_LEGEND.map((l) => {
                const active = edgeKindsActive.has(l.kind);
                return (
                  <button
                    key={l.kind}
                    type="button"
                    onClick={() => onToggleEdgeKind(l.kind)}
                    aria-pressed={active}
                    className={`flex items-center gap-1.5 rounded-full border border-border py-[3px] px-2.5 text-[11px] leading-none transition-colors ${
                      active
                        ? "bg-muted text-foreground"
                        : "bg-transparent text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    <span
                      className="inline-block h-2 w-2 rounded-full"
                      style={{
                        background: edgeColorByKind[l.kind],
                        opacity: active ? 1 : 0.4,
                      }}
                    />
                    {l.label}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Canvas */}
          <div ref={canvasWrapRef} className="relative flex-1 min-h-0">
            {isEmpty ? (
              <div
                className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-sm text-muted-foreground"
                aria-live="polite"
                aria-busy={isLoadingData}
              >
                {isLoadingData ? (
                  <>
                    <div className="h-3 w-3 rounded-full bg-foreground/30 animate-pulse" />
                    <div className="text-[11px] uppercase tracking-[0.18em]">
                      Loading the network…
                    </div>
                    <div className="text-[11px] text-muted-foreground/70 text-mono">
                      streaming {fetchingProspects > 0 ? "prospects" : ""}
                      {fetchingScores > 0 ? " · scores" : ""}
                      {fetchingSignals > 0 ? " · signals" : ""}
                    </div>
                  </>
                ) : (
                  "No prospects loaded yet — the graph will populate when data lands."
                )}
              </div>
            ) : (
              canvasSize.w > 0 &&
              canvasSize.h > 0 && (
                <ForceGraph2D<GraphNode, { kind: EdgeKind; id: string; color?: string; width?: number }>
                  ref={fgRef}
                  graphData={graphData}
                  width={canvasSize.w}
                  height={canvasSize.h}
                  nodeId="id"
                  nodeRelSize={4}
                  // Pure organic force-directed (Obsidian-style). DAG mode
                  // collapses every level into a horizontal stripe with this
                  // many nodes — wrong shape for what we want.
                  cooldownTime={1500}
                  cooldownTicks={90}
                  warmupTicks={20}
                  d3AlphaDecay={0.045}
                  d3VelocityDecay={0.32}
                  linkDirectionalParticles={0}
                  backgroundColor="transparent"
                  linkColor="color"
                  linkWidth="width"
                  linkVisibility={linkVisibility}
                  nodeVisibility={nodeVisibility}
                  onNodeClick={onNodeClickStable}
                  onBackgroundClick={onBackgroundClickStable}
                  onEngineStop={onEngineStop}
                  nodeCanvasObject={nodeCanvasObject}
                />
              )
            )}
            {/* U4 — warmup skeleton overlay while the simulation cools down. */}
            {!isEmpty && engineRunning && (
              <div
                className="pointer-events-none absolute inset-0 flex items-center justify-center bg-background/60 backdrop-blur-[1px]"
                aria-live="polite"
                aria-busy="true"
              >
                <div className="flex flex-col items-center gap-2">
                  <div className="h-3 w-3 rounded-full bg-foreground/30 animate-pulse" />
                  <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
                    Settling network…
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Right: inspector */}
        <NodeInspector
          node={selectedNode}
          onClose={() => setSelectedId(null)}
          prospect={selectedProspect}
          score={selectedScore}
          signals={selectedSignals}
          onNavigateToProspect={(id) => navigate(`/prospect/${id}`)}
        />
      </div>
    </div>
  );
};

const ZoomBtn = ({
  onClick,
  children,
}: {
  onClick: () => void;
  children: React.ReactNode;
}) => (
  <button
    type="button"
    onClick={onClick}
    className="h-7 min-w-7 px-2 border border-border hover:bg-muted transition-colors text-[11px]"
  >
    {children}
  </button>
);

export default Discover;
