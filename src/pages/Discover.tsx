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

// Cap how many prospects we let into the force-directed canvas. ForceGraph2D
// stops being legible (and starts hitching) past ~500 nodes; with 10k
// prospects the colleague edges alone would balloon past 100k. The chat
// copilot still operates on the full set via AgentContext.nodes/edges
// (rebuilt from the un-sliced array) so "find me X" queries don't lose
// recall. v1 strategy: top-N by overall_score, falling back to insertion
// order if score is missing.
const MAX_PROSPECTS_RENDERED = 500;

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
  const navigate = useNavigate();
  const allProspects = useProspects();
  const allProspectIds = useMemo(() => allProspects.map((p) => p._id), [allProspects]);
  const scores = useScoresFor(allProspectIds);
  // Render only the top-N by overall_score. Stable sort: ties resolved by
  // insertion order. The agent context below still receives the full
  // nodes/edges so `filter` / `focus_node` retain full recall.
  const prospects = useMemo(() => {
    if (allProspects.length <= MAX_PROSPECTS_RENDERED) return allProspects;
    const ranked = [...allProspects].sort((a, b) => {
      const sa = scores[a._id]?.overall_score ?? -1;
      const sb = scores[b._id]?.overall_score ?? -1;
      return sb - sa;
    });
    return ranked.slice(0, MAX_PROSPECTS_RENDERED);
  }, [allProspects, scores]);
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

  // Build graph (memoized). Pre-bakes color on every node + edge.
  const { nodes, edges } = useMemo(
    () => buildGraph({ prospects, scores, signalsById, theme }),
    [prospects, scores, signalsById, theme],
  );

  // ─── URL-synced local state (?edges=…&selected=…) ──────────────────────────
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
  const [selectedId, setSelectedId] = useState<string | null>(
    () => searchParams.get("selected"),
  );
  const [visibleNodeIds, setVisibleNodeIds] = useState<Set<string> | null>(null);
  const [edgeKindsActive, setEdgeKindsActive] = useState<Set<EdgeKind>>(initialEdgeKinds);

  // Push state -> URL (replace, not push, so the back button stays clean).
  useEffect(() => {
    const next = new URLSearchParams(searchParams);
    if (selectedId) next.set("selected", selectedId);
    else next.delete("selected");
    next.set("edges", encodeEdgeKinds(edgeKindsActive));
    // Avoid identity churn — only update if something actually changed.
    if (next.toString() !== searchParams.toString()) {
      setSearchParams(next, { replace: true });
    }
  }, [selectedId, edgeKindsActive, searchParams, setSearchParams]);

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

  // Build the graphData ForceGraph2D consumes (nodes + links). `color` is
  // pre-baked by buildGraph(); `width` is set here from the base width and
  // mutated in place on selection (see effect below) so the simulation
  // doesn't reset every time you click a node.
  const graphData = useMemo(
    () => ({
      nodes: nodes as FGNode[],
      links: edges.map<FGLink>((e) => ({
        source: e.source,
        target: e.target,
        kind: e.kind,
        id: e.id,
        color: e.color,
        width: 0.6,
      })),
    }),
    [nodes, edges],
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
    if (nodes.length) setEngineRunning(true);
  }, [nodes, edges]);
  const onEngineStop = useCallback(() => {
    setEngineRunning(false);
    // Fit on settle rather than via timer — guarantees we frame the final
    // layout, not a half-cooled one.
    fgRef.current?.zoomToFit(400, 60);
  }, []);

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

  const onNodeClickStable = useCallback(
    (node: FGNode) => setSelectedId(node.id as string),
    [],
  );
  const onBackgroundClickStable = useCallback(() => setSelectedId(null), []);

  const nodeCanvasObject = useCallback(
    (node: FGNode, ctx2d: CanvasRenderingContext2D, globalScale: number) => {
      const gn = node as unknown as GraphNode;
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      // Visual rank — bigger nodes for higher levels in the hierarchy so
      // the eye lands on Technology → Industry/City → Company/Role → Person
      // top-down. Selected gets a small bump.
      const isRoot = gn.id === "industry:technology";
      const isSelected = gn.id === selectedId;
      const baseR = isRoot
        ? 11
        : gn.kind === "industry" || gn.kind === "city"
          ? 7.5
          : gn.kind === "company" || gn.kind === "role"
            ? 6
            : gn.kind === "school" || gn.kind === "conference"
              ? 5
              : 4.5; // person
      const r = isSelected ? baseR + 1.5 : baseR;

      // Neighborhood-fade: when nothing's filtered AND a node is
      // selected, dim non-neighbors so the focal cluster pops.
      let alpha = 1;
      if (selectedId && visibleNodeIds === null) {
        const isSelf = gn.id === selectedId;
        const isNeighbor = neighborByNode.get(selectedId)?.has(gn.id) ?? false;
        if (!isSelf && !isNeighbor) alpha = 0.2;
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

      // Always-visible labels, with size keyed off the hierarchy. The font
      // size is in px — divide by globalScale so it stays readable across
      // zoom levels. We DON'T gate by globalScale anymore; user wants all
      // titles visible.
      const labelPx = isRoot ? 14 : gn.kind === "industry" || gn.kind === "city" ? 11 : gn.kind === "company" || gn.kind === "role" ? 10 : 9;
      ctx2d.font = `${isRoot || isSelected ? "600 " : ""}${labelPx / globalScale}px ui-sans-serif, system-ui, sans-serif`;
      ctx2d.fillStyle = isRoot
        ? "hsl(0 0% 15%)"
        : isSelected
          ? "hsl(0 0% 20%)"
          : gn.kind === "person"
            ? "hsl(0 0% 35%)"
            : "hsl(0 0% 28%)";
      ctx2d.textAlign = "center";
      ctx2d.textBaseline = "top";
      ctx2d.fillText(gn.name, x, y + r + 2);

      ctx2d.restore();
    },
    [selectedId, visibleNodeIds, neighborByNode, nodeColorByKind],
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
                  // DAG layout: bottom-up so source nodes (children) sit BELOW
                  // target nodes (parents). The edge direction Person → Role →
                  // Industry → Technology reads as a top-down hierarchy on
                  // screen.
                  dagMode="td"
                  dagLevelDistance={70}
                  // colleague + partnership are symmetric and may form cycles;
                  // ForceGraph throws by default. Swallow it — non-DAG nodes
                  // just fall back to standard force layout.
                  onDagError={() => undefined}
                  // Perf: shorter sim + faster cooldown. Audit (FrostyOtter)
                  // measured 3.6s warmup with the defaults; these knobs cut it
                  // to ~700ms while still settling visually.
                  cooldownTime={1500}
                  cooldownTicks={90}
                  warmupTicks={20}
                  d3AlphaDecay={0.04}
                  d3VelocityDecay={0.3}
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
