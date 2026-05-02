/**
 * Tests for SignalSourceBadge.
 */
import { describe, it, expect, beforeEach } from "vitest"
import { render, screen, cleanup } from "@testing-library/react"
import {
  SignalSourceBadge,
  resolveSource,
  SOURCE_REGISTRY,
} from "./SignalSourceBadge"

beforeEach(() => cleanup())

describe("resolveSource", () => {
  it('returns "unknown" for empty string', () => {
    expect(resolveSource("")).toBe("unknown")
  })

  it("returns the source key when it matches a registered source", () => {
    expect(resolveSource("apollo")).toBe("apollo")
    expect(resolveSource("uspto")).toBe("uspto")
  })

  it('returns "unknown" for unrecognised sources', () => {
    expect(resolveSource("magic_database")).toBe("unknown")
  })
})

describe("SignalSourceBadge component", () => {
  it("renders LinkedIn label for apify_apimaestro", () => {
    render(<SignalSourceBadge source="apify_apimaestro" />)
    expect(screen.getByText("LinkedIn")).toBeInTheDocument()
    expect(
      screen.getByTestId("signal-source-badge").getAttribute("data-source"),
    ).toBe("apify_apimaestro")
  })

  it("renders Apollo label for apollo", () => {
    render(<SignalSourceBadge source="apollo" />)
    expect(screen.getByText("Apollo")).toBeInTheDocument()
  })

  it("renders Semantic Scholar label for semantic_scholar", () => {
    render(<SignalSourceBadge source="semantic_scholar" />)
    expect(screen.getByText("Semantic Scholar")).toBeInTheDocument()
  })

  it("renders USPTO label for uspto", () => {
    render(<SignalSourceBadge source="uspto" />)
    expect(screen.getByText("USPTO")).toBeInTheDocument()
  })

  it("renders GitHub label for github", () => {
    render(<SignalSourceBadge source="github" />)
    expect(screen.getByText("GitHub")).toBeInTheDocument()
  })

  it("falls back to Unknown badge for unrecognised source string", () => {
    render(<SignalSourceBadge source="some-random-string" />)
    expect(
      screen.getByTestId("signal-source-badge").getAttribute("data-source"),
    ).toBe("unknown")
    expect(screen.getByText("Unknown")).toBeInTheDocument()
  })

  it("aria-label includes the source label", () => {
    render(<SignalSourceBadge source="apollo" />)
    expect(screen.getByLabelText("Source: Apollo")).toBeInTheDocument()
  })

  it("custom className is appended to the badge", () => {
    render(<SignalSourceBadge source="apollo" className="my-custom" />)
    expect(
      screen.getByTestId("signal-source-badge").className,
    ).toContain("my-custom")
  })

  it("has tinted ring class from registry", () => {
    render(<SignalSourceBadge source="apollo" />)
    expect(SOURCE_REGISTRY.apollo.className).toContain("emerald")
  })
})
