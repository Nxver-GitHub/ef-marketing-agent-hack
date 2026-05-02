/**
 * Tests for WarmPathBadge — pure helpers + render behaviour.
 */
import { describe, it, expect, vi, beforeEach } from "vitest"
import { render, screen, cleanup, fireEvent } from "@testing-library/react"
import {
  WarmPathBadge,
  bestStrength,
  strengthBand,
  buildTooltip,
} from "./WarmPathBadge"

beforeEach(() => cleanup())

// ── Pure helpers ───────────────────────────────────────────────────────────

describe("bestStrength", () => {
  it("returns 0 for empty array", () => {
    expect(bestStrength([])).toBe(0)
  })

  it("returns the maximum strength", () => {
    expect(
      bestStrength([{ strength: 0.3 }, { strength: 0.9 }, { strength: 0.5 }]),
    ).toBe(0.9)
  })
})

describe("strengthBand", () => {
  it("classifies ≥0.70 as strong", () => {
    expect(strengthBand(0.7)).toBe("strong")
    expect(strengthBand(0.95)).toBe("strong")
  })

  it("classifies 0.40–0.69 as moderate", () => {
    expect(strengthBand(0.4)).toBe("moderate")
    expect(strengthBand(0.69)).toBe("moderate")
  })

  it("classifies <0.40 as weak", () => {
    expect(strengthBand(0.39)).toBe("weak")
    expect(strengthBand(0)).toBe("weak")
  })
})

describe("buildTooltip", () => {
  it('returns "No warm paths" for empty input', () => {
    expect(buildTooltip([])).toBe("No warm paths")
  })

  it("renders one line per path with pct and hops", () => {
    const out = buildTooltip([
      { strength: 0.85, hopCount: 1, explanation: "co-invented Patent X" },
    ])
    expect(out).toContain("85%")
    expect(out).toContain("1 hop")
    expect(out).toContain("co-invented Patent X")
  })

  it("pluralises hop counts", () => {
    const out = buildTooltip([{ strength: 0.5, hopCount: 2 }])
    expect(out).toContain("2 hops")
  })

  it("sorts paths by strength descending and shows up to 3", () => {
    const out = buildTooltip([
      { strength: 0.3, hopCount: 3 },
      { strength: 0.9, hopCount: 1 },
      { strength: 0.6, hopCount: 2 },
      { strength: 0.5, hopCount: 2 },
    ])
    const lines = out.split("\n")
    expect(lines[0].startsWith("90%")).toBe(true)
    expect(lines[1].startsWith("60%")).toBe(true)
    expect(lines[2].startsWith("50%")).toBe(true)
    expect(lines[3]).toBe("+1 more")
  })
})

// ── Component render ───────────────────────────────────────────────────────

describe("WarmPathBadge component", () => {
  it("returns null when paths is empty", () => {
    const { container } = render(<WarmPathBadge paths={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it("renders count = paths.length", () => {
    render(
      <WarmPathBadge
        paths={[
          { strength: 0.5, hopCount: 1 },
          { strength: 0.7, hopCount: 2 },
        ]}
      />,
    )
    expect(screen.getByTestId("warm-path-badge-count").textContent).toBe("2")
  })

  it("dot data-band reflects strongest path", () => {
    render(
      <WarmPathBadge
        paths={[
          { strength: 0.3, hopCount: 3 },
          { strength: 0.85, hopCount: 1 },
        ]}
      />,
    )
    const dot = screen.getByTestId("warm-path-badge-dot")
    expect(dot.getAttribute("data-band")).toBe("strong")
  })

  it("renders as <span> when onClick is not provided", () => {
    render(<WarmPathBadge paths={[{ strength: 0.5, hopCount: 1 }]} />)
    const badge = screen.getByTestId("warm-path-badge")
    expect(badge.tagName).toBe("SPAN")
  })

  it("renders as <button> when onClick is provided and fires it", () => {
    const handler = vi.fn()
    render(
      <WarmPathBadge
        paths={[{ strength: 0.5, hopCount: 1 }]}
        onClick={handler}
      />,
    )
    const badge = screen.getByTestId("warm-path-badge")
    expect(badge.tagName).toBe("BUTTON")
    fireEvent.click(badge)
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it("stops click propagation so parent row click does not fire", () => {
    const inner = vi.fn()
    const outer = vi.fn()
    render(
      <div onClick={outer}>
        <WarmPathBadge
          paths={[{ strength: 0.5, hopCount: 1 }]}
          onClick={inner}
        />
      </div>,
    )
    fireEvent.click(screen.getByTestId("warm-path-badge"))
    expect(inner).toHaveBeenCalledTimes(1)
    expect(outer).not.toHaveBeenCalled()
  })

  it("aria-label uses singular for 1 path", () => {
    render(<WarmPathBadge paths={[{ strength: 0.5, hopCount: 1 }]} />)
    expect(
      screen.getByLabelText("1 warm path"),
    ).toBeInTheDocument()
  })

  it("title attr exposes tooltip with strongest paths", () => {
    render(
      <WarmPathBadge
        paths={[
          { strength: 0.85, hopCount: 1, explanation: "Patent A" },
          { strength: 0.5, hopCount: 2, explanation: "Career overlap" },
        ]}
      />,
    )
    const title = screen
      .getByTestId("warm-path-badge")
      .getAttribute("title")
    expect(title).toContain("85%")
    expect(title).toContain("Patent A")
  })

  it("size=md applies bigger padding classes", () => {
    render(
      <WarmPathBadge paths={[{ strength: 0.5, hopCount: 1 }]} size="md" />,
    )
    const badge = screen.getByTestId("warm-path-badge")
    expect(badge.className).toContain("text-xs")
  })
})
