/**
 * EmploymentEducationDialog — Radix Dialog wrapper around CareerTimeline +
 * EducationTimeline. Renders the person's full work + school history in a
 * single modal, with a header summarising who they are.
 *
 * Pure: stateless aside from the controlled `open` prop. No data fetching;
 * caller passes employment + education arrays.
 */
import type { JSX } from "react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  CareerTimeline,
  type EmploymentPeriod,
} from "@/components/CareerTimeline"
import {
  EducationTimeline,
  type EducationPeriod,
} from "@/components/EducationTimeline"

export interface EmploymentEducationDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  person: {
    canonical_name: string
    current_title?: string | null
    current_company_name?: string | null
  }
  employment: EmploymentPeriod[]
  education: EducationPeriod[]
}

// ── Pure helpers (exported for tests) ───────────────────────────────────────

export function buildSubheading(
  current_title?: string | null,
  current_company_name?: string | null,
): string {
  const parts: string[] = []
  if (current_title && current_title.trim()) parts.push(current_title.trim())
  if (current_company_name && current_company_name.trim()) {
    parts.push(current_company_name.trim())
  }
  return parts.join(" · ")
}

// ── Component ──────────────────────────────────────────────────────────────

export function EmploymentEducationDialog({
  open,
  onOpenChange,
  person,
  employment,
  education,
}: EmploymentEducationDialogProps): JSX.Element {
  const subheading = buildSubheading(
    person.current_title,
    person.current_company_name,
  )
  const hasEmployment = employment.length > 0
  const hasEducation = education.length > 0
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="ee-dialog"
        className="max-w-2xl max-h-[85vh] overflow-y-auto"
      >
        <DialogHeader>
          <DialogTitle data-testid="ee-dialog-title">
            {person.canonical_name}
          </DialogTitle>
          {subheading ? (
            <DialogDescription data-testid="ee-dialog-subheading">
              {subheading}
            </DialogDescription>
          ) : null}
        </DialogHeader>

        <section
          data-testid="ee-dialog-employment-section"
          className="mt-4"
          aria-labelledby="ee-employment-heading"
        >
          <h3
            id="ee-employment-heading"
            className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400"
          >
            Employment ({employment.length})
          </h3>
          {hasEmployment ? (
            <CareerTimeline employment={employment} />
          ) : (
            <p
              data-testid="ee-dialog-employment-empty"
              className="text-sm text-slate-500 dark:text-slate-400"
            >
              No employment history on file.
            </p>
          )}
        </section>

        <section
          data-testid="ee-dialog-education-section"
          className="mt-6"
          aria-labelledby="ee-education-heading"
        >
          <h3
            id="ee-education-heading"
            className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400"
          >
            Education ({education.length})
          </h3>
          {hasEducation ? (
            <EducationTimeline education={education} />
          ) : (
            <p
              data-testid="ee-dialog-education-empty"
              className="text-sm text-slate-500 dark:text-slate-400"
            >
              No education history on file.
            </p>
          )}
        </section>
      </DialogContent>
    </Dialog>
  )
}

export default EmploymentEducationDialog
