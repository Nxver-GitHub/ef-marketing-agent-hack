/**
 * EducationTimeline — pure presentational vertical timeline of
 * education_periods. Same structural shape as CareerTimeline but with
 * diamond markers and year-only date ranges.
 *
 * Caller passes the array as a prop; this component does NOT fetch.
 */
import { useState, type JSX } from "react";
import { cn } from "@/lib/utils";
import { formatDateRange as sharedFormatDateRange } from "@/components/_timelineDateFormat";

// ── Types ──────────────────────────────────────────────────────────────────

export interface EducationPeriod {
  school_name: string;
  school_linkedin_url?: string | null;
  degree?: string | null;
  start_year?: number | null;
  end_year?: number | null;
  field_of_study?: string | null;
}

export interface EducationTimelineProps {
  education: EducationPeriod[];
  className?: string;
  /** If set, render only the first N rows + a "Show all (N)" expand link. */
  maxRows?: number;
}

// ── Helpers ────────────────────────────────────────────────────────────────

/**
 * Re-export of the shared formatter (year-only mode by default for this
 * component). The exposed signature still matches CareerTimeline's so the
 * function can be used for the unit-test cases listed in the contract.
 */
export function formatDateRange(
  startYear: number | null | undefined,
  startMonth: number | null | undefined,
  endYear: number | null | undefined,
  endMonth: number | null | undefined,
  isCurrent: boolean = false,
): string {
  return sharedFormatDateRange(startYear, startMonth, endYear, endMonth, isCurrent);
}

/** Year-only formatter used internally by the row renderer. */
function formatYearRange(
  startYear: number | null | undefined,
  endYear: number | null | undefined,
): string {
  return sharedFormatDateRange(startYear, null, endYear, null, false, { yearOnly: true });
}

/**
 * Sort education desc by start_year, secondary by end_year desc. Pure —
 * returns a new array. Rows with null start_year sort to the end.
 */
export function sortEducationDesc(rows: EducationPeriod[]): EducationPeriod[] {
  const decorated = rows.map((row, idx) => ({ row, idx }));
  decorated.sort((a, b) => {
    const aYear = a.row.start_year;
    const bYear = b.row.start_year;
    if (aYear == null && bYear == null) return a.idx - b.idx;
    if (aYear == null) return 1;
    if (bYear == null) return -1;
    if (aYear !== bYear) return bYear - aYear;

    const aEnd = a.row.end_year ?? 0;
    const bEnd = b.row.end_year ?? 0;
    if (aEnd !== bEnd) return bEnd - aEnd;

    return a.idx - b.idx;
  });
  return decorated.map((d) => d.row);
}

// ── Sub-components ─────────────────────────────────────────────────────────

interface RowProps {
  period: EducationPeriod;
  rowIndex: number;
}

function Row({ period, rowIndex }: RowProps): JSX.Element {
  const dateLabel = formatYearRange(period.start_year, period.end_year);

  const schoolEl = period.school_linkedin_url ? (
    <a
      href={period.school_linkedin_url}
      target="_blank"
      rel="noopener noreferrer"
      className="text-foreground hover:underline"
      data-testid="education-school-link"
    >
      {period.school_name}
    </a>
  ) : (
    <span className="text-foreground" data-testid="education-school-text">
      {period.school_name}
    </span>
  );

  return (
    <li
      className="relative pl-6 pb-5"
      data-testid="education-row"
      data-row-index={rowIndex}
    >
      {/* Diamond marker — rotated square. */}
      <span
        aria-hidden="true"
        data-testid="education-marker-diamond"
        className="absolute left-[-5px] top-1.5 w-2 h-2 rotate-45 bg-background border border-foreground"
      />
      <div className="space-y-0.5">
        <div className="text-sm font-semibold text-foreground leading-tight">
          {schoolEl}
        </div>
        {period.degree ? (
          <div className="text-xs text-muted-foreground">{period.degree}</div>
        ) : null}
        {period.field_of_study ? (
          <div className="text-[11px] text-muted-foreground italic">
            {period.field_of_study}
          </div>
        ) : null}
        <div className="text-[11px] text-muted-foreground font-mono pt-0.5">
          {dateLabel}
        </div>
      </div>
    </li>
  );
}

// ── Main component ─────────────────────────────────────────────────────────

export function EducationTimeline(props: EducationTimelineProps): JSX.Element {
  const { education, className, maxRows } = props;

  const sorted = sortEducationDesc(education);

  const [expanded, setExpanded] = useState(false);
  const shouldCollapse =
    typeof maxRows === "number" && maxRows >= 0 && sorted.length > maxRows && !expanded;
  const visible = shouldCollapse ? sorted.slice(0, maxRows) : sorted;
  const hiddenCount = sorted.length - visible.length;

  if (sorted.length === 0) {
    return (
      <div
        className={cn("text-xs text-muted-foreground italic", className)}
        data-testid="education-timeline-empty"
      >
        No education history available.
      </div>
    );
  }

  return (
    <div className={cn("relative", className)} data-testid="education-timeline">
      <ol className="relative border-l-2 border-border ml-1 pl-3 list-none">
        {visible.map((period, i) => (
          <Row
            key={`${period.school_name}|${period.degree ?? ""}|${period.start_year ?? "?"}|${i}`}
            period={period}
            rowIndex={i}
          />
        ))}
      </ol>
      {shouldCollapse && hiddenCount > 0 && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          data-testid="education-show-all"
          className="text-xs text-muted-foreground hover:text-foreground underline ml-4"
        >
          Show all ({sorted.length})
        </button>
      )}
    </div>
  );
}

export default EducationTimeline;
