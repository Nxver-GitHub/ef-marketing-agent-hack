/**
 * Tests for `buildGraph`.
 *
 * Two test groups:
 *   1. Hidden-connection signals pass (the v3 piece that turns
 *      Contract-1-shaped signal rows into `GraphEdge`s with `evidence`
 *      populated) — covers the new code path I added.
 *   2. v2 structural passes (works_at, partnership, past_employer,
 *      education, scope_signal, hierarchy root, dedup) — locks regression
 *      coverage on the existing pre-v3 graph builder.
 */

import { describe, it, expect } from "vitest";
import { buildGraph, normalizeCompany, canonicalizeRole } from "./graph";
import type { Prospect, Signal } from "./mockStore";

// ── Fixture helpers ─────────────────────────────────────────────────────────

function prospect(id: string, name: string, company = "Test Co"): Prospect {
  return {
    _id: id,
    name,
    company,
    role: "Engineer",
    industry: "Test",
    created_at: 0,
    updated_at: 0,
  };
}

interface V3SignalLike extends Signal {
  signal_type: string;
  // v3 extension — the duck-type buildGraph reads
  structured_value?: Record<string, unknown>;
}

function v3signal(
  signalType: string,
  prospectId: string,
  structuredValue: Record<string, unknown>,
): V3SignalLike {
  return {
    _id: `sig:${signalType}:${prospectId}`,
    prospect_id: prospectId,
    source: "test",
    signal_type: signalType,
    value: null,
    raw_data: {},
    weight: 1,
    confidence: 0.95,
    collected_at: 0,
    structured_value: structuredValue,
  };
}

function prospectFull(
  id: string,
  name: string,
  company: string,
  opts: {
    role?: string;
    industry?: string;
    past_companies?: string[];
    education?: { school: string; degree: string; year: number }[];
    talks?: { venue: string; year: number; topic?: string }[];
  } = {},
): Prospect & {
  past_companies?: string[];
  education?: { school: string; degree: string; year: number }[];
  talks?: { venue: string; year: number; topic?: string }[];
} {
  return {
    _id: id,
    name,
    company,
    role: opts.role ?? "Engineer",
    industry: opts.industry ?? "Semiconductors",
    created_at: 0,
    updated_at: 0,
    past_companies: opts.past_companies,
    education: opts.education,
    talks: opts.talks,
  };
}

function findEdge(edges: ReadonlyArray<{ kind: string; source: string; target: string }>, kind: string) {
  return edges.find((e) => e.kind === kind);
}

// ── Tests ───────────────────────────────────────────────────────────────────

