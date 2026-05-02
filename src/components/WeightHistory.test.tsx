/**
 * Tests for WeightHistory. Pure presentational, no mocks needed.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import {
  WeightHistory,
  type WeightVersionEntry,
  diffCount,
  diffSummary,
  formatTimestamp,
  formatRecomputeCount,
  sortVersionsDesc,
} from "./WeightHistory";

beforeEach(() => {
  cleanup();
});

function v(
  version_number: number,
  overrides: Partial<WeightVersionEntry> = {},
): WeightVersionEntry {
  return {
    id: `v-${version_number}`,
    version_number,
    created_at: "2026-05-01T12:00:00Z",
    ...overrides,
  };
}

// ── Helpers ─────────────────────────────────────────────────────────────────

describe("diffCount", () => {
  it("counts keys", () => {
    expect(diffCount({ a: { old: 1, new: 2 }, b: { old: 3, new: 4 } })).toBe(2);
  });

  it("returns 0 for null / undefined / empty", () => {
    expect(diffCount(null)).toBe(0);
    expect(diffCount(undefined)).toBe(0);
    expect(diffCount({})).toBe(0);
  });
});

describe("diffSummary", () => {
  it("singular vs plural", () => {
    expect(diffSummary({ a: { old: 1, new: 2 } })).toBe("1 weight changed");
    expect(diffSummary({ a: { old: 1, new: 2 }, b: { old: 1, new: 2 } })).toBe(
      "2 weights changed",
    );
  });

  it('returns "no diff" for null / undefined / empty', () => {
    expect(diffSummary(null)).toBe("no diff");
    expect(diffSummary({})).toBe("no diff");
  });
});

describe("formatTimestamp", () => {
  it("formats valid ISO datetime", () => {
    expect(formatTimestamp("2026-05-01T12:34:00Z")).toBe(
      "2026-05-01 12:34 UTC",
    );
  });

  it("returns empty for invalid input", () => {
    expect(formatTimestamp(null)).toBe("");
    expect(formatTimestamp(undefined)).toBe("");
    expect(formatTimestamp("")).toBe("");
    expect(formatTimestamp("not-a-date")).toBe("");
  });
});

describe("formatRecomputeCount", () => {
  it("groups thousands", () => {
    expect(formatRecomputeCount(1582)).toBe("1,582");
    expect(formatRecomputeCount(0)).toBe("0");
  });

  it("returns empty for null / NaN", () => {
    expect(formatRecomputeCount(null)).toBe("");
    expect(formatRecomputeCount(undefined)).toBe("");
    expect(formatRecomputeCount(NaN)).toBe("");
  });
});

describe("sortVersionsDesc", () => {
  it("sorts by version_number desc", () => {
    const out = sortVersionsDesc([v(2), v(5), v(1), v(3)]);
    expect(out.map((x) => x.version_number)).toEqual([5, 3, 2, 1]);
  });

  it("does not mutate input", () => {
    const input = [v(2), v(5), v(1)];
    const before = input.map((x) => x.version_number);
    sortVersionsDesc(input);
    expect(input.map((x) => x.version_number)).toEqual(before);
  });
});

// ── Component ───────────────────────────────────────────────────────────────

describe("WeightHistory rendering", () => {
  it("renders empty placeholder for empty array", () => {
    render(<WeightHistory versions={[]} />);
    expect(screen.getByTestId("weight-history-empty")).toBeInTheDocument();
    expect(screen.getByText("No weight versions recorded yet.")).toBeInTheDocument();
  });

  it("renders versions sorted descending by version_number", () => {
    render(
      <WeightHistory
        versions={[
          v(1, { id: "v-1" }),
          v(3, { id: "v-3" }),
          v(2, { id: "v-2" }),
        ]}
      />,
    );
    const items = screen.getAllByTestId(/^weight-version-/);
    expect(items.map((el) => el.getAttribute("data-testid"))).toEqual([
      "weight-version-v-3",
      "weight-version-v-2",
      "weight-version-v-1",
    ]);
  });

  it("respects maxRows cap", () => {
    const versions = Array.from({ length: 8 }, (_, i) => v(i + 1, { id: `v-${i + 1}` }));
    render(<WeightHistory versions={versions} maxRows={3} />);
    const items = screen.getAllByTestId(/^weight-version-/);
    expect(items.length).toBe(3);
    // Highest 3 versions visible.
    expect(screen.getByTestId("weight-version-v-8")).toBeInTheDocument();
    expect(screen.getByTestId("weight-version-v-6")).toBeInTheDocument();
    expect(screen.queryByTestId("weight-version-v-5")).toBeNull();
  });

  it("displays diff summary per row", () => {
    render(
      <WeightHistory
        versions={[
          v(1, {
            weights_diff: {
              authenticity: { old: 0.4, new: 0.5 },
              authority: { old: 0.4, new: 0.3 },
            },
          }),
        ]}
      />,
    );
    expect(screen.getByText(/2 weights changed/)).toBeInTheDocument();
  });

  it('displays "no diff" when weights_diff is null', () => {
    render(<WeightHistory versions={[v(1, { weights_diff: null })]} />);
    expect(screen.getByText(/no diff/)).toBeInTheDocument();
  });

  it("displays formatted timestamp", () => {
    render(
      <WeightHistory
        versions={[v(1, { created_at: "2026-04-30T08:15:00Z" })]}
      />,
    );
    expect(screen.getByText(/2026-04-30 08:15 UTC/)).toBeInTheDocument();
  });

  it("displays created_by when provided", () => {
    render(<WeightHistory versions={[v(1, { created_by: "alice@example.com" })]} />);
    expect(screen.getByText("alice@example.com")).toBeInTheDocument();
  });

  it("displays recompute count when provided", () => {
    render(
      <WeightHistory versions={[v(1, { scores_recomputed_count: 12345 })]} />,
    );
    expect(screen.getByText("12,345")).toBeInTheDocument();
  });

  it("shows count summary when truncating", () => {
    const versions = Array.from({ length: 20 }, (_, i) => v(i + 1, { id: `v-${i + 1}` }));
    render(<WeightHistory versions={versions} maxRows={5} />);
    // Header shows "5 / 20"
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByText("/ 20")).toBeInTheDocument();
  });
});
