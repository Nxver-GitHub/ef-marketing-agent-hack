/**
 * Warm-path BFS engine — Contract 2 implementation.
 *
 * Pure function. Walks a small in-memory graph and returns the top-K
 * highest-strength paths from a target node back to one or more source
 * nodes, traversing only edges whose kind is in the warm set.
 *
 * References:
 * - CONTRACTS.md → Contract 2 (signature, invariants, test conditions)
 * - CLAUDE.md → "Warm Path Engine" §, "Suggested Opener Generation" §
 * - src/lib/strength.ts (Track H) — STRENGTH_TABLE, ConnectionType
 * - src/lib/graph.ts (Track G) — EdgeKind, GraphNode, GraphEdge
 *
 * Notes on the algorithm — Contract 2 names it "BFS from each source." The
 * implementation here is DFS with a visited set. They produce the same set
 * of (path-from-source-to-target, strength) tuples for this problem because
 * the result set is unordered before sort/dedup; depth is bounded by
 * `maxHops`, and pruning is on path strength, not on layer index. DFS is
 * easier to reason about with the explicit visited-set + strength bookkeeping
 * needed by the Contract 2 invariants. The downstream observable behavior
 * (output shape, ordering after sort/dedup) is identical.
 */

import {
  DEFAULT_WARM_EDGE_KINDS,
  EDGE_CONFIGS,
  type EdgeEvidence,
  type EdgeKind,
  type GraphEdge,
  type GraphNode,
} from "./graph";

// ── Public types ─────────────────────────────────────────────────────────────

export interface WarmPathOptions {
  /** Max edges per path. Default 3, clamped to [1, 5]. */
  maxHops?: number;
  /** Minimum total path strength to retain. Default 0.30, clamped to (0, 1]. */
  minStrength?: number;
  /** Edge kinds the BFS may traverse. Default = kinds with baseStrength >= 0.50. */
  warmEdgeKinds?: EdgeKind[];
  /** Maximum number of paths returned after sort + dedup. Default 10. */
  topK?: number;
  /** How to deduplicate paths covering the same nodes. Default "node-set". */
  dedupePolicy?: "node-set" | "edge-set";
}

export interface WarmPath {
  /** Ordered: [sourceNode, ...intermediates, targetNode]. */
  nodes: GraphNode[];
  /** edges[i] connects nodes[i] and nodes[i+1]. Length === nodes.length - 1. */
  edges: GraphEdge[];
  /** Product of edge strengths along the path; in [0, 0.99]. */
  strength: number;
  /** Number of edges in the path. */
  hopCount: number;
  /** Specific, human-readable description of the connection. */
  explanation: string;
  /** Outreach email opening sentence; references the actual connection. */
  suggested_opener: string;
}

// ── Edge strength via EDGE_CONFIGS (Contract 3) ──────────────────────────────
//
// Per Contract 3 §"Single source of truth", `graph.ts` is the canonical home
// for edge metadata. Each EdgeKind has `baseStrength` + `connectionType` +
// `isWarmByDefault` populated from `STRENGTH_TABLE` and `DECAY_RATES` in
// strength.ts. Structural edges (works_at, located_in, etc.) have
// `baseStrength: 0` and are filtered out of the warm set automatically.
//
// `partnership` is a company-to-company edge with `connectionType: null`;
// the BFS traverses person-to-person semantics only and ignores structural
// edges via the warm-set filter.

function strengthForEdge(edge: GraphEdge): number {
  return EDGE_CONFIGS[edge.kind]?.baseStrength ?? 0;
}

// ── Defaults + clamping ──────────────────────────────────────────────────────

const DEFAULT_MAX_HOPS = 3;
const DEFAULT_MIN_STRENGTH = 0.3;
const DEFAULT_TOP_K = 10;
const STRENGTH_OUTPUT_CAP = 0.99;

function clampInt(n: number | undefined, dflt: number, min: number, max: number): number {
  if (n === undefined || !Number.isFinite(n)) return dflt;
  return Math.max(min, Math.min(max, Math.floor(n)));
}

function clampMinStrength(n: number | undefined): number {
  if (n === undefined || !Number.isFinite(n)) return DEFAULT_MIN_STRENGTH;
  // Avoid 0 — would disable the strength-based prune and let the BFS explore
  // every reachable path up to maxHops, which is a footgun on dense graphs.
  return Math.max(0.0001, Math.min(1.0, n));
}

