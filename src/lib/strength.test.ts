import { describe, expect, it } from "vitest";

import {
  ALL_CONNECTION_TYPES,
  DECAY_RATES,
  STRENGTH_CAP,
  STRENGTH_TABLE,
  computeStrength,
  computeStrengthForType,
} from "./strength";

describe("STRENGTH_TABLE / DECAY_RATES", () => {
  it("has matching keys in both tables", () => {
    expect(Object.keys(STRENGTH_TABLE).sort()).toEqual(
      Object.keys(DECAY_RATES).sort(),
    );
  });

  it("ALL_CONNECTION_TYPES enumerates every key in STRENGTH_TABLE", () => {
    expect([...ALL_CONNECTION_TYPES].sort()).toEqual(
      Object.keys(STRENGTH_TABLE).sort(),
    );
  });

  it("matches CLAUDE.md base-strength values", () => {
    expect(STRENGTH_TABLE.patent_co_inventor).toBe(0.95);
    expect(STRENGTH_TABLE.same_phd_advisor).toBe(0.92);
    expect(STRENGTH_TABLE.career_overlap_same_team).toBe(0.88);
    expect(STRENGTH_TABLE.alumni_network).toBe(0.25);
    expect(STRENGTH_TABLE.conference_co_attendee).toBe(0.2);
  });

  it("matches CLAUDE.md decay-rate values", () => {
    expect(DECAY_RATES.patent_co_inventor).toBe(0.01);
    expect(DECAY_RATES.career_overlap_same_team).toBe(0.04);
    expect(DECAY_RATES.alumni_network).toBe(0.08);
    expect(DECAY_RATES.conference_co_attendee).toBe(0.2);
  });

  it("matches V3_PT2.md education-cohort base-strength values", () => {
    expect(STRENGTH_TABLE.same_mba_cohort).toBe(0.85);
    expect(STRENGTH_TABLE.same_phd_program).toBe(0.78);
    expect(STRENGTH_TABLE.executive_education).toBe(0.7);
    expect(STRENGTH_TABLE.same_undergrad_cohort).toBe(0.62);
  });

  it("matches V3_PT2.md education-cohort decay-rate values", () => {
    expect(DECAY_RATES.same_mba_cohort).toBe(0.02);
    expect(DECAY_RATES.same_phd_program).toBe(0.02);
    expect(DECAY_RATES.executive_education).toBe(0.03);
    expect(DECAY_RATES.same_undergrad_cohort).toBe(0.04);
  });

  it("STRENGTH_TABLE is frozen", () => {
    expect(Object.isFrozen(STRENGTH_TABLE)).toBe(true);
    expect(Object.isFrozen(DECAY_RATES)).toBe(true);
  });
});