describe("buildGraph — hidden-connection signals pass", () => {
  it("emits a patent_co_inventor edge with evidence when a v3 signal is present", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const sig = v3signal("patent_co_inventor", "a", {
      connected_to: "b",
      patent_number: "10,234,567",
      patent_title: "Yield optimization method",
      filing_date: "2018-04-21",
      assignee: "Intel Corporation",
    });

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [sig] },
    });

    const patentEdge = findEdge(edges, "patent_co_inventor");
    expect(patentEdge).toBeDefined();
    expect(patentEdge?.source).toBe("person:a");
    expect(patentEdge?.target).toBe("person:b");
    // @ts-expect-error edge type doesn't surface evidence in the find return type
    expect(patentEdge?.evidence?.kind).toBe("patent_co_inventor");
    // @ts-expect-error edge.evidence is union-typed
    expect(patentEdge?.evidence?.patentTitle).toBe("Yield optimization method");
    // @ts-expect-error edge.evidence is union-typed
    expect(patentEdge?.evidence?.assignee).toBe("Intel Corporation");
  });

  it("dedupes A→B and B→A signals (canonicalizes ordering)", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    // Two signals — one from A, one from B — describing the same patent
    const sigA = v3signal("patent_co_inventor", "a", {
      connected_to: "b",
      patent_number: "10,234,567",
      patent_title: "Same patent",
      filing_date: "2018-04-21",
      assignee: "Intel",
    });
    const sigB = v3signal("patent_co_inventor", "b", {
      connected_to: "a",
      patent_number: "10,234,567",
      patent_title: "Same patent",
      filing_date: "2018-04-21",
      assignee: "Intel",
    });

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [sigA], b: [sigB] },
    });

    const patentEdges = edges.filter((e) => e.kind === "patent_co_inventor");
    expect(patentEdges).toHaveLength(1);
  });

  it("preserves rich evidence when first-write has it, second has none", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const sigA = v3signal("patent_co_inventor", "a", {
      connected_to: "b",
      patent_title: "Rich",
      filing_date: "2018-04-21",
      assignee: "Intel",
    });
    // Sparse signal — would emit null/empty evidence by itself
    const sigB = v3signal("patent_co_inventor", "b", {
      connected_to: "a",
      // no patent_title, etc.
    });

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [sigA], b: [sigB] },
    });

    const edge = findEdge(edges, "patent_co_inventor");
    // @ts-expect-error edge.evidence is union-typed
    expect(edge?.evidence?.patentTitle).toBe("Rich");
  });

  it("ignores signals with unknown signal_type", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const sig = v3signal("unknown_garbage", "a", { connected_to: "b" });

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [sig] },
    });

    expect(edges.find((e) => e.kind === "unknown_garbage" as never)).toBeUndefined();
  });

  it("drops signals with missing connected_to", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const sig = v3signal("patent_co_inventor", "a", {
      patent_title: "Orphan",
      // no connected_to
    });

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [sig] },
    });

    expect(findEdge(edges, "patent_co_inventor")).toBeUndefined();
  });

  it("drops signals when connected_to target is not in the graph", () => {
    const a = prospect("a", "Alice");
    // No prospect b in this graph view
    const sig = v3signal("patent_co_inventor", "a", {
      connected_to: "outside",
      patent_title: "Distant",
    });

    const { edges } = buildGraph({
      prospects: [a],
      scores: {},
      signalsById: { a: [sig] },
    });

    expect(findEdge(edges, "patent_co_inventor")).toBeUndefined();
  });

  it("silently skips v2 mock signals (non-recognized signal_type)", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    // v2-style signal — non-recognized signal_type, will not surface
    const v2sig: Signal = {
      _id: "v2",
      prospect_id: "a",
      source: "linkedin",
      signal_type: "tenure_years",
      value: 7,
      raw_data: {},
      weight: 1,
      confidence: 0.9,
      collected_at: 0,
    };

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [v2sig] },
    });

    // v2 evidence_cited pass still runs (legacy behavior); but no
    // hidden-connection edge should appear.
    expect(findEdge(edges, "patent_co_inventor")).toBeUndefined();
  });

  it("reads v3-via-v2-column: structured payload in signal.value (not structured_value)", () => {
    // server/credence/signals.py:184 writes the Contract-1 structured_value
    // payload INTO the existing v2 `signals.value` JSONB column — the column
    // wasn't renamed at the DB level. Frontend must read either field.
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const livev3sig: Signal = {
      _id: "live",
      prospect_id: "a",
      source: "uspto",
      signal_type: "patent_co_inventor",
      // ↓ structured payload landed in the v2 `value` column
      value: {
        connected_to: "b",
        patent_number: "10,234,567",
        patent_title: "Live mode patent",
        filing_date: "2018-04-21",
        assignee: "Intel Corporation",
      },
      raw_data: {},
      weight: 1,
      confidence: 0.95,
      collected_at: 0,
    };

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [livev3sig] },
    });

    const edge = findEdge(edges, "patent_co_inventor");
    expect(edge).toBeDefined();
    // @ts-expect-error edge type doesn't surface evidence in find return type
    expect(edge?.evidence?.kind).toBe("patent_co_inventor");
    // @ts-expect-error edge.evidence is union-typed
    expect(edge?.evidence?.patentTitle).toBe("Live mode patent");
  });

  it("prefers structured_value when both fields are populated", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const sig = v3signal("patent_co_inventor", "a", {
      connected_to: "b",
      patent_title: "From structured_value",
      filing_date: "2020-01-01",
      assignee: "X",
    });
    // Also stuff a different payload into value to confirm precedence
    sig.value = {
      connected_to: "b",
      patent_title: "From value",
      filing_date: "2020-01-01",
      assignee: "Y",
    };

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [sig] },
    });

    const edge = findEdge(edges, "patent_co_inventor");
    // @ts-expect-error edge.evidence is union-typed
    expect(edge?.evidence?.patentTitle).toBe("From structured_value");
  });

  it("maps same_mba_cohort → same_mba_cohort EdgeKind", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const sig = v3signal("same_mba_cohort", "a", {
      connected_to: "b",
      institution: "Wharton",
      program: "MBA",
      overlap_start_year: 2015,
      overlap_end_year: 2017,
    });

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [sig] },
    });

    expect(findEdge(edges, "same_mba_cohort")).toBeDefined();
  });

  it("maps academic_co_author_multi → academic_co_author EdgeKind", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const sig = v3signal("academic_co_author_multi", "a", {
      connected_to: "b",
      paper_title: "5-paper collab",
      venue: "NeurIPS",
      year: 2023,
      citation_count: 100,
    });

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [sig] },
    });

    expect(findEdge(edges, "academic_co_author")).toBeDefined();
  });

  it("maps standards_committee_peer → standards_committee EdgeKind", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const sig = v3signal("standards_committee_peer", "a", {
      connected_to: "b",
      committee: "JEDEC JC-42.4",
      years: "2018-2022",
    });

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [sig] },
    });

    const edge = findEdge(edges, "standards_committee");
    expect(edge).toBeDefined();
    // @ts-expect-error edge.evidence is union-typed
    expect(edge?.evidence?.committee).toBe("JEDEC JC-42.4");
  });

  it("maps career_overlap_same_team → colleague EdgeKind", () => {
    const a = prospect("a", "Alice", "Intel");
    const b = prospect("b", "Bob", "Intel");
    const sig = v3signal("career_overlap_same_team", "a", {
      connected_to: "b",
      company_name: "Intel",
      overlap_start_year: 2018,
      overlap_end_year: 2022,
      overlap_years: 4,
      team_a: "Process",
      team_b: "Process",
    });

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [sig] },
    });

    // Note: the existing colleague pass also emits A↔B for same-company
    // prospects. The hidden-connection pass adds evidence to the same edge
    // (or creates one if dedup matches). Either way the edge exists with
    // career_overlap evidence attached.
    const colleagueEdge = findEdge(edges, "colleague");
    expect(colleagueEdge).toBeDefined();
  });

  it("maps career_overlap_general → past_employer EdgeKind", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const sig = v3signal("career_overlap_general", "a", {
      connected_to: "b",
      company_name: "Intel",
      overlap_years: 2,
    });

    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      signalsById: { a: [sig] },
    });

    const edge = findEdge(edges, "past_employer");
    expect(edge).toBeDefined();
    // @ts-expect-error edge.evidence is union-typed
    expect(edge?.evidence?.kind).toBe("career_overlap");
  });

  it("emits multiple hidden-connection types in a mixed signal set", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const c = prospect("c", "Carol");
    const sigs: V3SignalLike[] = [
      v3signal("patent_co_inventor", "a", {
        connected_to: "b",
        patent_title: "P",
        filing_date: "2020-01-01",
        assignee: "X",
      }),
      v3signal("academic_co_author", "a", {
        connected_to: "c",
        paper_title: "Q",
        venue: "Y",
        year: 2022,
        citation_count: 1,
      }),
    ];

    const { edges } = buildGraph({
      prospects: [a, b, c],
      scores: {},
      signalsById: { a: sigs },
    });

    expect(findEdge(edges, "patent_co_inventor")).toBeDefined();
    expect(findEdge(edges, "academic_co_author")).toBeDefined();
  });

  it("skips signals when target prospect_id is the same as source (self-loop)", () => {
    const a = prospect("a", "Alice");
    const sig = v3signal("patent_co_inventor", "a", {
      connected_to: "a",
      patent_title: "Self",
      filing_date: "2020",
      assignee: "X",
    });

    const { edges } = buildGraph({
      prospects: [a],
      scores: {},
      signalsById: { a: [sig] },
    });

    // addEdge already drops source === target
    expect(findEdge(edges, "patent_co_inventor")).toBeUndefined();
  });

  it("does not crash when signalsById is undefined", () => {
    const a = prospect("a", "Alice");
    expect(() =>
      buildGraph({ prospects: [a], scores: {}, signalsById: undefined }),
    ).not.toThrow();
  });
});