// ── Public entry point ──────────────────────────────────────────────────────

export function findWarmPaths(
  targetNodeId: string,
  sourceNodeIds: string[],
  graph: { nodes: GraphNode[]; edges: GraphEdge[] },
  options?: WarmPathOptions,
): WarmPath[] {
  // Defensive: typeof checks instead of trusting input shape, since this is a
  // public API that might be called from JS without compile-time guarantees.
  if (typeof targetNodeId !== "string" || targetNodeId.length === 0) {
    if (typeof console !== "undefined") {
      console.warn("findWarmPaths: targetNodeId must be a non-empty string");
    }
    return [];
  }
  if (!Array.isArray(sourceNodeIds) || sourceNodeIds.length === 0) return [];
  if (!graph || !Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) return [];

  const maxHops = clampInt(options?.maxHops, DEFAULT_MAX_HOPS, 1, 5);
  const minStrength = clampMinStrength(options?.minStrength);
  const warmEdgeKinds = options?.warmEdgeKinds ?? DEFAULT_WARM_EDGE_KINDS;
  const topK = Math.max(0, options?.topK ?? DEFAULT_TOP_K);
  const dedupePolicy: "node-set" | "edge-set" = options?.dedupePolicy ?? "node-set";

  const nodesById = new Map<string, GraphNode>();
  for (const n of graph.nodes) nodesById.set(n.id, n);

  if (!nodesById.has(targetNodeId)) {
    if (typeof console !== "undefined") {
      console.warn(`findWarmPaths: targetNodeId "${targetNodeId}" not in graph; returning []`);
    }
    return [];
  }

  // Filter to known sources only; silently drop unknowns per Contract 2.
  const sources: string[] = [];
  for (const id of sourceNodeIds) if (nodesById.has(id)) sources.push(id);
  if (sources.length === 0) return [];

  const warmKindSet = new Set<EdgeKind>(warmEdgeKinds);

  // Adjacency map keyed on node id. Edges are undirected per Contract 7, so
  // each edge is indexed under both endpoints. Filter out non-warm kinds up
  // front so the inner loop doesn't have to.
  const adjacency = new Map<string, GraphEdge[]>();
  for (const edge of graph.edges) {
    if (!warmKindSet.has(edge.kind)) continue;
    pushOrCreate(adjacency, edge.source, edge);
    pushOrCreate(adjacency, edge.target, edge);
  }

  // Optimistic remaining-edge bound for pruning. Use the strongest warm kind
  // we might still traverse; if every remaining edge somehow took this max
  // and we still couldn't reach minStrength, prune early.
  let maxWarmStrength = 0;
  for (const kind of warmEdgeKinds) {
    const s = EDGE_CONFIGS[kind]?.baseStrength ?? 0;
    if (s > maxWarmStrength) maxWarmStrength = s;
  }
  if (maxWarmStrength === 0) return [];

  type FrontierItem = {
    nodeId: string;
    pathNodes: string[];
    pathEdges: GraphEdge[];
    visited: Set<string>;
    strength: number;
  };

  const collected: WarmPath[] = [];

  for (const sourceId of sources) {
    if (sourceId === targetNodeId) continue; // 0-hop "path" is meaningless
    const stack: FrontierItem[] = [
      {
        nodeId: sourceId,
        pathNodes: [sourceId],
        pathEdges: [],
        visited: new Set([sourceId]),
        strength: 1.0,
      },
    ];

    while (stack.length > 0) {
      const item = stack.pop()!;

      // If the current node IS the target and we have at least one edge in
      // hand, this is a complete path. Don't extend further.
      if (item.nodeId === targetNodeId && item.pathEdges.length > 0) {
        const built = buildWarmPath(
          item.pathNodes,
          item.pathEdges,
          item.strength,
          nodesById,
        );
        if (built) collected.push(built);
        continue;
      }

      // Hop budget exhausted — can't add another edge.
      if (item.pathEdges.length >= maxHops) continue;

      // Optimistic-bound prune: if even the best remaining edge can't lift us
      // above minStrength, stop walking this branch.
      if (item.strength * maxWarmStrength < minStrength) continue;

      const neighbors = adjacency.get(item.nodeId);
      if (!neighbors || neighbors.length === 0) continue;

      for (const edge of neighbors) {
        const nextId = edge.source === item.nodeId ? edge.target : edge.source;
        if (item.visited.has(nextId)) continue; // no revisits within a path
        const edgeStrength = strengthForEdge(edge);
        if (edgeStrength === 0) continue;
        const nextStrength = item.strength * edgeStrength;
        if (nextStrength < minStrength) continue;
        const nextVisited = new Set(item.visited);
        nextVisited.add(nextId);
        stack.push({
          nodeId: nextId,
          pathNodes: [...item.pathNodes, nextId],
          pathEdges: [...item.pathEdges, edge],
          visited: nextVisited,
          strength: nextStrength,
        });
      }
    }
  }

  const deduped = dedupePaths(collected, dedupePolicy);

  // Stable sort: strength desc → hopCount asc → first-node id asc.
  deduped.sort((a, b) => {
    if (b.strength !== a.strength) return b.strength - a.strength;
    if (a.hopCount !== b.hopCount) return a.hopCount - b.hopCount;
    const aId = a.nodes[0]?.id ?? "";
    const bId = b.nodes[0]?.id ?? "";
    return aId.localeCompare(bId);
  });

  return deduped.slice(0, topK);
}

