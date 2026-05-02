/**
 * /org/:companyId — full company org chart page.
 *
 * Renders org_reporting_edges for one company in a top-down ReactFlow layout,
 * color-coded by org_functional_clusters domain, edge confidence shaded by
 * path_confidence. Click an edge → OrgCorrectionDialog. Click a person node →
 * navigate to /prospect/:id. Click an unresolved (stub) node → no-op (just
 * highlights, per Decision 4: render but don't navigate).
 *
 * The page is intentionally self-contained — no Zustand graphStore writes,
 * local React state only — so it can be exercised in isolation.
 *
 * `CompanyHeaderCard` and `orgClusters` are being built in parallel by
 * SwiftElk (msg 246). At write time neither file existed; we degrade
 * gracefully with inline fallbacks if either is missing.
 */
import {
  useEffect,
  useMemo,
  useState,
  useCallback,
  Suspense,
  lazy,
  type CSSProperties,
} from "react";
import { useNavigate, useParams } from "react-router-dom";
import ReactFlow, {
  Background,
  Controls,
  type Node,
  type Edge,
  type NodeProps,
  type NodeTypes,
} from "reactflow";
import "reactflow/dist/style.css";

import { supabase } from "@/lib/supabase";
import { OrgCorrectionDialog } from "@/components/OrgCorrectionDialog";

// ── Conditional imports for files being built in parallel ──────────────────
// Both `CompanyHeaderCard` and `orgClusters` may not exist at build time.
// React.lazy + Suspense gives us a runtime-safe import that falls back
// cleanly. The dynamic-import strings are wrapped so vitest/vite don't
// fail-fast at module-resolution time.

type CompanyHeaderCardModule = typeof import("@/components/CompanyHeaderCard");

const CompanyHeaderCardLazy = lazy<
  React.ComponentType<{
    company: CompanyRow;
    enriched_count: number;
    top_persons: Array<{ id: string; canonical_name: string; current_title: string | null; current_seniority_score: number | null }>;
  }>
>(async () => {
  try {
    const mod = (await import(
      /* @vite-ignore */ "@/components/CompanyHeaderCard"
    )) as Partial<CompanyHeaderCardModule>;
    if (mod.CompanyHeaderCard) {
      return { default: mod.CompanyHeaderCard };
    }
    if ("default" in mod && typeof mod.default === "function") {
      return { default: mod.default as never };
    }
  } catch {
    // Module doesn't exist yet — fall through to placeholder.
  }
  return { default: PlaceholderHeaderCard };
});

type OrgClustersModule = typeof import("@/lib/orgClusters");

interface OrgClustersFallback {
  domainColor: (domain: string | null | undefined) => string;
}

let orgClustersPromise: Promise<OrgClustersFallback> | null = null;
function loadOrgClusters(): Promise<OrgClustersFallback> {
  if (!orgClustersPromise) {
    orgClustersPromise = (async () => {
      try {
        const mod = (await import(
          /* @vite-ignore */ "@/lib/orgClusters"
        )) as Partial<OrgClustersModule>;
        if (typeof mod.domainColor === "function") {
          return { domainColor: mod.domainColor };
        }
      } catch {
        // fall through
      }
      return { domainColor: fallbackDomainColor };
    })();
  }
  return orgClustersPromise;
}

// Static fallback palette per CLAUDE.md functional domain taxonomy.
const FALLBACK_DOMAIN_PALETTE: Record<string, string> = {
  hardware_engineering: "#3B82F6", // blue
  software_engineering: "#10B981", // emerald
  product_management: "#8B5CF6", // violet
  manufacturing_ops: "#F59E0B", // amber
  sales_marketing: "#EC4899", // pink
  research: "#14B8A6", // teal
  finance_legal: "#6B7280", // gray
  people_ops: "#F97316", // orange
  general_management: "#EF4444", // red
  uncategorized: "#9CA3AF", // neutral
};

function fallbackDomainColor(domain: string | null | undefined): string {
  if (!domain) return FALLBACK_DOMAIN_PALETTE.uncategorized;
  return FALLBACK_DOMAIN_PALETTE[domain] ?? FALLBACK_DOMAIN_PALETTE.uncategorized;
}

