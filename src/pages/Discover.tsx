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
  canonicalizeRole,
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

// ─── Label helpers — Figma-style 2-line labels ───────────────────────────────
// Each visible node gets a primary name (line 1) + a tiny uppercase tracked
// sub-label (line 2). The sub-label is kind-aware:
//   person     → abbreviated current title ("VP ENG", "STAFF ENG")
//   company    → "INDUSTRY · CITY" when both resolve, else either alone
//   role       → "ROLE"
//   city       → country name (uppercased) — orients the metro inside a region
//   school     → "SCHOOL"
//   conference → "CONFERENCE · YYYY" when year is known
//   industry   → "VERTICAL", or "ROOT" for the Technology anchor

const ROLE_ABBREV: ReadonlyArray<readonly [RegExp, string]> = [
  [/\bvice\s+president\b/gi, "VP"],
  [/\bsenior\b/gi, "Sr"],
  [/\bprincipal\b/gi, "Principal"],
  [/\bdirector\b/gi, "Dir"],
  [/\bmanager\b/gi, "Mgr"],
  [/\bsoftware\s+engineer\b/gi, "SWE"],
  [/\bsoftware\s+engineering\b/gi, "Eng"],
  [/\bengineering\b/gi, "Eng"],
  [/\bengineer\b/gi, "Eng"],
  [/\boperations\b/gi, "Ops"],
  [/\bbusiness\s+development\b/gi, "BD"],
];

function abbreviateRole(canonical: string): string {
  let s = canonicalizeRole(canonical);
  for (const [re, rep] of ROLE_ABBREV) s = s.replace(re, rep);
  s = s.replace(/\s+/g, " ").trim();
  if (s.length > 16) s = `${s.slice(0, 15).trimEnd()}…`;
  return s;
}

function subLabelFor(node: GraphNode, byId: Map<string, GraphNode>): string | null {
  if (node.id === "industry:technology") return "ROOT";
  switch (node.kind) {
    case "person": {
      if (!node.role) return null;
      return abbreviateRole(node.role).toUpperCase();
    }
    case "company": {
      const ind = node.industryId ? byId.get(node.industryId) : undefined;
      const city = node.locationId ? byId.get(node.locationId) : undefined;
      const indName = ind?.kind === "industry" ? ind.name : null;
      const cityName = city?.kind === "city" ? city.name : null;
      if (indName && cityName) return `${indName} · ${cityName}`.toUpperCase();
      if (indName) return indName.toUpperCase();
      if (cityName) return cityName.toUpperCase();
      return null;
    }
    case "role":
      return "ROLE";
    case "city":
      return (node.country ?? "CITY").toUpperCase();
    case "school":
      return "SCHOOL";
    case "conference":
      return node.year ? `CONFERENCE · ${node.year}` : "CONFERENCE";
    case "industry":
      return "VERTICAL";
  }
}

// ─── Score → node color for person nodes ────────────────────────────────────

function scoreToNodeColor(score: number | undefined): string {
  if (score === undefined || score <= 0) return "hsl(224 80% 68%)";
  if (score >= 75) return "hsl(142 71% 58%)";  // strong — green
  if (score >= 50) return "hsl(38 92% 58%)";   // plausible — amber
  return "hsl(0 72% 62%)";                      // weak — red
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
  // vertical (industry rollup) is the largest single edge family — without it
  // the "see structure" view degenerates to a sparse hub-and-spoke. On by
  // default; user toggles off if it's too noisy.
  "vertical",
];

// Cap how many prospects we let into the force-directed canvas at the
// "wide" (no focus) view. We're after an Obsidian-style web — readable at
// first glance, with focusable clusters, not a 500-node hairball. Once the
// user clicks a node, we drill into a focal subgraph that's bounded
// When drilled into a person focal node, we cap to the immediate
// 1-hop neighborhood (still bounded — direct neighbors only).
const FOCAL_PEOPLE_CAP = 20;
// When the focal node is an aggregation (company / industry / role / city /
// school / conference) the user wants to see *who's there* — every prospect.
// No cap: ForceGraph2D handles a few thousand nodes, and a silently-truncated
// org chart hides the answer the user came for.
const FOCAL_AGG_PEOPLE_CAP = Number.POSITIVE_INFINITY;
// Singleton root id (mirrors graph.ts). We pin this in every focal
// subgraph so the user has a click-to-home anchor without needing a back
// button.
const TECH_ROOT_ID = "industry:technology";

