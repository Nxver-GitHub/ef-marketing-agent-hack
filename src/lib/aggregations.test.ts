import { describe, expect, it } from "vitest";
import { computeHubStats, prospectIdsForAggregation, type AggregationProspect } from "./aggregations";
import { normalizeCompany, normalizeKey } from "./graph";

const mk = (over: Partial<AggregationProspect> & { _id: string }): AggregationProspect => ({
  _id: over._id,
  name: `Person ${over._id}`,
  company: "Micron Technology",
  role: "Senior Engineer",
  industry: "Semiconductors",
  past_companies: [],
  education: [],
  talks: [],
  ...over,
});

describe("prospectIdsForAggregation", () => {
  const people: AggregationProspect[] = [
    mk({ _id: "a", company: "Micron Technology" }),
    mk({ _id: "b", company: "Intel", past_companies: ["Micron"] }),
    mk({ _id: "c", company: "Nvidia" }),
    mk({ _id: "d", company: "Intel", role: "VP of Engineering" }),
    mk({ _id: "e", education: [{ school: "MIT" }], company: "Apple" }),
  ];

  it("returns null for null/person/unsupported ids", () => {
    expect(prospectIdsForAggregation(null, people)).toBeNull();
    expect(prospectIdsForAggregation("", people)).toBeNull();
    expect(prospectIdsForAggregation("person:abc", people)).toBeNull();
    expect(prospectIdsForAggregation("noColon", people)).toBeNull();
  });

  it("matches current and past company members", () => {
    const ids = prospectIdsForAggregation(
      `company:${normalizeCompany("Micron Technology")}`,
      people,
    );
    expect(ids).not.toBeNull();
    expect(ids!.has("a")).toBe(true);
    expect(ids!.has("b")).toBe(true);
    expect(ids!.has("c")).toBe(false);
  });

  it("matches school members via education[]", () => {
    const ids = prospectIdsForAggregation(`school:${normalizeKey("MIT")}`, people);
    expect(ids).not.toBeNull();
    expect(ids!.has("e")).toBe(true);
  });

  it("matches role members via canonicalized role", () => {
    const ids = prospectIdsForAggregation(
      `role:${normalizeKey("VP of Engineering")}`,
      people,
    );
    expect(ids).not.toBeNull();
    expect(ids!.has("d")).toBe(true);
  });
});

describe("computeHubStats", () => {
  const people: AggregationProspect[] = [
    mk({ _id: "a", company: "Micron", role: "Engineer", industry: "Semiconductors" }),
    mk({ _id: "b", company: "Micron", role: "Engineer", industry: "Semiconductors" }),
    mk({ _id: "c", company: "Micron", role: "VP of Engineering", industry: "Semiconductors" }),
  ];
  const scores = {
    a: { overall_score: 82 },
    b: { overall_score: 60 },
    c: { overall_score: 90 },
  };

  it("rolls up totals, averages, and high-confidence count", () => {
    const stats = computeHubStats(new Set(["a", "b", "c"]), people, scores);
    expect(stats.total).toBe(3);
    expect(stats.avgScore).toBeCloseTo((82 + 60 + 90) / 3, 1);
    expect(stats.highConf).toBe(2); // 82 and 90
  });

  it("returns top roles and industries", () => {
    const stats = computeHubStats(new Set(["a", "b", "c"]), people, scores);
    expect(stats.topRoles[0].count).toBeGreaterThanOrEqual(1);
    expect(stats.topIndustries[0].label).toBe("Semiconductors");
  });

  it("ranks topPeople by score desc", () => {
    const stats = computeHubStats(new Set(["a", "b", "c"]), people, scores);
    expect(stats.topPeople[0].id).toBe("c");
    expect(stats.topPeople[1].id).toBe("a");
  });
});
