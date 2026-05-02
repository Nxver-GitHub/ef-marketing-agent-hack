/**
 * Shared date-range formatter for CareerTimeline + EducationTimeline.
 *
 * No external date library — pure runtime Date math. Handles the 5 cases:
 *   1. is_current=true → "Mar 2018 – Present · 7 yrs 2 mos"
 *   2. both end_year + end_month → "Mar 2018 – Aug 2024 · 6 yrs 5 mos"
 *   3. missing months → "2018 – 2024 · 6 yrs"
 *   4. missing end → "Mar 2018 – ? · ongoing"
 *   5. missing start → "Date unknown"
 *
 * yearOnly=true forces month-suppression (used by EducationTimeline) so e.g.
 * "2010 – 2014 · 4 yrs" even when month fields exist.
 */

const MONTH_ABBR: readonly string[] = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function fmtMonthYear(year: number, month: number | null | undefined): string {
  if (month && month >= 1 && month <= 12) {
    return `${MONTH_ABBR[month - 1]} ${year}`;
  }
  return `${year}`;
}

/**
 * Compute total months between two (year, month) pairs. month is 1-indexed
 * (1=Jan). Falsy month is treated as January for the start side and as
 * December for the end side so a year-only range gets a sensible duration.
 */
function totalMonths(
  startYear: number,
  startMonth: number | null | undefined,
  endYear: number,
  endMonth: number | null | undefined,
): number {
  const sm = startMonth && startMonth >= 1 && startMonth <= 12 ? startMonth : 1;
  // For an open / year-only end, count through the full end year (Dec).
  const em = endMonth && endMonth >= 1 && endMonth <= 12 ? endMonth : 12;
  return (endYear - startYear) * 12 + (em - sm) + 1;
}

function fmtDuration(months: number, includeMonths: boolean): string {
  const safe = Math.max(0, months);
  const years = Math.floor(safe / 12);
  const mos = safe % 12;
  if (!includeMonths) {
    if (years <= 0) return "<1 yr";
    return years === 1 ? "1 yr" : `${years} yrs`;
  }
  const yPart = years === 1 ? "1 yr" : `${years} yrs`;
  if (mos === 0) {
    return years <= 0 ? "<1 mo" : yPart;
  }
  const mPart = mos === 1 ? "1 mo" : `${mos} mos`;
  if (years <= 0) return mPart;
  return `${yPart} ${mPart}`;
}

export interface FormatDateRangeOptions {
  /** Force year-only display (used by EducationTimeline). */
  yearOnly?: boolean;
}

export function formatDateRange(
  startYear: number | null | undefined,
  startMonth: number | null | undefined,
  endYear: number | null | undefined,
  endMonth: number | null | undefined,
  isCurrent: boolean = false,
  options: FormatDateRangeOptions = {},
): string {
  if (startYear == null) return "Date unknown";

  const yearOnly = options.yearOnly === true;
  const effectiveStartMonth = yearOnly ? null : startMonth;
  const effectiveEndMonth = yearOnly ? null : endMonth;

  const startLabel = fmtMonthYear(startYear, effectiveStartMonth);

  // Whether to include months in the duration suffix. Only when we have
  // a complete (year+month) on both sides — otherwise we'd be making up
  // precision. yearOnly mode never shows months in duration.
  const includeMonths =
    !yearOnly &&
    !!effectiveStartMonth &&
    ((isCurrent) || (endYear != null && !!effectiveEndMonth));

  // Case 1: currently active.
  if (isCurrent) {
    const now = new Date();
    const nowYear = now.getFullYear();
    const nowMonth = now.getMonth() + 1; // 1-indexed
    const months = totalMonths(startYear, effectiveStartMonth, nowYear, nowMonth);
    return `${startLabel} – Present · ${fmtDuration(months, includeMonths)}`;
  }

  // Case 5 (already returned above): missing start.

  // Case 4: missing end_year entirely.
  if (endYear == null) {
    return `${startLabel} – ? · ongoing`;
  }

  // Cases 2 / 3.
  const endLabel = fmtMonthYear(endYear, effectiveEndMonth);
  const months = totalMonths(startYear, effectiveStartMonth, endYear, effectiveEndMonth);
  return `${startLabel} – ${endLabel} · ${fmtDuration(months, includeMonths)}`;
}