// Seniority ranking. Lower number = higher in the org. Used to order the
// focal-company subgraph from CEO down. Free-text role strings are messy, so
// we match against word-boundary keywords in priority order.
const SENIORITY_TIERS: ReadonlyArray<{ rank: number; pattern: RegExp }> = [
  { rank: 0,  pattern: /\b(?:ceo|chief executive|founder & ceo|co[- ]?founder & ceo)\b/i },
  { rank: 5,  pattern: /\b(?:president|coo|cfo|cto|cmo|cpo|ciso|cdo|chief\b[^,]+officer)\b/i },
  { rank: 10, pattern: /\b(?:founder|co[- ]?founder|board|chairman|chairwoman)\b/i },
  { rank: 15, pattern: /\b(?:evp|executive vice president|svp|senior vice president)\b/i },
  { rank: 20, pattern: /\bvice president\b|\bvp\b/i },
  { rank: 25, pattern: /\b(?:senior director|sr\.? director|head of)\b/i },
  { rank: 30, pattern: /\bdirector\b/i },
  { rank: 35, pattern: /\b(?:principal|distinguished|fellow|architect|staff)\b/i },
  { rank: 40, pattern: /\b(?:senior manager|sr\.? manager|group manager)\b/i },
  { rank: 45, pattern: /\bmanager\b/i },
  { rank: 50, pattern: /\b(?:senior|sr\.?|lead)\b/i },
];