// ── Path construction ──────────────────────────────────────────────────────

function buildWarmPath(
  pathNodeIds: string[],
  pathEdges: GraphEdge[],
  strength: number,
  nodesById: Map<string, GraphNode>,
): WarmPath | null {
  const nodes: GraphNode[] = [];
  for (const id of pathNodeIds) {
    const n = nodesById.get(id);
    // Invariant: every node id added to the path was already verified to
    // exist in nodesById. If not, something is wrong upstream; skip the
    // path defensively rather than crashing the BFS.
    if (!n) return null;
    nodes.push(n);
  }
  return {
    nodes,
    edges: pathEdges,
    strength: Math.min(STRENGTH_OUTPUT_CAP, strength),
    hopCount: pathEdges.length,
    explanation: generateExplanation(nodes, pathEdges),
    suggested_opener: generateSuggestedOpener(nodes, pathEdges),
  };
}

// ── Deduplication ──────────────────────────────────────────────────────────

function dedupePaths(
  paths: WarmPath[],
  policy: "node-set" | "edge-set",
): WarmPath[] {
  const byKey = new Map<string, WarmPath>();
  for (const path of paths) {
    let key: string;
    if (policy === "node-set") {
      key = path.nodes
        .map((n) => n.id)
        .slice()
        .sort()
        .join("|");
    } else {
      key = path.edges
        .map((e) => e.id)
        .slice()
        .sort()
        .join("|");
    }
    const existing = byKey.get(key);
    if (!existing || path.strength > existing.strength) byKey.set(key, path);
  }
  return Array.from(byKey.values());
}

// ── Explanation + opener generators ────────────────────────────────────────
//
// CLAUDE.md L711-767 prescribes per-kind templates that reference real
// evidence (patent titles, paper titles, venues, citation counts, years).
// When `firstEdge.evidence` is populated (extractors landed real data), the
// generators interpolate those fields into rich strings. When it's absent
// (v2 mock graph, demo placeholders), they fall back to generic phrases
// using only the node names + edge kind — never fabricate values to fill
// missing evidence fields (CLAUDE.md "Common Mistakes" #6).
//
// Multi-hop paths (2+ edges): `generateExplanation` describes the first
// (highest-strength) connection in the same detail as a 1-hop path, then
// appends a chain suffix like "→ Wei Chen (via Intel colleague)". This gives
// the sales rep an actionable read: lead with the direct connection evidence,
// then show the bridge to the target.

function nodeName(n: GraphNode): string {
  if ("name" in n && typeof n.name === "string" && n.name.length > 0) return n.name;
  return n.id;
}

/** Short human-readable label for an edge kind, e.g. "patent co-inventor". */
function edgeKindLabel(kind: GraphEdge["kind"]): string {
  switch (kind) {
    case "patent_co_inventor":      return "patent co-inventor";
    case "academic_co_author":      return "co-author";
    case "conference_co_presenter": return "conference co-presenter";
    case "standards_committee":     return "standards committee peer";
    case "colleague":               return "colleague";
    case "past_employer":           return "past colleague";
    case "same_mba_cohort":         return "MBA cohort";
    case "same_phd_program":        return "PhD cohort";
    case "executive_education":     return "exec-ed cohort";
    case "same_undergrad_cohort":   return "undergrad cohort";
    case "education":               return "alma mater";
    default:                        return kind.replace(/_/g, " ");
  }
}