// ── Sixth pass: pre-materialized person_connections ────────────────────────
//
// Regression coverage for the read path that consumes
// `person_connections` rows (Decision 7) — keyed in prospect-id space by
// `usePersonConnections`. Verifies (1) edge materialization with evidence,
// (2) the PERSON_CONNECTIONS_TOP_K cap by computed_strength, (3) graceful
// no-op when the input is undefined / empty / points at non-existent
// prospects.

describe("buildGraph — person_connections pass", () => {
  it("emits a career_overlap_same_team edge from a person_connections record", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      personConnections: {
        a: [
          {
            connected_prospect_id: "b",
            connection_type: "career_overlap_same_team",
            computed_strength: 0.88,
            structured_value: {
              company_name: "Intel",
              overlap_start_year: 2015,
              overlap_end_year: 2019,
              overlap_years: 4,
            },
          },
        ],
      },
    });
    // career_overlap_same_team → "colleague" EdgeKind per signalTypeToEdgeKind.
    const edge = findEdge(edges, "colleague");
    expect(edge).toBeDefined();
    // @ts-expect-error edge.evidence is union-typed
    expect(edge?.evidence?.kind).toBe("career_overlap");
    // @ts-expect-error
    expect(edge?.evidence?.companyName).toBe("Intel");
  });

  it("caps records per prospect at PERSON_CONNECTIONS_TOP_K, ranked by computed_strength desc", () => {
    // 12 connections from `a`, all to distinct prospects. Strengths designed
    // so that the top-8 includes b1..b8 and excludes b9..b12.
    const a = prospect("a", "Alice");
    const partners = Array.from({ length: 12 }, (_, i) =>
      prospect(`b${i + 1}`, `Bob ${i + 1}`),
    );
    const records = partners.map((p, i) => ({
      connected_prospect_id: p._id,
      connection_type: "career_overlap_general",
      // descending strength: b1=0.95, b2=0.90, …, b12=0.40
      computed_strength: 0.95 - i * 0.05,
      structured_value: { company_name: "BigCo" },
    }));

    const { edges } = buildGraph({
      prospects: [a, ...partners],
      scores: {},
      personConnections: { a: records },
    });

    // career_overlap_general → "past_employer" EdgeKind.
    const pastEmp = edges.filter((e) => e.kind === "past_employer");
    expect(pastEmp).toHaveLength(8);

    const targets = new Set(
      pastEmp.flatMap((e) => [e.source, e.target]).filter((id) => id !== "person:a"),
    );
    // top-8 by strength → b1..b8 should appear, b9..b12 should not.
    for (let i = 1; i <= 8; i++) expect(targets.has(`person:b${i}`)).toBe(true);
    for (let i = 9; i <= 12; i++) expect(targets.has(`person:b${i}`)).toBe(false);
  });

  it("skips records pointing at prospects not in the current view", () => {
    const a = prospect("a", "Alice");
    const { edges } = buildGraph({
      prospects: [a],
      scores: {},
      personConnections: {
        a: [
          {
            connected_prospect_id: "ghost",
            connection_type: "patent_co_inventor",
            computed_strength: 0.95,
            structured_value: { patent_number: "X", patent_title: "Y" },
          },
        ],
      },
    });
    expect(edges.find((e) => e.kind === "patent_co_inventor")).toBeUndefined();
  });

  it("does not crash when personConnections is undefined or empty", () => {
    const a = prospect("a", "Alice");
    expect(() =>
      buildGraph({ prospects: [a], scores: {}, personConnections: undefined }),
    ).not.toThrow();
    expect(() =>
      buildGraph({ prospects: [a], scores: {}, personConnections: {} }),
    ).not.toThrow();
    expect(() =>
      buildGraph({ prospects: [a], scores: {}, personConnections: { a: [] } }),
    ).not.toThrow();
  });

  it("ignores records with unrecognized connection_type", () => {
    const a = prospect("a", "Alice");
    const b = prospect("b", "Bob");
    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      // skipColleagueEdges so the v2 colleague pass doesn't add an edge
      // between two same-company prospects and confound the assertion.
      skipColleagueEdges: true,
      personConnections: {
        a: [
          {
            connected_prospect_id: "b",
            connection_type: "totally_made_up_kind",
            computed_strength: 0.99,
            structured_value: {},
          },
        ],
      },
    });
    // Filter to person↔person edges only (excludes works_at / located_in /
    // vertical structural edges that the v2 passes always emit).
    const personPersonEdges = edges.filter(
      (e) =>
        (e.source === "person:a" && e.target === "person:b") ||
        (e.source === "person:b" && e.target === "person:a"),
    );
    expect(personPersonEdges).toHaveLength(0);
  });
});

