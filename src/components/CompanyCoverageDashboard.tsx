/**
 * CompanyCoverageDashboard — top-of-page summary for the `/companies` view.
 *
 * Pure presentational. Visualises enrichment coverage progress across the
 * 59 target companies as four stat cards, a horizontal coverage bar chart,
 * and a 2x2 grid of mini-summaries (tier breakdown + three top-5 lists).
 *
 * Design notes:
 *   - No fetching, no async, no mutation. Caller owns data; we render it.
 *   - All sort/aggregate work is memoised. We never sort the input array
 *     in place (immutability). All maps/reductions emit new objects.
 *   - Tier colors are inlined here as CSS-var driven backgrounds. We do
 *     not import from `orgClusters` because the canonical taxonomy there
 *     is per-functional-domain, not per-company-tier. If the team later
 *     consolidates tier colors into a shared module, swap the local
 *     `tierAccent` map for an import.
 */
import type { CSSProperties, JSX } from "react";
import { useMemo } from "react";
import { cn } from "@/lib/utils";

// ── Types ───────────────────────────────────────────────────────────────────

export interface CompanyCoverageRow {
  id: string;
  canonical_name: string;
  industry?: string | null;
  tier?:
    | "semiconductor"
    | "defense"
    | "aerospace"
    | "research_lab"
    | "other"
    | string
    | null;
  enriched_count: number;
  target_count?: number;
  warm_paths_count?: number;
  edge_kinds_count?: number;
  org_chart_confidence?: number | null;
}

export interface CompanyCoverageDashboardProps {
  companies: CompanyCoverageRow[];
  className?: string;
  onCompanyClick?: (id: string) => void;
}

// ── Constants ───────────────────────────────────────────────────────────────

const DEFAULT_TARGET = 500;

/**
 * Per-tier accent colors. Plain Tailwind utilities so the component
 * stays self-contained — caller does not need to register any CSS vars.
 * Falls back to neutral gray for unknown tiers.
 */
const TIER_BAR_CLASSES: Record<string, string> = {
  semiconductor: "bg-sky-500",
  defense: "bg-rose-500",
  aerospace: "bg-violet-500",
  research_lab: "bg-emerald-500",
  other: "bg-slate-400",
};

const TIER_LABELS: Record<string, string> = {
  semiconductor: "Semiconductor",
  defense: "Defense",
  aerospace: "Aerospace",
  research_lab: "Research Lab",
  other: "Other",
};

const TIER_FALLBACK_CLASS = "bg-slate-400";

// ── Pure helpers ────────────────────────────────────────────────────────────

/** Resolve target_count, defaulting null/undefined to DEFAULT_TARGET. */
export function resolveTarget(row: CompanyCoverageRow): number {
  const t = row.target_count;
  if (typeof t !== "number" || !Number.isFinite(t) || t <= 0) return DEFAULT_TARGET;
  return t;
}

/** enriched / target capped at [0, 1]. */
export function coverageRatio(row: CompanyCoverageRow): number {
  const target = resolveTarget(row);
  const enriched = Number.isFinite(row.enriched_count) ? row.enriched_count : 0;
  if (target <= 0) return 0;
  return Math.max(0, Math.min(1, enriched / target));
}

/** Percentage 0-100 (rounded) for the coverage ratio. */
export function coveragePercent(row: CompanyCoverageRow): number {
  return Math.round(coverageRatio(row) * 100);
}

/** Pick the tier styling class. */
export function tierBarClass(tier: CompanyCoverageRow["tier"]): string {
  if (typeof tier !== "string") return TIER_FALLBACK_CLASS;
  return TIER_BAR_CLASSES[tier] ?? TIER_FALLBACK_CLASS;
}

/** Human-readable tier label. */
function tierLabel(tier: CompanyCoverageRow["tier"]): string {
  if (typeof tier !== "string") return "Unknown";
  return TIER_LABELS[tier] ?? tier;
}

// ── Aggregations ────────────────────────────────────────────────────────────

interface Totals {
  totalEnriched: number;
  companyCount: number;
  atTargetCount: number;
  totalWarmPaths: number;
  avgEdgeKinds: number;
}

