/**
 * CompanyHeaderCard — header card for the upcoming `/org/:companyId` page.
 *
 * Pure presentational. Surfaces every piece of company-level enrichment we
 * have on a single card: identity, geography, enrichment progress toward
 * the 500-person target, org-chart confidence, and a clickable top-N
 * persons list. Caller passes in the company object — no fetching, no
 * useState, no useEffect.
 *
 * Visual language follows `PersonProfileCard.tsx` (border + bg-card + p-*
 * containers, label-eyebrow uppercase tracking, monospaced numbers).
 */
import type { JSX } from "react";
import { cn } from "@/lib/utils";
import { flagEmoji, formatCount } from "@/components/PersonProfileCard";

// ── Types ───────────────────────────────────────────────────────────────────

export interface CompanyHeaderCardCompany {
  id: string;
  canonical_name: string;
  industry?: string | null;
  hq_country?: string | null;
  employee_count_estimate?: number | null;
  domains?: string[] | null;
  org_chart_confidence?: number | null;
  org_chart_signal_count?: number | null;
}

export interface CompanyHeaderCardTopPerson {
  id: string;
  canonical_name: string;
  current_title?: string | null;
  score?: number | null;
}

export interface CompanyHeaderCardProps {
  company: CompanyHeaderCardCompany;
  /** Number of persons we have enriched at this company. */
  enriched_count: number;
  /** Coverage target — defaults to 500 per PROSPECT_ENRICHMENT_TASK.md. */
  target_count?: number;
  /** Optional top-N persons (already sorted upstream). Caller controls order. */
  top_persons?: CompanyHeaderCardTopPerson[];
  /** Optional click handler when a top-person row is clicked. */
  onPersonClick?: (personId: string) => void;
  /**
   * Compact mode: render only identity + enrichment progress, suppress
   * domains chips, org-chart confidence, and top-persons list. Designed
   * for the `/companies` virtualized list where each row needs to stay
   * tight; the full card renders on `/org/:companyId`.
   */
  compact?: boolean;
  /**
   * Optional click handler when the card itself is clicked. Used by the
   * `/companies` list to navigate into `/org/:companyId`. The whole card
   * becomes a button when set; ignored when undefined.
   */
  onClick?: (companyId: string) => void;
  className?: string;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

/** Clamp progress to [0, 1] and percentify. Returns "" on invalid inputs. */
export function progressPercent(
  enriched: number,
  target: number,
): string {
  if (
    typeof enriched !== "number" ||
    typeof target !== "number" ||
    !Number.isFinite(enriched) ||
    !Number.isFinite(target) ||
    target <= 0
  ) {
    return "";
  }
  const pct = Math.max(0, Math.min(1, enriched / target)) * 100;
  return `${Math.round(pct)}%`;
}

/** Confidence percentage label. Returns "" when confidence is null/invalid. */
export function confidenceLabel(conf: number | null | undefined): string {
  if (typeof conf !== "number" || !Number.isFinite(conf)) return "";
  return `${Math.round(Math.max(0, Math.min(1, conf)) * 100)}%`;
}

// ── Component ───────────────────────────────────────────────────────────────

const TOP_N = 5;

export function CompanyHeaderCard({
  company,
  enriched_count,
  target_count = 500,
  top_persons,
  onPersonClick,
  compact = false,
  onClick,
  className,
}: CompanyHeaderCardProps): JSX.Element {
  const flag = flagEmoji(company.hq_country);
  const pct = progressPercent(enriched_count, target_count);
  // Clamp inline width to [0, 100]% to bound the bar cleanly.
  const barRatio =
    target_count > 0
      ? Math.max(0, Math.min(1, enriched_count / target_count))
      : 0;
  const confLabel = confidenceLabel(company.org_chart_confidence);
  const visiblePersons = (top_persons ?? []).slice(0, TOP_N);
  const domains = (company.domains ?? []).filter(
    (d): d is string => typeof d === "string" && d.length > 0,
  );

  // When `onClick` is set, the whole card becomes a clickable surface.
  // We render as a `button` with text-align:left so the inner layout
  // unchanged. Stops the per-person buttons from double-firing via
  // event.stopPropagation in the inner handler.
  const Wrapper = onClick ? "button" : "article";
  const interactive = onClick !== undefined;
  const wrapperProps = interactive
    ? {
        type: "button" as const,
        onClick: () => onClick?.(company.id),
        className: cn(
          "block w-full text-left border border-border bg-card",
          compact ? "p-3 space-y-2" : "p-4 space-y-3",
          "hover:border-accent focus-visible:border-accent transition-colors",
          className,
        ),
      }
    : {
        className: cn(
          "border border-border bg-card",
          compact ? "p-3 space-y-2" : "p-4 space-y-3",
          className,
        ),
      };

  return (
    <Wrapper
      {...wrapperProps}
      data-testid="company-header-card"
      data-company-id={company.id}
    >
      {/* Identity */}
      <header className="space-y-1">
        <h2 className={cn("font-semibold text-foreground", compact ? "text-sm" : "text-base")}>
          {company.canonical_name}
        </h2>
        {(company.industry || flag) && (
          <p className="text-[12px] text-muted-foreground flex items-center gap-2">
            {company.industry && <span>{company.industry}</span>}
            {flag && (
              <span aria-label={company.hq_country ?? "country"}>{flag}</span>
            )}
          </p>
        )}
      </header>

      {/* Domains — suppressed in compact mode */}
      {!compact && domains.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {domains.map((d) => (
            <span
              key={d}
              className="text-[10px] text-muted-foreground border border-border px-1.5 py-0.5"
            >
              {d}
            </span>
          ))}
        </div>
      )}

      {/* Enrichment progress */}
      <div className="space-y-1">
        <div className="flex items-baseline justify-between">
          <span className="label-eyebrow">Enrichment</span>
          <span className="text-[11px] text-mono text-muted-foreground">
            {formatCount(enriched_count)}/{formatCount(target_count)}
            {pct && <span className="ml-2 text-foreground">{pct}</span>}
          </span>
        </div>
        <div
          className="h-1.5 bg-secondary"
          role="progressbar"
          aria-valuenow={Math.round(barRatio * 100)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className="h-full bg-accent transition-[width] duration-300"
            style={{ width: `${barRatio * 100}%` }}
          />
        </div>
      </div>

      {/* Org chart confidence — suppressed in compact mode */}
      {!compact && (confLabel ||
        typeof company.org_chart_signal_count === "number") && (
        <div className="flex items-baseline justify-between">
          <span className="label-eyebrow">Org chart</span>
          <span className="text-[11px] text-mono text-muted-foreground">
            {confLabel && (
              <span className="text-foreground">{confLabel}</span>
            )}
            {typeof company.org_chart_signal_count === "number" && (
              <span className="ml-2">
                {formatCount(company.org_chart_signal_count)} signals
              </span>
            )}
          </span>
        </div>
      )}

      {/* Top persons — suppressed in compact mode */}
      {!compact && visiblePersons.length > 0 && (
        <div className="space-y-1.5">
          <span className="label-eyebrow">Top persons</span>
          <ul className="space-y-1">
            {visiblePersons.map((p) => (
              <li key={p.id}>
                <button
                  type="button"
                  className={cn(
                    "w-full text-left text-[12px] flex items-baseline justify-between",
                    "hover:text-accent focus-visible:text-accent transition-colors",
                    "border-b border-transparent hover:border-border py-0.5",
                  )}
                  onClick={() => onPersonClick?.(p.id)}
                  data-testid={`top-person-${p.id}`}
                >
                  <span className="truncate">
                    <span className="text-foreground">{p.canonical_name}</span>
                    {p.current_title && (
                      <span className="text-muted-foreground ml-2">
                        {p.current_title}
                      </span>
                    )}
                  </span>
                  {typeof p.score === "number" && Number.isFinite(p.score) && (
                    <span className="text-[10px] text-mono text-muted-foreground ml-2">
                      {Math.round(p.score)}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Wrapper>
  );
}