// ── Types ──────────────────────────────────────────────────────────────────

export interface CompanyRow {
  id: string;
  canonical_name: string;
  industry?: string | null;
  hq_country?: string | null;
  enriched_count?: number | null;
}

export interface OrgPersonRow {
  id: string;
  canonical_name: string;
  current_title: string | null;
  current_seniority_score: number | null;
  current_functional_domain: string | null;
  is_unresolved_target: boolean;
}

export interface OrgEdgeRow {
  id: string;
  manager_id: string;
  report_id: string;
  confidence: number | null;
  path_confidence: number | null;
  inference_method: string;
  is_current: boolean;
  valid_from: string | null;
  valid_to: string | null;
}

export interface OrgClusterRow {
  id: string;
  functional_domain: string;
  sub_domain: string | null;
}

export interface OrgChartData {
  company: CompanyRow | null;
  edges: OrgEdgeRow[];
  persons: Map<string, OrgPersonRow>;
  clusters: Map<string, OrgClusterRow>;
  /** person_id → cluster.functional_domain for color-coding. */
  personDomain: Map<string, string>;
  topPersons: Array<{
    id: string;
    canonical_name: string;
    current_title: string | null;
    current_seniority_score: number | null;
  }>;
}

// ── Data fetching (inline; uses untyped supabase client) ───────────────────
// db.ts has no helpers for these v3 tables and we are not allowed to edit it,
// so we go through the existing `supabase` client directly. Same untyped
// pattern as ProspectDetail.tsx fetchOrgV3.

// Permissive chainable shape — every call returns the same thenable so the
// real supabase chain (.select().eq().eq().order().limit() / maybeSingle /
// .in()) terminates by awaiting. This mirrors the shape used in
// ProspectDetail.tsx::fetchOrgV3 but loosened so any chain depth resolves.
type UntypedChain = {
  select: (cols: string) => UntypedChain;
  eq: (col: string, v: unknown) => UntypedChain;
  in: (col: string, v: unknown[]) => UntypedChain;
  order: (col: string, opts?: unknown) => UntypedChain;
  limit: (n: number) => UntypedChain;
  maybeSingle: () => Promise<{ data: unknown; error: unknown }>;
  then: <T>(
    onF: (v: { data: unknown; error: unknown }) => T,
    onR?: (e: unknown) => T,
  ) => Promise<T>;
};

interface UntypedSupabase {
  from: (table: string) => UntypedChain;
}

