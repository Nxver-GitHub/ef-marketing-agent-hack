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
import {
  STRENGTH_TABLE,
  DECAY_RATES,
  type ConnectionType,
} from "./strength";
import {
  evidenceFromSignal,
  RECOGNIZED_CONNECTION_SIGNAL_TYPES,
} from "./evidenceFromSignal";

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
  | "evidence_cited"
  // Hidden-connection edges (CLAUDE.md v3 §"Current State" / §"What is missing").
  // Sourced from public records (USPTO, Semantic Scholar, conference programs,
  // standards rosters) — the warm-path engine reads these with the highest
  // base strengths in STRENGTH_TABLE. Renderers + filters fall through to the
  // EdgeKind exhaustive switches, so adding here gates downstream wiring.
  | "patent_co_inventor"
  | "academic_co_author"
  | "conference_co_presenter"
  | "standards_committee"
  // Education-cohort kinds (V3_PT2.md L376-381). Surfaced from PDL
  // education[] data. Same EDGE_CONFIGS pattern; baseStrength + decayRate
  // delegated to STRENGTH_TABLE / DECAY_RATES via warmFromTable().
  | "same_mba_cohort"
  | "same_phd_program"
  | "executive_education"
  | "same_undergrad_cohort";

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
  /**
   * Optional evidence backing this edge. Shapes mirror the
   * `structured_value` payloads in CONTRACTS.md Contract 1 §"structured_value
   * shapes". Populated when an extractor (Track J) writes a real signal;
   * absent for v2 mock graph data and the placeholder demo edges.
   *
   * Consumers (warmPaths.ts explanation/opener generators) MUST tolerate
   * `evidence === undefined` and fall back to generic strings — never
   * fabricate values to fill missing fields (CLAUDE.md "Common Mistakes" #6).
   */
  evidence?: EdgeEvidence | null;
};

// ─── EdgeEvidence — discriminated union per Contract 1 ──────────────────────
//
// Each variant's `kind` matches both the CONTRACTS.md Contract 1 `signal_type`
// and the EdgeKind. When extractors land (J.4 USPTO, J.5 Scholar, future
// standards/conference scrapers), they marshal Contract 1 `structured_value`
// dicts into these shapes and attach them to the GraphEdge they emit.
//
// Field naming: camelCase here (frontend convention). When the bridge from
// the backend's snake_case structured_value to this shape is wired (a future
// data-loader concern), it can use a small mapping function — kept out of
// this type so the type itself stays pure.

export interface PatentCoInventorEvidence {
  readonly kind: "patent_co_inventor";
  /** US patent number, e.g. "10,234,567". */
  readonly patentNumber: string;
  /** Patent title from the USPTO record. */
  readonly patentTitle: string;
  /** ISO date string (YYYY-MM-DD). */
  readonly filingDate: string;
  /** ISO date string; null when not yet granted. */
  readonly grantDate?: string | null;
  /** Assignee organization (the company that holds the patent). */
  readonly assignee: string;
  /** Public USPTO URL for citation. */
  readonly usptoUrl?: string | null;
}

export interface AcademicCoAuthorEvidence {
  readonly kind: "academic_co_author";
  readonly paperTitle: string;
  /** Venue (conference / journal name). */
  readonly venue: string;
  readonly year: number;
  /** Citation count at time of extraction. */
  readonly citationCount: number;
  readonly semanticScholarId?: string | null;
  readonly doi?: string | null;
}

export interface ConferenceCoPresenterEvidence {
  readonly kind: "conference_co_presenter";
  /** Event name, e.g., "SPIE Advanced Lithography 2024". */
  readonly event: string;
  readonly year: number;
}

export interface StandardsCommitteeEvidence {
  readonly kind: "standards_committee";
  /** Committee name, e.g., "JEDEC JC-42.4 / Memory Module Subcommittee". */
  readonly committee: string;
  /** Active years window, e.g., "2018-2022" (free-form for now). */
  readonly years: string;
}

