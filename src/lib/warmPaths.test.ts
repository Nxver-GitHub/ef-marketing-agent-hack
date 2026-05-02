/**
 * Contract 2 test suite for findWarmPaths.
 *
 * Mirrors the vitest convention used by `scoreMath.test.ts` and
 * `strength.test.ts`. Covers all 7 test conditions from CONTRACTS.md
 * Contract 2 plus invariants (immutability, determinism, top-K cap).
 */

import { describe, it, expect } from "vitest";
import type {
  AcademicCoAuthorEvidence,
  CareerOverlapEvidence,
  ConferenceCoPresenterEvidence,
  GraphEdge,
  GraphNode,
  PatentCoInventorEvidence,
  StandardsCommitteeEvidence,
} from "./graph";
import { findWarmPaths, type WarmPath, type WarmPathOptions } from "./warmPaths";
import { STRENGTH_TABLE } from "./strength";

// ── Fixture helpers ─────────────────────────────────────────────────────────

const PATENT_BASE = STRENGTH_TABLE.patent_co_inventor;            // 0.95
const PAPER_BASE = STRENGTH_TABLE.academic_co_author_single;      // 0.85
const CONF_BASE = STRENGTH_TABLE.conference_co_presenter;         // 0.80
const STANDARDS_BASE = STRENGTH_TABLE.standards_committee_peer;   // 0.82

function person(id: string, name: string): GraphNode {
  return {
    id,
    kind: "person",
    name,
    role: "Test",
    companyId: "company:test",
    raw: {
      _id: id,
      name,
      company: "Test",
      role: "Test",
      industry: "Test",
      created_at: 0,
      updated_at: 0,
    },
  };
}

function edge(id: string, source: string, target: string, kind: GraphEdge["kind"]): GraphEdge {
  return { id, source, target, kind };
}

// ── Test suite ──────────────────────────────────────────────────────────────

