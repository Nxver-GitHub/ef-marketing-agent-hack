/**
 * Unit tests for `evidenceFromSignal` — the Contract 1 → EdgeEvidence bridge.
 *
 * Covers: each of the 5 EdgeEvidence variants, all backend signal_type
 * aliases, fallback for unknown types, fallback for malformed input, sparse-
 * field tolerance.
 */

import { describe, it, expect } from "vitest";
import {
  RECOGNIZED_CONNECTION_SIGNAL_TYPES,
  evidenceFromSignal,
  type SignalLike,
} from "./evidenceFromSignal";

// ── Patent ───────────────────────────────────────────────────────────────────

describe("evidenceFromSignal — patent_co_inventor", () => {
  const sig: SignalLike = {
    signal_type: "patent_co_inventor",
    structured_value: {
      connected_to: "p:c",
      patent_number: "10,234,567",
      patent_title: "Method for 3nm yield optimization",
      filing_date: "2018-04-21",
      grant_date: "2020-01-14",
      assignee: "Intel Corporation",
      uspto_url: "https://patents.uspto.gov/10234567",
    },
  };

  it("maps full patent record", () => {
    expect(evidenceFromSignal(sig)).toEqual({
      kind: "patent_co_inventor",
      patentNumber: "10,234,567",
      patentTitle: "Method for 3nm yield optimization",
      filingDate: "2018-04-21",
      grantDate: "2020-01-14",
      assignee: "Intel Corporation",
      usptoUrl: "https://patents.uspto.gov/10234567",
    });
  });

  it("returns null grant_date and uspto_url when missing", () => {
    const sparse = {
      ...sig,
      structured_value: {
        ...sig.structured_value,
        grant_date: undefined,
        uspto_url: undefined,
      },
    } as SignalLike;
    const result = evidenceFromSignal(sparse);
    expect(result?.kind).toBe("patent_co_inventor");
    if (result?.kind === "patent_co_inventor") {
      expect(result.grantDate).toBeNull();
      expect(result.usptoUrl).toBeNull();
    }
  });

  it("propagates empty strings to required fields when missing", () => {
    const result = evidenceFromSignal({
      signal_type: "patent_co_inventor",
      structured_value: { connected_to: "p:c" },
    });
    expect(result?.kind).toBe("patent_co_inventor");
    if (result?.kind === "patent_co_inventor") {
      expect(result.patentNumber).toBe("");
      expect(result.patentTitle).toBe("");
      expect(result.assignee).toBe("");
    }
  });
});

// ── Paper (3 backend aliases) ────────────────────────────────────────────────

describe("evidenceFromSignal — academic_co_author + variants", () => {
  const base = {
    paper_title: "Accelerator design for LLM training",
    venue: "NeurIPS",
    year: 2023,
    citation_count: 42,
    semantic_scholar_id: "abcd1234",
    doi: "10.1234/example",
  };

  it("maps academic_co_author signal_type", () => {
    const result = evidenceFromSignal({
      signal_type: "academic_co_author",
      structured_value: base,
    });
    expect(result?.kind).toBe("academic_co_author");
    if (result?.kind === "academic_co_author") {
      expect(result.paperTitle).toBe("Accelerator design for LLM training");
      expect(result.venue).toBe("NeurIPS");
      expect(result.year).toBe(2023);
      expect(result.citationCount).toBe(42);
    }
  });

  it("maps academic_co_author_single (backend alias)", () => {
    const result = evidenceFromSignal({
      signal_type: "academic_co_author_single",
      structured_value: base,
    });
    expect(result?.kind).toBe("academic_co_author");
  });

  it("maps academic_co_author_multi (backend alias)", () => {
    const result = evidenceFromSignal({
      signal_type: "academic_co_author_multi",
      structured_value: base,
    });
    expect(result?.kind).toBe("academic_co_author");
  });

  it("coerces stringified numbers", () => {
    const result = evidenceFromSignal({
      signal_type: "academic_co_author",
      structured_value: { ...base, year: "2023", citation_count: "42" },
    });
    if (result?.kind === "academic_co_author") {
      expect(result.year).toBe(2023);
      expect(result.citationCount).toBe(42);
    }
  });

  it("preserves NaN year when value is invalid (downstream renders 'year unknown')", () => {
    const result = evidenceFromSignal({
      signal_type: "academic_co_author",
      structured_value: { ...base, year: "not-a-year" },
    });
    if (result?.kind === "academic_co_author") {
      expect(Number.isNaN(result.year)).toBe(true);
    }
  });
});

// ── Conference ──────────────────────────────────────────────────────────────

describe("evidenceFromSignal — conference_co_presenter", () => {
  it("maps full conference record", () => {
    const result = evidenceFromSignal({
      signal_type: "conference_co_presenter",
      structured_value: {
        event: "SPIE Advanced Lithography",
        year: 2024,
      },
    });
    expect(result).toEqual({
      kind: "conference_co_presenter",
      event: "SPIE Advanced Lithography",
      year: 2024,
    });
  });

  it("returns NaN year when omitted", () => {
    const result = evidenceFromSignal({
      signal_type: "conference_co_presenter",
      structured_value: { event: "SPIE" },
    });
    if (result?.kind === "conference_co_presenter") {
      expect(Number.isNaN(result.year)).toBe(true);
    }
  });
});

// ── Standards (2 backend aliases) ────────────────────────────────────────────