/**
 * For multi-hop paths (hopCount >= 2), append a chain suffix after the
 * first-hop explanation, e.g.:
 *   "Sarah Kim co-invented 'US10234567' with Bob Jones at Intel (2018)
 *    → Wei Chen (via past colleague at Intel)"
 *
 * Each intermediate hop is compressed to "→ <name> (via <edge label>)".
 * The final arrow always points at the target node.
 */
function multiHopSuffix(nodes: GraphNode[], edges: GraphEdge[]): string {
  if (edges.length <= 1) return "";
  const parts: string[] = [];
  // Skip the first edge (already described by the main explanation).
  // nodes[1] is the first intermediate; nodes[2..] are additional intermediates
  // and the final target.
  for (let i = 1; i < edges.length; i++) {
    const nextNode = nodes[i + 1];
    if (!nextNode) break;
    const via = edgeKindLabel(edges[i].kind);
    parts.push(`→ ${nodeName(nextNode)} (via ${via})`);
  }
  return parts.length > 0 ? " " + parts.join(" ") : "";
}

/** Type guard for narrowing `EdgeEvidence | null | undefined` by `kind`. */
function evidenceIs<K extends EdgeEvidence["kind"]>(
  evidence: EdgeEvidence | null | undefined,
  kind: K,
): evidence is Extract<EdgeEvidence, { kind: K }> {
  return evidence != null && evidence.kind === kind;
}

/** Render a year from an ISO date string ("2018-04-21" → "2018"). Falls
 *  back to a generic placeholder when the input is missing or malformed. */
function yearFromDate(d: string | null | undefined): string {
  if (typeof d === "string" && /^\d{4}/.test(d)) return d.slice(0, 4);
  return "year unknown";
}