function computeTotals(companies: CompanyCoverageRow[]): Totals {
  if (companies.length === 0) {
    return {
      totalEnriched: 0,
      companyCount: 0,
      atTargetCount: 0,
      totalWarmPaths: 0,
      avgEdgeKinds: 0,
    };
  }
  let totalEnriched = 0;
  let atTargetCount = 0;
  let totalWarmPaths = 0;
  let edgeKindSum = 0;
  let edgeKindRows = 0;
  for (const c of companies) {
    const enriched = Number.isFinite(c.enriched_count) ? c.enriched_count : 0;
    totalEnriched += enriched;
    if (enriched >= resolveTarget(c)) atTargetCount += 1;
    if (typeof c.warm_paths_count === "number" && Number.isFinite(c.warm_paths_count)) {
      totalWarmPaths += c.warm_paths_count;
    }
    if (typeof c.edge_kinds_count === "number" && Number.isFinite(c.edge_kinds_count)) {
      edgeKindSum += c.edge_kinds_count;
      edgeKindRows += 1;
    }
  }
  const avgEdgeKinds = edgeKindRows === 0 ? 0 : edgeKindSum / edgeKindRows;
  return {
    totalEnriched,
    companyCount: companies.length,
    atTargetCount,
    totalWarmPaths,
    avgEdgeKinds,
  };
}

interface TierBucket {
  tier: string;
  count: number;
  enriched: number;
}

function bucketByTier(companies: CompanyCoverageRow[]): TierBucket[] {
  const map = new Map<string, TierBucket>();
  for (const c of companies) {
    const key = typeof c.tier === "string" && c.tier.length > 0 ? c.tier : "other";
    const enriched = Number.isFinite(c.enriched_count) ? c.enriched_count : 0;
    const existing = map.get(key);
    if (existing) {
      // Immutable update — emit a new object, do not mutate the bucket.
      map.set(key, {
        tier: existing.tier,
        count: existing.count + 1,
        enriched: existing.enriched + enriched,
      });
    } else {
      map.set(key, { tier: key, count: 1, enriched });
    }
  }
  // Sort by enriched desc — biggest tier first.
  return [...map.values()].sort((a, b) => b.enriched - a.enriched);
}

// ── Sub-components ──────────────────────────────────────────────────────────

interface StatCardProps {
  label: string;
  value: string;
  hint?: string;
  testId: string;
}

function StatCard({ label, value, hint, testId }: StatCardProps): JSX.Element {
  return (
    <div
      data-testid={testId}
      className="rounded-md border border-border bg-card px-3 py-2.5"
    >
      <div className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-foreground">
        {value}
      </div>
      {hint ? (
        <div className="mt-0.5 text-[11px] text-muted-foreground">{hint}</div>
      ) : null}
    </div>
  );
}

interface CoverageBarRowProps {
  row: CompanyCoverageRow;
  onClick?: (id: string) => void;
}

function CoverageBarRow({ row, onClick }: CoverageBarRowProps): JSX.Element {
  const target = resolveTarget(row);
  const enriched = Number.isFinite(row.enriched_count) ? row.enriched_count : 0;
  const ratio = coverageRatio(row);
  const pct = Math.round(ratio * 100);
  const widthStyle: CSSProperties = { width: `${pct}%` };
  const barClass = tierBarClass(row.tier);
  const interactive = typeof onClick === "function";

  const handleClick = (): void => {
    if (interactive) onClick!(row.id);
  };
  const handleKey = (e: React.KeyboardEvent<HTMLDivElement>): void => {
    if (!interactive) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onClick!(row.id);
    }
  };

  return (
    <div
      data-testid={`coverage-row-${row.id}`}
      data-tier={row.tier ?? "unknown"}
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      onClick={interactive ? handleClick : undefined}
      onKeyDown={interactive ? handleKey : undefined}
      className={cn(
        "grid grid-cols-[160px_1fr_auto] items-center gap-3 rounded-sm px-2 py-1.5",
        interactive
          ? "cursor-pointer hover:bg-muted/40 focus:outline-none focus-visible:ring-1 focus-visible:ring-foreground/40"
          : "",
      )}
    >
      <div className="truncate text-sm text-foreground" title={row.canonical_name}>
        {row.canonical_name}
      </div>
      <div className="relative h-2 overflow-hidden rounded-full bg-muted">
        <div
          data-testid={`coverage-bar-fill-${row.id}`}
          className={cn("h-full rounded-full", barClass)}
          style={widthStyle}
        />
      </div>
      <div className="flex items-center gap-2 text-[11px] tabular-nums text-muted-foreground">
        <span data-testid={`coverage-label-${row.id}`}>
          {enriched} / {target} ({pct}%)
        </span>
        {typeof row.warm_paths_count === "number" &&
        Number.isFinite(row.warm_paths_count) ? (
          <span
            data-testid={`coverage-warm-${row.id}`}
            className="rounded-full border border-border bg-background px-1.5 py-0.5 text-[10px] text-foreground"
            title="Warm paths from your team"
          >
            {row.warm_paths_count} warm
          </span>
        ) : null}
      </div>
    </div>
  );
}