describe("evidenceFromSignal — standards_committee + peer alias", () => {
  it("maps standards_committee signal_type", () => {
    const result = evidenceFromSignal({
      signal_type: "standards_committee",
      structured_value: {
        committee: "JEDEC JC-42.4",
        years: "2018-2022",
      },
    });
    expect(result).toEqual({
      kind: "standards_committee",
      committee: "JEDEC JC-42.4",
      years: "2018-2022",
    });
  });

  it("maps standards_committee_peer (backend alias)", () => {
    const result = evidenceFromSignal({
      signal_type: "standards_committee_peer",
      structured_value: {
        committee: "IEEE 802.3",
        years: "2015-2020",
      },
    });
    expect(result?.kind).toBe("standards_committee");
  });
});

// ── Career overlap (3 sub-types) ─────────────────────────────────────────────

describe("evidenceFromSignal — career_overlap variants", () => {
  const base = {
    company_id: "co:1",
    company_name: "Intel",
    overlap_start_year: 2007,
    overlap_end_year: 2011,
    overlap_years: 4,
    team_a: "Process Engineering",
    team_b: "Process Engineering",
    domain_a: "hardware_engineering",
    domain_b: "hardware_engineering",
    seniority_gap: 5,
  };

  it("maps career_overlap_same_team", () => {
    const result = evidenceFromSignal({
      signal_type: "career_overlap_same_team",
      structured_value: base,
    });
    expect(result?.kind).toBe("career_overlap");
    if (result?.kind === "career_overlap") {
      expect(result.companyName).toBe("Intel");
      expect(result.overlapYears).toBe(4);
      expect(result.teamA).toBe("Process Engineering");
      expect(result.teamB).toBe("Process Engineering");
      expect(result.seniorityGap).toBe(5);
    }
  });

  it("maps career_overlap_same_domain", () => {
    const result = evidenceFromSignal({
      signal_type: "career_overlap_same_domain",
      structured_value: { ...base, team_a: null, team_b: null },
    });
    if (result?.kind === "career_overlap") {
      expect(result.teamA).toBeNull();
      expect(result.teamB).toBeNull();
      expect(result.domainA).toBe("hardware_engineering");
    }
  });

  it("maps career_overlap_general", () => {
    const result = evidenceFromSignal({
      signal_type: "career_overlap_general",
      structured_value: base,
    });
    expect(result?.kind).toBe("career_overlap");
  });
});

// ── Negative paths ──────────────────────────────────────────────────────────

describe("evidenceFromSignal — fallback to null", () => {
  it("returns null for unknown signal_type", () => {
    expect(
      evidenceFromSignal({ signal_type: "unknown_signal", structured_value: {} }),
    ).toBeNull();
  });

  it("returns null for empty signal_type", () => {
    expect(evidenceFromSignal({ signal_type: "", structured_value: {} })).toBeNull();
  });

  it("returns null when structured_value is missing", () => {
    // @ts-expect-error testing runtime defense
    expect(evidenceFromSignal({ signal_type: "patent_co_inventor" })).toBeNull();
  });

  it("returns null when structured_value is null", () => {
    expect(
      evidenceFromSignal({
        signal_type: "patent_co_inventor",
        // @ts-expect-error testing runtime defense
        structured_value: null,
      }),
    ).toBeNull();
  });

  it("returns null when structured_value is an array", () => {
    expect(
      evidenceFromSignal({
        signal_type: "patent_co_inventor",
        // @ts-expect-error testing runtime defense
        structured_value: [],
      }),
    ).toBeNull();
  });

  it("returns null when sig itself is malformed", () => {
    // @ts-expect-error testing runtime defense
    expect(evidenceFromSignal(null)).toBeNull();
    // @ts-expect-error testing runtime defense
    expect(evidenceFromSignal(undefined)).toBeNull();
    // @ts-expect-error testing runtime defense
    expect(evidenceFromSignal("string")).toBeNull();
  });
});

// ── Catalog of recognized types ──────────────────────────────────────────────

describe("RECOGNIZED_CONNECTION_SIGNAL_TYPES", () => {
  it("includes the 4 v3 hidden-connection types", () => {
    expect(RECOGNIZED_CONNECTION_SIGNAL_TYPES).toContain("patent_co_inventor");
    expect(RECOGNIZED_CONNECTION_SIGNAL_TYPES).toContain("academic_co_author");
    expect(RECOGNIZED_CONNECTION_SIGNAL_TYPES).toContain("conference_co_presenter");
    expect(RECOGNIZED_CONNECTION_SIGNAL_TYPES).toContain("standards_committee");
  });

  it("includes the 3 career_overlap sub-types", () => {
    expect(RECOGNIZED_CONNECTION_SIGNAL_TYPES).toContain("career_overlap_same_team");
    expect(RECOGNIZED_CONNECTION_SIGNAL_TYPES).toContain("career_overlap_same_domain");
    expect(RECOGNIZED_CONNECTION_SIGNAL_TYPES).toContain("career_overlap_general");
  });

  it("each recognized type maps to a non-null evidence record", () => {
    // Provide a stub structured_value with all common fields so each adapter
    // can produce something. Recognized types must round-trip to a non-null
    // evidence — that's the contract of being on this list.
    const allFields = {
      patent_number: "1",
      patent_title: "x",
      filing_date: "2024-01-01",
      assignee: "x",
      paper_title: "x",
      venue: "x",
      year: 2024,
      citation_count: 0,
      event: "x",
      committee: "x",
      years: "x",
      company_name: "x",
      overlap_start_year: 2020,
      overlap_end_year: 2022,
      overlap_years: 2,
    };
    for (const t of RECOGNIZED_CONNECTION_SIGNAL_TYPES) {
      const result = evidenceFromSignal({ signal_type: t, structured_value: allFields });
      expect(result, `signal_type ${t} produced null`).not.toBeNull();
    }
  });
});
