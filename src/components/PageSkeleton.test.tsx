/**
 * Tests for PageSkeleton — pure presentational, no mocks needed.
 */
import { describe, it, expect, beforeEach } from "vitest"
import { render, screen, cleanup } from "@testing-library/react"
import { PageSkeleton } from "./PageSkeleton"

beforeEach(() => {
  cleanup()
})

describe("PageSkeleton", () => {
  it("renders the list variant by default", () => {
    render(<PageSkeleton />)
    const root = screen.getByTestId("page-skeleton")
    expect(root.getAttribute("data-variant")).toBe("list")
  })

  it("renders the requested number of rows for list variant", () => {
    render(<PageSkeleton variant="list" rows={4} />)
    const rows = screen.getAllByTestId("page-skeleton-row")
    expect(rows.length).toBe(4)
  })

  it("clamps rows to >=1 even if 0 is passed", () => {
    render(<PageSkeleton variant="list" rows={0} />)
    const rows = screen.getAllByTestId("page-skeleton-row")
    expect(rows.length).toBe(1)
  })

  it("renders chart variant with canvas-shaped block", () => {
    render(<PageSkeleton variant="chart" />)
    expect(screen.getByTestId("page-skeleton-canvas")).toBeInTheDocument()
    // No list rows in chart variant.
    expect(screen.queryAllByTestId("page-skeleton-row").length).toBe(0)
  })

  it("renders detail variant with left + right rails", () => {
    render(<PageSkeleton variant="detail" />)
    expect(screen.getByTestId("page-skeleton-left")).toBeInTheDocument()
    expect(screen.getByTestId("page-skeleton-right")).toBeInTheDocument()
  })

  it("sets aria-busy + role=status for screen readers", () => {
    render(<PageSkeleton />)
    const root = screen.getByTestId("page-skeleton")
    expect(root.getAttribute("aria-busy")).toBe("true")
    expect(root.getAttribute("role")).toBe("status")
  })

  it("forwards a custom className", () => {
    render(<PageSkeleton className="extra-class" />)
    const root = screen.getByTestId("page-skeleton")
    expect(root.className).toContain("extra-class")
  })
})
