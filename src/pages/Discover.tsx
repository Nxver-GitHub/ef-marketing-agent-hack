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
import {
  useProspects,
  useScoresFor,
  useSignalsFor,
  useSignalsForMany,
  usePersonConnections,
  useWeights,
} from "@/lib/db";
import { useDocumentTitle } from "@/lib/useDocumentTitle";
import {
  ALL_EDGE_KINDS,
  EDGE_CONFIGS,
  buildGraph,
  canonicalizeRole,
  type EdgeKind,
  type GraphNode,
  type NodeKind,
  type ThemeTokens,
} from "@/lib/graph";
import {
  prospectIdsForAggregation,
  computeHubStats,
  type AggregationProspect,
} from "@/lib/aggregations";
import type { AgentContext } from "@/lib/agent";
import { useGraphStore, isDemoMode } from "@/store/graphStore";

// ─── CSS-var color helpers ───────────────────────────────────────────────────
//
// Edge → CSS-variable resolution moved into `EDGE_CONFIGS` (`graph.ts`,
// per Contract 3). Read `EDGE_CONFIGS[kind].cssVarName` instead of building
// the var name in this file.

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

// `ALL_EDGE_KINDS` now lives in `graph.ts` (derived from `EDGE_CONFIGS`).
// See Contract 3 for the registry pattern. Imported above.

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
  // Education-cohort kinds (V3_PT2.md §"New Edge Kinds"). Live in
  // EDGE_CONFIGS since Phase 1.1; on by default so the
  // bulk_education_signals output (283+ edges live as of 2026-04-30)
  // surfaces without users needing to toggle them on.
  "same_mba_cohort",
  "same_phd_program",
  "same_undergrad_cohort",
  "executive_education",
];

// Cap how many prospects we let into the force-directed canvas at the
// "wide" (no focus) view. We're after an Obsidian-style web — readable at
// first glance, with focusable clusters, not a 500-node hairball. Once the
// user clicks a node, we drill into a focal subgraph that's bounded
// When drilled into a person focal node, we cap colleague-style neighbors
// in the rendered subgraph. Set generously so a top-scored prospect's
// inferred org cluster (focused person + their colleagues at the same
// company) reads as a believable team, not 20 dots — the all-pairs
// colleague mesh that this would normally drag in is dropped in
// `focalEdges`, so the higher cap doesn't bring back the hairball.
const FOCAL_PEOPLE_CAP = 40;
// Score threshold above which a person is treated as a "headline prospect"
// — for these we always include the company, role, city, and at least
// HEADLINE_MIN_COLLEAGUES colleagues, regardless of how many edges exist
// in the prebuilt graph. Below the threshold we still show what's there
// but don't force-pad.
const HEADLINE_PROSPECT_SCORE = 75;
const HEADLINE_MIN_COLLEAGUES = 12;
// When the focal node is an aggregation (company / industry / role / city /
// school / conference) the user wants to see *who's there* — every prospect.
// No cap: ForceGraph2D handles a few thousand nodes, and a silently-truncated
// org chart hides the answer the user came for.
const FOCAL_AGG_PEOPLE_CAP = Number.POSITIVE_INFINITY;
// Singleton root id (mirrors graph.ts). We pin this in every focal
// subgraph so the user has a click-to-home anchor without needing a back
// button.
const TECH_ROOT_ID = "industry:technology";

// ─── View modes ──────────────────────────────────────────────────────────────
// Curated presets that swap the active edge + node kinds in one click so the
// user can isolate the relationship they care about without hunting through
// the per-pill edge filter. "All" preserves the current default.
type ViewMode = "all" | "org" | "roles" | "geo" | "industries";

