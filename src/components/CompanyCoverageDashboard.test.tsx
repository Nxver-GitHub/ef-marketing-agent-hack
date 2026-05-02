/**
 * Tests for CompanyCoverageDashboard. Pure presentational, no mocks needed.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, cleanup, fireEvent, within } from "@testing-library/react";
import {
  CompanyCoverageDashboard,
  coverageRatio,
  coveragePercent,
  resolveTarget,
  tierBarClass,
  type CompanyCoverageRow,
} from "./CompanyCoverageDashboard";

beforeEach(() => {
  cleanup();
});

// ── Sample data ─────────────────────────────────────────────────────────────

const sample: CompanyCoverageRow[] = [
  {
    id: "co-1",
    canonical_name: "Alpha Semis",
    tier: "semiconductor",
    enriched_count: 500,
    target_count: 500,
    warm_paths_count: 32,
    edge_kinds_count: 6,
  },
  {
    id: "co-2",
    canonical_name: "Beta Defense",
    tier: "defense",
    enriched_count: 250,
    target_count: 500,
    warm_paths_count: 12,
    edge_kinds_count: 4,
  },
  {
    id: "co-3",
    canonical_name: "Gamma Aero",
    tier: "aerospace",
    enriched_count: 100,
    target_count: 500,
    warm_paths_count: 5,
    edge_kinds_count: 3,
  },
  {
    id: "co-4",
    canonical_name: "Delta Labs",
    tier: "research_lab",
    enriched_count: 50,
    target_count: 500,
    warm_paths_count: 2,
    edge_kinds_count: 2,
  },
  {
    id: "co-5",
    canonical_name: "Epsilon Other",
    tier: "other",
    enriched_count: 800, // overflow case
    target_count: 500,
    warm_paths_count: 40,
    edge_kinds_count: 5,
  },
  {
    id: "co-6",
    canonical_name: "Zeta NoTarget",
    tier: "semiconductor",
    enriched_count: 250,
    // No target_count → defaults to 500.
    warm_paths_count: 8,
    edge_kinds_count: 3,
  },
];

// ── Helper ──────────────────────────────────────────────────────────────────

function renderDashboard(
  rows: CompanyCoverageRow[] = sample,
  onCompanyClick?: (id: string) => void,
): void {
  render(
    <CompanyCoverageDashboard
      companies={rows}
      onCompanyClick={onCompanyClick}
    />,
  );
}

// ── Pure helper tests ───────────────────────────────────────────────────────

describe("resolveTarget", () => {
  it("returns provided target when valid", () => {
    expect(resolveTarget({ ...sample[0] })).toBe(500);
    expect(resolveTarget({ ...sample[0], target_count: 250 })).toBe(250);
  });

  it("defaults to 500 when target_count is null/undefined/invalid", () => {
    expect(resolveTarget({ ...sample[0], target_count: undefined })).toBe(500);
    expect(
      resolveTarget({ ...sample[0], target_count: null as unknown as number }),
    ).toBe(500);
    expect(resolveTarget({ ...sample[0], target_count: 0 })).toBe(500);
    expect(resolveTarget({ ...sample[0], target_count: -10 })).toBe(500);
    expect(resolveTarget({ ...sample[0], target_count: NaN })).toBe(500);
  });
});

describe("coverageRatio", () => {
  it("computes enriched/target", () => {
    expect(coverageRatio({ ...sample[2] })).toBeCloseTo(0.2);
  });

  it("clamps over-100 at 1.0 (visual cap)", () => {
    expect(coverageRatio({ ...sample[4] })).toBe(1);
  });

  it("clamps negatives at 0", () => {
    expect(
      coverageRatio({ ...sample[0], enriched_count: -50 }),
    ).toBe(0);
  });
});

describe("coveragePercent", () => {
  it("returns rounded integer percentage", () => {
    expect(coveragePercent({ ...sample[1] })).toBe(50);
    expect(coveragePercent({ ...sample[4] })).toBe(100);
  });
});

describe("tierBarClass", () => {
  it("maps known tiers", () => {
    expect(tierBarClass("semiconductor")).toContain("sky");
    expect(tierBarClass("defense")).toContain("rose");
    expect(tierBarClass("aerospace")).toContain("violet");
    expect(tierBarClass("research_lab")).toContain("emerald");
    expect(tierBarClass("other")).toContain("slate");
  });

  it("falls back to gray for unknown/null tier", () => {
    expect(tierBarClass(null)).toContain("slate");
    expect(tierBarClass(undefined)).toContain("slate");
    expect(tierBarClass("does_not_exist")).toContain("slate");
  });
});

// ── Component tests ─────────────────────────────────────────────────────────

describe("CompanyCoverageDashboard — stat cards", () => {
  it("(1) renders all four stat cards with computed totals", () => {
    renderDashboard();
    expect(screen.getByTestId("stat-total-enriched")).toBeInTheDocument();
    expect(screen.getByTestId("stat-at-target")).toBeInTheDocument();
    expect(screen.getByTestId("stat-warm-paths")).toBeInTheDocument();
    expect(screen.getByTestId("stat-edge-diversity")).toBeInTheDocument();
  });

  it("(2) sums enriched_count correctly", () => {
    renderDashboard();
    // 500 + 250 + 100 + 50 + 800 + 250 = 1950
    const card = screen.getByTestId("stat-total-enriched");
    expect(within(card).getByText("1,950")).toBeInTheDocument();
    // hint mentions company count
    expect(within(card).getByText(/of 6 companies total/)).toBeInTheDocument();
  });

  it("(3) counts at-target correctly (>=500 of target)", () => {
    renderDashboard();
    // co-1 (500/500) and co-5 (800/500) → 2 at target.
    const card = screen.getByTestId("stat-at-target");
    expect(within(card).getByText("2")).toBeInTheDocument();
  });

  it("sums warm_paths_count correctly", () => {
    renderDashboard();
    // 32 + 12 + 5 + 2 + 40 + 8 = 99
    const card = screen.getByTestId("stat-warm-paths");
    expect(within(card).getByText("99")).toBeInTheDocument();
  });

  it("computes average edge_kinds_count correctly", () => {
    renderDashboard();
    // (6 + 4 + 3 + 2 + 5 + 3) / 6 = 23/6 = 3.833...
    const card = screen.getByTestId("stat-edge-diversity");
    expect(within(card).getByText("3.8")).toBeInTheDocument();
  });
});

describe("CompanyCoverageDashboard — empty state", () => {
  it("(4) renders empty state when companies is []", () => {
    render(<CompanyCoverageDashboard companies={[]} />);
    expect(screen.getByTestId("coverage-empty")).toBeInTheDocument();
    // Stat cards still render with zero values.
    const total = screen.getByTestId("stat-total-enriched");
    expect(within(total).getByText("0")).toBeInTheDocument();
    expect(within(total).getByText(/of 0 companies total/)).toBeInTheDocument();
  });
});

describe("CompanyCoverageDashboard — coverage bars", () => {
  it("(5) bar widths match enriched/target ratios", () => {
    renderDashboard();
    const fillCo2 = screen.getByTestId("coverage-bar-fill-co-2");
    expect(fillCo2.style.width).toBe("50%");
    const fillCo3 = screen.getByTestId("coverage-bar-fill-co-3");
    expect(fillCo3.style.width).toBe("20%");
    const fillCo4 = screen.getByTestId("coverage-bar-fill-co-4");
    expect(fillCo4.style.width).toBe("10%");
  });

  it("(9) caps overflow bars at 100% width", () => {
    renderDashboard();
    // co-5: 800/500 = 160% raw → must be capped at 100%
    const fill = screen.getByTestId("coverage-bar-fill-co-5");
    expect(fill.style.width).toBe("100%");
    // The display label shows the capped percentage too.
    expect(screen.getByTestId("coverage-label-co-5").textContent).toContain(
      "(100%)",
    );
  });

  it("(10) handles null/undefined target_count by defaulting to 500", () => {
    renderDashboard();
    // co-6 has no target_count. 250/500 = 50%.
    const fill = screen.getByTestId("coverage-bar-fill-co-6");
    expect(fill.style.width).toBe("50%");
    expect(screen.getByTestId("coverage-label-co-6").textContent).toContain(
      "250 / 500",
    );
  });

  it("(8) bar color reflects tier (className contains tier accent)", () => {
    renderDashboard();
    expect(
      screen.getByTestId("coverage-bar-fill-co-1").className,
    ).toContain("sky-500");
    expect(
      screen.getByTestId("coverage-bar-fill-co-2").className,
    ).toContain("rose-500");
    expect(
      screen.getByTestId("coverage-bar-fill-co-3").className,
    ).toContain("violet-500");
    expect(
      screen.getByTestId("coverage-bar-fill-co-4").className,
    ).toContain("emerald-500");
  });

  it("(6) clicking a row calls onCompanyClick with the right id", () => {
    const onClick = vi.fn();
    renderDashboard(sample, onClick);
    const row = screen.getByTestId("coverage-row-co-3");
    fireEvent.click(row);
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(onClick).toHaveBeenCalledWith("co-3");
  });

  it("rows are non-interactive when no onCompanyClick provided", () => {
    renderDashboard();
    const row = screen.getByTestId("coverage-row-co-1");
    expect(row.getAttribute("role")).not.toBe("button");
  });

  it("sorts rows by coverage percent descending", () => {
    renderDashboard();
    const bars = screen.getByTestId("coverage-bars");
    const rows = within(bars).getAllByTestId(/^coverage-row-/);
    const ids = rows.map((r) => r.getAttribute("data-testid"));
    // Top: co-1 (100%) and co-5 (100% capped) come first;
    // followed by co-2 / co-6 (50%); then co-3 (20%); then co-4 (10%).
    expect(ids[0]).toMatch(/co-(1|5)$/);
    expect(ids[1]).toMatch(/co-(1|5)$/);
    expect(ids[ids.length - 1]).toBe("coverage-row-co-4");
  });

  it("renders warm-path badge when warm_paths_count is provided", () => {
    renderDashboard();
    expect(screen.getByTestId("coverage-warm-co-1").textContent).toContain("32");
  });
});

describe("CompanyCoverageDashboard — top-5 lists", () => {
  it("(7) top-5 best-covered renders in correct sort order", () => {
    renderDashboard();
    const list = screen.getByTestId("top-best-covered");
    const items = within(list).getAllByTestId(/^top-best-covered-row-/);
    expect(items.length).toBe(5);
    // First two are co-1 / co-5 (both at 100%); last is co-3 (20%) since
    // co-4 (10%) gets pushed off after slicing 5.
    const firstId = items[0].getAttribute("data-testid");
    expect(firstId).toMatch(/co-(1|5)$/);
    expect(items[items.length - 1].getAttribute("data-testid")).toBe(
      "top-best-covered-row-co-3",
    );
  });

  it("top-5 most warm paths sorts descending by warm_paths_count", () => {
    renderDashboard();
    const list = screen.getByTestId("top-most-paths");
    const items = within(list).getAllByTestId(/^top-most-paths-row-/);
    expect(items[0].getAttribute("data-testid")).toBe(
      "top-most-paths-row-co-5",
    );
    expect(items[1].getAttribute("data-testid")).toBe(
      "top-most-paths-row-co-1",
    );
  });

  it("top-5 needs-attention shows only companies under 50% target", () => {
    renderDashboard();
    const list = screen.getByTestId("top-needs-attention");
    const items = within(list).getAllByTestId(/^top-needs-attention-row-/);
    const ids = items.map((it) => it.getAttribute("data-testid"));
    // Under 50%: co-3 (20%) and co-4 (10%). co-2 and co-6 are at exactly 50%
    // → not under 50%.
    expect(ids).toContain("top-needs-attention-row-co-3");
    expect(ids).toContain("top-needs-attention-row-co-4");
    expect(ids).not.toContain("top-needs-attention-row-co-2");
    expect(ids).not.toContain("top-needs-attention-row-co-6");
    // Most-behind first: co-4 (10%) above co-3 (20%).
    expect(ids[0]).toBe("top-needs-attention-row-co-4");
  });

  it("clicking a top-5 row calls onCompanyClick with that id", () => {
    const onClick = vi.fn();
    renderDashboard(sample, onClick);
    const list = screen.getByTestId("top-most-paths");
    const row = within(list).getByTestId("top-most-paths-row-co-1");
    fireEvent.click(row);
    expect(onClick).toHaveBeenCalledWith("co-1");
  });
});

describe("CompanyCoverageDashboard — tier breakdown", () => {
  it("renders one row per tier with people counts", () => {
    renderDashboard();
    const breakdown = screen.getByTestId("tier-breakdown");
    expect(within(breakdown).getByTestId("tier-row-semiconductor")).toBeInTheDocument();
    expect(within(breakdown).getByTestId("tier-row-defense")).toBeInTheDocument();
    expect(within(breakdown).getByTestId("tier-row-aerospace")).toBeInTheDocument();
    expect(within(breakdown).getByTestId("tier-row-research_lab")).toBeInTheDocument();
    expect(within(breakdown).getByTestId("tier-row-other")).toBeInTheDocument();
  });

  it("aggregates enriched per tier (semiconductor: co-1 500 + co-6 250 = 750)", () => {
    renderDashboard();
    const row = screen.getByTestId("tier-row-semiconductor");
    expect(row.textContent).toContain("750");
    // 2 companies in this tier.
    expect(row.textContent).toContain("2 co");
  });
});

describe("CompanyCoverageDashboard — props passthrough", () => {
  it("(11) className passthrough lands on the root element", () => {
    render(
      <CompanyCoverageDashboard
        companies={sample}
        className="custom-test-class"
      />,
    );
    const root = screen.getByTestId("company-coverage-dashboard");
    expect(root.className).toContain("custom-test-class");
  });

  it("does not mutate the input companies array", () => {
    const original = [...sample];
    const originalIds = sample.map((c) => c.id);
    renderDashboard(sample);
    expect(sample.map((c) => c.id)).toEqual(originalIds);
    expect(sample).toEqual(original);
  });
});
