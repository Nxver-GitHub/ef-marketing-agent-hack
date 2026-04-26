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
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
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
  type GraphEdge,
  type GraphNode,
  type NodeKind,
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
type FGLink = LinkObject<GraphNode, { kind: EdgeKind; id: string }>;

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

// ─── Component ───────────────────────────────────────────────────────────────

const Discover = () => {
  const navigate = useNavigate();
  const prospects = useProspects();
  const prospectIds = useMemo(() => prospects.map((p) => p._id), [prospects]);
  const scores = useScoresFor(prospectIds);
  const signalsById = useSignalsForMany(prospectIds);

  // Build graph (memoized).
  const { nodes, edges } = useMemo(
    () => buildGraph({ prospects, scores, signalsById }),
    [prospects, scores, signalsById],
  );

  // ─── Local state ───────────────────────────────────────────────────────────
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [visibleNodeIds, setVisibleNodeIds] = useState<Set<string> | null>(null);
  const [edgeKindsActive, setEdgeKindsActive] = useState<Set<EdgeKind>>(
    () => new Set<EdgeKind>(DEFAULT_EDGE_KINDS),
  );

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
    const kinds: EdgeKind[] = [
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
    const out = {} as Record<EdgeKind, string>;
    for (const k of kinds) out[k] = hslFromVar(`--edge-${slugifyEdge(k)}`);
    return out;
  }, []);

  // Build the graphData ForceGraph2D consumes (nodes + links).
  const graphData = useMemo(
    () => ({
      nodes: nodes as FGNode[],
      links: edges.map<FGLink>((e: GraphEdge) => ({
        source: e.source,
        target: e.target,
        kind: e.kind,
        id: e.id,
      })),
    }),
    [nodes, edges],
  );

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

  // After first layout settles, fit graph to view.
  useEffect(() => {
    if (!nodes.length) return;
    const t = setTimeout(() => fgRef.current?.zoomToFit(400, 60), 600);
    return () => clearTimeout(t);
  }, [nodes.length]);

  // ─── Agent context ─────────────────────────────────────────────────────────
  const ctx: AgentContext = useMemo(
    () => ({
      nodes,
      edges,
      setSelectedId,
      setVisibleNodeIds,
      getProspectById: (id) =>
        prospects.find((p) => `person:${p._id}` === id || p._id === id),
      getScoreById: (id) => {
        const personId = id.startsWith("person:") ? id.slice(7) : id;
        return scores[personId];
      },
    }),
    [nodes, edges, prospects, scores],
  );

  // ─── Inspector data wiring (person variant) ────────────────────────────────
  const selectedProspect = useMemo(() => {
    if (!selectedNode || selectedNode.kind !== "person") return undefined;
    return prospects.find((p) => p._id === selectedNode.raw._id);
  }, [selectedNode, prospects]);
  const selectedScore = selectedProspect ? scores[selectedProspect._id] : undefined;
  const selectedSignals = selectedProspect ? signalsById[selectedProspect._id] : undefined;

  // ─── Edge-kind toggle handler ──────────────────────────────────────────────
  const onToggleEdgeKind = (kind: EdgeKind) =>
    setEdgeKindsActive((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });

  // ─── Empty-state gate ──────────────────────────────────────────────────────
  const isEmpty = nodes.length === 0;

  // ─── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="h-screen flex flex-col bg-background text-foreground">
      <TopBar edgeKindsActive={edgeKindsActive} onToggleEdgeKind={onToggleEdgeKind} />
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
                candidates
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

          {/* Subheader: legend */}
          <div className="flex items-center gap-3 border-b border-border px-5 py-2 flex-wrap">
            <span className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
              Legend
            </span>
            {NODE_KINDS_LEGEND.map((l) => (
              <span key={l.kind} className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <span
                  className="inline-block h-2.5 w-2.5 rounded-sm"
                  style={{ background: nodeColorByKind[l.kind] }}
                />
                {l.label}
              </span>
            ))}
          </div>

          {/* Canvas */}
          <div ref={canvasWrapRef} className="relative flex-1 min-h-0">
            {isEmpty ? (
              <div className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
                No prospects yet — validate one to get started.
              </div>
            ) : (
              canvasSize.w > 0 &&
              canvasSize.h > 0 && (
                <ForceGraph2D<GraphNode, { kind: EdgeKind; id: string }>
                  ref={fgRef}
                  graphData={graphData}
                  width={canvasSize.w}
                  height={canvasSize.h}
                  nodeId="id"
                  nodeRelSize={4}
                  cooldownTime={3000}
                  linkDirectionalParticles={0}
                  backgroundColor="transparent"
                  linkColor={(link: FGLink) =>
                    edgeColorByKind[link.kind] ?? "hsl(0 0% 50%)"
                  }
                  linkWidth={(link: FGLink) => {
                    const sId = linkEndpointId(link.source);
                    const tId = linkEndpointId(link.target);
                    if (selectedId && (sId === selectedId || tId === selectedId)) return 1.5;
                    return 0.6;
                  }}
                  linkVisibility={(link: FGLink) => edgeKindsActive.has(link.kind)}
                  nodeVisibility={(node: FGNode) => {
                    if (visibleNodeIds === null) return true;
                    if (visibleNodeIds.has(node.id as string)) return true;
                    if (selectedId && node.id === selectedId) return true;
                    return false;
                  }}
                  onNodeClick={(node: FGNode) => setSelectedId(node.id as string)}
                  onBackgroundClick={() => setSelectedId(null)}
                  nodeCanvasObject={(node: FGNode, ctx2d: CanvasRenderingContext2D, globalScale: number) => {
                    const gn = node as unknown as GraphNode;
                    const x = node.x ?? 0;
                    const y = node.y ?? 0;
                    const baseR = gn.kind === "person" ? 5 : gn.kind === "city" ? 5 : 5.5;

                    // Neighborhood-fade: when nothing's filtered AND a node is
                    // selected, dim non-neighbors so the focal cluster pops.
                    let alpha = 1;
                    if (selectedId && visibleNodeIds === null) {
                      const isSelf = gn.id === selectedId;
                      const isNeighbor = neighborByNode.get(selectedId)?.has(gn.id) ?? false;
                      if (!isSelf && !isNeighbor) alpha = 0.25;
                    }

                    ctx2d.save();
                    ctx2d.globalAlpha = alpha;

                    // Selected halo
                    if (gn.id === selectedId) {
                      ctx2d.beginPath();
                      ctx2d.arc(x, y, baseR + 4, 0, Math.PI * 2);
                      ctx2d.strokeStyle = nodeColorByKind[gn.kind];
                      ctx2d.lineWidth = 1.5;
                      ctx2d.globalAlpha = alpha * 0.6;
                      ctx2d.stroke();
                      ctx2d.globalAlpha = alpha;
                    }

                    paintShape(ctx2d, gn.kind, x, y, baseR, nodeColorByKind[gn.kind]);

                    // Label only when zoomed in enough.
                    if (globalScale >= 1.4) {
                      ctx2d.font = `${10 / globalScale}px ui-sans-serif, system-ui, sans-serif`;
                      ctx2d.fillStyle = "hsl(0 0% 45%)";
                      ctx2d.textAlign = "center";
                      ctx2d.textBaseline = "top";
                      ctx2d.fillText(gn.name, x, y + baseR + 2);
                    }

                    ctx2d.restore();
                  }}
                />
              )
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