describe("findWarmPaths", () => {
  // Test condition 5: empty / unknown / degenerate inputs
  it("returns [] when graph is empty", () => {
    expect(findWarmPaths("p:c", ["p:a"], { nodes: [], edges: [] })).toEqual([]);
  });

  it("returns [] when targetNodeId is not in the graph", () => {
    const nodes = [person("p:a", "Alice")];
    expect(findWarmPaths("p:nonexistent", ["p:a"], { nodes, edges: [] })).toEqual([]);
  });

  it("returns [] when sourceNodeIds is empty", () => {
    const nodes = [person("p:a", "Alice"), person("p:c", "Carol")];
    expect(findWarmPaths("p:c", [], { nodes, edges: [] })).toEqual([]);
  });

  it("returns [] when no source matches a known node", () => {
    const nodes = [person("p:a", "Alice"), person("p:c", "Carol")];
    expect(findWarmPaths("p:c", ["p:ghost"], { nodes, edges: [] })).toEqual([]);
  });

  it("returns [] for invalid targetNodeId types", () => {
    // Defensive: tests the runtime guard in case findWarmPaths is called from JS.
    const nodes = [person("p:a", "Alice")];
    // @ts-expect-error testing runtime defense
    expect(findWarmPaths(undefined, ["p:a"], { nodes, edges: [] })).toEqual([]);
  });

  // Test condition 1: 1-hop patent connection
  it("finds a 1-hop patent_co_inventor path with strength 0.95", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:c", "patent_co_inventor");
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result).toHaveLength(1);
    expect(result[0].hopCount).toBe(1);
    expect(result[0].nodes.map((n) => n.id)).toEqual(["p:a", "p:c"]);
    expect(result[0].edges.map((e) => e.id)).toEqual(["e1"]);
    expect(result[0].strength).toBeCloseTo(PATENT_BASE, 6);
  });

  // Test condition 2: 2-hop patent + paper, strength = product
  it("finds a 2-hop path with strength = product of edge strengths", () => {
    const a = person("p:a", "Alice");
    const b = person("p:b", "Bob");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:b", "patent_co_inventor");
    const e2 = edge("e2", "p:b", "p:c", "academic_co_author");
    const result = findWarmPaths("p:c", ["p:a"], {
      nodes: [a, b, c],
      edges: [e1, e2],
    });
    expect(result).toHaveLength(1);
    expect(result[0].hopCount).toBe(2);
    expect(result[0].nodes.map((n) => n.id)).toEqual(["p:a", "p:b", "p:c"]);
    expect(result[0].strength).toBeCloseTo(PATENT_BASE * PAPER_BASE, 6); // 0.95 * 0.85 = 0.8075
  });

  // Test condition 3: minStrength prunes the 2-hop path
  it("prunes paths below minStrength", () => {
    const a = person("p:a", "Alice");
    const b = person("p:b", "Bob");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:b", "patent_co_inventor");
    const e2 = edge("e2", "p:b", "p:c", "academic_co_author");
    const result = findWarmPaths(
      "p:c",
      ["p:a"],
      { nodes: [a, b, c], edges: [e1, e2] },
      { minStrength: 0.9 },
    );
    expect(result).toEqual([]); // 0.95 * 0.85 = 0.8075 < 0.9
  });

  // Test condition 4: maxHops:1 prunes 2-hop paths
  it("prunes paths above maxHops", () => {
    const a = person("p:a", "Alice");
    const b = person("p:b", "Bob");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:b", "patent_co_inventor");
    const e2 = edge("e2", "p:b", "p:c", "academic_co_author");
    const result = findWarmPaths(
      "p:c",
      ["p:a"],
      { nodes: [a, b, c], edges: [e1, e2] },
      { maxHops: 1 },
    );
    expect(result).toEqual([]);
  });

  it("traverses a 3-hop path when allowed by maxHops", () => {
    const a = person("p:a", "Alice");
    const b = person("p:b", "Bob");
    const c = person("p:c", "Carol");
    const d = person("p:d", "Dave");
    const e1 = edge("e1", "p:a", "p:b", "patent_co_inventor");
    const e2 = edge("e2", "p:b", "p:c", "patent_co_inventor");
    const e3 = edge("e3", "p:c", "p:d", "patent_co_inventor");
    const result = findWarmPaths("p:d", ["p:a"], {
      nodes: [a, b, c, d],
      edges: [e1, e2, e3],
    });
    expect(result).toHaveLength(1);
    expect(result[0].hopCount).toBe(3);
    expect(result[0].strength).toBeCloseTo(PATENT_BASE ** 3, 6); // 0.857...
  });

  // Test condition 6: input arrays not mutated
  it("does not mutate input nodes, edges, or sourceNodeIds", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:c", "patent_co_inventor");
    const nodes = [a, c];
    const edges = [e1];
    const sources = ["p:a"];
    const options: WarmPathOptions = { maxHops: 2, topK: 5 };

    const nodesSnap = JSON.stringify(nodes);
    const edgesSnap = JSON.stringify(edges);
    const sourcesSnap = JSON.stringify(sources);
    const optionsSnap = JSON.stringify(options);

    findWarmPaths("p:c", sources, { nodes, edges }, options);

    expect(JSON.stringify(nodes)).toBe(nodesSnap);
    expect(JSON.stringify(edges)).toBe(edgesSnap);
    expect(JSON.stringify(sources)).toBe(sourcesSnap);
    expect(JSON.stringify(options)).toBe(optionsSnap);
  });

  // Test condition 7: explanation is specific (mentions actual node names + edge kind)
  it("produces a specific, non-generic explanation", () => {
    const a = person("p:a", "Alice Smith");
    const c = person("p:c", "Carol Jones");
    const e1 = edge("e1", "p:a", "p:c", "patent_co_inventor");
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result[0].explanation).toContain("Alice Smith");
    expect(result[0].explanation).toContain("Carol Jones");
    expect(result[0].explanation).toMatch(/patent/i);
  });

  it("opener references the connector and ends with a period", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:c", "patent_co_inventor");
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result[0].suggested_opener.startsWith("Alice")).toBe(true);
    expect(result[0].suggested_opener.endsWith(".")).toBe(true);
  });

  // Edge-filter behavior
  it("does not traverse non-warm edge kinds (e.g., works_at)", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:c", "works_at");
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result).toEqual([]);
  });

  it("respects an explicit warmEdgeKinds override", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:c", "academic_co_author");
    // Restrict to patent_co_inventor only — paper edge should be ignored
    const result = findWarmPaths(
      "p:c",
      ["p:a"],
      { nodes: [a, c], edges: [e1] },
      { warmEdgeKinds: ["patent_co_inventor"] },
    );
    expect(result).toEqual([]);
  });

  // Deduplication
  it("dedupes paths covering the same node set, keeping the strongest", () => {
    const a = person("p:a", "Alice");
    const b = person("p:b", "Bob");
    const c = person("p:c", "Carol");
    // Two parallel paths A→B→C: a stronger one and a weaker one
    const e1Strong = edge("e1", "p:a", "p:b", "patent_co_inventor"); // 0.95
    const e2Strong = edge("e2", "p:b", "p:c", "patent_co_inventor"); // 0.95
    const e3Weak = edge("e3", "p:a", "p:b", "academic_co_author"); // 0.85
    const e4Weak = edge("e4", "p:b", "p:c", "academic_co_author"); // 0.85
    const result = findWarmPaths(
      "p:c",
      ["p:a"],
      { nodes: [a, b, c], edges: [e1Strong, e2Strong, e3Weak, e4Weak] },
      { dedupePolicy: "node-set" },
    );
    // node-set {a, b, c} dedupes to one path; the stronger one wins
    expect(result).toHaveLength(1);
    expect(result[0].strength).toBeCloseTo(PATENT_BASE * PATENT_BASE, 6);
  });

  it("edge-set dedupePolicy keeps both parallel paths", () => {
    const a = person("p:a", "Alice");
    const b = person("p:b", "Bob");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:b", "patent_co_inventor");
    const e2 = edge("e2", "p:b", "p:c", "patent_co_inventor");
    const e3 = edge("e3", "p:a", "p:b", "academic_co_author");
    const e4 = edge("e4", "p:b", "p:c", "academic_co_author");
    const result = findWarmPaths(
      "p:c",
      ["p:a"],
      { nodes: [a, b, c], edges: [e1, e2, e3, e4] },
      { dedupePolicy: "edge-set" },
    );
    // 4 distinct 2-hop paths through {e1,e2}, {e1,e4}, {e3,e2}, {e3,e4}
    expect(result.length).toBeGreaterThan(1);
  });

  // Determinism
  it("is deterministic — same input produces the same output", () => {
    const a = person("p:a", "Alice");
    const b = person("p:b", "Bob");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:b", "patent_co_inventor");
    const e2 = edge("e2", "p:b", "p:c", "academic_co_author");
    const e3 = edge("e3", "p:a", "p:c", "conference_co_presenter");
    const graph = { nodes: [a, b, c], edges: [e1, e2, e3] };
    const r1 = findWarmPaths("p:c", ["p:a"], graph);
    const r2 = findWarmPaths("p:c", ["p:a"], graph);
    expect(serializePaths(r1)).toEqual(serializePaths(r2));
  });

  it("sorts paths by strength desc, hopCount asc, source-id asc", () => {
    const a = person("p:a", "Alice");
    const b = person("p:b", "Bob");
    const c = person("p:c", "Carol");
    const eAC = edge("eAC", "p:a", "p:c", "conference_co_presenter"); // 0.80, 1 hop
    const eAB = edge("eAB", "p:a", "p:b", "patent_co_inventor"); // 0.95
    const eBC = edge("eBC", "p:b", "p:c", "patent_co_inventor"); // 0.95, 2 hops total = 0.9025
    const result = findWarmPaths("p:c", ["p:a"], {
      nodes: [a, b, c],
      edges: [eAC, eAB, eBC],
    });
    expect(result.length).toBeGreaterThanOrEqual(2);
    // First should be highest strength (the 2-hop patent path beats the 1-hop conference)
    expect(result[0].strength).toBeGreaterThan(result[1].strength);
  });

  // Top-K cap
  it("returns at most topK paths", () => {
    const a = person("p:a", "Alice");
    const b1 = person("p:b1", "B1");
    const b2 = person("p:b2", "B2");
    const b3 = person("p:b3", "B3");
    const c = person("p:c", "Carol");
    const edges: GraphEdge[] = [
      edge("e1", "p:a", "p:b1", "patent_co_inventor"),
      edge("e2", "p:a", "p:b2", "patent_co_inventor"),
      edge("e3", "p:a", "p:b3", "patent_co_inventor"),
      edge("e4", "p:b1", "p:c", "patent_co_inventor"),
      edge("e5", "p:b2", "p:c", "patent_co_inventor"),
      edge("e6", "p:b3", "p:c", "patent_co_inventor"),
      edge("e7", "p:a", "p:c", "patent_co_inventor"), // direct 1-hop
    ];
    const result = findWarmPaths(
      "p:c",
      ["p:a"],
      { nodes: [a, b1, b2, b3, c], edges },
      { topK: 2, dedupePolicy: "edge-set" },
    );
    expect(result.length).toBeLessThanOrEqual(2);
  });

  it("excludes 0-hop self-paths (source === target)", () => {
    const a = person("p:a", "Alice");
    const result = findWarmPaths("p:a", ["p:a"], { nodes: [a], edges: [] });
    expect(result).toEqual([]);
  });

  it("considers multiple source nodes and ranks all paths together", () => {
    const a1 = person("p:a1", "Alice One");
    const a2 = person("p:a2", "Alice Two");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a1", "p:c", "academic_co_author"); // 0.85
    const e2 = edge("e2", "p:a2", "p:c", "patent_co_inventor"); // 0.95
    const result = findWarmPaths("p:c", ["p:a1", "p:a2"], {
      nodes: [a1, a2, c],
      edges: [e1, e2],
    });
    expect(result).toHaveLength(2);
    expect(result[0].nodes[0].id).toBe("p:a2"); // stronger path first
    expect(result[0].strength).toBeCloseTo(PATENT_BASE, 6);
    expect(result[1].nodes[0].id).toBe("p:a1");
    expect(result[1].strength).toBeCloseTo(PAPER_BASE, 6);
  });

  it("strength never exceeds 0.99 cap", () => {
    // Use a synthetic high-base configuration: even 0.95 alone is < 0.99.
    // To stress the cap we'd need corroboration / source diversity, which
    // the BFS doesn't apply (it reads STRENGTH_TABLE base directly per
    // Contract 2). So we just confirm output stays in [0, 0.99].
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:c", "patent_co_inventor");
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result[0].strength).toBeLessThanOrEqual(0.99);
    expect(result[0].strength).toBeGreaterThan(0);
  });

  // Standards committee
  it("traverses standards_committee edges", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:c", "standards_committee");
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result).toHaveLength(1);
    expect(result[0].strength).toBeCloseTo(STANDARDS_BASE, 6);
    expect(result[0].explanation).toContain("standards committee");
  });

  // Conference co-presenter
  it("traverses conference_co_presenter edges", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const e1 = edge("e1", "p:a", "p:c", "conference_co_presenter");
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result).toHaveLength(1);
    expect(result[0].strength).toBeCloseTo(CONF_BASE, 6);
  });

  it("treats edges as undirected (target → source traversal works)", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    // Edge from C to A; BFS source = A, target = C — must still find it
    const e1 = edge("e1", "p:c", "p:a", "patent_co_inventor");
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result).toHaveLength(1);
    expect(result[0].hopCount).toBe(1);
  });
});