function generateExplanation(nodes: GraphNode[], edges: GraphEdge[]): string {
  if (edges.length === 0 || nodes.length === 0) return "";
  const firstEdge = edges[0];
  const a = nodeName(nodes[0]);
  // For multi-hop paths, b is the first intermediate node (the person who
  // directly knows the source), not the final target.
  const b = nodeName(nodes[1] ?? nodes[nodes.length - 1]);
  const evidence = firstEdge.evidence ?? null;
  const suffix = multiHopSuffix(nodes, edges);

  switch (firstEdge.kind) {
    case "patent_co_inventor": {
      if (evidenceIs(evidence, "patent_co_inventor")) {
        const title = evidence.patentTitle || "a patent";
        const assignee = evidence.assignee || "their shared employer";
        const year = yearFromDate(evidence.filingDate);
        return `${a} co-invented "${title}" with ${b} at ${assignee} (${year})${suffix}`;
      }
      return `${a} co-invented a patent with ${b}${suffix}`;
    }

    case "academic_co_author": {
      if (evidenceIs(evidence, "academic_co_author")) {
        const title = evidence.paperTitle || "a paper";
        const venue = evidence.venue || "a venue";
        const year = Number.isFinite(evidence.year) ? String(evidence.year) : "year unknown";
        const cites =
          Number.isFinite(evidence.citationCount) && evidence.citationCount >= 0
            ? `${evidence.citationCount} citations`
            : "citation count unknown";
        return `${a} co-authored "${title}" with ${b} at ${venue} (${year}, ${cites})${suffix}`;
      }
      return `${a} co-authored a paper with ${b}${suffix}`;
    }

    case "conference_co_presenter": {
      if (evidenceIs(evidence, "conference_co_presenter")) {
        const event = evidence.event || "a conference";
        const year = Number.isFinite(evidence.year) ? String(evidence.year) : "year unknown";
        return `${a} and ${b} co-presented at ${event} (${year})${suffix}`;
      }
      return `${a} and ${b} co-presented at a conference${suffix}`;
    }

    case "standards_committee": {
      if (evidenceIs(evidence, "standards_committee")) {
        const committee = evidence.committee || "a standards committee";
        const years = evidence.years || "active period unknown";
        return `${a} and ${b} served on the ${committee} together (${years})${suffix}`;
      }
      return `${a} and ${b} served on a standards committee together${suffix}`;
    }

    case "past_employer": {
      if (evidenceIs(evidence, "career_overlap")) {
        const company = evidence.companyName || "a past employer";
        const span =
          Number.isFinite(evidence.overlapYears) && evidence.overlapYears > 0
            ? `${evidence.overlapYears}-year overlap`
            : "shared tenure";
        return `${a} and ${b} worked together at ${company} (${span})${suffix}`;
      }
      return `${a} and ${b} worked together at a past employer${suffix}`;
    }

    case "colleague": {
      if (evidenceIs(evidence, "career_overlap")) {
        const company = evidence.companyName || "the same company";
        const team = evidence.teamA && evidence.teamA === evidence.teamB ? evidence.teamA : null;
        if (team) return `${a} and ${b} are colleagues on the ${team} team at ${company}${suffix}`;
        return `${a} and ${b} are current colleagues at ${company}${suffix}`;
      }
      return `${a} and ${b} are current colleagues${suffix}`;
    }

    case "education":
      return `${a} and ${b} share an alma mater${suffix}`;

    // Education-cohort edges: interpolate institution + cohort years from
    // evidence when available (populated by education_cohort_clustering job).
    case "same_mba_cohort": {
      if (evidenceIs(evidence, "education_cohort") && evidence.institution) {
        const cohort = evidence.overlapStartYear ? ` (class of ${evidence.overlapStartYear})` : "";
        return `${a} and ${b} were in the same MBA cohort at ${evidence.institution}${cohort}${suffix}`;
      }
      return `${a} and ${b} were in the same MBA cohort${suffix}`;
    }
    case "same_phd_program": {
      if (evidenceIs(evidence, "education_cohort") && evidence.institution) {
        const prog = evidence.program ? `, ${evidence.program}` : "";
        return `${a} and ${b} were in the same PhD program at ${evidence.institution}${prog}${suffix}`;
      }
      return `${a} and ${b} were in the same PhD program${suffix}`;
    }
    case "executive_education": {
      if (evidenceIs(evidence, "education_cohort") && evidence.institution) {
        const prog = evidence.program ? ` (${evidence.program})` : "";
        return `${a} and ${b} attended the same executive education program at ${evidence.institution}${prog}${suffix}`;
      }
      return `${a} and ${b} attended the same executive education program${suffix}`;
    }
    case "same_undergrad_cohort": {
      if (evidenceIs(evidence, "education_cohort") && evidence.institution) {
        const cohort = evidence.overlapStartYear ? ` (class of ${evidence.overlapStartYear})` : "";
        return `${a} and ${b} were in the same undergraduate cohort at ${evidence.institution}${cohort}${suffix}`;
      }
      return `${a} and ${b} were in the same undergraduate cohort${suffix}`;
    }

    default:
      return `${a} and ${b} have a ${firstEdge.kind.replace(/_/g, " ")} connection${suffix}`;
  }
}