export async function fetchOrgChartData(
  companyId: string,
): Promise<OrgChartData> {
  const empty: OrgChartData = {
    company: null,
    edges: [],
    persons: new Map(),
    clusters: new Map(),
    personDomain: new Map(),
    topPersons: [],
  };
  if (!supabase) return empty;
  const sb = supabase as unknown as UntypedSupabase;

  // 1. Company row.
  let company: CompanyRow | null = null;
  try {
    const r = await sb
      .from("companies")
      .select("id, canonical_name, industry, hq_country, enriched_count")
      .eq("id", companyId)
      .maybeSingle();
    if (!r.error && r.data && typeof r.data === "object") {
      company = r.data as CompanyRow;
    }
  } catch (err) {
    console.error("[OrgChart] company fetch failed", err);
  }

  // 2. Reporting edges for this company. The edges table itself has no
  //    company_id, so we resolve through current employees of the company
  //    first, then filter edges to that person id set.
  let personIds: string[] = [];
  try {
    const r = await sb
      .from("employment_periods")
      .select("person_id")
      .eq("company_id", companyId)
      .eq("is_current", true)
      .order("seniority_score", { ascending: false })
      .limit(1000);
    if (!r.error && Array.isArray(r.data)) {
      personIds = Array.from(
        new Set(
          (r.data as Array<{ person_id: string }>)
            .map((row) => row.person_id)
            .filter((v): v is string => typeof v === "string"),
        ),
      );
    }
  } catch (err) {
    console.error("[OrgChart] employment_periods fetch failed", err);
  }

  if (personIds.length === 0) {
    return { ...empty, company };
  }

  let rawEdges: OrgEdgeRow[] = [];
  try {
    const r = await sb
      .from("org_reporting_edges")
      .select(
        "id, manager_id, report_id, confidence, path_confidence, inference_method, is_current, valid_from, valid_to",
      )
      .in("manager_id", personIds);
    if (!r.error && Array.isArray(r.data)) {
      const idSet = new Set(personIds);
      rawEdges = (r.data as OrgEdgeRow[]).filter(
        (e) =>
          typeof e.manager_id === "string" &&
          typeof e.report_id === "string" &&
          idSet.has(e.manager_id) &&
          idSet.has(e.report_id),
      );
    }
  } catch (err) {
    console.error("[OrgChart] org_reporting_edges fetch failed", err);
  }

  // 3. Persons referenced by edges (managers + reports).
  const endpointIds = Array.from(
    new Set(rawEdges.flatMap((e) => [e.manager_id, e.report_id])),
  );
  const personsMap = new Map<string, OrgPersonRow>();
  if (endpointIds.length > 0) {
    try {
      const r = await sb
        .from("persons")
        .select(
          "id, canonical_name, current_title, current_seniority_score, current_functional_domain, is_unresolved_target",
        )
        .in("id", endpointIds);
      if (!r.error && Array.isArray(r.data)) {
        for (const row of r.data as OrgPersonRow[]) {
          personsMap.set(row.id, {
            id: row.id,
            canonical_name: row.canonical_name,
            current_title: row.current_title ?? null,
            current_seniority_score:
              typeof row.current_seniority_score === "number"
                ? row.current_seniority_score
                : null,
            current_functional_domain: row.current_functional_domain ?? null,
            is_unresolved_target: row.is_unresolved_target === true,
          });
        }
      }
    } catch (err) {
      console.error("[OrgChart] persons fetch failed", err);
    }
  }

  // 4. Functional clusters for color-coding (domain by person via cluster
  //    membership). One read each.
  const clustersMap = new Map<string, OrgClusterRow>();
  const personDomain = new Map<string, string>();
  try {
    const r = await sb
      .from("org_functional_clusters")
      .select("id, functional_domain, sub_domain, company_id")
      .eq("company_id", companyId);
    if (!r.error && Array.isArray(r.data)) {
      for (const row of r.data as OrgClusterRow[]) {
        clustersMap.set(row.id, {
          id: row.id,
          functional_domain: row.functional_domain,
          sub_domain: row.sub_domain ?? null,
        });
      }
    }
  } catch (err) {
    console.error("[OrgChart] org_functional_clusters fetch failed", err);
  }

  if (clustersMap.size > 0 && endpointIds.length > 0) {
    try {
      const r = await sb
        .from("org_cluster_members")
        .select("cluster_id, person_id")
        .in("person_id", endpointIds);
      if (!r.error && Array.isArray(r.data)) {
        for (const row of r.data as Array<{ cluster_id: string; person_id: string }>) {
          const cluster = clustersMap.get(row.cluster_id);
          if (cluster && !personDomain.has(row.person_id)) {
            personDomain.set(row.person_id, cluster.functional_domain);
          }
        }
      }
    } catch (err) {
      console.error("[OrgChart] org_cluster_members fetch failed", err);
    }
  }

  // 5. Top 5 highest-scoring persons at this company (for header card).
  const topPersons: OrgChartData["topPersons"] = [];
  try {
    const r = await sb
      .from("persons")
      .select("id, canonical_name, current_title, current_seniority_score")
      .eq("current_company_id", companyId)
      .order("current_seniority_score", { ascending: false })
      .limit(5);
    if (!r.error && Array.isArray(r.data)) {
      for (const row of r.data as Array<{
        id: string;
        canonical_name: string;
        current_title: string | null;
        current_seniority_score: number | null;
      }>) {
        topPersons.push({
          id: row.id,
          canonical_name: row.canonical_name,
          current_title: row.current_title ?? null,
          current_seniority_score: row.current_seniority_score ?? null,
        });
      }
    }
  } catch (err) {
    console.error("[OrgChart] top persons fetch failed", err);
  }

  return {
    company,
    edges: rawEdges,
    persons: personsMap,
    clusters: clustersMap,
    personDomain,
    topPersons,
  };
}

