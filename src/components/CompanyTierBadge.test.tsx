/**
 * Tests for CompanyTierBadge.
 */
import { describe, it, expect, beforeEach } from "vitest"
import { render, screen, cleanup } from "@testing-library/react"
import { CompanyTierBadge, resolveTier } from "./CompanyTierBadge"

beforeEach(() => cleanup())

describe("resolveTier", () => {
  it("returns the tier when it matches the union", () => {
    expect(resolveTier("semiconductor")).toBe("semiconductor")
    expect(resolveTier("defense")).toBe("defense")
    expect(resolveTier("aerospace")).toBe("aerospace")
    expect(resolveTier("research_lab")).toBe("research_lab")
    expect(resolveTier("other")).toBe("other")
  })

  it('falls back to "other" for unknown tier strings', () => {
    expect(resolveTier("biotech")).toBe("other")
    expect(resolveTier("")).toBe("other")
  })
})

describe("CompanyTierBadge component", () => {
  it("renders Semiconductor with blue ring class", () => {
    render(<CompanyTierBadge tier="semiconductor" />)
    expect(screen.getByText("Semiconductor")).toBeInTheDocument()
    expect(
      screen.getByTestId("company-tier-badge").className,
    ).toContain("blue")
  })

  it("renders Defense with red ring class", () => {
    render(<CompanyTierBadge tier="defense" />)
    expect(screen.getByText("Defense")).toBeInTheDocument()
    expect(
      screen.getByTestId("company-tier-badge").className,
    ).toContain("red")
  })

  it("renders Aerospace with indigo ring class", () => {
    render(<CompanyTierBadge tier="aerospace" />)
    expect(screen.getByText("Aerospace")).toBeInTheDocument()
    expect(
      screen.getByTestId("company-tier-badge").className,
    ).toContain("indigo")
  })

  it("renders Research Lab with green ring class", () => {
    render(<CompanyTierBadge tier="research_lab" />)
    expect(screen.getByText("Research Lab")).toBeInTheDocument()
    expect(
      screen.getByTestId("company-tier-badge").className,
    ).toContain("green")
  })

  it('renders Other for "other" tier and unknown strings', () => {
    render(<CompanyTierBadge tier="other" />)
    expect(screen.getByText("Other")).toBeInTheDocument()
    cleanup()
    render(<CompanyTierBadge tier="weird-tier-string" />)
    expect(
      screen.getByTestId("company-tier-badge").getAttribute("data-tier"),
    ).toBe("other")
  })

  it("aria-label includes the tier label", () => {
    render(<CompanyTierBadge tier="defense" />)
    expect(screen.getByLabelText("Tier: Defense")).toBeInTheDocument()
  })

  it("custom className is appended to the badge", () => {
    render(
      <CompanyTierBadge tier="defense" className="my-custom-cls" />,
    )
    expect(
      screen.getByTestId("company-tier-badge").className,
    ).toContain("my-custom-cls")
  })
})
