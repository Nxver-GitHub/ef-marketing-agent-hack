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
 * Resolve COMPANY_META entry by normalized name. Falls back to an
 * Unknown/Unknown placeholder so every company still produces a city +
 * industry edge (graph stays connected).
 */
function resolveCompanyMeta(rawName: string): CompanyMeta {
  const norm = normalizeCompany(rawName);
  for (const [key, meta] of Object.entries(COMPANY_META)) {
    if (normalizeCompany(key) === norm) return meta;
  }
  return { country: "Unknown", industry: "Unknown" };
}

function resolvePartnerships(rawName: string): string[] {
  const norm = normalizeCompany(rawName);
  for (const [key, meta] of Object.entries(COMPANY_META)) {
    if (normalizeCompany(key) === norm) return meta.partnerships ?? [];
  }
  return [];
}

// ─── Optional Prospect fields injected by the mock_enrichment subagent ───────
// `past_companies`, `education`, `talks` are being added in parallel; treat
// every entry as defensively-optional. Define a local widened type rather
// than touching mockStore.ts.

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

    // Current company.
    const meta = resolveCompanyMeta(raw.company);
    const cityName = meta.state ?? meta.country;
    const cityId = `city:${normalizeKey(cityName)}`;
    const industryId = `industry:${normalizeKey(meta.industry)}`;

    addNode({
      id: companyId,
      kind: "company",
      name: raw.company,
      locationId: cityId,
      industryId,
    });
    addNode({ id: cityId, kind: "city", name: cityName, country: meta.country });
    addNode({ id: industryId, kind: "industry", name: meta.industry });

    addEdge(personId, companyId, "works_at");
    addEdge(companyId, cityId, "located_in");
    addEdge(companyId, industryId, "vertical");

    // Track for colleague edges.
    const bucket = peopleByCompany.get(companyId);
    if (bucket) bucket.push(personId);
    else peopleByCompany.set(companyId, [personId]);

    // Past companies — each becomes a company node + past_employer edge from
    // the person, plus a vertical edge if we know the industry.
    for (const past of raw.past_companies ?? []) {
      if (!past) continue;
      const pastNorm = normalizeCompany(past);
      if (!pastNorm) continue;
      const pastId = `company:${pastNorm}`;
      const pastMeta = resolveCompanyMeta(past);
      const pastCityName = pastMeta.state ?? pastMeta.country;
      const pastCityId = `city:${normalizeKey(pastCityName)}`;
      const pastIndustryId = `industry:${normalizeKey(pastMeta.industry)}`;
      addNode({
        id: pastId,
        kind: "company",
        name: past,
        locationId: pastCityId,
        industryId: pastIndustryId,
      });
      addNode({
        id: pastCityId,
        kind: "city",
        name: pastCityName,
        country: pastMeta.country,
      });
      addNode({ id: pastIndustryId, kind: "industry", name: pastMeta.industry });
      addEdge(personId, pastId, "past_employer");
      addEdge(pastId, pastCityId, "located_in");
      addEdge(pastId, pastIndustryId, "vertical");
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

    // Role node — exact-match clustering on lowercased role string. Every
    // person with the same role string ends up sharing one role node.
    if (raw.role) {
      const roleId = `role:${normalizeKey(raw.role)}`;
      addNode({ id: roleId, kind: "role", name: raw.role });
      addEdge(personId, roleId, "scope_signal");
    }
  }

  // Second pass: colleague edges (any two persons sharing a companyId).
  for (const persons of peopleByCompany.values()) {
    for (let i = 0; i < persons.length; i++) {
      for (let j = i + 1; j < persons.length; j++) {
        addEdge(persons[i], persons[j], "colleague");
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
        const partnerCity = partnerMeta.state ?? partnerMeta.country;
        const partnerCityId = `city:${normalizeKey(partnerCity)}`;
        const partnerIndustryId = `industry:${normalizeKey(partnerMeta.industry)}`;
        addNode({
          id: partnerId,
          kind: "company",
          name: partner,
          locationId: partnerCityId,
          industryId: partnerIndustryId,
        });
        addNode({
          id: partnerCityId,
          kind: "city",
          name: partnerCity,
          country: partnerMeta.country,
        });
        addNode({
          id: partnerIndustryId,
          kind: "industry",
          name: partnerMeta.industry,
        });
        addEdge(partnerId, partnerCityId, "located_in");
        addEdge(partnerId, partnerIndustryId, "vertical");
      }
      addEdge(company.id, partnerId, "partnership");
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