// ── Layout (top-down by seniority) ─────────────────────────────────────────
//
// dagre is NOT in package.json — we cannot rely on it. Instead, we group by
// BFS-depth from roots (people with no manager in this edge set) and within
// each level group siblings by functional_domain so peers cluster nicely.
//
// ROW_H × COL_W chosen empirically to match the spacing in
// ProspectDetail.tsx::layoutOrgTree, with extra horizontal slack so 100-person
// charts don't crash into each other.

const ROW_H = 160;
const COL_W = 240;

interface LayoutEntry {
  x: number;
  y: number;
  depth: number;
}

export function layoutOrgChart(
  edges: OrgEdgeRow[],
  persons: Map<string, OrgPersonRow>,
  personDomain: Map<string, string>,
): Map<string, LayoutEntry> {
  const layout = new Map<string, LayoutEntry>();
  if (edges.length === 0) return layout;

  const childrenOf = new Map<string, string[]>();
  const hasManager = new Set<string>();
  const allIds = new Set<string>();
  for (const e of edges) {
    allIds.add(e.manager_id);
    allIds.add(e.report_id);
    hasManager.add(e.report_id);
    const arr = childrenOf.get(e.manager_id) ?? [];
    arr.push(e.report_id);
    childrenOf.set(e.manager_id, arr);
  }

  // Roots = nodes with no manager among the visible edge set. Sort by
  // seniority desc so the most senior root sits leftmost.
  const roots = Array.from(allIds)
    .filter((id) => !hasManager.has(id))
    .sort((a, b) => {
      const sa = persons.get(a)?.current_seniority_score ?? 0;
      const sb = persons.get(b)?.current_seniority_score ?? 0;
      return sb - sa;
    });
  if (roots.length === 0 && allIds.size > 0) {
    // Cycle-only edge set — synthesize a root from the highest-seniority node.
    const fallback = Array.from(allIds).sort((a, b) => {
      const sa = persons.get(a)?.current_seniority_score ?? 0;
      const sb = persons.get(b)?.current_seniority_score ?? 0;
      return sb - sa;
    })[0];
    roots.push(fallback);
  }

  // BFS to assign depth per Decision 2 in CLAUDE.md (cluster by domain
  // before assigning hierarchy — at this layer the hierarchy is already
  // resolved by the edges, so we group siblings by domain within each
  // depth row).
  const depth = new Map<string, number>();
  const queue: string[] = [];
  for (const r of roots) {
    depth.set(r, 0);
    queue.push(r);
  }
  while (queue.length > 0) {
    const id = queue.shift() as string;
    const d = depth.get(id) ?? 0;
    for (const child of childrenOf.get(id) ?? []) {
      if (!depth.has(child)) {
        depth.set(child, d + 1);
        queue.push(child);
      }
    }
  }
  for (const id of allIds) {
    if (!depth.has(id)) depth.set(id, 0);
  }

  // Place by depth, ordering siblings: domain ASC, then seniority DESC, then
  // name ASC for determinism.
  const byDepth = new Map<number, string[]>();
  for (const [id, d] of depth) {
    const arr = byDepth.get(d) ?? [];
    arr.push(id);
    byDepth.set(d, arr);
  }
  for (const arr of byDepth.values()) {
    arr.sort((a, b) => {
      const da =
        personDomain.get(a) ??
        persons.get(a)?.current_functional_domain ??
        "zzz";
      const db =
        personDomain.get(b) ??
        persons.get(b)?.current_functional_domain ??
        "zzz";
      if (da !== db) return da.localeCompare(db);
      const sa = persons.get(a)?.current_seniority_score ?? 0;
      const sb = persons.get(b)?.current_seniority_score ?? 0;
      if (sa !== sb) return sb - sa;
      const na = persons.get(a)?.canonical_name ?? a;
      const nb = persons.get(b)?.canonical_name ?? b;
      return na.localeCompare(nb);
    });
  }

  for (const [d, ids] of byDepth) {
    const mid = (ids.length - 1) / 2;
    ids.forEach((id, i) => {
      layout.set(id, { x: (i - mid) * COL_W, y: d * ROW_H, depth: d });
    });
  }
  return layout;
}