const VIEW_MODES: ReadonlyArray<{
  id: ViewMode;
  label: string;
  edges: ReadonlySet<EdgeKind>;
  nodes: ReadonlySet<NodeKind>;
}> = [
  {
    id: "all",
    label: "All",
    edges: new Set(ALL_EDGE_KINDS),
    nodes: new Set(["person", "company", "role", "city", "school", "conference", "industry"] as NodeKind[]),
  },
  {
    id: "org",
    label: "Org",
    edges: new Set<EdgeKind>(["works_at", "colleague", "reports_to"]),
    nodes: new Set<NodeKind>(["person", "company"]),
  },
  {
    id: "roles",
    label: "Roles",
    edges: new Set<EdgeKind>(["works_at", "scope_signal"]),
    nodes: new Set<NodeKind>(["person", "company", "role"]),
  },
  {
    id: "geo",
    label: "Geography",
    edges: new Set<EdgeKind>(["located_in", "vertical"]),
    nodes: new Set<NodeKind>(["company", "city", "industry"]),
  },
  {
    id: "industries",
    label: "Industries",
    edges: new Set<EdgeKind>(["vertical", "partnership"]),
    nodes: new Set<NodeKind>(["company", "industry"]),
  },
];

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

// Edge-pill order — drives the toggle row beneath the legend (rendered at
// EDGE_LEGEND.map below). Order: structural kinds first (matching v2 layout)
// then the four hidden-connection kinds at the end, baseline-strength desc
// within the warm group (patent 0.95 → co-author 0.85 → standards 0.82 →
// conference 0.80) so the strongest warm-path edges read left-to-right.
//
// Derived from `EDGE_CONFIGS` (single source of truth, Contract 3) — labels
// and ordering preserved exactly to avoid visual drift from the previous
// hand-maintained list.
const EDGE_LEGEND_ORDER: ReadonlyArray<EdgeKind> = [
  "reports_to",
  "works_at",
  "located_in",
  "evidence_cited",
  "scope_signal",
  "partnership",
  "past_employer",
  "education",
  "vertical",
  "patent_co_inventor",
  "academic_co_author",
  "standards_committee",
  "conference_co_presenter",
  // Education-cohort kinds — appended after the v3 hidden-connection group.
  // These are stronger than the generic `education` kind (mba 0.85 / phd 0.78
  // vs alumni 0.25) but distinct from `academic_co_author` — they represent
  // structural cohort overlap, not paper co-authorship.
  "same_mba_cohort",
  "same_phd_program",
  "executive_education",
  "same_undergrad_cohort",
];
const EDGE_LEGEND: ReadonlyArray<{ kind: EdgeKind; label: string }> =
  EDGE_LEGEND_ORDER.map((kind) => ({
    kind,
    label: EDGE_CONFIGS[kind].displayLabel,
  }));