export interface CareerOverlapEvidence {
  /** Catches all three career_overlap_* sub-types. The connection_type lives
   *  on the GraphEdge.kind via `colleague` / `past_employer` already; this
   *  evidence shape carries the per-overlap details. */
  readonly kind: "career_overlap";
  readonly companyName: string;
  readonly overlapStartYear: number;
  readonly overlapEndYear: number;
  readonly overlapYears: number;
  readonly teamA?: string | null;
  readonly teamB?: string | null;
  readonly domainA?: string | null;
  readonly domainB?: string | null;
  readonly seniorityGap?: number | null;
}

export interface EducationCohortEvidence {
  /** Catches all 4 education-cohort sub-types
   *  (same_mba_cohort, same_phd_program, executive_education,
   *  same_undergrad_cohort). The specific kind is on GraphEdge.kind. */
  readonly kind: "education_cohort";
  readonly institution: string;
  readonly program?: string | null;
  readonly overlapStartYear?: number | null;
  readonly overlapEndYear?: number | null;
}

export type EdgeEvidence =
  | PatentCoInventorEvidence
  | AcademicCoAuthorEvidence
  | ConferenceCoPresenterEvidence
  | StandardsCommitteeEvidence
  | CareerOverlapEvidence
  | EducationCohortEvidence;

export interface ThemeTokens {
  nodeColors: Record<NodeKind, string>;
  edgeColors: Record<EdgeKind, string>;
}

// ─── EDGE_CONFIGS — single source of truth for edge metadata ──────────────────
//
// Per CONTRACTS.md Contract 3 §"Single source of truth": every consumer of
// EdgeKind metadata (display labels, CSS variable lookups, default visibility,
// strength model wiring) reads from this one record. Adding a new edge kind
// requires four updates IN THIS ORDER:
//   1. Add to the `EdgeKind` union above
//   2. Add a row to `EDGE_CONFIGS` here
//   3. Add a `--edge-<slug>` CSS variable in `src/index.css` (both `:root` and `.dark`)
//   4. Done — TopBar pills, canvas labels, NodeInspector pills, warmPaths.ts
//      strength lookups all derive from this record. No other files need edits.
//
// `connectionType` maps the edge to a key in strength.ts's `STRENGTH_TABLE` so
// the warm-path BFS can look up `baseStrength` and `decayRate` without
// duplicating the values here. Structural edges (works_at, located_in, etc.)
// have `connectionType: null` and are excluded from warm-path traversal.

export interface EdgeConfig {
  readonly kind: EdgeKind;
  /** Long-form label, used in NodeInspector pills + Discover legend block. */
  readonly displayLabel: string;
  /** Compact uppercase label, used for canvas mid-edge tags + TopBar pills. */
  readonly displayLabelShort: string;
  /** Slug used to derive the CSS variable name (`--edge-<slug>`). */
  readonly slug: string;
  /** CSS custom property name; must match a definition in src/index.css. */
  readonly cssVarName: string;
  /** Initial visibility in the TopBar filter. */
  readonly defaultVisible: boolean;
  /** Whether warmPaths.ts should traverse this edge by default (baseStrength >= 0.50). */
  readonly isWarmByDefault: boolean;
  /** Mapping into strength.ts. Null for structural edges; warm kinds map to a real key. */
  readonly connectionType: ConnectionType | null;
  /** Base strength from STRENGTH_TABLE; 0 for structural edges. */
  readonly baseStrength: number;
  /** Decay rate per year inactive from DECAY_RATES; 0 for structural edges. */
  readonly decayRate: number;
  /** Suppress canvas mid-edge labels (e.g., colleague would carpet the graph). */
  readonly suppressCanvasLabel: boolean;
}

function warmFromTable(type: ConnectionType, suppressCanvasLabel = false) {
  const baseStrength = STRENGTH_TABLE[type];
  const decayRate = DECAY_RATES[type];
  return {
    isWarmByDefault: baseStrength >= 0.5,
    connectionType: type,
    baseStrength,
    decayRate,
    suppressCanvasLabel,
  } as const;
}