describe("computeStrength", () => {
  it("matches the CLAUDE.md worked example: patent, 7y inactive, 2 corrob, 2 sources → capped at 0.99", () => {
    // CLAUDE.md says: 0.95 * exp(-0.01*7) * (1 + ln(2)*0.15) * (1 + 2*0.10)
    //               = 0.95 * 0.9324 * 1.10397 * 1.20
    //               ≈ 1.173 → min(0.99, 1.173) = 0.99
    const s = computeStrength({
      base: 0.95,
      decayRate: 0.01,
      yearsSinceActive: 7,
      corroborationCount: 2,
      sourceTypeCount: 2,
    });
    expect(s).toBe(STRENGTH_CAP);
  });

  it("returns base strength when years=0, corrob=1, sources=1 (after frequency/corroboration multipliers)", () => {
    // recency = 1, frequency = 1 + ln(1)*0.15 = 1, corroboration = 1 + 0.10 = 1.10
    // → base * 1 * 1 * 1.10
    const s = computeStrength({
      base: 0.5,
      decayRate: 0.05,
      yearsSinceActive: 0,
      corroborationCount: 1,
      sourceTypeCount: 1,
    });
    expect(s).toBeCloseTo(0.55, 10);
  });

  it("decays exponentially with years inactive", () => {
    const fresh = computeStrength({
      base: 0.8,
      decayRate: 0.05,
      yearsSinceActive: 0,
    });
    const old = computeStrength({
      base: 0.8,
      decayRate: 0.05,
      yearsSinceActive: 20,
    });
    expect(old).toBeLessThan(fresh);
    // 0.8 * exp(-0.05 * 20) = 0.8 * 0.3679 = 0.2943; * 1.10 (default sources) = 0.3237
    expect(old).toBeCloseTo(0.8 * Math.exp(-1) * 1.1, 6);
  });

  it("caps at STRENGTH_CAP (0.99)", () => {
    const s = computeStrength({
      base: 0.99,
      decayRate: 0,
      yearsSinceActive: 0,
      corroborationCount: 100,
      sourceTypeCount: 5,
    });
    expect(s).toBe(STRENGTH_CAP);
  });

  it("is deterministic — same inputs produce same output across calls", () => {
    const args = {
      base: 0.7,
      decayRate: 0.04,
      yearsSinceActive: 3,
      corroborationCount: 4,
      sourceTypeCount: 2,
    };
    expect(computeStrength(args)).toBe(computeStrength(args));
  });

  it("does not mutate input", () => {
    const args = {
      base: 0.6,
      decayRate: 0.04,
      yearsSinceActive: 5,
      corroborationCount: 2,
      sourceTypeCount: 1,
    };
    const snapshot = { ...args };
    computeStrength(args);
    expect(args).toEqual(snapshot);
  });

  describe("input validation", () => {
    it("throws on base out of [0,1]", () => {
      expect(() =>
        computeStrength({ base: -0.1, decayRate: 0.01, yearsSinceActive: 1 }),
      ).toThrow(RangeError);
      expect(() =>
        computeStrength({ base: 1.5, decayRate: 0.01, yearsSinceActive: 1 }),
      ).toThrow(RangeError);
    });
    it("throws on negative decayRate", () => {
      expect(() =>
        computeStrength({ base: 0.5, decayRate: -0.01, yearsSinceActive: 1 }),
      ).toThrow(RangeError);
    });
    it("throws on negative yearsSinceActive", () => {
      expect(() =>
        computeStrength({ base: 0.5, decayRate: 0.01, yearsSinceActive: -1 }),
      ).toThrow(RangeError);
    });
    it("throws on corroborationCount < 1", () => {
      expect(() =>
        computeStrength({
          base: 0.5,
          decayRate: 0.01,
          yearsSinceActive: 1,
          corroborationCount: 0,
        }),
      ).toThrow(RangeError);
    });
    it("throws on sourceTypeCount < 1", () => {
      expect(() =>
        computeStrength({
          base: 0.5,
          decayRate: 0.01,
          yearsSinceActive: 1,
          sourceTypeCount: 0,
        }),
      ).toThrow(RangeError);
    });
    it("throws on non-integer corroborationCount", () => {
      expect(() =>
        computeStrength({
          base: 0.5,
          decayRate: 0.01,
          yearsSinceActive: 1,
          corroborationCount: 1.5,
        }),
      ).toThrow(RangeError);
    });
  });
});

describe("computeStrengthForType", () => {
  it("uses STRENGTH_TABLE / DECAY_RATES lookups", () => {
    const direct = computeStrength({
      base: STRENGTH_TABLE.patent_co_inventor,
      decayRate: DECAY_RATES.patent_co_inventor,
      yearsSinceActive: 5,
      corroborationCount: 1,
      sourceTypeCount: 1,
    });
    const viaType = computeStrengthForType("patent_co_inventor", 5, 1, 1);
    expect(viaType).toBe(direct);
  });

  it("orders types correctly: fresh patent > stale alumni at year 0", () => {
    expect(computeStrengthForType("patent_co_inventor", 0)).toBeGreaterThan(
      computeStrengthForType("alumni_network", 0),
    );
  });

  it("conference_co_attendee decays sharply (decay 0.20)", () => {
    const fresh = computeStrengthForType("conference_co_attendee", 0);
    const aged = computeStrengthForType("conference_co_attendee", 10);
    // exp(-0.20 * 10) = exp(-2) ≈ 0.135 → ratio of strengths matches recency
    expect(aged / fresh).toBeCloseTo(Math.exp(-2), 6);
  });
});