// Compact mid-edge labels — drawn by linkCanvasObject in zoomed-in views so
// the user can tell apart works_at / past_employer / education / partnership
// without consulting the legend. `colleague` is intentionally suppressed
// (`EDGE_CONFIGS.colleague.suppressCanvasLabel === true`) — it would carpet
// the canvas with redundant "Colleague" tags between every pair of co-workers.
const EDGE_LABEL_SHORT: Partial<Record<EdgeKind, string>> = Object.freeze(
  Object.fromEntries(
    ALL_EDGE_KINDS.filter((k) => !EDGE_CONFIGS[k].suppressCanvasLabel).map(
      (k) => [k, EDGE_CONFIGS[k].displayLabelShort],
    ),
  ),
);

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
  const weights = useWeights();

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
  // Hover lives in a ref, not state — the canvas redraws every frame from
  // the force sim, so reading the latest hover from a ref keeps the ring
  // responsive without triggering a Discover re-render on every mousemove
  // (which was making the page lag during hover with 100+ nodes).
  const hoveredIdRef = useRef<string | null>(null);
  const [visibleNodeIds, setVisibleNodeIds] = useState<Set<string> | null>(null);
  const [edgeKindsActive, setEdgeKindsActive] = useState<Set<EdgeKind>>(initialEdgeKinds);
  const [viewMode, setViewMode] = useState<ViewMode>("all");
  const activeViewNodes = useMemo<ReadonlySet<NodeKind>>(
    () => VIEW_MODES.find((v) => v.id === viewMode)?.nodes ?? VIEW_MODES[0].nodes,
    [viewMode],
  );
  const onPickViewMode = useCallback((mode: ViewMode) => {
    setViewMode(mode);
    const preset = VIEW_MODES.find((v) => v.id === mode);
    if (preset) setEdgeKindsActive(new Set(preset.edges));
  }, []);

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

  // Render caps removed 2026-04-30 by user direction — every prospect feeds
  // the force graph regardless of count. ForceGraph2D performance past a few
  // thousand nodes is the new known constraint; revisit if/when frame rates
  // become unworkable on the target hardware. The chat copilot already sees
  // the full prospect set; this aligns the rendered graph with that set.

  // Hub matching for focal expansion — shared with NodeInspector counts via
  // `lib/aggregations.ts`. Person nodes return null here; colleague drill-down
  // for a focused person is handled separately below.
  const focalAggregationIds = useMemo<Set<string> | null>(
    () => prospectIdsForAggregation(focusId, allProspects as AggregationProspect[]),
    [focusId, allProspects],
  );

  const prospects = useMemo(() => {
    const byScoreDesc = (a: { _id: string }, b: { _id: string }) => {
      const sa = scores[a._id]?.overall_score ?? -1;
      const sb = scores[b._id]?.overall_score ?? -1;
      return sb - sa;
    };

    if (chatPromotedIds) {
      const matched = allProspects.filter((p) => chatPromotedIds.has(p._id));
      if (matched.length > 0) return matched;
    }

    const colonIdx = focusId?.indexOf(":") ?? -1;
    const focusKind = focusId && colonIdx > 0 ? focusId.slice(0, colonIdx) : null;
    const focusName = focusId && colonIdx > 0 ? focusId.slice(colonIdx + 1) : null;

    // Person focus: ensure the clicked prospect is present, then everyone
    // else sorted by score. Same-company colleagues are no longer specially
    // promoted because every prospect renders unconditionally now.
    if (focusKind === "person" && focusName) {
      const me = allProspects.find((p) => p._id === focusName);
      if (me) {
        const ranked = [...allProspects].sort(byScoreDesc);
        if (ranked.some((p) => p._id === me._id)) return ranked;
        return [me, ...ranked];
      }
    }

    if (focalAggregationIds) {
      const matched = allProspects
        .filter((p) => focalAggregationIds.has(p._id))
        .sort(byScoreDesc);
      const remaining = allProspects
        .filter((p) => !focalAggregationIds.has(p._id))
        .sort(byScoreDesc);
      return [...matched, ...remaining];
    }

    return [...allProspects].sort(byScoreDesc);
  }, [allProspects, scores, chatPromotedIds, focalAggregationIds, focusId]);

  const prospectIds = useMemo(() => prospects.map((p) => p._id), [prospects]);
  const signalsById = useSignalsForMany(prospectIds);
  // Pre-materialized person↔person edges from `person_connections` (Decision 7).
  // Empty until the persons↔prospects ID linkage migration lands; harmless
  // no-op against current DB.
  const personConnections = usePersonConnections(prospectIds);

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
    for (const k of ALL_EDGE_KINDS) out[k] = hslFromVar(EDGE_CONFIGS[k].cssVarName);
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
  // Skip the O(n²) colleague-edge mesh on the rendered build. With caps removed
  // (2026-04-30 user direction) the prospect set is up to ~20k; a single
  // company with 446 prospects emits ~99k colleague edges by itself, which
  // tanks ForceGraph2D's tick into multi-second frames. Chat copilot already
  // does the skipColleagueEdges build at line 969 — same flag, same reason.
  // Hidden-connection edges (patent/paper/conference/standards) + works_at +
  // located_in + partnership + vertical all still render.
  const { nodes, edges } = useMemo(
    () => buildGraph({ prospects, scores, signalsById, personConnections, theme, skipColleagueEdges: true }),
    [prospects, scores, signalsById, personConnections, theme],
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

  // ─── graphStore mirror (Track L2) ────────────────────────────────────────
  // Local state (focusId / selectedId / edgeKindsActive / built nodes+edges)
  // remains canonical for now; we mirror it into graphStore so that
  // downstream consumers — warmPaths.ts (Track I), NodeInspector v2,
  // demo-mode plumbing (Wave 3) — can read from a single source of truth
  // without coupling back into this page's local React state. Full
  // replacement of the local hooks with store reads is staged for L3.
  //
  // Demo-mode skip: when `?demo=true`, App.tsx has already called
  // `initDemoMode([...DEMO_GRAPH_NODES], [...DEMO_EDGES])` which seeds the
  // store with the canonical pre-built demo graph. Mirroring Discover's
  // local buildGraph output on top would clobber that seed (Bug 2 in
  // SunnyRidge's [REPORT demo-smoke] — buildGraph fires before useProspects
  // resolves, writing `{[], []}` and overwriting the demo data). Skipping
  // the mirror in demo mode preserves the seed for warmPaths / banner
  // consumers that read from the store. The selection + edge-kind mirrors
  // still run because those are user-driven and harmless in demo mode.
  const storeSetGraph = useGraphStore((s) => s.setGraph);
  const storeSelectNode = useGraphStore((s) => s.selectNode);
  const storeSetVisibleEdgeKinds = useGraphStore((s) => s.setVisibleEdgeKinds);
  const _DEMO_MIRROR_SKIP = isDemoMode();

  useEffect(() => {
    storeSelectNode(selectedId);
  }, [selectedId, storeSelectNode]);

  useEffect(() => {
    storeSetVisibleEdgeKinds(edgeKindsActive);
  }, [edgeKindsActive, storeSetVisibleEdgeKinds]);

  useEffect(() => {
    if (_DEMO_MIRROR_SKIP) return;
    storeSetGraph({ nodes, edges });
  }, [nodes, edges, storeSetGraph, _DEMO_MIRROR_SKIP]);

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
  // selectedNode resolution. Defined later (after fullGraph) so it can fall
  // back to the agent's full node set when the click came from a chat-
  // surfaced node that isn't in the rendered top-250. See `selectedNode`
  // declaration below.

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
    // Hierarchical default (2026-04-30): when nothing is focused, render the
    // top of the tree — verticals (industry) + companies + city + country +
    // tech root. Person/role/school stay hidden until the user drills in.
    // Keeps the cold-load force-graph small instead of 20k. Defensively
    // falls back to all nodes if the filter would yield <10 (catches
    // sparse-COMPANY_META scenarios where industries didn't get emitted).
    const topLevelKinds: ReadonlySet<string> = new Set([
      "industry",
      "company",
      "city",
      "country",
    ]);
    const buildTopLevel = (): GraphNode[] => {
      const filtered = nodes.filter(
        (n) => topLevelKinds.has(n.kind) || n.id === TECH_ROOT_ID,
      );
      // Fallback: if the hierarchy didn't materialize enough hubs, return
      // the full set — better an over-dense graph than a near-empty one.
      return filtered.length >= 10 ? filtered : nodes;
    };
    if (!focusId) return buildTopLevel();
    const focal = nodeById.get(focusId);
    if (!focal) return buildTopLevel();
    // Industry-focus drill-down: show the focused industry + every company
    // that rolls up to it + the tech root. Companies open the next level
    // when clicked (existing company-focus path, line ~715).
    if (focal.kind === "industry") {
      const visible = new Set<string>([focal.id]);
      if (nodeById.has(TECH_ROOT_ID)) visible.add(TECH_ROOT_ID);
      const neighbors = neighborByNode.get(focal.id);
      if (neighbors) {
        for (const id of neighbors) {
          const n = nodeById.get(id);
          if (n && n.kind === "company") visible.add(id);
        }
      }
      return nodes.filter((n) => visible.has(n.id));
    }
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

      // Top-scored person backstop: top-scored prospects are the demo's
      // headline. If for any reason their direct edges aren't enough to
      // reach HEADLINE_MIN_COLLEAGUES (small company, sparse colleague
      // edges in the rendered slice, etc.) walk allProspects directly to
      // pull in the missing same-company peers ranked by score.
      if (!focalIsAggregation && focal.kind === "person") {
        const focalScore = (focal as { score?: number }).score ?? 0;
        if (focalScore >= HEADLINE_PROSPECT_SCORE) {
          const me = allProspects.find((p) => `person:${p._id}` === focusId);
          if (me) {
            const norm = (s: string) => s.trim().toLowerCase();
            const sameCompany = allProspects
              .filter((p) => p._id !== me._id && norm(p.company) === norm(me.company))
              .map((p) => ({ id: `person:${p._id}`, score: scores[p._id]?.overall_score ?? -1 }))
              .sort((a, b) => b.score - a.score);
            let added = persons.slice(0, cap).length;
            for (const c of sameCompany) {
              if (added >= HEADLINE_MIN_COLLEAGUES) break;
              if (visible.has(c.id)) continue;
              if (!nodeById.has(c.id)) continue;
              visible.add(c.id);
              added++;
            }
          }
        }
      }
    }
    const out: GraphNode[] = [];
    for (const n of nodes) {
      if (!visible.has(n.id)) continue;
      // View-mode filter: keep the focused node itself even if its kind is
      // hidden (otherwise clicking a Role node in Geography mode strands you);
      // otherwise only keep node kinds the active view allows.
      if (n.id !== focusId && !activeViewNodes.has(n.kind)) continue;
      out.push(n);
    }
    return out;
  }, [focusId, nodes, nodeById, neighborByNode, activeViewNodes, allProspects, scores]);

  const focalEdges = useMemo(() => {
    if (!focusId) {
      // Match the hierarchical default node filter — keep only edges
      // between visible top-level nodes (industry/company/tech root).
      const visible = new Set(focalNodes.map((n) => n.id));
      return edges.filter((e) => visible.has(e.source) && visible.has(e.target));
    }
    const visible = new Set(focalNodes.map((n) => n.id));
    const focal = nodeById.get(focusId);
    // For person focus we want the org-chart shape, not just an ego graph.
    // Keep:
    //   - every edge that touches the focused person (her own connections)
    //   - works_at edges from any visible colleague to any visible company
    //     (so the cluster reads as "everyone at Intel" rather than 20 dots)
    //   - non-colleague structural edges between visible nodes (e.g. company
    //     → industry, role → person) so the surrounding context remains
    // Drop ONLY the all-pairs colleague mesh between the focused person's
    // colleagues — that was the hairball, and it carries no extra signal
    // beyond what the works_at edges already convey.
    if (focal?.kind === "person") {
      return edges.filter((e) => {
        if (!visible.has(e.source) || !visible.has(e.target)) return false;
        if (e.source === focusId || e.target === focusId) return true;
        if (e.kind === "colleague") return false;
        return true;
      });
    }
    return edges.filter((e) => visible.has(e.source) && visible.has(e.target));
  }, [focusId, edges, focalNodes, nodeById]);

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
        personConnections,
        theme,
        skipColleagueEdges: true,
      }),
    [allProspects, scores, signalsById, personConnections, theme],
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

  // selectedNode — first try the rendered nodeById (fast), then the agent's
  // fullGraph (covers every prospect / company / city in the DB). Without
  // the fallback, clicking a person whose colleague edge or chat-surfaced
  // node hadn't yet been promoted to the rendered set left the inspector
  // empty even though the click registered.
  const fullNodeById = useMemo(() => {
    const m = new Map<string, GraphNode>();
    for (const n of fullGraph.nodes) m.set(n.id, n);
    return m;
  }, [fullGraph]);
  const selectedNode = useMemo<GraphNode | null>(() => {
    if (!selectedId) return null;
    return nodeById.get(selectedId) ?? fullNodeById.get(selectedId) ?? null;
  }, [nodeById, fullNodeById, selectedId]);

  // ─── Inspector data wiring (person variant) ────────────────────────────────
  const selectedProspect = useMemo(() => {
    if (!selectedNode || selectedNode.kind !== "person") return undefined;
    // Fall back to allProspects when the clicked person isn't in the
    // top-250 rendered slice — the focal-expand bumps them in for the next
    // render, but selectedProspect needs to resolve THIS render or the
    // inspector renders a header-only stub.
    return (
      prospects.find((p) => p._id === selectedNode.raw._id) ??
      allProspects.find((p) => p._id === selectedNode.raw._id)
    );
  }, [selectedNode, prospects, allProspects]);
  const selectedScore = selectedProspect ? scores[selectedProspect._id] : undefined;
  // Signals lookup falls back to a live useSupaSignalsFor when the bulk
  // signalsById doesn't have the prospect — covers the same off-render-set
  // case as selectedProspect.
  const fallbackSignals = useSignalsFor(
    selectedProspect && !signalsById[selectedProspect._id] ? selectedProspect._id : undefined,
  );
  const selectedSignals = selectedProspect
    ? (signalsById[selectedProspect._id] ?? fallbackSignals)
    : undefined;

  // ─── Inspector data wiring (aggregation variants) ──────────────────────────
  // Compute live counts for the selected hub so the right rail can show
  // honest numbers ("428 people connected to Micron") instead of placeholder
  // firmographics. Computed against the *full* prospect pool, not the
  // rendered top-N, so the inspector matches what the focal expansion will
  // surface.
  const hubStats = useMemo(() => {
    if (!selectedNode || selectedNode.kind === "person") return undefined;
    const ids = prospectIdsForAggregation(
      selectedNode.id,
      allProspects as AggregationProspect[],
    );
    if (!ids) return undefined;
    return computeHubStats(ids, allProspects as AggregationProspect[], scores);
  }, [selectedNode, allProspects, scores]);

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

  // Chat-driven visibility expansion: when the chat surfaces a set of
  // prospects, also include their immediate hubs (companies / roles /
  // cities / industries) so the org chart actually renders connected
  // instead of showing floating person nodes. Without this, calling
  // setVisibleNodeIds([5 person ids]) paints 5 lonely circles with no
  // edges; with it, the canvas reveals the org chart neighborhood.
  const visibleWithHubs = useMemo<Set<string> | null>(() => {
    if (!visibleNodeIds) return null;
    const expanded = new Set<string>(visibleNodeIds);
    for (const id of visibleNodeIds) {
      const ns = neighborByNode.get(id);
      if (!ns) continue;
      for (const n of ns) expanded.add(n);
    }
    return expanded;
  }, [visibleNodeIds, neighborByNode]);

  const nodeVisibility = useCallback(
    (node: FGNode) => {
      if (visibleWithHubs === null) return true;
      if (visibleWithHubs.has(node.id as string)) return true;
      if (selectedId && node.id === selectedId) return true;
      return false;
    },
    [visibleWithHubs, selectedId],
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
      hoveredIdRef.current = null;
      setEngineRunning(true);
      return;
    }
    setFocusId(id);
    setSelectedId(id);
    hoveredIdRef.current = null;
    setEngineRunning(true);
  }, []);
  const onNodeHoverStable = useCallback((node: FGNode | null) => {
    hoveredIdRef.current = node ? (node.id as string) : null;
  }, []);
  const nodeLabelStable = useCallback(
    (node: FGNode) => (node as unknown as GraphNode).name,
    [],
  );
  // Background click is a no-op — preserves the current focal universe.
  // Resetting to the wide view is intentional here: the user clicks the
  // Technology root (always present in the focal subgraph) to "go home".
  const onBackgroundClickStable = useCallback(() => undefined, []);

  // Shared radius helper. Used by both the visual painter and the pointer-
  // area painter so the click hit-box always matches the painted shape —
  // ForceGraph's default hit area is `nodeRelSize * sqrt(nodeVal)` ≈ 4px
  // around the center, which made big hub nodes feel half-broken because
  // only the very middle was clickable.
  const nodeRadius = useCallback(
    (gn: GraphNode, isSelected: boolean): number => {
      // Hierarchical sizing (2026-04-30): top-of-tree nodes get a real
      // size boost so the cold-load (verticals + companies only) reads
      // as a clean, clickable hub-and-spoke instead of a dot field.
      const isRoot = gn.id === "industry:technology";
      const baseR = isRoot
        ? 28
        : gn.kind === "industry"
          ? 22
          : gn.kind === "city"
            ? 14
            : gn.kind === "company"
              ? 14
              : gn.kind === "role"
                ? 9
                : gn.kind === "school" || gn.kind === "conference"
                  ? 6
                  : 5; // person
      return isSelected ? baseR + 3 : baseR;
    },
    [],
  );

  const nodePointerAreaPaint = useCallback(
    (node: FGNode, color: string, ctx2d: CanvasRenderingContext2D) => {
      const gn = node as unknown as GraphNode;
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      const r = nodeRadius(gn, gn.id === selectedId);
      // Generous hit area — paint slightly larger than the visible shape so
      // the click target feels forgiving on a busy canvas.
      ctx2d.fillStyle = color;
      ctx2d.beginPath();
      ctx2d.arc(x, y, r + 4, 0, Math.PI * 2);
      ctx2d.fill();
    },
    [nodeRadius, selectedId],
  );

  const nodeCanvasObject = useCallback(
    (node: FGNode, ctx2d: CanvasRenderingContext2D, globalScale: number) => {
      const gn = node as unknown as GraphNode;
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      // Node radius — slight rank by kind so the eye lands on the bigger
      // hubs (Technology root, industries, cities) before the leaves.
      const isRoot = gn.id === "industry:technology";
      const isSelected = gn.id === selectedId;
      const r = nodeRadius(gn, isSelected);

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
      if (gn.id === hoveredIdRef.current && !isSelected) {
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
      visibleNodeIds,
      neighborByNode,
      nodeColorByKind,
      nodeById,
      labelStrong,
      labelMuted,
      labelHalo,
      nodeRadius,
    ],
  );

  // Edge-label painter. Renders a compact uppercase label centered on each
  // edge (`WORKS AT`, `EDUCATED AT`, `EX`, `PARTNER`, …) so the canvas reads
  // like a Figma sketch instead of an unlabeled spider web. Painted in
  // `after` mode so default link rendering still draws the line; we only
  // overlay the text. Suppressed at low zoom and for `colleague` edges
  // (which would carpet the canvas with redundant tags).
  const linkCanvasObject = useCallback(
    (link: FGLink, ctx2d: CanvasRenderingContext2D, globalScale: number) => {
      const kind = (link as unknown as { kind?: EdgeKind }).kind;
      if (!kind) return;
      const label = EDGE_LABEL_SHORT[kind];
      if (!label) return; // colleague intentionally has no entry

      // Show labels: (a) when zoomed in >= 1.4×, OR (b) for edges touching
      // the selected node at any zoom. This mirrors the node-label policy
      // so clicking a node reveals all its relationship labels immediately.
      const srcNode = link.source as unknown as { id?: string; x?: number; y?: number };
      const tgtNode = link.target as unknown as { id?: string; x?: number; y?: number };
      const srcId = srcNode?.id;
      const tgtId = tgtNode?.id;
      const zoomedIn = globalScale >= 1.4;
      const isAdjacent =
        selectedId != null &&
        (srcId === selectedId || tgtId === selectedId);
      if (!zoomedIn && !isAdjacent) return;

      const sx = srcNode?.x;
      const sy = srcNode?.y;
      const tx = tgtNode?.x;
      const ty = tgtNode?.y;
      if (sx == null || sy == null || tx == null || ty == null) return;
      // Midpoint, slightly offset toward the target so labels on adjacent
      // edges from the same node don't pile up at the source.
      const mx = sx + (tx - sx) * 0.55;
      const my = sy + (ty - sy) * 0.55;
      // Edge angle — orient the label along the edge for that "wire diagram"
      // feel. Flip when the edge runs right-to-left so text isn't upside down.
      let angle = Math.atan2(ty - sy, tx - sx);
      if (angle > Math.PI / 2 || angle < -Math.PI / 2) angle += Math.PI;
      const fontPx = 9 / globalScale; // keep visual size constant across zooms
      ctx2d.save();
      ctx2d.translate(mx, my);
      ctx2d.rotate(angle);
      ctx2d.font = `500 ${fontPx}px Inter, system-ui, sans-serif`;
      ctx2d.textAlign = "center";
      ctx2d.textBaseline = "middle";
      // Halo against the line color (taken from the edge itself if pre-baked).
      const linkAny = link as unknown as { color?: string };
      const fill = linkAny.color ?? labelMuted;
      ctx2d.lineWidth = 3 / globalScale;
      ctx2d.strokeStyle = labelHalo;
      ctx2d.strokeText(label, 0, 0);
      ctx2d.fillStyle = fill;
      ctx2d.fillText(label, 0, 0);
      ctx2d.restore();
    },
    [selectedId, labelHalo, labelMuted],
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
                {allProspects.length > focalNodes.filter((n) => n.kind === "person").length
                  ? `of ${allProspects.length.toLocaleString()} candidates`
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

          {/* Subheader: legend (view + nodes + edges) — U5 */}
          <div className="flex flex-col gap-1.5 border-b border-border px-5 py-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground w-12 shrink-0">
                View
              </span>
              {VIEW_MODES.map((v) => {
                const active = viewMode === v.id;
                return (
                  <button
                    key={v.id}
                    type="button"
                    onClick={() => onPickViewMode(v.id)}
                    aria-pressed={active}
                    className={`rounded-full border border-border py-[3px] px-3 text-[11px] leading-none transition-colors ${
                      active
                        ? "bg-foreground text-background"
                        : "bg-transparent text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {v.label}
                  </button>
                );
              })}
            </div>
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
                  // Hit area = nodeRelSize * sqrt(nodeVal). With our custom
                  // painter the visible radii are 5–12px, so size the default
                  // hit area to 12 to keep clicks landing even if the custom
                  // pointer-area painter ever falls back.
                  nodeRelSize={12}
                  // Pure organic force-directed (Obsidian-style). DAG mode
                  // collapses every level into a horizontal stripe with this
                  // many nodes — wrong shape for what we want.
                  // Smoother cool-down: longer warmup with slower velocity
                  // decay so motion fades instead of slamming to a halt.
                  cooldownTime={1500}
                  cooldownTicks={60}
                  warmupTicks={20}
                  d3AlphaDecay={0.05}
                  d3VelocityDecay={0.5}
                  // Curved edges — subtle arc gives the canvas a spider-web
                  // feel instead of the rigid hub-and-spoke that straight
                  // lines produce. 0.22 gives a more organic web feel.
                  linkCurvature={0.22}
                  // Directional particles animate per frame on every edge —
                  // disabled to keep the canvas at 60fps for the demo.
                  linkDirectionalParticles={0}
                  backgroundColor="transparent"
                  linkColor="color"
                  linkWidth="width"
                  linkCanvasObjectMode={() => "after"}
                  linkCanvasObject={linkCanvasObject}
                  linkVisibility={linkVisibility}
                  nodeVisibility={nodeVisibility}
                  onNodeClick={onNodeClickStable}
                  onNodeHover={onNodeHoverStable}
                  onBackgroundClick={onBackgroundClickStable}
                  onEngineStop={onEngineStop}
                  nodeLabel={nodeLabelStable}
                  nodeCanvasObject={nodeCanvasObject}
                  nodePointerAreaPaint={nodePointerAreaPaint}
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
          weights={weights}
          hubStats={hubStats}
          onSelectProspect={(id) => setSelectedId(`person:${id}`)}
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