const STRUCTURAL = {
  isWarmByDefault: false,
  connectionType: null,
  baseStrength: 0,
  decayRate: 0,
  suppressCanvasLabel: false,
} as const;

export const EDGE_CONFIGS: Readonly<Record<EdgeKind, EdgeConfig>> = Object.freeze({
  works_at: {
    kind: "works_at",
    displayLabel: "Employer",
    displayLabelShort: "WORKS AT",
    slug: "employer",
    cssVarName: "--edge-employer",
    defaultVisible: true,
    ...STRUCTURAL,
  },
  colleague: {
    kind: "colleague",
    displayLabel: "Colleague",
    displayLabelShort: "COLLEAGUE",
    slug: "employer", // borrows employer color; no own CSS var
    cssVarName: "--edge-employer",
    defaultVisible: true,
    // Suppressed on canvas: would carpet the layout with redundant tags
    // between every pair of co-workers.
    ...warmFromTable("career_overlap_same_team", true),
  },
  located_in: {
    kind: "located_in",
    displayLabel: "Location",
    displayLabelShort: "LOCATED IN",
    slug: "location",
    cssVarName: "--edge-location",
    defaultVisible: true,
    ...STRUCTURAL,
  },
  reports_to: {
    kind: "reports_to",
    displayLabel: "Reports",
    displayLabelShort: "REPORTS",
    slug: "reports",
    cssVarName: "--edge-reports",
    defaultVisible: true,
    ...STRUCTURAL,
  },
  past_employer: {
    kind: "past_employer",
    displayLabel: "Past empl.",
    displayLabelShort: "EX",
    slug: "past-empl",
    cssVarName: "--edge-past-empl",
    defaultVisible: true,
    ...warmFromTable("career_overlap_general"),
  },
  partnership: {
    kind: "partnership",
    displayLabel: "Partnership",
    displayLabelShort: "PARTNER",
    slug: "partnership",
    cssVarName: "--edge-partnership",
    defaultVisible: true,
    ...STRUCTURAL,
  },
  education: {
    kind: "education",
    displayLabel: "Education",
    displayLabelShort: "EDUCATED AT",
    slug: "education",
    cssVarName: "--edge-education",
    defaultVisible: true,
    // alumni_network's baseStrength (0.25) is below the warm threshold, so
    // warmPaths.ts won't traverse education edges by default. Still rendered.
    ...warmFromTable("alumni_network"),
  },
  scope_signal: {
    kind: "scope_signal",
    displayLabel: "Scope",
    displayLabelShort: "SCOPE",
    slug: "scope",
    cssVarName: "--edge-scope",
    defaultVisible: true,
    ...STRUCTURAL,
  },
  vertical: {
    kind: "vertical",
    displayLabel: "Vertical",
    displayLabelShort: "VERTICAL",
    slug: "vertical",
    cssVarName: "--edge-vertical",
    defaultVisible: true,
    ...STRUCTURAL,
  },
  evidence_cited: {
    kind: "evidence_cited",
    displayLabel: "Evidence",
    displayLabelShort: "EVIDENCE",
    slug: "evidence",
    cssVarName: "--edge-evidence",
    defaultVisible: true,
    ...STRUCTURAL,
  },
  // Hidden-connection edges (v3 warm-path engine).
  patent_co_inventor: {
    kind: "patent_co_inventor",
    displayLabel: "Patent",
    displayLabelShort: "PATENT",
    slug: "patent",
    cssVarName: "--edge-patent",
    defaultVisible: true,
    ...warmFromTable("patent_co_inventor"),
  },
  academic_co_author: {
    kind: "academic_co_author",
    displayLabel: "Co-author",
    displayLabelShort: "CO-AUTHOR",
    slug: "coauthor",
    cssVarName: "--edge-coauthor",
    defaultVisible: true,
    // Default to the single-paper variant; multi-paper detection happens at
    // edge-write time when corroboration count is known.
    ...warmFromTable("academic_co_author_single"),
  },
  standards_committee: {
    kind: "standards_committee",
    displayLabel: "Standards",
    displayLabelShort: "STANDARDS",
    slug: "standards",
    cssVarName: "--edge-standards",
    defaultVisible: true,
    ...warmFromTable("standards_committee_peer"),
  },
  conference_co_presenter: {
    kind: "conference_co_presenter",
    displayLabel: "Conference",
    displayLabelShort: "CONFERENCE",
    slug: "conference",
    cssVarName: "--edge-conference",
    defaultVisible: true,
    ...warmFromTable("conference_co_presenter"),
  },
  // ── Education-cohort kinds (V3_PT2.md §"New Edge Kinds") ─────────────────
  same_mba_cohort: {
    kind: "same_mba_cohort",
    displayLabel: "MBA Cohort",
    displayLabelShort: "MBA",
    slug: "same-mba-cohort",
    cssVarName: "--edge-same-mba-cohort",
    defaultVisible: true,
    ...warmFromTable("same_mba_cohort"),
  },
  same_phd_program: {
    kind: "same_phd_program",
    displayLabel: "PhD Program",
    displayLabelShort: "PHD",
    slug: "same-phd-program",
    cssVarName: "--edge-same-phd-program",
    defaultVisible: true,
    ...warmFromTable("same_phd_program"),
  },
  executive_education: {
    kind: "executive_education",
    displayLabel: "Executive Education",
    displayLabelShort: "EXEC ED",
    slug: "executive-education",
    cssVarName: "--edge-executive-education",
    defaultVisible: true,
    ...warmFromTable("executive_education"),
  },
  same_undergrad_cohort: {
    kind: "same_undergrad_cohort",
    displayLabel: "Undergrad Cohort",
    displayLabelShort: "UNDERGRAD",
    slug: "same-undergrad-cohort",
    cssVarName: "--edge-same-undergrad-cohort",
    defaultVisible: true,
    ...warmFromTable("same_undergrad_cohort"),
    // Promoted to warm-by-default 2026-04-30. The earlier V3_PT2.md L421
    // override held undergrad cohorts back pending school-size + same-major
    // refinement, but the bulk_education_signals output (283 cohort edges
    // shipped, 204 of which are undergrad) is the dominant cohort source
    // today. Without this flip, every Hock-Tan-class node ("WARM PATHS: 0")
    // looks edgeless to warm-path BFS even though the data is there.
    // baseStrength remains 0.62 — above the 0.5 threshold. School-size
    // refinement is a future filter at edge-write time, not a gate on
    // traversal.
  },
});

