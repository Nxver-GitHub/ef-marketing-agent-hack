import { describe, expect, it } from "vitest";
import {
  breakdownScore,
  fabricateBreakdown,
  fabricateFalsificationNotes,
  normalize01to100,
  numericFromValue,
  synthesizeBreakdown,
} from "./scoreMath";
import type { Signal, SignalWeight } from "./mockStore";

const sig = (over: Partial<Signal> & { signal_type: string }): Signal => ({
  _id: `sig-${Math.random().toString(36).slice(2, 8)}`,
  prospect_id: "p1",
  source: "linkedin_profile",
  signal_type: over.signal_type,
  value: 8,
  raw_data: {},
  weight: 1,
  confidence: 0.8,
  collected_at: Date.now(),
  ...over,
});

const wt = (
  signal_type: string,
  authenticity: number,
  authority: number,
  warmth: number,
): SignalWeight => ({
  _id: `w-${signal_type}`,
  signal_type,
  authenticity_weight: authenticity,
  authority_weight: authority,
  warmth_weight: warmth,
});

describe("normalize01to100", () => {
  it("clamps to [0,100]", () => {
    expect(normalize01to100(0)).toBe(0);
    expect(normalize01to100(1000)).toBeCloseTo(100, 1);
    expect(normalize01to100(-50)).toBe(0);
  });

  it("monotonic for positive inputs", () => {
    const a = normalize01to100(2);
    const b = normalize01to100(8);
    const c = normalize01to100(20);
    expect(b).toBeGreaterThan(a);
    expect(c).toBeGreaterThan(b);
  });
});

describe("numericFromValue", () => {
  it("parses numbers and numeric strings", () => {
    expect(numericFromValue(7)).toBe(7);
    expect(numericFromValue("12")).toBe(12);
    expect(numericFromValue("oops")).toBe(0);
  });

  it("unwraps nested {value} blobs", () => {
    expect(numericFromValue({ value: 4 })).toBe(4);
    expect(numericFromValue({ value: { value: 9 } })).toBe(9);
  });

  it("returns 0 for unknown shapes", () => {
    expect(numericFromValue(null)).toBe(0);
    expect(numericFromValue(undefined)).toBe(0);
    expect(numericFromValue({})).toBe(0);
  });
});

describe("breakdownScore", () => {
  it("attributes contributions to the right sub-score buckets", () => {
    const signals = [
      sig({ signal_type: "tenure_years", value: 6 }),
      sig({ signal_type: "patent_count", value: 4 }),
      sig({ signal_type: "mutual_connections", value: 12 }),
    ];
    const weights = [
      wt("tenure_years", 0.7, 0.2, 0.1),
      wt("patent_count", 0.1, 0.8, 0.1),
      wt("mutual_connections", 0.0, 0.1, 0.9),
    ];

    const out = breakdownScore(signals, weights);
    // mutual_connections has authenticity_weight=0 so it should not contribute to authenticity
    expect(out.authenticity.length).toBe(2);
    expect(out.authority.length).toBe(3);
    expect(out.warmth.length).toBe(3);

    const tenureRow = out.authenticity.find((c) => c.signal_type === "tenure_years");
    expect(tenureRow?.pctOfSubScore).toBeGreaterThan(0);
    expect(out.subScores.overall).toBeGreaterThan(0);
  });

  it("ignores signals with no matching weight row", () => {
    const signals = [sig({ signal_type: "unknown_type" })];
    const out = breakdownScore(signals, [wt("tenure_years", 1, 1, 1)]);
    expect(out.authenticity).toHaveLength(0);
    expect(out.authority).toHaveLength(0);
    expect(out.warmth).toHaveLength(0);
  });
});

describe("synthesizeBreakdown", () => {
  it("builds plausible rows from signals when weights are unmapped", () => {
    const signals = [
      sig({ signal_type: "tenure_years", value: 5 }),
      sig({ signal_type: "patent_count", value: 3 }),
      sig({ signal_type: "github_commits", value: 100 }),
    ];
    const out = synthesizeBreakdown(signals);
    const rows =
      out.authenticity.length + out.authority.length + out.warmth.length;
    expect(rows).toBeGreaterThan(0);
  });
});

describe("fabricateBreakdown / fabricateFalsificationNotes", () => {
  it("produces stable rows for the same id", () => {
    const subs = { authenticity: 70, authority: 80, warmth: 60 };
    const a = fabricateBreakdown("person:42", subs);
    const b = fabricateBreakdown("person:42", subs);
    expect(a.authenticity.map((c) => c.signal_type)).toEqual(
      b.authenticity.map((c) => c.signal_type),
    );
  });

  it("emits at least one falsification note for non-zero overall", () => {
    const notes = fabricateFalsificationNotes("person:9", {
      authenticity: 50,
      authority: 70,
      warmth: 40,
    });
    expect(notes.length).toBeGreaterThan(0);
  });
});
