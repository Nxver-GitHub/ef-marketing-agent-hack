/**
 * Tests for EmploymentEducationDialog.
 *
 * The Dialog is portal-rendered by Radix, so queries use document.body
 * via screen.* (Radix attaches its portal there).
 */
import { describe, it, expect, vi, beforeEach } from "vitest"
import { render, screen, cleanup, fireEvent } from "@testing-library/react"
import {
  EmploymentEducationDialog,
  buildSubheading,
} from "./EmploymentEducationDialog"
import type { EmploymentPeriod } from "@/components/CareerTimeline"
import type { EducationPeriod } from "@/components/EducationTimeline"

beforeEach(() => cleanup())

const sampleEmployment: EmploymentPeriod[] = [
  {
    title: "Senior Engineer",
    company_name: "Intel",
    start_year: 2018,
    end_year: 2022,
    is_current: false,
  },
  {
    title: "Principal Engineer",
    company_name: "Nvidia",
    start_year: 2022,
    is_current: true,
  },
]

const sampleEducation: EducationPeriod[] = [
  {
    school_name: "MIT",
    degree: "BS",
    field_of_study: "EECS",
    start_year: 2010,
    end_year: 2014,
  },
]

const samplePerson = {
  canonical_name: "Wei Chen",
  current_title: "Principal Engineer",
  current_company_name: "Nvidia",
}

// ── Pure helpers ───────────────────────────────────────────────────────────

describe("buildSubheading", () => {
  it("returns empty string for both null", () => {
    expect(buildSubheading(null, null)).toBe("")
    expect(buildSubheading(undefined, undefined)).toBe("")
  })

  it("returns just title when company missing", () => {
    expect(buildSubheading("CTO", null)).toBe("CTO")
  })

  it("returns just company when title missing", () => {
    expect(buildSubheading(null, "Acme")).toBe("Acme")
  })

  it("joins title and company with dot separator", () => {
    expect(buildSubheading("CTO", "Acme")).toBe("CTO · Acme")
  })

  it("trims whitespace and treats empty strings as missing", () => {
    expect(buildSubheading("  ", "Acme")).toBe("Acme")
    expect(buildSubheading("CTO", "  ")).toBe("CTO")
  })
})

// ── Component render ───────────────────────────────────────────────────────

describe("EmploymentEducationDialog", () => {
  it("does not render content when open=false", () => {
    render(
      <EmploymentEducationDialog
        open={false}
        onOpenChange={() => undefined}
        person={samplePerson}
        employment={sampleEmployment}
        education={sampleEducation}
      />,
    )
    expect(screen.queryByTestId("ee-dialog")).not.toBeInTheDocument()
  })

  it("renders title + subheading + both sections when open=true", () => {
    render(
      <EmploymentEducationDialog
        open={true}
        onOpenChange={() => undefined}
        person={samplePerson}
        employment={sampleEmployment}
        education={sampleEducation}
      />,
    )
    expect(screen.getByTestId("ee-dialog-title").textContent).toBe(
      "Wei Chen",
    )
    expect(screen.getByTestId("ee-dialog-subheading").textContent).toBe(
      "Principal Engineer · Nvidia",
    )
    expect(
      screen.getByTestId("ee-dialog-employment-section"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("ee-dialog-education-section"),
    ).toBeInTheDocument()
  })

  it("shows employment-empty placeholder when employment is []", () => {
    render(
      <EmploymentEducationDialog
        open={true}
        onOpenChange={() => undefined}
        person={samplePerson}
        employment={[]}
        education={sampleEducation}
      />,
    )
    expect(
      screen.getByTestId("ee-dialog-employment-empty"),
    ).toBeInTheDocument()
  })

  it("shows education-empty placeholder when education is []", () => {
    render(
      <EmploymentEducationDialog
        open={true}
        onOpenChange={() => undefined}
        person={samplePerson}
        employment={sampleEmployment}
        education={[]}
      />,
    )
    expect(
      screen.getByTestId("ee-dialog-education-empty"),
    ).toBeInTheDocument()
  })

  it("calls onOpenChange(false) when Escape is pressed", () => {
    const onOpenChange = vi.fn()
    render(
      <EmploymentEducationDialog
        open={true}
        onOpenChange={onOpenChange}
        person={samplePerson}
        employment={sampleEmployment}
        education={sampleEducation}
      />,
    )
    fireEvent.keyDown(document.body, { key: "Escape" })
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it("section headings include counts", () => {
    render(
      <EmploymentEducationDialog
        open={true}
        onOpenChange={() => undefined}
        person={samplePerson}
        employment={sampleEmployment}
        education={sampleEducation}
      />,
    )
    expect(screen.getByText(/Employment \(2\)/)).toBeInTheDocument()
    expect(screen.getByText(/Education \(1\)/)).toBeInTheDocument()
  })
})
