/**
 * Tests for EdgeFilterPills.
 *
 * graphStore is mocked so each test can drive `visibleEdgeKinds`, `edges`,
 * `toggleEdgeKind`, and `setVisibleEdgeKinds` deterministically without
 * touching the real Zustand store. The mock implements the same selector
 * API (`useGraphStore(selector)`) the component uses.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, cleanup, within } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import {
  ALL_EDGE_KINDS,
  EDGE_CONFIGS,
  type EdgeKind,
  type GraphEdge,
} from "@/lib/graph";

// ── graphStore mock ─────────────────────────────────────────────────────────

interface MockState {
  visibleEdgeKinds: Set<EdgeKind>;
  edges: GraphEdge[];
  toggleEdgeKind: ReturnType<typeof vi.fn>;
  setVisibleEdgeKinds: ReturnType<typeof vi.fn>;
}

const mockState: MockState = {
  visibleEdgeKinds: new Set<EdgeKind>(),
  edges: [],
  toggleEdgeKind: vi.fn(),
  setVisibleEdgeKinds: vi.fn(),
};

vi.mock("@/store/graphStore", () => ({
  useGraphStore: <T,>(selector: (s: MockState) => T): T => selector(mockState),
}));

// Import after mock so the component picks it up.
import { EdgeFilterPills } from "./EdgeFilterPills";

// ── Fixtures ────────────────────────────────────────────────────────────────

function makeEdge(id: string, kind: EdgeKind): GraphEdge {
  return { id, source: "a", target: "b", kind };
}

function defaultVisibleSet(): Set<EdgeKind> {
  return new Set<EdgeKind>(
    ALL_EDGE_KINDS.filter((k) => EDGE_CONFIGS[k].defaultVisible),
  );
}

beforeEach(() => {
  cleanup();
  mockState.visibleEdgeKinds = new Set<EdgeKind>(ALL_EDGE_KINDS);
  mockState.edges = [];
  mockState.toggleEdgeKind = vi.fn();
  mockState.setVisibleEdgeKinds = vi.fn();
});

// ── Tests ───────────────────────────────────────────────────────────────────

describe("EdgeFilterPills", () => {
  it("renders one pill per EdgeKind in EDGE_CONFIGS", () => {
    render(<EdgeFilterPills />);
    for (const kind of ALL_EDGE_KINDS) {
      const pill = document.querySelector(`[data-edge-kind="${kind}"]`);
      expect(pill, `missing pill for ${kind}`).not.toBeNull();
    }
    // Exactly one pill per kind, no extras.
    const allPills = document.querySelectorAll("[data-edge-kind]");
    expect(allPills.length).toBe(ALL_EDGE_KINDS.length);
  });

  it("displays each pill's displayLabel and uses its cssVarName-driven swatch", () => {
    render(<EdgeFilterPills />);
    for (const kind of ALL_EDGE_KINDS) {
      const cfg = EDGE_CONFIGS[kind];
      const pill = document.querySelector(`[data-edge-kind="${kind}"]`);
      expect(pill).not.toBeNull();
      expect(pill?.textContent).toContain(cfg.displayLabel);
      const swatch = pill?.querySelector(`[data-testid="edge-pill-swatch-${kind}"]`);
      expect(swatch, `missing swatch for ${kind}`).not.toBeNull();
      const inlineStyle = (swatch as HTMLElement).getAttribute("style") ?? "";
      // The inline style must reference the cssVar for this edge kind.
      expect(inlineStyle).toContain(cfg.cssVarName);
    }
  });

  it("hidden EdgeKinds render with reduced opacity and a line-through label", () => {
    // All kinds visible EXCEPT patent_co_inventor.
    const visible = new Set<EdgeKind>(ALL_EDGE_KINDS);
    visible.delete("patent_co_inventor");
    mockState.visibleEdgeKinds = visible;

    render(<EdgeFilterPills />);

    const hiddenPill = document.querySelector(
      '[data-edge-kind="patent_co_inventor"]',
    ) as HTMLElement | null;
    expect(hiddenPill).not.toBeNull();
    expect(hiddenPill!.className).toMatch(/opacity-40/);
    expect(hiddenPill!.getAttribute("aria-pressed")).toBe("false");
    // Label has line-through styling.
    const lineThrough = hiddenPill!.querySelector(".line-through");
    expect(lineThrough).not.toBeNull();

    // A still-visible pill keeps full opacity and aria-pressed="true".
    const visiblePill = document.querySelector(
      '[data-edge-kind="works_at"]',
    ) as HTMLElement | null;
    expect(visiblePill!.className).toMatch(/opacity-100/);
    expect(visiblePill!.getAttribute("aria-pressed")).toBe("true");
  });

  it("clicking a pill calls toggleEdgeKind with that kind", () => {
    render(<EdgeFilterPills />);
    const pill = document.querySelector(
      '[data-edge-kind="academic_co_author"]',
    ) as HTMLElement;
    fireEvent.click(pill);
    expect(mockState.toggleEdgeKind).toHaveBeenCalledTimes(1);
    expect(mockState.toggleEdgeKind).toHaveBeenCalledWith("academic_co_author");
  });

  it("'Show all' calls setVisibleEdgeKinds with every EdgeKind", () => {
    mockState.visibleEdgeKinds = new Set<EdgeKind>();
    render(<EdgeFilterPills />);
    fireEvent.click(screen.getByText("Show all"));
    expect(mockState.setVisibleEdgeKinds).toHaveBeenCalledTimes(1);
    const arg = mockState.setVisibleEdgeKinds.mock.calls[0][0] as Set<EdgeKind>;
    expect(arg).toBeInstanceOf(Set);
    for (const kind of ALL_EDGE_KINDS) {
      expect(arg.has(kind)).toBe(true);
    }
    expect(arg.size).toBe(ALL_EDGE_KINDS.length);
  });

  it("'Hide all' calls setVisibleEdgeKinds with an empty set", () => {
    render(<EdgeFilterPills />);
    fireEvent.click(screen.getByText("Hide all"));
    expect(mockState.setVisibleEdgeKinds).toHaveBeenCalledTimes(1);
    const arg = mockState.setVisibleEdgeKinds.mock.calls[0][0] as Set<EdgeKind>;
    expect(arg).toBeInstanceOf(Set);
    expect(arg.size).toBe(0);
  });

  it("'Reset' restores defaultVisible from EDGE_CONFIGS", () => {
    // Start from an arbitrary state — the action should still produce defaults.
    mockState.visibleEdgeKinds = new Set<EdgeKind>();
    render(<EdgeFilterPills />);
    fireEvent.click(screen.getByText("Reset"));
    expect(mockState.setVisibleEdgeKinds).toHaveBeenCalledTimes(1);
    const arg = mockState.setVisibleEdgeKinds.mock.calls[0][0] as Set<EdgeKind>;
    const expected = defaultVisibleSet();
    expect(arg.size).toBe(expected.size);
    for (const kind of expected) {
      expect(arg.has(kind)).toBe(true);
    }
    // Also: any kind whose defaultVisible is false must NOT be in the set.
    for (const kind of ALL_EDGE_KINDS) {
      if (!EDGE_CONFIGS[kind].defaultVisible) {
        expect(arg.has(kind)).toBe(false);
      }
    }
  });

  it("live edge count badge reflects graphStore.edges", () => {
    mockState.edges = [
      makeEdge("e1", "patent_co_inventor"),
      makeEdge("e2", "patent_co_inventor"),
      makeEdge("e3", "patent_co_inventor"),
      makeEdge("e4", "academic_co_author"),
      makeEdge("e5", "works_at"),
    ];
    render(<EdgeFilterPills />);

    const countSpanText = (kind: EdgeKind): string => {
      const pill = document.querySelector(
        `[data-edge-kind="${kind}"]`,
      ) as HTMLElement;
      // Last text-bearing span in the pill is the count badge.
      const spans = pill.querySelectorAll("span");
      return spans[spans.length - 1].textContent ?? "";
    };

    expect(countSpanText("patent_co_inventor")).toBe("3");
    expect(countSpanText("academic_co_author")).toBe("1");
    expect(countSpanText("works_at")).toBe("1");
    // A kind with no edges still renders, with count 0.
    expect(countSpanText("colleague")).toBe("0");
  });

  it("header shows 'Edge filters · X of Y visible'", () => {
    const visible = new Set<EdgeKind>(ALL_EDGE_KINDS);
    // Hide two arbitrary kinds.
    visible.delete("patent_co_inventor");
    visible.delete("colleague");
    mockState.visibleEdgeKinds = visible;
    render(<EdgeFilterPills />);
    const expected = `Edge filters · ${ALL_EDGE_KINDS.length - 2} of ${ALL_EDGE_KINDS.length} visible`;
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it("renders categories in the documented order (Warm → Career → Education → Structural)", () => {
    render(<EdgeFilterPills />);
    const sections = Array.from(
      document.querySelectorAll("[data-category]"),
    ).map((el) => el.getAttribute("data-category"));
    // Filter to only categories that should be present (all four are, given
    // the current EDGE_CONFIGS has at least one member of each).
    expect(sections).toEqual(["Warm", "Career", "Education", "Structural"]);
  });

  it("each category contains exactly the expected EdgeKinds", () => {
    render(<EdgeFilterPills />);
    const findKinds = (cat: string): string[] =>
      Array.from(
        document
          .querySelector(`[data-category="${cat}"]`)!
          .querySelectorAll("[data-edge-kind]"),
      )
        .map((el) => el.getAttribute("data-edge-kind"))
        .filter((v): v is string => v != null);

    expect(new Set(findKinds("Warm"))).toEqual(
      new Set([
        "patent_co_inventor",
        "academic_co_author",
        "conference_co_presenter",
        "standards_committee",
      ]),
    );
    expect(new Set(findKinds("Career"))).toEqual(
      new Set(["past_employer", "colleague", "works_at", "reports_to"]),
    );
    expect(new Set(findKinds("Education"))).toEqual(
      new Set([
        "same_undergrad_cohort",
        "same_mba_cohort",
        "same_phd_program",
        "executive_education",
        "education",
      ]),
    );
    expect(new Set(findKinds("Structural"))).toEqual(
      new Set([
        "located_in",
        "partnership",
        "vertical",
        "scope_signal",
        "evidence_cited",
      ]),
    );
  });

  it("pills are <button> elements with aria-pressed reflecting visibility", () => {
    const visible = new Set<EdgeKind>(["patent_co_inventor", "works_at"]);
    mockState.visibleEdgeKinds = visible;
    render(<EdgeFilterPills />);
    for (const kind of ALL_EDGE_KINDS) {
      const pill = document.querySelector(
        `[data-edge-kind="${kind}"]`,
      ) as HTMLElement;
      expect(pill.tagName).toBe("BUTTON");
      const expectedPressed = visible.has(kind) ? "true" : "false";
      expect(pill.getAttribute("aria-pressed")).toBe(expectedPressed);
    }
  });

  it("color swatches have aria-hidden='true'", () => {
    render(<EdgeFilterPills />);
    const swatches = document.querySelectorAll("[data-testid^='edge-pill-swatch-']");
    expect(swatches.length).toBe(ALL_EDGE_KINDS.length);
    for (const sw of Array.from(swatches)) {
      expect(sw.getAttribute("aria-hidden")).toBe("true");
    }
  });
});