// ── Evidence-aware explanation + opener (Contract 2 + CLAUDE.md L711-767) ──

describe("findWarmPaths — evidence-aware templates", () => {
  it("uses richer patent template when patent evidence is present", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const evidence: PatentCoInventorEvidence = {
      kind: "patent_co_inventor",
      patentNumber: "10,234,567",
      patentTitle: "Method for 3nm yield optimization",
      filingDate: "2018-04-21",
      grantDate: "2020-01-14",
      assignee: "Intel Corporation",
      usptoUrl: "https://patents.uspto.gov/10234567",
    };
    const e1: GraphEdge = { ...edge("e1", "p:a", "p:c", "patent_co_inventor"), evidence };
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result).toHaveLength(1);
    expect(result[0].explanation).toContain("Method for 3nm yield optimization");
    expect(result[0].explanation).toContain("Intel Corporation");
    expect(result[0].explanation).toContain("2018"); // year extracted from filingDate
    expect(result[0].suggested_opener).toContain("Method for 3nm yield optimization");
    expect(result[0].suggested_opener).toContain("Intel Corporation");
  });

  it("uses richer paper template with citation count", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const evidence: AcademicCoAuthorEvidence = {
      kind: "academic_co_author",
      paperTitle: "Accelerator design for LLM training",
      venue: "NeurIPS",
      year: 2023,
      citationCount: 42,
    };
    const e1: GraphEdge = { ...edge("e1", "p:a", "p:c", "academic_co_author"), evidence };
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result[0].explanation).toContain("Accelerator design for LLM training");
    expect(result[0].explanation).toContain("NeurIPS");
    expect(result[0].explanation).toContain("2023");
    expect(result[0].explanation).toContain("42 citations");
    expect(result[0].suggested_opener).toContain("Accelerator design for LLM training");
    expect(result[0].suggested_opener).toContain("2023");
  });

  it("uses richer conference template with event + year", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const evidence: ConferenceCoPresenterEvidence = {
      kind: "conference_co_presenter",
      event: "SPIE Advanced Lithography",
      year: 2024,
    };
    const e1: GraphEdge = { ...edge("e1", "p:a", "p:c", "conference_co_presenter"), evidence };
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result[0].explanation).toContain("SPIE Advanced Lithography");
    expect(result[0].explanation).toContain("2024");
    expect(result[0].suggested_opener).toContain("SPIE Advanced Lithography");
    expect(result[0].suggested_opener).toContain("2024");
  });

  it("uses richer standards template with committee + active years", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const evidence: StandardsCommitteeEvidence = {
      kind: "standards_committee",
      committee: "JEDEC JC-42.4 Memory Module Subcommittee",
      years: "2018-2022",
    };
    const e1: GraphEdge = { ...edge("e1", "p:a", "p:c", "standards_committee"), evidence };
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result[0].explanation).toContain("JEDEC JC-42.4 Memory Module Subcommittee");
    expect(result[0].explanation).toContain("2018-2022");
    expect(result[0].suggested_opener).toContain("JEDEC JC-42.4 Memory Module Subcommittee");
    expect(result[0].suggested_opener).toContain("2018-2022");
  });

  it("uses richer past_employer template with company + overlap span", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const evidence: CareerOverlapEvidence = {
      kind: "career_overlap",
      companyName: "Intel",
      overlapStartYear: 2007,
      overlapEndYear: 2011,
      overlapYears: 4,
      teamA: "Process Engineering",
      teamB: "Process Engineering",
    };
    const e1: GraphEdge = { ...edge("e1", "p:a", "p:c", "past_employer"), evidence };
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result[0].explanation).toContain("Intel");
    expect(result[0].explanation).toContain("4-year overlap");
    expect(result[0].suggested_opener).toContain("Intel");
  });

  it("uses richer colleague template with company + same-team detection", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const evidence: CareerOverlapEvidence = {
      kind: "career_overlap",
      companyName: "ASML",
      overlapStartYear: 2020,
      overlapEndYear: 2024,
      overlapYears: 4,
      teamA: "Lithography",
      teamB: "Lithography",
    };
    const e1: GraphEdge = { ...edge("e1", "p:a", "p:c", "colleague"), evidence };
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result[0].explanation).toContain("Lithography");
    expect(result[0].explanation).toContain("ASML");
  });

  it("falls back to generic strings when evidence is undefined (v2 mock graph)", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    // No evidence attached — the existing v2 / demo placeholder pattern
    const e1 = edge("e1", "p:a", "p:c", "patent_co_inventor");
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result[0].explanation).toContain("Alice");
    expect(result[0].explanation).toContain("Carol");
    expect(result[0].explanation).toContain("patent");
    // Not a hallucinated patent number
    expect(result[0].explanation).not.toMatch(/\d{4,}/);
    // Generic opener still ends with a period and starts with the connector
    expect(result[0].suggested_opener.startsWith("Alice")).toBe(true);
    expect(result[0].suggested_opener.endsWith(".")).toBe(true);
  });

  it("falls back to generic strings when evidence is null", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const e1: GraphEdge = { ...edge("e1", "p:a", "p:c", "academic_co_author"), evidence: null };
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result[0].explanation).toContain("co-authored");
    expect(result[0].explanation).not.toContain('""'); // no empty-quote artifacts
  });

  it("tolerates missing evidence sub-fields without fabricating", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    // Evidence present but with sparse fields — common during partial extractor passes
    const evidence: PatentCoInventorEvidence = {
      kind: "patent_co_inventor",
      patentNumber: "",
      patentTitle: "",
      filingDate: "",
      assignee: "",
    };
    const e1: GraphEdge = { ...edge("e1", "p:a", "p:c", "patent_co_inventor"), evidence };
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    // Empty fields fall through to documented placeholder strings — not garbage
    expect(result[0].explanation).toContain("a patent");
    expect(result[0].explanation).toContain("their shared employer");
    expect(result[0].explanation).toContain("year unknown");
  });

  it("tolerates invalid year values without crashing", () => {
    const a = person("p:a", "Alice");
    const c = person("p:c", "Carol");
    const evidence = {
      kind: "academic_co_author",
      paperTitle: "Some paper",
      venue: "Some venue",
      year: NaN,
      citationCount: -1,
    } as AcademicCoAuthorEvidence;
    const e1: GraphEdge = { ...edge("e1", "p:a", "p:c", "academic_co_author"), evidence };
    const result = findWarmPaths("p:c", ["p:a"], { nodes: [a, c], edges: [e1] });
    expect(result[0].explanation).toContain("year unknown");
    expect(result[0].explanation).toContain("citation count unknown");
  });
});

// ── Helpers ─────────────────────────────────────────────────────────────────

function serializePaths(paths: WarmPath[]): string {
  return JSON.stringify(
    paths.map((p) => ({
      ids: p.nodes.map((n) => n.id),
      edgeIds: p.edges.map((e) => e.id),
      hopCount: p.hopCount,
      strength: Math.round(p.strength * 1e8),
    })),
  );
}