interface MiniListProps {
  title: string;
  rows: ReadonlyArray<{ id: string; label: string; value: string }>;
  testId: string;
  emptyMessage: string;
  onRowClick?: (id: string) => void;
}

function MiniList({
  title,
  rows,
  testId,
  emptyMessage,
  onRowClick,
}: MiniListProps): JSX.Element {
  return (
    <div
      data-testid={testId}
      className="flex flex-col gap-1 rounded-md border border-border bg-card p-3"
    >
      <div className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
        {title}
      </div>
      {rows.length === 0 ? (
        <div className="py-2 text-xs text-muted-foreground">{emptyMessage}</div>
      ) : (
        <ul className="flex flex-col gap-0.5">
          {rows.map((r) => {
            const interactive = typeof onRowClick === "function";
            return (
              <li
                key={r.id}
                data-testid={`${testId}-row-${r.id}`}
                role={interactive ? "button" : undefined}
                tabIndex={interactive ? 0 : undefined}
                onClick={interactive ? (): void => onRowClick!(r.id) : undefined}
                onKeyDown={
                  interactive
                    ? (e): void => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onRowClick!(r.id);
                        }
                      }
                    : undefined
                }
                className={cn(
                  "flex items-center justify-between gap-2 rounded-sm px-1.5 py-1 text-xs",
                  interactive
                    ? "cursor-pointer hover:bg-muted/40 focus:outline-none focus-visible:ring-1 focus-visible:ring-foreground/40"
                    : "",
                )}
              >
                <span className="truncate text-foreground" title={r.label}>
                  {r.label}
                </span>
                <span className="shrink-0 tabular-nums text-muted-foreground">
                  {r.value}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

interface TierBreakdownProps {
  buckets: TierBucket[];
  totalEnriched: number;
}

function TierBreakdown({ buckets, totalEnriched }: TierBreakdownProps): JSX.Element {
  return (
    <div
      data-testid="tier-breakdown"
      className="flex flex-col gap-1 rounded-md border border-border bg-card p-3"
    >
      <div className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
        Tier breakdown
      </div>
      {buckets.length === 0 ? (
        <div className="py-2 text-xs text-muted-foreground">No data</div>
      ) : (
        <ul className="flex flex-col gap-1">
          {buckets.map((b) => {
            const pct =
              totalEnriched <= 0
                ? 0
                : Math.round((b.enriched / totalEnriched) * 100);
            return (
              <li
                key={b.tier}
                data-testid={`tier-row-${b.tier}`}
                className="flex items-center gap-2 text-xs"
              >
                <span
                  aria-hidden="true"
                  data-testid={`tier-swatch-${b.tier}`}
                  className={cn("inline-block h-2 w-2 rounded-sm", tierBarClass(b.tier))}
                />
                <span className="flex-1 truncate text-foreground">
                  {tierLabel(b.tier)}
                </span>
                <span className="tabular-nums text-muted-foreground">
                  {b.count} co · {b.enriched} ppl ({pct}%)
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// ── Main ────────────────────────────────────────────────────────────────────

export function CompanyCoverageDashboard(
  props: CompanyCoverageDashboardProps,
): JSX.Element {
  const { companies, className, onCompanyClick } = props;

  // Empty state — render the shell so the page layout doesn't jump, but
  // surface a clear message instead of empty cards.
  const isEmpty = companies.length === 0;

  // ---- Aggregations (memoised; immutable copies of input) ----
  const totals = useMemo(() => computeTotals(companies), [companies]);

  const sortedByCoverage = useMemo(() => {
    // New array — never mutate caller input.
    return [...companies].sort((a, b) => coverageRatio(b) - coverageRatio(a));
  }, [companies]);

  const sortedByWarmPaths = useMemo(() => {
    return [...companies].sort((a, b) => {
      const av = typeof a.warm_paths_count === "number" ? a.warm_paths_count : 0;
      const bv = typeof b.warm_paths_count === "number" ? b.warm_paths_count : 0;
      return bv - av;
    });
  }, [companies]);

  const tierBuckets = useMemo(() => bucketByTier(companies), [companies]);

  const top5BestCovered = useMemo(
    () => sortedByCoverage.slice(0, 5),
    [sortedByCoverage],
  );

  const top5MostPaths = useMemo(
    () => sortedByWarmPaths.slice(0, 5),
    [sortedByWarmPaths],
  );

  const needsAttention = useMemo(() => {
    // Under 50% of target. Sort ascending so the most-behind appears first.
    return [...companies]
      .filter((c) => coverageRatio(c) < 0.5)
      .sort((a, b) => coverageRatio(a) - coverageRatio(b))
      .slice(0, 5);
  }, [companies]);

  return (
    <section
      data-testid="company-coverage-dashboard"
      className={cn("flex flex-col gap-3", className)}
    >
      {/* ── Stat cards ─────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          testId="stat-total-enriched"
          label="Total enriched"
          value={totals.totalEnriched.toLocaleString()}
          hint={`of ${totals.companyCount} ${totals.companyCount === 1 ? "company" : "companies"} total`}
        />
        <StatCard
          testId="stat-at-target"
          label="At target (≥500)"
          value={totals.atTargetCount.toLocaleString()}
          hint="companies fully enriched"
        />
        <StatCard
          testId="stat-warm-paths"
          label="Total warm paths"
          value={totals.totalWarmPaths.toLocaleString()}
          hint="across all companies"
        />
        <StatCard
          testId="stat-edge-diversity"
          label="Edge type diversity"
          value={totals.avgEdgeKinds.toFixed(1)}
          hint="avg distinct edge kinds / co"
        />
      </div>

      {/* ── Coverage bars ──────────────────────────────────────────── */}
      <div
        data-testid="coverage-bars"
        className="flex flex-col gap-0.5 rounded-md border border-border bg-card p-2"
      >
        <div className="px-2 py-1 text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
          Coverage by company
        </div>
        {isEmpty ? (
          <div
            data-testid="coverage-empty"
            className="px-2 py-6 text-center text-xs text-muted-foreground"
          >
            No companies to display.
          </div>
        ) : (
          sortedByCoverage.map((row) => (
            <CoverageBarRow key={row.id} row={row} onClick={onCompanyClick} />
          ))
        )}
      </div>

      {/* ── 2x2 mini grid ──────────────────────────────────────────── */}
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        <TierBreakdown
          buckets={tierBuckets}
          totalEnriched={totals.totalEnriched}
        />
        <MiniList
          testId="top-best-covered"
          title="Top 5 best-covered"
          emptyMessage="No companies enriched yet"
          rows={top5BestCovered.map((c) => ({
            id: c.id,
            label: c.canonical_name,
            value: `${coveragePercent(c)}%`,
          }))}
          onRowClick={onCompanyClick}
        />
        <MiniList
          testId="top-most-paths"
          title="Top 5 most warm paths"
          emptyMessage="No warm paths recorded"
          rows={top5MostPaths.map((c) => ({
            id: c.id,
            label: c.canonical_name,
            value:
              typeof c.warm_paths_count === "number"
                ? `${c.warm_paths_count}`
                : "0",
          }))}
          onRowClick={onCompanyClick}
        />
        <MiniList
          testId="top-needs-attention"
          title="Top 5 needs attention (<50%)"
          emptyMessage="All companies above 50%"
          rows={needsAttention.map((c) => ({
            id: c.id,
            label: c.canonical_name,
            value: `${coveragePercent(c)}%`,
          }))}
          onRowClick={onCompanyClick}
        />
      </div>
    </section>
  );
}