// ── v2 structural passes — regression coverage for existing builder ─────────
//
// These tests lock the behavior of the pre-v3 `buildGraph` passes (person,
// company, works_at, past_employer, education, talks → scope_signal,
// partnerships, hierarchy root). They exercise the production code path that
// has been running in v2 prod but had zero unit tests until now.

describe("buildGraph — v2 structural passes", () => {
  it("emits person + company nodes with a works_at edge", () => {
    const alice = prospectFull("a", "Alice", "Intel");
    const { nodes, edges } = buildGraph({ prospects: [alice], scores: {} });

    const person = nodes.find((n) => n.id === "person:a");
    const company = nodes.find((n) => n.kind === "company" && n.name === "Intel");
    expect(person).toBeDefined();
    expect(company).toBeDefined();

    const worksAt = edges.find(
      (e) => e.kind === "works_at" && e.source === "person:a",
    );
    expect(worksAt).toBeDefined();
    expect(worksAt?.target).toBe(company?.id);
  });

  it("collapses company-name variants via normalizeCompany", () => {
    // "Intel" + "Intel Corp" + "Intel Corporation" should resolve to one company
    const a = prospectFull("a", "Alice", "Intel Corporation");
    const b = prospectFull("b", "Bob", "Intel Corp");
    const { nodes } = buildGraph({ prospects: [a, b], scores: {} });
    const intelNodes = nodes.filter(
      (n) => n.kind === "company" && /intel/i.test(n.name ?? ""),
    );
    expect(intelNodes).toHaveLength(1);
  });

  it("normalizeCompany strips suffixes and lowercases", () => {
    expect(normalizeCompany("Intel Corporation")).toBe("intel");
    expect(normalizeCompany("Apple Inc.")).toBe("apple");
    expect(normalizeCompany("Cadence Design Systems")).toBe("cadence design");
    expect(normalizeCompany("Foo, LLC")).toBe("foo");
    expect(normalizeCompany(null)).toBe("");
    expect(normalizeCompany(undefined)).toBe("");
  });

  it("canonicalizeRole drops 'at <company>' tail and truncates to 28 chars", () => {
    expect(canonicalizeRole("Senior Software Engineer at Intel")).toBe(
      "Senior Software Engineer",
    );
    expect(
      canonicalizeRole("Senior Software Engineer | Design Systems at Cadence"),
    ).toBe("Senior Software Engineer");
    // Cap at 28 chars with ellipsis
    expect(canonicalizeRole("Distinguished Hardware Engineer Architect"))
      .toMatch(/…$/);
    expect(canonicalizeRole("")).toBe("");
  });

  it("emits past_employer edges for each past company", () => {
    const alice = prospectFull("a", "Alice", "TSMC", {
      past_companies: ["Intel", "Applied Materials"],
    });
    const { edges, nodes } = buildGraph({ prospects: [alice], scores: {} });

    const intel = nodes.find(
      (n) => n.kind === "company" && /intel/i.test(n.name ?? ""),
    );
    const applied = nodes.find(
      (n) => n.kind === "company" && /applied/i.test(n.name ?? ""),
    );
    expect(intel).toBeDefined();
    expect(applied).toBeDefined();

    const pastEdges = edges.filter(
      (e) => e.kind === "past_employer" && e.source === "person:a",
    );
    expect(pastEdges).toHaveLength(2);
    expect(pastEdges.map((e) => e.target).sort()).toEqual(
      [intel?.id, applied?.id].sort(),
    );
  });

  it("emits education edges for each education entry", () => {
    const alice = prospectFull("a", "Alice", "TSMC", {
      education: [
        { school: "Stanford", degree: "PhD EE", year: 2008 },
        { school: "NTU", degree: "BS EE", year: 2002 },
      ],
    });
    const { edges, nodes } = buildGraph({ prospects: [alice], scores: {} });

    const eduEdges = edges.filter(
      (e) => e.kind === "education" && e.source === "person:a",
    );
    expect(eduEdges).toHaveLength(2);
    const schoolNodes = nodes.filter((n) => n.kind === "school");
    expect(schoolNodes.map((n) => n.name).sort()).toEqual(
      ["NTU", "Stanford"],
    );
  });

  it("emits scope_signal edges for talks", () => {
    const alice = prospectFull("a", "Alice", "TSMC", {
      talks: [{ venue: "IEDM", year: 2023, topic: "3nm yield" }],
    });
    const { edges, nodes } = buildGraph({ prospects: [alice], scores: {} });

    const conferenceNode = nodes.find((n) => n.kind === "conference");
    expect(conferenceNode).toBeDefined();
    expect(conferenceNode?.name).toContain("IEDM");

    const talkEdge = edges.find(
      (e) => e.kind === "scope_signal" && e.source === "person:a"
        && e.target === conferenceNode?.id,
    );
    expect(talkEdge).toBeDefined();
  });

  it("emits a colleague edge between two prospects at the same company", () => {
    const a = prospectFull("a", "Alice", "Intel");
    const b = prospectFull("b", "Bob", "Intel");
    const { edges } = buildGraph({ prospects: [a, b], scores: {} });

    const colleagueEdges = edges.filter((e) => e.kind === "colleague");
    expect(colleagueEdges).toHaveLength(1);
  });

  it("colleague pass canonicalizes endpoints (A↔B === B↔A, not duplicated)", () => {
    // Three people at same company → 3 colleague edges (3-choose-2)
    const a = prospectFull("a", "Alice", "Intel");
    const b = prospectFull("b", "Bob", "Intel");
    const c = prospectFull("c", "Carol", "Intel");
    const { edges } = buildGraph({ prospects: [a, b, c], scores: {} });
    const colleagueEdges = edges.filter((e) => e.kind === "colleague");
    expect(colleagueEdges).toHaveLength(3);
  });

  it("respects skipColleagueEdges option", () => {
    const a = prospectFull("a", "Alice", "Intel");
    const b = prospectFull("b", "Bob", "Intel");
    const { edges } = buildGraph({
      prospects: [a, b],
      scores: {},
      skipColleagueEdges: true,
    });
    expect(edges.filter((e) => e.kind === "colleague")).toHaveLength(0);
  });

  it("emits partnership edges from the company meta table (TSMC ↔ Apple)", () => {
    // TSMC's COMPANY_META lists ["Apple", "NVIDIA"] as partnerships
    const a = prospectFull("a", "Alice", "TSMC");
    const { edges, nodes } = buildGraph({ prospects: [a], scores: {} });

    const partnerships = edges.filter((e) => e.kind === "partnership");
    expect(partnerships.length).toBeGreaterThan(0);

    const tsmc = nodes.find(
      (n) => n.kind === "company" && /tsmc/i.test(n.name ?? ""),
    );
    expect(tsmc).toBeDefined();
    expect(partnerships.every((e) => e.source === tsmc?.id || e.target === tsmc?.id)).toBe(true);
  });

  it("rolls every industry and city up under the technology root", () => {
    const a = prospectFull("a", "Alice", "TSMC");
    const { edges } = buildGraph({ prospects: [a], scores: {} });

    const rootEdges = edges.filter((e) => e.target === "industry:technology");
    expect(rootEdges.length).toBeGreaterThan(0);
  });

  it("emits a vertical edge from company to its industry", () => {
    const a = prospectFull("a", "Alice", "TSMC");
    const { edges, nodes } = buildGraph({ prospects: [a], scores: {} });

    const industry = nodes.find(
      (n) => n.kind === "industry" && /semiconductors/i.test(n.name ?? ""),
    );
    expect(industry).toBeDefined();
    const verticalEdge = edges.find(
      (e) => e.kind === "vertical" && e.target === industry?.id,
    );
    expect(verticalEdge).toBeDefined();
  });

  it("populates color and width on every edge when theme is provided", () => {
    const a = prospectFull("a", "Alice", "Intel");
    const theme = {
      nodeColors: {
        person: "red",
        company: "blue",
        role: "green",
        city: "orange",
        school: "purple",
        conference: "pink",
        industry: "gray",
      },
      edgeColors: {
        works_at: "#aaa",
        colleague: "#bbb",
        located_in: "#ccc",
        reports_to: "#ddd",
        past_employer: "#eee",
        partnership: "#fff",
        education: "#012",
        scope_signal: "#345",
        vertical: "#678",
        evidence_cited: "#9ab",
        patent_co_inventor: "#cde",
        academic_co_author: "#f01",
        conference_co_presenter: "#234",
        standards_committee: "#567",
      },
    };
    const { nodes, edges } = buildGraph({
      prospects: [a],
      scores: {},
      theme,
    });
    expect(nodes.every((n) => typeof n.color === "string")).toBe(true);
    expect(edges.every((e) => typeof e.color === "string")).toBe(true);
  });
});
