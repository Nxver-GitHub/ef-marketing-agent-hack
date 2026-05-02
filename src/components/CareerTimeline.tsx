/**
 * CareerTimeline — pure presentational vertical timeline of employment_periods.
 *
 * Renders one rail with circular markers (filled = currently held, outlined =
 * past) and a row per employment period. Shape mirrors the
 * `employment_periods` table written by ``credence.enrichment.writer``.
 *
 * Caller passes the array as a prop; this component does NOT fetch.
 */
import { useState, type JSX } from "react";
import { cn } from "@/lib/utils";
import { formatDateRange as sharedFormatDateRange } from "@/components/_timelineDateFormat";

// ── Types ──────────────────────────────────────────────────────────────────

export interface EmploymentPeriod {
  title: string;
  company_name: string;
  company_linkedin_url?: string | null;
  start_year?: number | null;
  start_month?: number | null;
  end_year?: number | null;
  end_month?: number | null;
  is_current?: boolean | null;
  // Optional enrichment fields that may or may not be populated.
  inferred_team?: string | null;
  functional_domain?: string | null;
  seniority_score?: number | null;
}

export interface CareerTimelineProps {
  employment: EmploymentPeriod[];
  className?: string;
  /** When false, hide rows where is_current is true. Defaults to true. */
  showCurrent?: boolean;
  /** If set, render only the first N rows + a "Show all (N)" expand link. */
  maxRows?: number;
}

// ── Helpers ────────────────────────────────────────────────────────────────

/**
 * Re-export of the shared formatter so callers can `import { formatDateRange }
 * from "@/components/CareerTimeline"` per the contract.
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

/**
 * Sort employment desc by start_year (current jobs first, then most recent
 * past). Stable secondary sort by start_month desc when years tie. Pure —
 * returns a new array, never mutates input. Rows with null start_year sort
 * to the very end so the timeline still renders sensibly.
 */
export function sortEmploymentDesc(rows: EmploymentPeriod[]): EmploymentPeriod[] {
  // Decorate-sort-undecorate to keep a stable order via the original index.
  const decorated = rows.map((row, idx) => ({ row, idx }));
  decorated.sort((a, b) => {
    const aCurrent = a.row.is_current === true ? 1 : 0;
    const bCurrent = b.row.is_current === true ? 1 : 0;
    if (aCurrent !== bCurrent) return bCurrent - aCurrent; // current first

    const aYear = a.row.start_year;
    const bYear = b.row.start_year;
    if (aYear == null && bYear == null) return a.idx - b.idx;
    if (aYear == null) return 1;
    if (bYear == null) return -1;
    if (aYear !== bYear) return bYear - aYear;

    const aMonth = a.row.start_month ?? 0;
    const bMonth = b.row.start_month ?? 0;
    if (aMonth !== bMonth) return bMonth - aMonth;

    return a.idx - b.idx;
  });
  return decorated.map((d) => d.row);
}

// ── Sub-components ─────────────────────────────────────────────────────────

function DomainPill({ domain }: { domain: string }): JSX.Element {
  // Light deterministic color tag so the eye can see the same domain twice.
  // Uses a hash on the domain string so it's stable across renders.
  const hue = Math.abs(
    [...domain].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) | 0, 0),
  ) % 360;
  const style = {
    backgroundColor: `hsl(${hue} 65% 92%)`,
    color: `hsl(${hue} 50% 28%)`,
    borderColor: `hsl(${hue} 45% 78%)`,
  } as const;
  return (
    <span
      className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded-sm"
      style={style}
      data-testid="career-domain-pill"
    >
      {domain.replace(/_/g, " ")}
    </span>
  );
}

function TeamPill({ team }: { team: string }): JSX.Element {
  return (
    <span
      className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 border border-border bg-muted text-muted-foreground rounded-sm"
      data-testid="career-team-pill"
    >
      {team}
    </span>
  );
}

interface RowProps {
  period: EmploymentPeriod;
  rowIndex: number;
}

function Row({ period, rowIndex }: RowProps): JSX.Element {
  const isCurrent = period.is_current === true;
  const dateLabel = formatDateRange(
    period.start_year,
    period.start_month,
    period.end_year,
    period.end_month,
    isCurrent,
  );

  const companyEl = period.company_linkedin_url ? (
    <a
      href={period.company_linkedin_url}
      target="_blank"
      rel="noopener noreferrer"
      className="text-foreground hover:underline"
      data-testid="career-company-link"
    >
      {period.company_name}
    </a>
  ) : (
    <span className="text-foreground" data-testid="career-company-text">
      {period.company_name}
    </span>
  );

  return (
    <li
      className="relative pl-6 pb-5"
      data-testid="career-row"
      data-row-index={rowIndex}
      data-is-current={isCurrent ? "true" : "false"}
    >
      {/* Marker — filled circle for current jobs, outlined for past. */}
      <span
        aria-hidden="true"
        data-testid={isCurrent ? "career-marker-current" : "career-marker-past"}
        className={cn(
          "absolute left-[-5px] top-1.5 w-2 h-2 rounded-full border border-foreground",
          isCurrent ? "bg-foreground" : "bg-background",
        )}
      />
      <div className="space-y-1">
        <div className="text-sm font-semibold text-foreground leading-tight">
          {period.title}
        </div>
        <div className="text-xs text-muted-foreground">{companyEl}</div>
        <div className="text-[11px] text-muted-foreground font-mono">{dateLabel}</div>
        {(period.inferred_team || period.functional_domain) && (
          <div className="flex flex-wrap gap-1 pt-0.5">
            {period.inferred_team ? <TeamPill team={period.inferred_team} /> : null}
            {period.functional_domain ? (
              <DomainPill domain={period.functional_domain} />
            ) : null}
          </div>
        )}
      </div>
    </li>
  );
}

// ── Main component ─────────────────────────────────────────────────────────

export function CareerTimeline(props: CareerTimelineProps): JSX.Element {
  const { employment, className, showCurrent = true, maxRows } = props;

  const filtered = showCurrent
    ? employment
    : employment.filter((row) => row.is_current !== true);

  const sorted = sortEmploymentDesc(filtered);

  const [expanded, setExpanded] = useState(false);
  const shouldCollapse =
    typeof maxRows === "number" && maxRows >= 0 && sorted.length > maxRows && !expanded;
  const visible = shouldCollapse ? sorted.slice(0, maxRows) : sorted;
  const hiddenCount = sorted.length - visible.length;

  if (sorted.length === 0) {
    return (
      <div
        className={cn("text-xs text-muted-foreground italic", className)}
        data-testid="career-timeline-empty"
      >
        No employment history available.
      </div>
    );
  }

  return (
    <div className={cn("relative", className)} data-testid="career-timeline">
      <ol className="relative border-l-2 border-border ml-1 pl-3 list-none">
        {visible.map((period, i) => (
          <Row
            key={`${period.company_name}|${period.title}|${period.start_year ?? "?"}|${i}`}
            period={period}
            rowIndex={i}
          />
        ))}
      </ol>
      {shouldCollapse && hiddenCount > 0 && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          data-testid="career-show-all"
          className="text-xs text-muted-foreground hover:text-foreground underline ml-4"
        >
          Show all ({sorted.length})
        </button>
      )}
    </div>
  );
}

export default CareerTimeline;