// ── Node radius from seniority ─────────────────────────────────────────────
// 40px (lowest) to 80px (CEO). Linear in [35, 100] seniority space.

function seniorityRadius(score: number | null | undefined): number {
  if (score == null) return 50;
  const clamped = Math.max(35, Math.min(100, score));
  // 35 → 40, 100 → 80
  return 40 + ((clamped - 35) / (100 - 35)) * 40;
}

// ── ReactFlow node components ──────────────────────────────────────────────

interface PersonNodeData {
  person: OrgPersonRow;
  fill: string;
  dimmed: boolean;
  highlighted: boolean;
}

const PersonCircleNode = ({ data }: NodeProps) => {
  const d = data as unknown as PersonNodeData;
  const r = seniorityRadius(d.person.current_seniority_score);
  const style: CSSProperties = {
    width: r * 2,
    height: r * 2,
    borderRadius: "50%",
    background: d.dimmed ? `${d.fill}55` : d.fill,
    border: d.highlighted
      ? "3px solid hsl(var(--foreground))"
      : "1px solid hsl(var(--border))",
    color: "white",
    fontSize: 11,
    fontFamily: "Inter",
    textAlign: "center",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    padding: 8,
    cursor: "pointer",
    opacity: d.dimmed ? 0.4 : 1,
    transition: "opacity 120ms, border 120ms",
  };
  const tooltip = `${d.person.canonical_name}${
    d.person.current_title ? `\n${d.person.current_title}` : ""
  }`;
  return (
    <div
      style={style}
      title={tooltip}
      data-testid={`person-node-${d.person.id}`}
      data-person-id={d.person.id}
      data-domain={d.person.current_functional_domain ?? "uncategorized"}
    >
      <div style={{ fontWeight: 600, lineHeight: 1.1 }}>
        {d.person.canonical_name}
      </div>
      {d.person.current_title && (
        <div style={{ fontSize: 9, marginTop: 2, opacity: 0.85 }}>
          {d.person.current_title.length > 28
            ? d.person.current_title.slice(0, 26) + "…"
            : d.person.current_title}
        </div>
      )}
    </div>
  );
};

const UnresolvedDiamondNode = ({ data }: NodeProps) => {
  const d = data as unknown as PersonNodeData;
  const r = seniorityRadius(d.person.current_seniority_score);
  const style: CSSProperties = {
    width: r * 2,
    height: r * 2,
    transform: "rotate(45deg)",
    background: "transparent",
    border: "2px dashed #9CA3AF",
    color: "#374151",
    fontSize: 10,
    fontFamily: "Inter",
    textAlign: "center",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    padding: 4,
    cursor: "default",
    opacity: d.dimmed ? 0.4 : 1,
  };
  return (
    <div
      style={style}
      title={d.person.canonical_name}
      data-testid={`unresolved-node-${d.person.id}`}
      data-person-id={d.person.id}
      data-unresolved="true"
    >
      <div style={{ transform: "rotate(-45deg)", fontStyle: "italic" }}>
        {d.person.canonical_name}
      </div>
    </div>
  );
};

const orgNodeTypes: NodeTypes = {
  person: PersonCircleNode,
  unresolved: UnresolvedDiamondNode,
};

// ── Placeholder header (used when CompanyHeaderCard isn't built yet) ───────

interface PlaceholderHeaderProps {
  company: CompanyRow;
  enriched_count: number;
  top_persons: OrgChartData["topPersons"];
}