function seniorityRank(role: string | undefined): number {
  if (!role) return 100;
  for (const { rank, pattern } of SENIORITY_TIERS) {
    if (pattern.test(role)) return rank;
  }
  return 60; // unranked individual contributor
}

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
  const [hoveredId, setHoveredId] = useState<string | null>(null);
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

  // Cap how many prospects feed the RENDERED force graph. ForceGraph2D + the
  // colleague-edge step (O(k²) within each company) become unworkable past a
  // few hundred — Intel alone has 446 prospects ≈ 99k colleague edges.
  // Top-N by overall_score, fallback to insertion order. The chat copilot
  // sees the FULL prospect set below (no cap; colleague edges skipped, so
  // the agent buildGraph stays O(n) over the entire DB).
  const RENDER_CAP = 250;

  const prospects = useMemo(() => {
    if (chatPromotedIds) {
      const matched = allProspects.filter((p) => chatPromotedIds.has(p._id));
      if (matched.length > 0) return matched.slice(0, RENDER_CAP);
    }
    // Focus-aware expansion: when the user clicks a company / industry / role,
    // pull in *that node's* prospects on top of the global top-N so the focal
    // subgraph isn't a 2-person sample of a 400-person org. Bounded so first-
    // render stays cheap; only triggers on click.
    const colonIdx = focusId?.indexOf(":") ?? -1;
    const focusKind = focusId && colonIdx > 0 ? focusId.slice(0, colonIdx) : null;
    const focusName = focusId && colonIdx > 0 ? focusId.slice(colonIdx + 1) : null;
    const focusMatches: typeof allProspects = (() => {
      if (!focusKind || !focusName || focusKind === "person") return [];
      const norm = (s: string) => s.trim().toLowerCase();
      switch (focusKind) {
        case "company":
          return allProspects.filter((p) => norm(p.company) === focusName);
        case "industry":
          return allProspects.filter((p) => norm(p.industry) === focusName);
        case "role":
          return allProspects.filter((p) => norm(p.role).includes(focusName));
        default:
          return [];
      }
    })();

    if (allProspects.length <= RENDER_CAP && focusMatches.length === 0) {
      return allProspects;
    }
    const ranked = [...allProspects].sort((a, b) => {
      const sa = scores[a._id]?.overall_score ?? -1;
      const sb = scores[b._id]?.overall_score ?? -1;
      return sb - sa;
    });
    const baseTopN = ranked.slice(0, RENDER_CAP);
    if (focusMatches.length === 0) return baseTopN;

    // Cap focal-context expansion. Without this, clicking Intel (446 people)
    // pushed `prospects` to ~700 and buildGraph's colleague pass — O(k²)
    // within Intel — generated ~99k edges, locking the canvas for seconds.
    // Score-rank focusMatches and take the top FOCAL_EXPAND_CAP.
    const FOCAL_EXPAND_CAP = 60;
    const rankedFocus = [...focusMatches].sort((a, b) => {
      const sa = scores[a._id]?.overall_score ?? -1;
      const sb = scores[b._id]?.overall_score ?? -1;
      return sb - sa;
    });
    const seen = new Set(baseTopN.map((p) => p._id));
    let added = 0;
    for (const p of rankedFocus) {
      if (added >= FOCAL_EXPAND_CAP) break;
      if (!seen.has(p._id)) {
        baseTopN.push(p);
        seen.add(p._id);
        added++;
      }
    }
    return baseTopN;
  }, [allProspects, chatPromotedIds, scores, focusId]);

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
      const focalIsAggregation = focal.kind !== "person";
      const focalIsCompany = focal.kind === "company";
      // For company focus, sort by org-chart seniority (CEO → C-suite → VP …),
      // tiebroken by overall_score; for any other focus, score-desc.
      persons.sort((a, b) => {
        const pa = nodeById.get(a) as Extract<GraphNode, { kind: "person" }> | undefined;
        const pb = nodeById.get(b) as Extract<GraphNode, { kind: "person" }> | undefined;
        if (focalIsCompany) {
          const ra = seniorityRank(pa?.role);
          const rb = seniorityRank(pb?.role);
          if (ra !== rb) return ra - rb;
        }
        return (pb?.score ?? -1) - (pa?.score ?? -1);
      });
      const cap = focalIsAggregation ? FOCAL_AGG_PEOPLE_CAP : FOCAL_PEOPLE_CAP;
      for (const id of persons.slice(0, cap)) visible.add(id);
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

  // Build the graphData ForceGraph2D consumes (nodes + links).
  //
  // SMOOTHNESS — node-position persistence across focal swaps:
  // When the user clicks a node, the focal subgraph rebuilds and ForceGraph
  // would normally reset every node's position to random. That makes the
  // graph "teleport" mid-click. Instead we snapshot positions from the
  // previous render (d3 mutates x/y on the live node objects) and re-seed
  // the new ones. Nodes that survive the swap keep their position; new
  // entrants are spawned in a small ring around the focal node so they
  // "expand outward" rather than scatter.
  const prevGraphDataRef = useRef<{ nodes: FGNode[]; links: FGLink[] } | null>(null);
  const graphData = useMemo(() => {
    const cache = new Map<string, { x: number; y: number }>();
    const prev = prevGraphDataRef.current;
    if (prev) {
      for (const n of prev.nodes) {
        if (typeof n.x === "number" && typeof n.y === "number") {
          cache.set(n.id as string, { x: n.x, y: n.y });
        }
      }
    }
    const focalPos = focusId ? (cache.get(focusId) ?? null) : null;

    const seeded = focalNodes.map((n) => {
      const c = cache.get(n.id);
      if (c) {
        // Survives the swap — keep the existing position so the user's eye
        // tracks it through the rearrange.
        return Object.assign({}, n, { x: c.x, y: c.y, vx: 0, vy: 0 }) as FGNode;
      }
      if (focalPos) {
        // New entrant — seed in a small ring around the focal node so the
        // expansion feels organic instead of teleporting in from nowhere.
        const angle = Math.random() * Math.PI * 2;
        const radius = 50 + Math.random() * 35;
        return Object.assign({}, n, {
          x: focalPos.x + Math.cos(angle) * radius,
          y: focalPos.y + Math.sin(angle) * radius,
          vx: 0,
          vy: 0,
        }) as FGNode;
      }
      return n as FGNode;
    });

    return {
      nodes: seeded,
      links: focalEdges.map<FGLink>((e) => ({
        source: e.source,
        target: e.target,
        kind: e.kind,
        id: e.id,
        color: e.color,
        width: 0.8,
      })),
    };
  }, [focalNodes, focalEdges, focusId]);

  // Keep the previous-graph ref in sync after each render, so the next
  // graphData computation can read fresh positions from the live sim.
  useEffect(() => {
    prevGraphDataRef.current = graphData;
  }, [graphData]);

  // Mutate link widths in place when selection changes. Avoids re-creating
  // graphData (which would reset the ForceGraph simulation).
  useEffect(() => {
    for (const link of graphData.links) {
      const sId = linkEndpointId(link.source);
      const tId = linkEndpointId(link.target);
      const focal =
        selectedId !== null && (sId === selectedId || tId === selectedId);
      link.width = focal ? 2.5 : 0.8;
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

  // Spread the simulation a bit so sub-labels don't collide. ForceGraph
  // recreates the d3 forces on every graphData identity change, so we apply
  // these every time the focal subgraph swaps. Larger link.distance + a
  // stronger negative charge gives each node a wider personal-space bubble.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    const linkF = fg.d3Force("link") as unknown as
      | { distance: (n: number) => unknown }
      | undefined;
    const chargeF = fg.d3Force("charge") as unknown as
      | { strength: (n: number) => unknown }
      | undefined;
    linkF?.distance(75);
    chargeF?.strength(-400);
    fg.d3ReheatSimulation();
  }, [graphData]);

  // ── Floating selected-node tooltip card ────────────────────────────────
  // Positioned absolutely over the canvas via a ref-only rAF loop so it
  // tracks the focal node every frame without triggering React re-renders.
  // The card itself stays mounted (opacity transitions handle visibility);
  // only its transform updates each tick.
  const tooltipDivRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!selectedId) return;
    let raf = 0;
    const tick = () => {
      const fg = fgRef.current;
      const div = tooltipDivRef.current;
      if (!fg || !div) {
        raf = requestAnimationFrame(tick);
        return;
      }
      const focal = graphData.nodes.find(
        (n) => n.id === selectedId,
      ) as (FGNode & { x?: number; y?: number }) | undefined;
      if (focal && typeof focal.x === "number" && typeof focal.y === "number") {
        const fgWithCoords = fg as unknown as {
          graph2ScreenCoords?: (x: number, y: number) => { x: number; y: number };
        };
        const screen = fgWithCoords.graph2ScreenCoords?.(focal.x, focal.y);
        if (screen) {
          // Centered horizontally on the node, sitting ~52px above it.
          div.style.transform = `translate3d(${screen.x}px, ${screen.y - 52}px, 0) translate(-50%, -100%)`;
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [selectedId, graphData]);


  // ─── Engine-running flag — drives the warmup skeleton overlay (U4) ─────────
  const [engineRunning, setEngineRunning] = useState(true);
  useEffect(() => {
    if (focalNodes.length) setEngineRunning(true);
  }, [focalNodes, focalEdges]);
  const onEngineStop = useCallback(() => {
    setEngineRunning(false);
    const fg = fgRef.current;
    if (!fg) return;
    // Longer camera animations than the previous 600ms — feels less abrupt.
    if (focusId) {
      const focal = focalNodes.find((n) => n.id === focusId) as
        | (GraphNode & { x?: number; y?: number })
        | undefined;
      if (focal && typeof focal.x === "number" && typeof focal.y === "number") {
        fg.centerAt(focal.x, focal.y, 1400);
        fg.zoom(2.0, 1400);
        return;
      }
    }
    fg.zoomToFit(1200, 80);
  }, [focusId, focalNodes]);

  // ─── Agent context ─────────────────────────────────────────────────────────
  // Built off the FULL prospect set (no cap) — the chat copilot keeps recall
  // over every candidate in the DB. We skip colleague edges (the only
  // O(n²) step in buildGraph) so this stays O(n) and finishes in <1s even
  // for 17k+ prospects. Chat tools do node lookups, not traversal, so
  // missing colleague edges has no behavioural cost.
  const fullGraph = useMemo(
    () =>
      buildGraph({
        prospects: allProspects,
        scores,
        signalsById,
        theme,
        skipColleagueEdges: true,
      }),
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

  // Tooltip body — kind-aware sub-line + optional score chip. Lives down
  // here so it can read selectedScore (declared just above).
  const tooltipMeta = useMemo<{
    name: string;
    sub: string;
    score?: number;
    accent: string;
  } | null>(() => {
    if (!selectedNode) return null;
    const accent =
      selectedNode.kind === "person"
        ? scoreToNodeColor((selectedNode as Extract<GraphNode, { kind: "person" }>).score)
        : nodeColorByKind[selectedNode.kind];
    const name = selectedNode.name;
    if (selectedNode.kind === "person") {
      const role = selectedNode.role ? canonicalizeRole(selectedNode.role) : "";
      const company = nodeById.get(selectedNode.companyId);
      const companyName = company?.kind === "company" ? company.name : "";
      const cityNode =
        company?.kind === "company" && company.locationId
          ? nodeById.get(company.locationId)
          : undefined;
      const cityName = cityNode?.kind === "city" ? cityNode.name : "";
      const sub = [role, companyName, cityName].filter(Boolean).join(" · ");
      return {
        name,
        sub: sub || "Person",
        score: selectedScore?.overall_score,
        accent,
      };
    }
    if (selectedNode.kind === "company") {
      const ind = selectedNode.industryId ? nodeById.get(selectedNode.industryId) : undefined;
      const city = selectedNode.locationId ? nodeById.get(selectedNode.locationId) : undefined;
      const indName = ind?.kind === "industry" ? ind.name : "";
      const cityName = city?.kind === "city" ? city.name : "";
      const sub = [indName, cityName].filter(Boolean).join(" · ") || "Company";
      return { name, sub, accent };
    }
    if (selectedNode.kind === "role") return { name, sub: "Target role", accent };
    if (selectedNode.kind === "city")
      return { name, sub: selectedNode.country ?? "City", accent };
    if (selectedNode.kind === "school") return { name, sub: "School", accent };
    if (selectedNode.kind === "conference")
      return {
        name,
        sub: selectedNode.year ? `Conference · ${selectedNode.year}` : "Conference",
        accent,
      };
    if (selectedNode.kind === "industry")
      return {
        name,
        sub: selectedNode.id === TECH_ROOT_ID ? "Root" : "Vertical",
        accent,
      };
    return null;
  }, [selectedNode, selectedScore, nodeColorByKind, nodeById]);

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
  // forward by clicking.
  //
  // Special case: clicking the Technology root resets to the wide view
  // (focusId = null) — that's the "go home" gesture. Drilling into
  // Technology's 1-hop neighborhood is just industries + cities, which is
  // a uselessly sparse hub-and-spoke. Wide view gives the rich web.
  const onNodeClickStable = useCallback((node: FGNode) => {
    const id = node.id as string;
    if (id === TECH_ROOT_ID) {
      setFocusId(null);
      setSelectedId(null);
      setHoveredId(null);
      setEngineRunning(true);
      return;
    }
    setFocusId(id);
    setSelectedId(id);
    setHoveredId(null);
    setEngineRunning(true);
  }, []);
  const onNodeHoverStable = useCallback((node: FGNode | null) => {
    setHoveredId(node ? (node.id as string) : null);
  }, []);
  const nodeLabelStable = useCallback(
    (node: FGNode) => (node as unknown as GraphNode).name,
    [],
  );
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
        ? 12
        : gn.kind === "industry" || gn.kind === "city"
          ? 8
          : gn.kind === "company" || gn.kind === "role"
            ? 7
            : gn.kind === "school" || gn.kind === "conference"
              ? 5.5
              : 5; // person
      const r = isSelected ? baseR + 2 : baseR;

      // Score-based color for persons; kind color for all other node types.
      const nodeColor =
        gn.kind === "person"
          ? scoreToNodeColor((gn as Extract<GraphNode, { kind: "person" }>).score)
          : nodeColorByKind[gn.kind];

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

      // Hover ring — drawn before the selected halo so a hovered-but-
      // not-selected node still gets feedback. Skip when the node is
      // selected (the halo below is more prominent).
      if (gn.id === hoveredId && !isSelected) {
        ctx2d.beginPath();
        ctx2d.arc(x, y, r + 4, 0, Math.PI * 2);
        ctx2d.strokeStyle = nodeColor;
        ctx2d.lineWidth = 1.5;
        ctx2d.globalAlpha = alpha * 0.5;
        ctx2d.stroke();
        ctx2d.globalAlpha = alpha;
      }

      // Selected halo — double ring for more presence
      if (isSelected) {
        ctx2d.beginPath();
        ctx2d.arc(x, y, r + 6, 0, Math.PI * 2);
        ctx2d.strokeStyle = nodeColor;
        ctx2d.lineWidth = 1;
        ctx2d.globalAlpha = alpha * 0.3;
        ctx2d.stroke();
        ctx2d.beginPath();
        ctx2d.arc(x, y, r + 3, 0, Math.PI * 2);
        ctx2d.strokeStyle = nodeColor;
        ctx2d.lineWidth = 2;
        ctx2d.globalAlpha = alpha * 0.7;
        ctx2d.stroke();
        ctx2d.globalAlpha = alpha;
      }

      // Glow — drawn as a blurred shadow behind the shape.
      ctx2d.shadowBlur = isSelected ? 24 : isNeighbor ? 14 : 10;
      ctx2d.shadowColor = nodeColor;
      paintShape(ctx2d, gn.kind, x, y, r, nodeColor);
      ctx2d.shadowBlur = 0;
      ctx2d.shadowColor = "transparent";

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
        const weight = isRoot ? 600 : isSelected ? 600 : isAnchor ? 500 : 400;
        const labelY = y + r + 3;

        // Common stroke setup — used to halo both the main label and the
        // sub-label so each line stays readable against busy edges.
        ctx2d.lineJoin = "round";
        ctx2d.miterLimit = 2;
        ctx2d.lineWidth = 5 / globalScale;
        ctx2d.strokeStyle = labelHalo;
        ctx2d.textAlign = "center";

        // ── Main label (line 1) ────────────────────────────────────────
        ctx2d.font = `${weight} ${labelPx / globalScale}px Inter, ui-sans-serif, system-ui, sans-serif`;
        // Modern Chrome / Safari respect this; older engines just ignore.
        (ctx2d as CanvasRenderingContext2D & { letterSpacing?: string }).letterSpacing =
          "-0.015em";
        ctx2d.textBaseline = "top";
        const mainFill = isRoot || isSelected || isAnchor ? labelStrong : labelMuted;
        ctx2d.strokeText(gn.name, x, labelY);
        ctx2d.fillStyle = mainFill;
        ctx2d.fillText(gn.name, x, labelY);

        // ── Sub-label (line 2) — kind-aware tracked uppercase ──────────
        // Skip on schools/conferences when zoomed out (low value, more
        // chance of overlap). Always show for the focal node + anchors.
        const showSub = isAnchor || isSelected || isNeighbor || zoomedIn;
        if (showSub) {
          const sub = subLabelFor(gn, nodeById);
          if (sub) {
            const subPx = Math.max(6.5, labelPx - 3);
            ctx2d.font = `500 ${subPx / globalScale}px Inter, ui-sans-serif, system-ui, sans-serif`;
            (ctx2d as CanvasRenderingContext2D & { letterSpacing?: string }).letterSpacing =
              "0.06em"; // tracked uppercase reads cleaner with positive spacing
            ctx2d.lineWidth = 4 / globalScale;
            const subY = labelY + (labelPx + 1) / globalScale;
            ctx2d.strokeText(sub, x, subY);
            ctx2d.fillStyle = labelMuted;
            ctx2d.fillText(sub, x, subY);
          }
        }
      }

      ctx2d.restore();
    },
    [
      selectedId,
      hoveredId,
      visibleNodeIds,
      neighborByNode,
      nodeColorByKind,
      nodeById,
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
          {/* Subheader: stats row — counts reflect what's actually painted
              (the focal subgraph when drilled in, the wide rendered set
              otherwise). Total prospect pool is also surfaced so the
              top-N truncation is honest. */}
          <div className="flex items-center justify-between gap-4 border-b border-border px-5 py-3">
            <div className="flex items-center gap-5 text-[11px] text-muted-foreground text-mono">
              <span>
                <span className="text-foreground">{focalNodes.length}</span> nodes
              </span>
              <span>
                <span className="text-foreground">{focalEdges.length}</span> edges
              </span>
              <span>
                <span className="text-foreground">
                  {focalNodes.filter((n) => n.kind === "person").length}
                </span>{" "}
                {"candidates"}
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
                  // Smoother cool-down: longer warmup with slower velocity
                  // decay so motion fades instead of slamming to a halt.
                  cooldownTime={2500}
                  cooldownTicks={120}
                  warmupTicks={50}
                  d3AlphaDecay={0.028}
                  d3VelocityDecay={0.35}
                  // Curved edges — subtle arc gives the canvas a spider-web
                  // feel instead of the rigid hub-and-spoke that straight
                  // lines produce. 0.22 gives a more organic web feel.
                  linkCurvature={0.22}
                  linkDirectionalParticles={2}
                  linkDirectionalParticleWidth={2}
                  linkDirectionalParticleSpeed={0.004}
                  backgroundColor="transparent"
                  linkColor="color"
                  linkWidth="width"
                  linkVisibility={linkVisibility}
                  nodeVisibility={nodeVisibility}
                  onNodeClick={onNodeClickStable}
                  onNodeHover={onNodeHoverStable}
                  onBackgroundClick={onBackgroundClickStable}
                  onEngineStop={onEngineStop}
                  nodeLabel={nodeLabelStable}
                  nodeCanvasObject={nodeCanvasObject}
                />
              )
            )}
            {/* U4 — warmup overlay. Crossfaded via opacity transition so it
                doesn't pop in/out on every focal swap. */}
            {!isEmpty && (
              <div
                className={`pointer-events-none absolute inset-0 flex items-center justify-center bg-background/60 backdrop-blur-[1px] transition-opacity duration-300 ${
                  engineRunning ? "opacity-100" : "opacity-0"
                }`}
                aria-live="polite"
                aria-busy={engineRunning}
              >
                <div className="flex flex-col items-center gap-2">
                  <div className="h-3 w-3 rounded-full bg-foreground/30 animate-pulse" />
                  <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
                    Settling network…
                  </div>
                </div>
              </div>
            )}

            {/* Floating selected-node tooltip card. Position is updated on
                every frame via the rAF loop above (writes directly to
                style.transform — no React re-renders). The card itself
                stays mounted; opacity flips on selection. */}
            <div
              ref={tooltipDivRef}
              className={`pointer-events-none absolute left-0 top-0 z-20 transition-opacity duration-200 ${
                tooltipMeta ? "opacity-100" : "opacity-0"
              }`}
              role="status"
              aria-live="polite"
            >
              {tooltipMeta && (
                <div className="relative flex items-center gap-3 rounded-lg bg-foreground/95 px-3 py-2 text-background shadow-xl backdrop-blur-sm">
                  <span
                    className="block h-2 w-2 shrink-0 rounded-full"
                    style={{ background: tooltipMeta.accent }}
                  />
                  <div className="flex flex-col gap-0.5 min-w-0">
                    <div className="text-[12px] font-semibold leading-tight whitespace-nowrap">
                      {tooltipMeta.name}
                    </div>
                    <div className="text-[10px] leading-tight opacity-75 whitespace-nowrap">
                      {tooltipMeta.sub}
                    </div>
                  </div>
                  {typeof tooltipMeta.score === "number" && (
                    <div className="ml-1 flex items-center gap-1">
                      <div className="h-3 w-px bg-background/30" />
                      <div className="text-[12px] font-semibold tabular-nums leading-tight">
                        {Math.round(tooltipMeta.score)}
                      </div>
                      <div className="text-[8px] uppercase tracking-[0.16em] opacity-60 leading-tight">
                        / 100
                      </div>
                    </div>
                  )}
                  {/* Pointer arrow at the bottom */}
                  <div
                    className="absolute left-1/2 top-full h-2 w-2 -translate-x-1/2 -translate-y-1 rotate-45 bg-foreground/95"
                    aria-hidden="true"
                  />
                </div>
              )}
            </div>
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
