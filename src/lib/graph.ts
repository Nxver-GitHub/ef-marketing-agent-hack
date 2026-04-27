/**
 * Graph data builder for the v2 Discover view (force-directed canvas).
 *
 * Pure function — given prospects + scores (+ optional signals), emits a
 * deduped {nodes, edges} bundle suitable for react-force-graph-2d. The shape
 * intentionally exceeds the v1 sketch in credence_2.0.md (which only had
 * person/company/location); see `todos.graph_lib` in that file's frontmatter
 * for the full type union.
 */
import type { Prospect, Score, Signal } from "./mockStore";
import { GENERATED_COMPANY_META } from "./company-meta.generated";

// ─── Node + edge schema ──────────────────────────────────────────────────────

export type NodeKind =
  | "person"
  | "company"
  | "role"
  | "city"
  | "school"
  | "conference"
  | "industry";

export type EdgeKind =
  | "works_at"
  | "colleague"
  | "located_in"
  | "reports_to"
  | "past_employer"
  | "partnership"
  | "education"
  | "scope_signal"
  | "vertical"
  | "evidence_cited";

// `color` is pre-baked per node/edge when a `theme` is passed to buildGraph().
// Render hot-paths (ForceGraph2D linkColor/nodeColor accessors) read it as a
// plain property instead of invoking a callback per-tick — eliminates the
// linkColor/linkWidth main-thread overhead flagged in the v2 perf audit.
export type GraphNode =
  | {
      id: string;
      kind: "person";
      name: string;
      role: string;
      companyId: string;
      score?: number;
      color?: string;
      raw: Prospect;
    }
  | {
      id: string;
      kind: "company";
      name: string;
      locationId?: string;
      industryId?: string;
      color?: string;
    }
  | { id: string; kind: "role"; name: string; description?: string; color?: string }
  | { id: string; kind: "city"; name: string; country?: string; color?: string }
  | { id: string; kind: "school"; name: string; color?: string }
  | { id: string; kind: "conference"; name: string; year?: number; color?: string }
  | { id: string; kind: "industry"; name: string; color?: string };

export type GraphEdge = {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  color?: string;
  width?: number;
};

export interface ThemeTokens {
  nodeColors: Record<NodeKind, string>;
  edgeColors: Record<EdgeKind, string>;
}

export interface BuildGraphArgs {
  prospects: Prospect[];
  scores: Record<string, Score>;
  signalsById?: Record<string, Signal[]>;
  /**
   * Optional theme tokens. When supplied, every node gets `color` and every
   * edge gets `color` set so the consumer can use property-name accessors
   * ("color") instead of per-tick callbacks.
   */
  theme?: ThemeTokens;
  /**
   * Skip the O(n²) "colleague" edge pass. Drops `colleague` edges entirely,
   * which is the only super-linear step in buildGraph: a company with 446
   * prospects emits ~99k colleague edges by itself. Set this true on the
   * agent-context build (chat copilot does name/company lookups, not
   * graph traversal) so the full DB can fit in the agent context without
   * locking up the main thread.
   */
  skipColleagueEdges?: boolean;
}

// ─── Company metadata ────────────────────────────────────────────────────────
// HQ city/country + industry vertical + known partnerships per company. Drives
// `located_in`, `vertical`, and `partnership` edge construction. Keys are the
// raw company strings used in mockStore.ts seed data + likely Supabase rows.
// Lookup is normalized (lowercased, suffix-stripped) so "Intel" and
// "Intel Corporation" resolve to the same entry.

interface CompanyMeta {
  country: string;
  state?: string;
  industry: string;
  partnerships?: string[];
}