function generateSuggestedOpener(nodes: GraphNode[], edges: GraphEdge[]): string {
  if (edges.length === 0 || nodes.length < 2) return "";
  const firstEdge = edges[0];
  const connector = nodeName(nodes[0]);
  const evidence = firstEdge.evidence ?? null;

  switch (firstEdge.kind) {
    case "patent_co_inventor": {
      if (evidenceIs(evidence, "patent_co_inventor")) {
        const title = evidence.patentTitle || "a patent";
        const assignee = evidence.assignee || "our shared employer";
        const year = yearFromDate(evidence.filingDate);
        return `${connector} — I noticed we co-invented "${title}" together at ${assignee} back in ${year}. I'm now at [Company] and would love to reconnect.`;
      }
      return `${connector} — I noticed we co-invented a patent together. I'm now at [Company] and would love to reconnect.`;
    }

    case "academic_co_author": {
      if (evidenceIs(evidence, "academic_co_author")) {
        const title = evidence.paperTitle || "a paper";
        const year = Number.isFinite(evidence.year) ? ` in ${evidence.year}` : "";
        return `${connector} — we co-authored "${title}"${year}. I'm now at [Company] working on [relevant problem] and thought it was worth reaching out.`;
      }
      return `${connector} — we co-authored a paper years ago. I'm now at [Company] working on [relevant problem] and thought it was worth reaching out.`;
    }

    case "conference_co_presenter": {
      if (evidenceIs(evidence, "conference_co_presenter")) {
        const event = evidence.event || "a conference";
        const year = Number.isFinite(evidence.year) ? ` in ${evidence.year}` : "";
        return `${connector} — we co-presented at ${event}${year}. I'm now at [Company] and wanted to reconnect.`;
      }
      return `${connector} — we co-presented at a conference. I'm now at [Company] and wanted to reconnect.`;
    }

    case "standards_committee": {
      if (evidenceIs(evidence, "standards_committee")) {
        const committee = evidence.committee || "a standards committee";
        const years = evidence.years ? ` for ${evidence.years}` : "";
        return `${connector} — we sat on the ${committee}${years}. I'm now at [Company] and wanted to reconnect.`;
      }
      return `${connector} — we sat on a standards committee together. I'm now at [Company] and wanted to reconnect.`;
    }

    case "past_employer": {
      if (evidenceIs(evidence, "career_overlap")) {
        const company = evidence.companyName || "a former employer";
        return `${connector} — we crossed paths at ${company}. I'm now at [Company] and thought it was worth reaching out.`;
      }
      return `${connector} — we crossed paths at a former employer. I'm now at [Company] and thought it was worth reaching out.`;
    }

    case "colleague": {
      if (evidenceIs(evidence, "career_overlap")) {
        const company = evidence.companyName ? ` at ${evidence.companyName}` : "";
        return `${connector} — we're current colleagues${company}; sending a quick note as I take on [Company] work.`;
      }
      return `${connector} — we're current colleagues; sending a quick note as I take on [Company] work.`;
    }

    case "education":
      return `${connector} — we share an alma mater. I'm now at [Company] working on [relevant problem] and wanted to introduce myself.`;

    // Education-cohort openers — interpolate institution + program when
    // evidence is populated by the education_cohort_clustering job.
    case "same_mba_cohort": {
      if (evidenceIs(evidence, "education_cohort") && evidence.institution) {
        const cohort = evidence.overlapStartYear ? `, class of ${evidence.overlapStartYear}` : "";
        return `${connector} — we were in the same MBA cohort at ${evidence.institution}${cohort}. I'm now at [Company] working on [relevant problem] and wanted to reconnect.`;
      }
      return `${connector} — we were in the same MBA cohort. I'm now at [Company] working on [relevant problem] and wanted to reconnect.`;
    }
    case "same_phd_program": {
      if (evidenceIs(evidence, "education_cohort") && evidence.institution) {
        const prog = evidence.program ? ` (${evidence.program})` : "";
        return `${connector} — we were in the same PhD program at ${evidence.institution}${prog}. I'm now at [Company] working on [relevant problem] and thought a quick conversation might be valuable.`;
      }
      return `${connector} — we were in the same PhD program. I'm now at [Company] working on [relevant problem] and thought a quick conversation might be valuable.`;
    }
    case "executive_education": {
      if (evidenceIs(evidence, "education_cohort") && evidence.institution) {
        const prog = evidence.program ? ` — ${evidence.program}` : "";
        return `${connector} — we went through the same executive education program at ${evidence.institution}${prog}. I'm now at [Company] working on [relevant problem] and wanted to reach out.`;
      }
      return `${connector} — we went through the same executive education program. I'm now at [Company] working on [relevant problem] and wanted to reach out.`;
    }
    case "same_undergrad_cohort": {
      if (evidenceIs(evidence, "education_cohort") && evidence.institution) {
        const cohort = evidence.overlapStartYear ? `, class of ${evidence.overlapStartYear}` : "";
        return `${connector} — we were in the same undergraduate class at ${evidence.institution}${cohort}. I'm now at [Company] working on [relevant problem] and thought it was worth reaching out.`;
      }
      return `${connector} — we were in the same undergraduate class. I'm now at [Company] working on [relevant problem] and thought it was worth reaching out.`;
    }

    default:
      return `${connector} — we crossed paths and I'm now at [Company] working on something I think would be relevant to you.`;
  }
}

// ── Tiny helpers ───────────────────────────────────────────────────────────

function pushOrCreate<K, V>(map: Map<K, V[]>, key: K, value: V): void {
  const list = map.get(key);
  if (list) list.push(value);
  else map.set(key, [value]);
}
