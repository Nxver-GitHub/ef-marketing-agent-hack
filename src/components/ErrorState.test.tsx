/**
 * Tests for ErrorState — pure presentational, no mocks needed.
 */
import { describe, it, expect, vi, beforeEach } from "vitest"
import { render, screen, cleanup, fireEvent } from "@testing-library/react"
import { ErrorState, errorMessage } from "./ErrorState"

beforeEach(() => {
  cleanup()
})

describe("errorMessage", () => {
  it("returns the string itself when given a string", () => {
    expect(errorMessage("rls denied")).toBe("rls denied")
  })

  it("returns Error.message when given an Error", () => {
    expect(errorMessage(new Error("network down"))).toBe("network down")
  })

  it("returns empty string for null / undefined", () => {
    expect(errorMessage(null)).toBe("")
    expect(errorMessage(undefined)).toBe("")
  })

  it("falls back to String(...) for arbitrary objects", () => {
    const out = errorMessage({ status: 500 } as unknown as Error)
    expect(typeof out).toBe("string")
  })
})

describe("ErrorState rendering", () => {
  it("renders default title + message", () => {
    render(<ErrorState error="Boom" />)
    expect(screen.getByTestId("error-state-title").textContent).toBe(
      "Something went wrong",
    )
    expect(screen.getByTestId("error-state-message").textContent).toBe("Boom")
  })

  it("uses custom title when provided", () => {
    render(<ErrorState error="" title="Cannot load companies" />)
    expect(screen.getByTestId("error-state-title").textContent).toBe(
      "Cannot load companies",
    )
  })

  it("hides the message paragraph when error is empty", () => {
    render(<ErrorState error="" />)
    expect(screen.queryByTestId("error-state-message")).toBeNull()
  })

  it("renders retry button only when retry callback is provided", () => {
    const { rerender } = render(<ErrorState error="oops" />)
    expect(screen.queryByTestId("error-state-retry")).toBeNull()
    rerender(<ErrorState error="oops" retry={() => {}} />)
    expect(screen.getByTestId("error-state-retry")).toBeInTheDocument()
  })

  it("fires retry callback on button click", () => {
    const retry = vi.fn()
    render(<ErrorState error="oops" retry={retry} />)
    fireEvent.click(screen.getByTestId("error-state-retry"))
    expect(retry).toHaveBeenCalledTimes(1)
  })

  it("renders Error instance.message", () => {
    render(<ErrorState error={new Error("connection refused")} />)
    expect(screen.getByText("connection refused")).toBeInTheDocument()
  })

  it("sets role=alert for screen readers", () => {
    render(<ErrorState error="oops" />)
    const root = screen.getByTestId("error-state")
    expect(root.getAttribute("role")).toBe("alert")
  })
})