const COMPANY_META: Record<string, CompanyMeta> = {
  TSMC: { country: "Taiwan", industry: "Semiconductors", partnerships: ["Apple", "NVIDIA"] },
  ASML: {
    country: "Netherlands",
    industry: "Semiconductors",
    partnerships: ["TSMC", "Intel"],
  },
  Intel: { country: "USA", state: "California", industry: "Semiconductors" },
  NVIDIA: {
    country: "USA",
    state: "California",
    industry: "Semiconductors",
    partnerships: ["TSMC"],
  },
  Infineon: { country: "Germany", industry: "Semiconductors" },
  // Common partnership targets / extras likely to appear once real Supabase
  // data lands. Keep extending as new seeds get added.
  Apple: { country: "USA", state: "California", industry: "Consumer Electronics" },
  Samsung: { country: "South Korea", industry: "Semiconductors" },
  AMD: { country: "USA", state: "California", industry: "Semiconductors" },
  Qualcomm: { country: "USA", state: "California", industry: "Semiconductors" },
  "Applied Materials": { country: "USA", state: "California", industry: "Semiconductors" },
  Nikon: { country: "Japan", industry: "Semiconductors" },
  "Carl Zeiss": { country: "Germany", industry: "Semiconductors", partnerships: ["ASML"] },
  Google: { country: "USA", state: "California", industry: "Internet" },
  Broadcom: { country: "USA", state: "California", industry: "Semiconductors" },
  Micron: { country: "USA", state: "Idaho", industry: "Semiconductors" },
  "Micron Technology": { country: "USA", state: "Idaho", industry: "Semiconductors" },
  Bosch: { country: "Germany", industry: "Industrial" },
  Lockheed: { country: "USA", state: "Maryland", industry: "Defense" },
  "Lockheed Martin": { country: "USA", state: "Maryland", industry: "Defense" },
  Raytheon: { country: "USA", state: "Virginia", industry: "Defense" },
  Boeing: { country: "USA", state: "Virginia", industry: "Aerospace" },
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Mirrors `normalizeCompany` in src/pages/ProspectDetail.tsx so company-name
 * variants ("Intel Corp" vs "Intel Corporation") collapse to one node.
 * Duplicated rather than imported to keep this module pure (no React deps).
 */
function normalizeCompany(s: string | null | undefined): string {
  return (s ?? "")
    .toLowerCase()
    .replace(
      /\b(corp\.?|corporation|inc\.?|incorporated|limited|ltd\.?|llc|plc|technologies|technology|semiconductor|semiconductors|systems?)\b/g,
      "",
    )
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

function normalizeKey(s: string): string {
  return s.trim().toLowerCase();
}

/**
 * Canonicalize a prospect's free-text role string into a short, dedupe-able
 * label. Source values are messy LinkedIn titles like
 *   "Senior Software Engineer | Design Systems | Custom Compute at Cadence Design Systems"
 * which would each spawn their own role node and break the canvas.
 *
 * Rules (applied in order):
 *  1. Drop the "at <company>" suffix — companies are already separate nodes.
 *  2. Truncate at the first separator (`|`, en-dash, em-dash, " - ", " · ").
 *  3. Collapse whitespace and drop trailing punctuation.
 *  4. Hard-cap at 28 chars with a trailing ellipsis.
 *
 * Two prospects with the same canonical role end up sharing one role node.
 */
export function canonicalizeRole(raw: string): string {
  if (!raw) return "";
  let s = raw.trim();
  // 1. Strip "at <company>" tail (case-insensitive).
  s = s.replace(/\s+at\s+.+$/i, "");
  // 2. Cut at the first separator. Tests for: |, em-dash, en-dash, " - ",
  //    " · ", " / ". Plain hyphens inside a single word ("Co-founder") are
  //    preserved because we only split on space-flanked variants.
  const sepIdx = s.search(/\s+[|·/]\s+|\s+[—–-]\s+/);
  if (sepIdx >= 0) s = s.slice(0, sepIdx);
  // 3. Collapse whitespace + strip trailing punctuation.
  s = s.replace(/\s+/g, " ").replace(/[,;:.]+$/, "").trim();
  // 4. Length cap.
  if (s.length > 28) s = s.slice(0, 27).trimEnd() + "…";
  return s;
}

// Pre-build a normalized lookup keyed off the LLM-generated meta. ~170
// entries; building it once at module load is fast and lets resolveCompanyMeta
// stay O(1) per call (vs. the previous O(meta-size) scan).
const NORMALIZED_GENERATED_META: Map<string, CompanyMeta> = (() => {
  const out = new Map<string, CompanyMeta>();
  for (const [key, gen] of Object.entries(GENERATED_COMPANY_META)) {
    if (!gen.country || !gen.industry) continue;
    out.set(normalizeCompany(key), {
      country: gen.country,
      state: gen.state || undefined,
      industry: gen.industry,
      partnerships: gen.partnerships?.length ? gen.partnerships : undefined,
    });
  }
  return out;
})();

/**
 * Resolve metadata for a company. Tries the hand-curated COMPANY_META first
 * (so any local overrides win), then falls back to the LLM-generated table
 * which covers ~170 of the 179 distinct companies in the live DB. Returns
 * null only when both miss — callers skip the city/industry edge in that
 * case to avoid Unknown placeholder hubs.
 */
function resolveCompanyMeta(rawName: string): CompanyMeta | null {
  const norm = normalizeCompany(rawName);
  for (const [key, meta] of Object.entries(COMPANY_META)) {
    if (normalizeCompany(key) === norm) return meta;
  }
  return NORMALIZED_GENERATED_META.get(norm) ?? null;
}

// Singleton root node id. Every industry, city, and role rolls up to this
// node so the canvas reads as a clean DAG: Technology → Industry/City →
// Company/Role → Person.
const TECH_ROOT_ID = "industry:technology";
const TECH_ROOT_NAME = "Technology";

function resolvePartnerships(rawName: string): string[] {
  const meta = resolveCompanyMeta(rawName);
  return meta?.partnerships ?? [];
}

// ─── Optional Prospect enrichment fields ─────────────────────────────────────
// `past_companies` / `education` / `talks` are populated by the backend ETL
// (scripts/etl_to_public.py) into denormalized JSONB columns on
// public.prospects per migration 20260426_prospect_enrichment.sql. Mock-mode
// prospects also expose them. Treat every entry as defensively-optional —
// a Supabase row whose ETL hasn't run yet will still return undefined for
// these fields, and graph.ts must not crash on that.

interface EducationEntry {
  school: string;
  degree?: string;
  year?: number;
}
interface TalkEntry {
  venue: string;
  year?: number;
}

type ProspectWithGraphFields = Prospect & {
  past_companies?: string[];
  education?: EducationEntry[];
  talks?: TalkEntry[];
};

// ─── buildGraph ──────────────────────────────────────────────────────────────

export function buildGraph(args: BuildGraphArgs): {
  nodes: GraphNode[];
  edges: GraphEdge[];
} {
  const { prospects, scores } = args;
  const nodes = new Map<string, GraphNode>();
  const edges = new Map<string, GraphEdge>();

  const SYMMETRIC: ReadonlySet<EdgeKind> = new Set<EdgeKind>(["partnership", "colleague"]);

  const addNode = (n: GraphNode): void => {
    if (!nodes.has(n.id)) nodes.set(n.id, n);
  };

  const addEdge = (source: string, target: string, kind: EdgeKind): void => {
    if (source === target) return;
    let a = source;
    let b = target;
    if (SYMMETRIC.has(kind) && a > b) {
      [a, b] = [b, a];
    }
    const id = `${a}|${b}|${kind}`;
    if (!edges.has(id)) {
      edges.set(id, { id, source: a, target: b, kind });
    }
  };

  // Track person→companyId to derive colleague edges in a second pass.
  const peopleByCompany = new Map<string, string[]>();
  // Track role → set of industry ids of its holders' companies, so roles can
  // hang off industries and slot into the DAG hierarchy at level 2 (next to
  // company nodes).
  const roleIndustries = new Map<string, Set<string>>();

  // First pass: person/company/city/industry/past/education/talks/role.
  for (const raw of prospects as ProspectWithGraphFields[]) {
    const personId = `person:${raw._id}`;
    const companyNorm = normalizeCompany(raw.company) || "unknown";
    const companyId = `company:${companyNorm}`;

    addNode({
      id: personId,
      kind: "person",
      name: raw.name,
      role: raw.role,
      companyId,
      score: scores[raw._id]?.overall_score,
      raw,
    });

    // Current company. City still gates on COMPANY_META (we don't have
    // per-prospect city signal yet), but industry now prefers the prospect's
    // own `industry` column — COMPANY_META only seeds ~30 known semis cos,
    // so without this fallback the Industry node degenerated to a single
    // "Semiconductors" hub even though the DB has Health Tech, Defense,
    // Aerospace, Quantum, etc.
    const meta = resolveCompanyMeta(raw.company);
    const cityName = meta ? (meta.state ?? meta.country) : undefined;
    const cityId = cityName ? `city:${normalizeKey(cityName)}` : undefined;
    const industryName =
      (raw.industry && raw.industry.trim()) || meta?.industry || undefined;
    const industryId = industryName
      ? `industry:${normalizeKey(industryName)}`
      : undefined;

    addNode({
      id: companyId,
      kind: "company",
      name: raw.company,
      locationId: cityId,
      industryId,
    });
    if (cityId && cityName && meta) {
      addNode({ id: cityId, kind: "city", name: cityName, country: meta.country });
      addEdge(companyId, cityId, "located_in");
    }
    if (industryId && industryName) {
      addNode({ id: industryId, kind: "industry", name: industryName });
      addEdge(companyId, industryId, "vertical");
    }

    addEdge(personId, companyId, "works_at");

    // Track for colleague edges.
    const bucket = peopleByCompany.get(companyId);
    if (bucket) bucket.push(personId);
    else peopleByCompany.set(companyId, [personId]);

    // Past companies — same Unknown gating as current company.
    for (const past of raw.past_companies ?? []) {
      if (!past) continue;
      const pastNorm = normalizeCompany(past);
      if (!pastNorm) continue;
      const pastId = `company:${pastNorm}`;
      const pastMeta = resolveCompanyMeta(past);
      const pastCityName = pastMeta ? (pastMeta.state ?? pastMeta.country) : undefined;
      const pastCityId = pastCityName ? `city:${normalizeKey(pastCityName)}` : undefined;
      const pastIndustryId =
        pastMeta && pastMeta.industry
          ? `industry:${normalizeKey(pastMeta.industry)}`
          : undefined;
      addNode({
        id: pastId,
        kind: "company",
        name: past,
        locationId: pastCityId,
        industryId: pastIndustryId,
      });
      if (pastCityId && pastCityName && pastMeta) {
        addNode({
          id: pastCityId,
          kind: "city",
          name: pastCityName,
          country: pastMeta.country,
        });
        addEdge(pastId, pastCityId, "located_in");
      }
      if (pastIndustryId && pastMeta) {
        addNode({ id: pastIndustryId, kind: "industry", name: pastMeta.industry });
        addEdge(pastId, pastIndustryId, "vertical");
      }
      addEdge(personId, pastId, "past_employer");
    }

    // Education.
    for (const ed of raw.education ?? []) {
      if (!ed?.school) continue;
      const schoolId = `school:${normalizeKey(ed.school)}`;
      addNode({ id: schoolId, kind: "school", name: ed.school });
      addEdge(personId, schoolId, "education");
    }

    // Conference talks — node id is "venue year" so the same conference in
    // different years stays distinct.
    for (const talk of raw.talks ?? []) {
      if (!talk?.venue) continue;
      const label = talk.year ? `${talk.venue} ${talk.year}` : talk.venue;
      const confId = `conference:${normalizeKey(label)}`;
      addNode({ id: confId, kind: "conference", name: label, year: talk.year });
      addEdge(personId, confId, "scope_signal");
    }

    // Role node — clustered by canonicalized role string (short, dedupe-able
    // form). "Senior Software Engineer | Design Systems at Cadence" and
    // "Senior Software Engineer at Intel" both collapse to "Senior Software
    // Engineer", so we end up with ~tens of role nodes instead of thousands.
    if (raw.role) {
      const canonical = canonicalizeRole(raw.role);
      if (canonical) {
        const roleId = `role:${normalizeKey(canonical)}`;
        addNode({ id: roleId, kind: "role", name: canonical });
        addEdge(personId, roleId, "scope_signal");
        // Track which industry a role's holders work in, so we can later
        // wire role → industry edges (puts roles at the same DAG level as
        // companies).
        if (industryId) {
          const set = roleIndustries.get(roleId) ?? new Set<string>();
          set.add(industryId);
          roleIndustries.set(roleId, set);
        }
      }
    }
  }

  // Second pass: colleague edges (any two persons sharing a companyId).
  // O(k²) per company, k = head-count at that company. Off in agent-context
  // builds where the chat copilot only needs node lookups, not traversal.
  if (!args.skipColleagueEdges) {
    for (const persons of peopleByCompany.values()) {
      for (let i = 0; i < persons.length; i++) {
        for (let j = i + 1; j < persons.length; j++) {
          addEdge(persons[i], persons[j], "colleague");
        }
      }
    }
  }

  // Third pass: partnership edges between companies. Iterate over the
  // companies actually present in the graph (not the full COMPANY_META map)
  // so we don't introduce orphan partner nodes for companies no prospect
  // works at — but DO materialize the partner if it's referenced by a
  // present company.
  const presentCompanyNodes = Array.from(nodes.values()).filter(
    (n): n is GraphNode & { kind: "company" } => n.kind === "company",
  );
  for (const company of presentCompanyNodes) {
    const partners = resolvePartnerships(company.name);
    for (const partner of partners) {
      const partnerNorm = normalizeCompany(partner);
      if (!partnerNorm) continue;
      const partnerId = `company:${partnerNorm}`;
      if (!nodes.has(partnerId)) {
        const partnerMeta = resolveCompanyMeta(partner);
        const partnerCity = partnerMeta ? (partnerMeta.state ?? partnerMeta.country) : undefined;
        const partnerCityId = partnerCity ? `city:${normalizeKey(partnerCity)}` : undefined;
        const partnerIndustryId =
          partnerMeta && partnerMeta.industry
            ? `industry:${normalizeKey(partnerMeta.industry)}`
            : undefined;
        addNode({
          id: partnerId,
          kind: "company",
          name: partner,
          locationId: partnerCityId,
          industryId: partnerIndustryId,
        });
        if (partnerCityId && partnerCity && partnerMeta) {
          addNode({
            id: partnerCityId,
            kind: "city",
            name: partnerCity,
            country: partnerMeta.country,
          });
          addEdge(partnerId, partnerCityId, "located_in");
        }
        if (partnerIndustryId && partnerMeta) {
          addNode({
            id: partnerIndustryId,
            kind: "industry",
            name: partnerMeta.industry,
          });
          addEdge(partnerId, partnerIndustryId, "vertical");
        }
      }
      addEdge(company.id, partnerId, "partnership");
    }
  }

  // Hierarchy pass: add the Technology root + roll every industry, city, and
  // role up to it. Direction matters for DAG layout — `addEdge(child, root)`
  // means the child sits BELOW the root in dagMode="bu" (bottom-up).
  // Skip if there are no companies at all (nothing meaningful to hang).
  if (presentCompanyNodes.length > 0) {
    addNode({ id: TECH_ROOT_ID, kind: "industry", name: TECH_ROOT_NAME });
    for (const node of nodes.values()) {
      if (node.id === TECH_ROOT_ID) continue;
      if (node.kind === "industry") addEdge(node.id, TECH_ROOT_ID, "vertical");
      if (node.kind === "city") addEdge(node.id, TECH_ROOT_ID, "located_in");
    }
    // Roles → their holders' industries (puts roles at level 2 alongside
    // companies). Falls back to direct → Technology if a role has no
    // resolvable industry (rare — only when every holder works at an
    // unknown company).
    for (const [roleId, industries] of roleIndustries) {
      if (industries.size === 0) {
        addEdge(roleId, TECH_ROOT_ID, "vertical");
      } else {
        for (const industryId of industries) {
          addEdge(roleId, industryId, "vertical");
        }
      }
    }
  }

  // Optional fourth pass: scope_signal edges from per-prospect signals. We
  // don't materialize signals as nodes (would balloon the graph), but if a
  // future caller passes signalsById we attach a synthetic evidence_cited
  // edge from person → role to flag "this person has supporting evidence".
  if (args.signalsById) {
    for (const [prospectId, sigs] of Object.entries(args.signalsById)) {
      if (!sigs?.length) continue;
      const personId = `person:${prospectId}`;
      if (!nodes.has(personId)) continue;
      const personNode = nodes.get(personId);
      if (!personNode || personNode.kind !== "person") continue;
      const roleId = `role:${normalizeKey(personNode.role)}`;
      if (nodes.has(roleId)) {
        addEdge(personId, roleId, "evidence_cited");
      }
    }
  }

  // Pre-bake colors so ForceGraph2D can read them as property names instead
  // of invoking a callback per-tick per-element.
  const nodeArr = Array.from(nodes.values());
  const edgeArr = Array.from(edges.values());
  if (args.theme) {
    const { nodeColors, edgeColors } = args.theme;
    for (const n of nodeArr) n.color = nodeColors[n.kind];
    for (const e of edgeArr) e.color = edgeColors[e.kind];
  }

  return { nodes: nodeArr, edges: edgeArr };
}