/** Every defined edge kind, in declaration order. Stable iteration order — UIs rely on it. */
export const ALL_EDGE_KINDS: ReadonlyArray<EdgeKind> = Object.freeze(
  Object.keys(EDGE_CONFIGS) as EdgeKind[],
);

/**
 * Default warm-set: kinds whose baseStrength is high enough to be worth
 * traversing in `findWarmPaths`. Equivalent to `[k for k in ALL_EDGE_KINDS
 * if EDGE_CONFIGS[k].isWarmByDefault]`. Kept as a precomputed export so
 * warmPaths.ts doesn't have to build it on every call.
 */
export const DEFAULT_WARM_EDGE_KINDS: ReadonlyArray<EdgeKind> = Object.freeze(
  ALL_EDGE_KINDS.filter((k) => EDGE_CONFIGS[k].isWarmByDefault),
);

export interface BuildGraphArgs {
  prospects: Prospect[];
  scores: Record<string, Score>;
  signalsById?: Record<string, Signal[]>;
  /**
   * Optional pre-materialized person↔person connection records, keyed by the
   * "from" prospect_id (the row already filtered to the current viewer's set).
   * Sourced from the `person_connections` table (CLAUDE.md Decision 7) via
   * `usePersonConnections` in db.ts. The hook owns the persons↔prospects ID
   * translation; buildGraph consumes prospect-id-shaped records exclusively.
   *
   * Each record carries `connection_type` (matching STRENGTH_TABLE keys),
   * `connected_prospect_id` (the partner endpoint), `computed_strength`
   * (0..0.99 from `compute_strength`), and the same Contract-1-shaped
   * `structured_value` payload that `signalsById` carries — this lets the
   * sixth pass reuse `evidenceFromSignal` + `signalTypeToEdgeKind` without
   * adding a parallel mapping path.
   */
  personConnections?: Record<string, PersonConnectionRecord[]>;
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

/**
 * A row from `person_connections` (Postgres) translated into prospect-id
 * space by `usePersonConnections`. The fields mirror Contract 1's
 * structured_value shapes so `evidenceFromSignal` works unmodified.
 */
export interface PersonConnectionRecord {
  readonly connected_prospect_id: string;
  readonly connection_type: string;
  readonly computed_strength: number;
  readonly structured_value: Record<string, unknown>;
}

/**
 * Per-prospect cap on edges materialized from `person_connections`. A single
 * dense node (e.g., a long-tenured engineer at a 5,000-person company) can
 * have hundreds of `career_overlap_*` rows — rendering all of them buries
 * the higher-strength edges that drive warm-path BFS. Cap at top-K by
 * `computed_strength` desc. Picked at 8: matches `COLLEAGUE_FANOUT = 6`
 * order-of-magnitude while leaving room for two cross-domain links.
 */
export const PERSON_CONNECTIONS_TOP_K = 8;

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
export function normalizeCompany(s: string | null | undefined): string {
  return (s ?? "")
    .toLowerCase()
    .replace(
      /\b(corp\.?|corporation|inc\.?|incorporated|limited|ltd\.?|llc|plc|technologies|technology|semiconductor|semiconductors|systems?)\b/g,
      "",
    )
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

export function normalizeKey(s: string): string {
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
export function resolveCompanyMeta(rawName: string): CompanyMeta | null {
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

// ─── Signal-type → EdgeKind mapping (Contract 1 → frontend EdgeKind) ────────
//
// Contract 1 returns signal_type values that are 1:1 with EdgeKind for the
// 4 hidden-connection types. The 3 career_overlap sub-types fold onto two
// EdgeKind variants based on tenure: same_team → colleague (current/strong),
// same_domain & general → past_employer (looser overlap). Returns null for
// signal types that don't map to a renderable hidden-connection edge.

function signalTypeToEdgeKind(signalType: string): EdgeKind | null {
  switch (signalType) {
    case "patent_co_inventor":
      return "patent_co_inventor";
    case "academic_co_author":
    case "academic_co_author_single":
    case "academic_co_author_multi":
      return "academic_co_author";
    case "conference_co_presenter":
      return "conference_co_presenter";
    case "standards_committee":
    case "standards_committee_peer":
      return "standards_committee";
    case "career_overlap_same_team":
      return "colleague";
    case "career_overlap_same_domain":
    case "career_overlap_general":
      return "past_employer";
    // Education-cohort kinds (V3_PT2.md L376-381) — 1:1 with EdgeKind.
    case "same_mba_cohort":
      return "same_mba_cohort";
    case "same_phd_program":
      return "same_phd_program";
    case "executive_education":
      return "executive_education";
    case "same_undergrad_cohort":
      return "same_undergrad_cohort";
    default:
      return null;
  }
}

// ─── buildGraph ──────────────────────────────────────────────────────────────

export function buildGraph(args: BuildGraphArgs): {
  nodes: GraphNode[];
  edges: GraphEdge[];
} {
  const { prospects, scores } = args;
  const nodes = new Map<string, GraphNode>();
  const edges = new Map<string, GraphEdge>();

  // Hidden-connection edges (patent_co_inventor / academic_co_author /
  // conference_co_presenter / standards_committee) are inherently undirected:
  // a co-invention between A and B is the same fact as one between B and A,
  // so dedup must canonicalize endpoints. They join `partnership` and
  // `colleague` in the symmetric set.
  //
  // Education-cohort kinds (same_mba_cohort, same_phd_program,
  // executive_education, same_undergrad_cohort) are also symmetric: if A and B
  // shared an MBA cohort the connection is the same regardless of direction.
  // Without this, a clustering job that emits both A→B and B→A rows (which
  // the idempotent upsert produces) would land two edges in the graph.
  //
  // past_employer is included because career_overlap signals between two
  // persons are written from both sides (signal for A says connected_to=B,
  // signal for B says connected_to=A) — both map to past_employer EdgeKind
  // and would produce two anti-parallel edges without canonicalization.
  // The directed person→company past_employer edges are unaffected: the
  // canonicalization only swaps source/target when source > target; the
  // resulting edge id is still valid for undirected dedup.
  const SYMMETRIC: ReadonlySet<EdgeKind> = new Set<EdgeKind>([
    "partnership",
    "colleague",
    "past_employer",
    "patent_co_inventor",
    "academic_co_author",
    "conference_co_presenter",
    "standards_committee",
    "same_mba_cohort",
    "same_phd_program",
    "executive_education",
    "same_undergrad_cohort",
  ]);

  const addNode = (n: GraphNode): void => {
    if (!nodes.has(n.id)) nodes.set(n.id, n);
  };

  const addEdge = (
    source: string,
    target: string,
    kind: EdgeKind,
    evidence?: EdgeEvidence | null,
  ): void => {
    if (source === target) return;
    let a = source;
    let b = target;
    if (SYMMETRIC.has(kind) && a > b) {
      [a, b] = [b, a];
    }
    const id = `${a}|${b}|${kind}`;
    const existing = edges.get(id);
    if (existing) {
      // First-write-wins on evidence: if the existing edge already has rich
      // evidence, don't overwrite with sparser evidence from the reverse-
      // direction signal. If the existing has none and we have some, fill in.
      if (evidence != null && existing.evidence == null) {
        existing.evidence = evidence;
      }
      return;
    }
    edges.set(id, {
      id,
      source: a,
      target: b,
      kind,
      ...(evidence != null ? { evidence } : {}),
    });
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

  // Second pass: colleague edges. Naïve O(n²) (every pair) explodes the
  // canvas for any company with >50 prospects (Micron has 428 → 91k edges,
  // which freezes the layout solver). Cap to a small per-person fan-out
  // instead — each person connects to up to COLLEAGUE_FANOUT others at the
  // same company. Off entirely in agent-context builds (`skipColleagueEdges`)
  // where the chat copilot only needs node lookups, not traversal.
  if (!args.skipColleagueEdges) {
    const COLLEAGUE_FANOUT = 6;
    for (const persons of peopleByCompany.values()) {
      if (persons.length <= 1) continue;
      if (persons.length <= COLLEAGUE_FANOUT + 1) {
        for (let i = 0; i < persons.length; i++) {
          for (let j = i + 1; j < persons.length; j++) {
            addEdge(persons[i], persons[j], "colleague");
          }
        }
        continue;
      }
      for (let i = 0; i < persons.length; i++) {
        const limit = Math.min(COLLEAGUE_FANOUT, persons.length - 1 - i);
        for (let k = 1; k <= limit; k++) {
          addEdge(persons[i], persons[i + k], "colleague");
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

  // Optional fifth pass: hidden-connection edges from per-prospect signals.
  //
  // When a Contract-1-shaped signal lands in `signalsById` (i.e., from
  // SunnyRidge's Track J extractors writing patent_co_inventor /
  // academic_co_author / conference_co_presenter / standards_committee /
  // career_overlap_* rows), we materialize a GraphEdge between the two
  // prospects with `evidence` populated via `evidenceFromSignal`. The
  // resulting edges feed `findWarmPaths` (Track I) so WarmPathPanel renders
  // CLAUDE.md's rich templates without further code changes.
  //
  // Duck-typed signal access — the v2 mock `Signal` (in mockStore.ts) uses
  // `value: unknown` and won't have `structured_value`; those signals are
  // silently skipped by `evidenceFromSignal`. v3 NormalizedSignal-shaped
  // rows from the live Supabase signals table will have `structured_value`
  // and round-trip cleanly. No code change required when v2/v3 cohabit.
  if (args.signalsById) {
    for (const [prospectId, sigs] of Object.entries(args.signalsById)) {
      if (!sigs?.length) continue;
      const personA = `person:${prospectId}`;
      if (!nodes.has(personA)) continue;

      for (const sig of sigs) {
        const sigAny = sig as Signal & {
          signal_type?: string;
          structured_value?: Record<string, unknown>;
        };
        const sigType = sigAny.signal_type;
        if (!sigType || !RECOGNIZED_CONNECTION_SIGNAL_TYPES.includes(sigType)) {
          continue;
        }
        // The v3 backend writes Contract 1's `structured_value` payload into
        // the existing v2 `signals.value` JSONB column (server/credence/
        // signals.py:184) — the column was renamed-via-content rather than
        // via ALTER TABLE. So when the frontend reads a live signal, the
        // structured dict arrives as `signal.value`, not `signal.structured_value`.
        // Tolerate both shapes: prefer `structured_value` when present
        // (any future Pydantic-shaped feed), fall back to `value` when it
        // is an object (the current v3-via-v2-column case).
        let sv: Record<string, unknown> | null = null;
        if (sigAny.structured_value && typeof sigAny.structured_value === "object") {
          sv = sigAny.structured_value;
        } else if (
          sigAny.value &&
          typeof sigAny.value === "object" &&
          !Array.isArray(sigAny.value)
        ) {
          sv = sigAny.value as Record<string, unknown>;
        }
        if (!sv) continue;

        const connectedTo =
          typeof sv.connected_to === "string" ? sv.connected_to : null;
        if (!connectedTo) continue;

        const personB = `person:${connectedTo}`;
        if (!nodes.has(personB)) continue; // target not in this graph view

        const edgeKind = signalTypeToEdgeKind(sigType);
        if (!edgeKind) continue;

        const evidence = evidenceFromSignal({
          signal_type: sigType,
          structured_value: sv,
        });
        addEdge(personA, personB, edgeKind, evidence);
      }
    }
  }

  // Sixth pass: pre-materialized person_connections rows (CLAUDE.md
  // Decision 7). The hook layer (`usePersonConnections` in db.ts) has
  // already done the persons.id → prospects.id translation and filtered
  // to the current view. We just need to:
  //   1. Cap each prospect to the top-K records by computed_strength desc
  //      (PERSON_CONNECTIONS_TOP_K) so a dense node doesn't bury its
  //      strongest edges under hundreds of weak career_overlap_general rows
  //   2. Map connection_type → EdgeKind via the same `signalTypeToEdgeKind`
  //      already used by the fifth pass — keeps a single source of truth
  //   3. Reuse evidenceFromSignal so the same warmPaths.ts templates render
  //
  // `addEdge` already canonicalizes endpoints for SYMMETRIC kinds, so a row
  // present on both `prospectA → prospectB` and `prospectB → prospectA` (the
  // hook returns each direction in its respective bucket) dedupes naturally.
  if (args.personConnections) {
    for (const [prospectId, records] of Object.entries(args.personConnections)) {
      if (!records?.length) continue;
      const personA = `person:${prospectId}`;
      if (!nodes.has(personA)) continue;

      const ranked = records.length <= PERSON_CONNECTIONS_TOP_K
        ? records
        : [...records]
            .sort((a, b) => b.computed_strength - a.computed_strength)
            .slice(0, PERSON_CONNECTIONS_TOP_K);

      for (const rec of ranked) {
        const personB = `person:${rec.connected_prospect_id}`;
        if (!nodes.has(personB)) continue;

        const edgeKind = signalTypeToEdgeKind(rec.connection_type);
        if (!edgeKind) continue;

        const evidence = evidenceFromSignal({
          signal_type: rec.connection_type,
          structured_value: rec.structured_value,
        });
        addEdge(personA, personB, edgeKind, evidence);
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
