/**
 * Tests for orgClusters — pure-utility coverage.
 */
import { describe, it, expect } from "vitest";
import {
  FUNCTIONAL_DOMAINS,
  isFunctionalDomain,
  domainLabel,
  domainColor,
  domainCssVar,
  sortBySeniority,
} from "./orgClusters";

describe("FUNCTIONAL_DOMAINS", () => {
  it("contains exactly 9 keys (matches Postgres CHECK constraint)", () => {
    expect(FUNCTIONAL_DOMAINS).toHaveLength(9);
  });

  it("contains every canonical key with no duplicates", () => {
    const set = new Set(FUNCTIONAL_DOMAINS);
    expect(set.size).toBe(FUNCTIONAL_DOMAINS.length);
    for (const k of [
      "hardware_engineering",
      "software_engineering",
      "product_management",
      "manufacturing_ops",
      "sales_marketing",
      "research",
      "finance_legal",
      "people_ops",
      "general_management",
    ]) {
      expect(set.has(k as never)).toBe(true);
    }
  });
});

describe("isFunctionalDomain", () => {
  it("accepts every canonical key", () => {
    for (const k of FUNCTIONAL_DOMAINS) {
      expect(isFunctionalDomain(k)).toBe(true);
    }
  });

  it("rejects unknown strings, null, undefined, non-strings", () => {
    expect(isFunctionalDomain("marketing")).toBe(false);
    expect(isFunctionalDomain("")).toBe(false);
    expect(isFunctionalDomain(null)).toBe(false);
    expect(isFunctionalDomain(undefined)).toBe(false);
    expect(isFunctionalDomain(42 as unknown as string)).toBe(false);
  });
});

describe("domainLabel", () => {
  it("returns human-readable label for canonical key", () => {
    expect(domainLabel("hardware_engineering")).toBe("Hardware Engineering");
    expect(domainLabel("software_engineering")).toBe("Software Engineering");
    expect(domainLabel("manufacturing_ops")).toBe("Manufacturing & Ops");
    expect(domainLabel("general_management")).toBe("General Management");
  });

  it('returns "Other" for unknown / null / undefined', () => {
    expect(domainLabel("not_a_domain")).toBe("Other");
    expect(domainLabel(null)).toBe("Other");
    expect(domainLabel(undefined)).toBe("Other");
    expect(domainLabel("")).toBe("Other");
  });
});

describe("domainColor", () => {
  it("returns an hsl() string for each canonical key", () => {
    for (const k of FUNCTIONAL_DOMAINS) {
      const c = domainColor(k);
      expect(c).toMatch(/^hsl\(\s*\d+/);
    }
  });

  it("returns deterministic colors per key", () => {
    expect(domainColor("hardware_engineering")).toBe(
      domainColor("hardware_engineering"),
    );
  });

  it("falls back to the slate neutral for unknown / null", () => {
    const fallback = domainColor("not_a_domain");
    expect(fallback).toMatch(/^hsl\(/);
    expect(domainColor(null)).toBe(fallback);
    expect(domainColor(undefined)).toBe(fallback);
  });

  it("distinct canonical keys produce distinct colors", () => {
    const colors = FUNCTIONAL_DOMAINS.map((k) => domainColor(k));
    expect(new Set(colors).size).toBe(FUNCTIONAL_DOMAINS.length);
  });
});

describe("domainCssVar", () => {
  it("maps canonical keys to their CSS variable form", () => {
    expect(domainCssVar("hardware_engineering")).toBe(
      "hsl(var(--domain-hardware-engineering))",
    );
    expect(domainCssVar("general_management")).toBe(
      "hsl(var(--domain-general-management))",
    );
  });

  it("returns the fallback CSS variable for unknown / null", () => {
    expect(domainCssVar("not_a_domain")).toBe("hsl(var(--domain-fallback))");
    expect(domainCssVar(null)).toBe("hsl(var(--domain-fallback))");
  });
});

describe("sortBySeniority", () => {
  // Local fixture type — `sortBySeniority` is generic over `T` so each test
  // declares the full row shape it cares about. Avoids the inferred-T trap
  // where `{id, seniority_score}` literals get narrowed to a type that
  // forbids the `id` property on subsequent calls without explicit T.
  type TestPerson = { id: string; seniority_score?: number | null };

  it("sorts descending by seniority_score", () => {
    const out = sortBySeniority<TestPerson>([
      { id: "a", seniority_score: 50 },
      { id: "b", seniority_score: 80 },
      { id: "c", seniority_score: 30 },
    ]);
    expect(out.map((x) => x.id)).toEqual(["b", "a", "c"]);
  });

  it("does not mutate the input array", () => {
    const input: TestPerson[] = [
      { id: "a", seniority_score: 30 },
      { id: "b", seniority_score: 80 },
    ];
    const snapshot = input.map((x) => x.id);
    sortBySeniority(input);
    expect(input.map((x) => x.id)).toEqual(snapshot);
  });

  it("places null / undefined / NaN / non-number scores at the end (stable input order)", () => {
    const out = sortBySeniority<TestPerson>([
      { id: "a", seniority_score: 50 },
      { id: "b", seniority_score: null },
      { id: "c", seniority_score: 80 },
      { id: "d" }, // undefined
      { id: "e", seniority_score: NaN },
    ]);
    expect(out.map((x) => x.id)).toEqual(["c", "a", "b", "d", "e"]);
  });

  it("breaks ties by input order (stable)", () => {
    const out = sortBySeniority<TestPerson>([
      { id: "first", seniority_score: 50 },
      { id: "second", seniority_score: 50 },
      { id: "third", seniority_score: 50 },
    ]);
    expect(out.map((x) => x.id)).toEqual(["first", "second", "third"]);
  });

  it("handles empty input", () => {
    expect(sortBySeniority([])).toEqual([]);
  });

  it("handles all-null scores (stable input order)", () => {
    const out = sortBySeniority<TestPerson>([
      { id: "a" },
      { id: "b" },
      { id: "c" },
    ]);
    expect(out.map((x) => x.id)).toEqual(["a", "b", "c"]);
  });

  it("rejects non-finite numbers (Infinity treated as null)", () => {
    const out = sortBySeniority<TestPerson>([
      { id: "a", seniority_score: Infinity },
      { id: "b", seniority_score: 50 },
    ]);
    expect(out.map((x) => x.id)).toEqual(["b", "a"]);
  });
});