function PlaceholderHeaderCard(props: PlaceholderHeaderProps) {
  return (
    <div
      data-testid="company-header-fallback"
      className="border-b border-border px-6 py-4"
    >
      <h1 className="text-2xl font-light tracking-tight">
        {props.company.canonical_name}
      </h1>
      <p className="text-sm text-muted-foreground mt-1">
        {props.company.industry ?? "Industry unknown"}
        {props.company.hq_country ? ` · ${props.company.hq_country}` : ""}
        {" · "}
        {props.enriched_count} enriched
      </p>
      {props.top_persons.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-muted-foreground">
          <span className="text-mono uppercase tracking-[0.16em] text-[10px]">
            Top:
          </span>
          {props.top_persons.map((p) => (
            <span key={p.id}>
              {p.canonical_name}
              {p.current_title ? ` (${p.current_title})` : ""}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Main page ──────────────────────────────────────────────────────────────

const OrgChart = () => {
  const { companyId } = useParams<{ companyId: string }>();
  const navigate = useNavigate();

  const [data, setData] = useState<OrgChartData | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Toolbar state
  const [showClusters, setShowClusters] = useState<boolean>(true);
  const [showHistorical, setShowHistorical] = useState<boolean>(false);
  const [search, setSearch] = useState<string>("");

  // Edge selection (for OrgCorrectionDialog)
  const [selectedEdge, setSelectedEdge] = useState<OrgEdgeRow | null>(null);
  const [correctionOpen, setCorrectionOpen] = useState<boolean>(false);

  // Domain color helper — resolved async; default to fallback at first paint.
  const [domainColor, setDomainColor] = useState<
    (d: string | null | undefined) => string
  >(() => fallbackDomainColor);

  useEffect(() => {
    let cancelled = false;
    void loadOrgClusters().then((mod) => {
      if (cancelled) return;
      setDomainColor(() => mod.domainColor);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Fetch data on mount / companyId change
  useEffect(() => {
    if (!companyId) {
      setLoading(false);
      setError("Missing company id in URL");
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchOrgChartData(companyId)
      .then((res) => {
        if (cancelled) return;
        setData(res);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        console.error("[OrgChart] fetch failed", err);
        setError(err instanceof Error ? err.message : "fetch failed");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [companyId]);

  // Derived: filtered edges by toggle + search highlight set
  const filteredEdges = useMemo<OrgEdgeRow[]>(() => {
    if (!data) return [];
    if (showHistorical) return data.edges;
    return data.edges.filter((e) => e.is_current === true);
  }, [data, showHistorical]);

  const matchedPersonIds = useMemo<Set<string>>(() => {
    if (!data) return new Set();
    const q = search.trim().toLowerCase();
    if (!q) return new Set();
    const ids = new Set<string>();
    for (const p of data.persons.values()) {
      if (p.canonical_name.toLowerCase().includes(q)) ids.add(p.id);
    }
    return ids;
  }, [data, search]);

  const layout = useMemo(() => {
    if (!data) return new Map<string, LayoutEntry>();
    return layoutOrgChart(filteredEdges, data.persons, data.personDomain);
  }, [data, filteredEdges]);

  // Build ReactFlow nodes
  const rfNodes = useMemo<Node[]>(() => {
    if (!data) return [];
    const nodes: Node[] = [];
    for (const [id, pos] of layout) {
      const person = data.persons.get(id);
      if (!person) continue;
      const domain =
        data.personDomain.get(id) ?? person.current_functional_domain ?? null;
      const fill = showClusters ? domainColor(domain) : "#9CA3AF";
      const isMatch = matchedPersonIds.has(id);
      const dimmed = search.trim().length > 0 && !isMatch;
      nodes.push({
        id,
        type: person.is_unresolved_target ? "unresolved" : "person",
        position: { x: pos.x, y: pos.y },
        data: {
          person,
          fill,
          dimmed,
          highlighted: isMatch,
        } as PersonNodeData,
      });
    }
    return nodes;
  }, [data, layout, showClusters, domainColor, search, matchedPersonIds]);

  // Build ReactFlow edges with confidence shading
  const rfEdges = useMemo<Edge[]>(() => {
    return filteredEdges.map((e) => {
      const conf = e.path_confidence ?? e.confidence ?? 0.5;
      let strokeDasharray: string | undefined;
      let stroke: string;
      if (conf >= 0.7) {
        strokeDasharray = undefined;
        stroke = "hsl(var(--foreground))";
      } else if (conf >= 0.4) {
        strokeDasharray = "6 4";
        stroke = "hsl(var(--muted-foreground))";
      } else {
        strokeDasharray = "2 4";
        stroke = "#9CA3AF";
      }
      const strokeWidth = e.is_current ? 2 : 1;
      return {
        id: e.id,
        source: e.manager_id,
        target: e.report_id,
        data: { edge: e },
        style: {
          stroke,
          strokeWidth,
          strokeDasharray,
        },
        label:
          conf >= 0.7
            ? undefined
            : `${Math.round(conf * 100)}%`,
        labelStyle: { fontSize: 9, fill: "hsl(var(--muted-foreground))" },
      } as Edge;
    });
  }, [filteredEdges]);

  // Handlers
  const handleNodeClick = useCallback(
    (_evt: React.MouseEvent, node: Node) => {
      if (!data) return;
      const person = data.persons.get(node.id);
      if (!person) return;
      // Decision 4 — unresolved nodes are rendered but not navigable.
      if (person.is_unresolved_target) return;
      navigate(`/prospect/${person.id}`);
    },
    [data, navigate],
  );

  const handleEdgeClick = useCallback(
    (_evt: React.MouseEvent, edge: Edge) => {
      const raw = (edge.data as { edge?: OrgEdgeRow } | undefined)?.edge;
      if (!raw) return;
      setSelectedEdge(raw);
      setCorrectionOpen(true);
    },
    [],
  );

  const handlePaneClick = useCallback(() => {
    setSelectedEdge(null);
  }, []);

  // Loading / error / empty states
  if (loading) {
    return (
      <div
        data-testid="org-chart-loading"
        className="flex h-screen items-center justify-center text-sm text-muted-foreground"
      >
        Loading org chart…
      </div>
    );
  }

  if (error || !data) {
    return (
      <div
        data-testid="org-chart-error"
        className="flex h-screen items-center justify-center text-sm text-destructive"
      >
        {error ?? "Couldn't load org chart"}
      </div>
    );
  }

  const company: CompanyRow = data.company ?? {
    id: companyId ?? "",
    canonical_name: "Unknown company",
  };

  const headerProps: PlaceholderHeaderProps = {
    company,
    enriched_count: company.enriched_count ?? data.persons.size,
    top_persons: data.topPersons,
  };

  return (
    <div className="flex flex-col h-screen" data-testid="org-chart-page">
      <Suspense fallback={<PlaceholderHeaderCard {...headerProps} />}>
        <CompanyHeaderCardLazy {...headerProps} />
      </Suspense>

      <Toolbar
        showClusters={showClusters}
        onToggleClusters={() => setShowClusters((v) => !v)}
        showHistorical={showHistorical}
        onToggleHistorical={() => setShowHistorical((v) => !v)}
        search={search}
        onSearch={setSearch}
        nodeCount={rfNodes.length}
        edgeCount={rfEdges.length}
      />

      <div className="flex flex-1 min-h-0">
        <div className="flex-1 min-w-0 relative" data-testid="org-chart-canvas">
          {rfEdges.length === 0 ? (
            <div
              data-testid="org-chart-empty"
              className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground text-center px-8"
            >
              No reporting edges found for {company.canonical_name}. Run the
              org-chart inference pipeline to populate this view.
            </div>
          ) : (
            <ReactFlow
              nodes={rfNodes}
              edges={rfEdges}
              nodeTypes={orgNodeTypes}
              fitView
              proOptions={{ hideAttribution: true }}
              onNodeClick={handleNodeClick}
              onEdgeClick={handleEdgeClick}
              onPaneClick={handlePaneClick}
              nodesDraggable={false}
              nodesConnectable={false}
            >
              <Background color="hsl(var(--border))" gap={24} />
              <Controls className="!bg-secondary !border-border" />
            </ReactFlow>
          )}
        </div>

        <SidePanel
          selectedEdge={selectedEdge}
          persons={data.persons}
          onClear={() => setSelectedEdge(null)}
        />
      </div>

      {selectedEdge && (
        <OrgCorrectionDialog
          open={correctionOpen}
          onOpenChange={(o) => {
            setCorrectionOpen(o);
            if (!o) setSelectedEdge(null);
          }}
          personAId={selectedEdge.report_id}
          personAName={
            data.persons.get(selectedEdge.report_id)?.canonical_name ?? "report"
          }
          defaultPersonBId={selectedEdge.manager_id}
          defaultPersonBName={
            data.persons.get(selectedEdge.manager_id)?.canonical_name ??
            undefined
          }
          defaultEdgeId={selectedEdge.id}
        />
      )}
    </div>
  );
};

// ── Toolbar ────────────────────────────────────────────────────────────────

interface ToolbarProps {
  showClusters: boolean;
  onToggleClusters: () => void;
  showHistorical: boolean;
  onToggleHistorical: () => void;
  search: string;
  onSearch: (v: string) => void;
  nodeCount: number;
  edgeCount: number;
}

function Toolbar(props: ToolbarProps) {
  return (
    <div
      data-testid="org-chart-toolbar"
      className="flex items-center gap-3 border-b border-border px-4 py-2 text-xs"
    >
      <button
        type="button"
        data-testid="toggle-clusters"
        aria-pressed={props.showClusters}
        onClick={props.onToggleClusters}
        className={`px-2 py-1 border ${
          props.showClusters
            ? "border-foreground"
            : "border-border text-muted-foreground"
        }`}
      >
        Functional clusters
      </button>
      <button
        type="button"
        data-testid="toggle-historical"
        aria-pressed={props.showHistorical}
        onClick={props.onToggleHistorical}
        className={`px-2 py-1 border ${
          props.showHistorical
            ? "border-foreground"
            : "border-border text-muted-foreground"
        }`}
      >
        Show historical edges
      </button>
      <input
        type="text"
        data-testid="search-input"
        value={props.search}
        onChange={(e) => props.onSearch(e.target.value)}
        placeholder="Search by name…"
        aria-label="Search by name"
        className="flex-1 max-w-xs bg-transparent border border-border px-2 py-1 outline-none"
      />
      <span className="text-muted-foreground text-[10px] text-mono ml-auto">
        {props.nodeCount} nodes · {props.edgeCount} edges
      </span>
    </div>
  );
}

// ── Side panel ─────────────────────────────────────────────────────────────

interface SidePanelProps {
  selectedEdge: OrgEdgeRow | null;
  persons: Map<string, OrgPersonRow>;
  onClear: () => void;
}

function SidePanel({ selectedEdge, persons, onClear }: SidePanelProps) {
  return (
    <aside
      data-testid="org-chart-side-panel"
      className="hidden md:flex flex-col w-72 border-l border-border p-4 text-xs overflow-auto"
    >
      {selectedEdge ? (
        <>
          <div className="label-eyebrow mb-2">Selected edge</div>
          <div className="text-sm font-medium">
            {persons.get(selectedEdge.manager_id)?.canonical_name ?? "?"}
            {" → "}
            {persons.get(selectedEdge.report_id)?.canonical_name ?? "?"}
          </div>
          <dl className="mt-3 space-y-1 text-[11px] text-muted-foreground">
            <div className="flex justify-between">
              <dt>Confidence</dt>
              <dd className="text-mono text-foreground">
                {selectedEdge.confidence != null
                  ? `${Math.round(selectedEdge.confidence * 100)}%`
                  : "—"}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt>Path conf.</dt>
              <dd className="text-mono text-foreground">
                {selectedEdge.path_confidence != null
                  ? `${Math.round(selectedEdge.path_confidence * 100)}%`
                  : "—"}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt>Method</dt>
              <dd className="text-mono text-foreground">
                {selectedEdge.inference_method}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt>Current</dt>
              <dd className="text-mono text-foreground">
                {selectedEdge.is_current ? "yes" : "no"}
              </dd>
            </div>
          </dl>
          <button
            type="button"
            onClick={onClear}
            className="mt-4 px-2 py-1 border border-border text-[11px] hover:bg-secondary"
          >
            Clear selection
          </button>
        </>
      ) : (
        <div className="text-muted-foreground text-[11px]">
          Click an edge to inspect inference details. Click a person to open
          their prospect page.
        </div>
      )}
    </aside>
  );
}

export default OrgChart;
