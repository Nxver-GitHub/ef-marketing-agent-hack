/**
 * Tests for EdgeKindLegend.
 */
import { describe, it, expect, beforeEach } from "vitest"
import { render, screen, cleanup, within } from "@testing-library/react"
import {
  EdgeKindLegend,
  categoryFor,
  groupEdgeKinds,
  formatStrength,
  LEGEND_CATEGORY_ORDER,
} from "./EdgeKindLegend"
import { ALL_EDGE_KINDS, EDGE_CONFIGS } from "@/lib/graph"

beforeEach(() => cleanup())

// ── Pure helpers ───────────────────────────────────────────────────────────

describe("categoryFor", () => {
  it("classifies patent_co_inventor as Warm", () => {
    expect(categoryFor("patent_co_inventor")).toBe("Warm")
  })

  it("classifies works_at as Career", () => {
    expect(categoryFor("works_at")).toBe("Career")
  })

  it("classifies education as Education", () => {
    expect(categoryFor("education")).toBe("Education")
  })

  it("classifies located_in as Structural", () => {
    expect(categoryFor("located_in")).toBe("Structural")
  })
})

describe("groupEdgeKinds", () => {
  it("groups kinds into category buckets", () => {
    const groups = groupEdgeKinds(["patent_co_inventor", "works_at"])
    expect(groups.find((g) => g.category === "Warm")?.kinds).toContain(
      "patent_co_inventor",
    )
    expect(groups.find((g) => g.category === "Career")?.kinds).toContain(
      "works_at",
    )
  })

  it("groups appear in LEGEND_CATEGORY_ORDER", () => {
    const groups = groupEdgeKinds(ALL_EDGE_KINDS)
    const cats = groups.map((g) => g.category)
    // each category in cats must respect LEGEND_CATEGORY_ORDER ordering
    let lastIdx = -1
    for (const c of cats) {
      const idx = LEGEND_CATEGORY_ORDER.indexOf(c)
      expect(idx).toBeGreaterThan(lastIdx)
      lastIdx = idx
    }
  })

  it("does not emit empty category buckets", () => {
    const groups = groupEdgeKinds(["works_at"])
    for (const g of groups) {
      expect(g.kinds.length).toBeGreaterThan(0)
    }
    expect(groups.find((g) => g.category === "Warm")).toBeUndefined()
  })
})

describe("formatStrength", () => {
  it("formats to 2 decimal places", () => {
    expect(formatStrength(0.85)).toBe("0.85")
    expect(formatStrength(0.9)).toBe("0.90")
    expect(formatStrength(0.123)).toBe("0.12")
  })
})

// ── Component render ───────────────────────────────────────────────────────

describe("EdgeKindLegend component", () => {
  it("renders one section per non-empty category", () => {
    render(<EdgeKindLegend />)
    const legend = screen.getByTestId("edge-kind-legend")
    const sections = within(legend).getAllByRole("list")
    // 1 outer role=list + 1 per category section's <ul>
    expect(sections.length).toBeGreaterThan(1)
  })

  it("renders a swatch + label for every EDGE_CONFIGS kind", () => {
    render(<EdgeKindLegend />)
    for (const kind of ALL_EDGE_KINDS) {
      const row = screen.getByTestId(`legend-row-${kind}`)
      expect(row).toBeInTheDocument()
      expect(
        screen.getByTestId(`legend-swatch-${kind}`),
      ).toBeInTheDocument()
      expect(row.textContent).toContain(EDGE_CONFIGS[kind].displayLabel)
    }
  })

  it("swatch references the EdgeConfig.cssVarName via data attribute", () => {
    render(<EdgeKindLegend />)
    for (const kind of ALL_EDGE_KINDS) {
      const swatch = screen.getByTestId(`legend-swatch-${kind}`)
      expect(swatch.getAttribute("data-css-var")).toBe(
        EDGE_CONFIGS[kind].cssVarName,
      )
    }
  })

  it("shows strength annotation when showStrength=true (default)", () => {
    render(<EdgeKindLegend />)
    // patent_co_inventor has baseStrength 0.95 — must render
    const node = screen.getByTestId("legend-strength-patent_co_inventor")
    expect(node.textContent).toContain("0.95")
  })

  it("hides strength annotation when showStrength=false", () => {
    render(<EdgeKindLegend showStrength={false} />)
    expect(
      screen.queryByTestId("legend-strength-patent_co_inventor"),
    ).not.toBeInTheDocument()
  })

  it("structural edges (baseStrength=0) never show strength annotation", () => {
    render(<EdgeKindLegend showStrength={true} />)
    // works_at is structural → baseStrength 0 → no annotation
    expect(
      screen.queryByTestId("legend-strength-works_at"),
    ).not.toBeInTheDocument()
  })

  it("custom className is appended", () => {
    render(<EdgeKindLegend className="my-custom" />)
    expect(
      screen.getByTestId("edge-kind-legend").className,
    ).toContain("my-custom")
  })

  it("each category section renders a heading with the category name", () => {
    render(<EdgeKindLegend />)
    // At least Warm + Career + Structural headings appear (Education
    // depends on whether the kind 'education' is in ALL_EDGE_KINDS, but
    // Warm/Career/Structural are guaranteed by the registry).
    expect(
      screen.getByTestId("legend-group-Career"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("legend-group-Structural"),
    ).toBeInTheDocument()
  })
})
