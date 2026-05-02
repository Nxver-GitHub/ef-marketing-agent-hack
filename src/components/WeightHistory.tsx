/**
 * WeightHistory — timeline of recent score-weight versions + their impact.
 *
 * Pure presentational. Caller passes in the version array (no fetching, no
 * useState, no useEffect). Designed for a Settings sub-section or a debug
 * pane where operators audit how scoring weights have evolved over time
 * and how many score rows each version touched.
 *
 * Visual language matches `WeightVersionBanner.tsx` (label-eyebrow, tight
 * monospaced numbers, per-row borders).
 */
import type { JSX } from "react";
import { cn } from "@/lib/utils";

// ── Types ───────────────────────────────────────────────────────────────────

export interface WeightVersionEntry {
  id: string;
  version_number: number;
  created_at: string;
  created_by?: string | null;
  /**
   * Diff vs the prior version. Map keyed by weight component name.
   * `{ old, new }` are the float weight values. Null when the row is the
   * first-ever version (nothing to diff against).
   */
  weights_diff?: Record<string, { old: number; new: number }> | null;
  /**
   * How many `score_records` rows were re-computed when this version
   * landed. Null when the recompute job hasn't finished or wasn't
   * tracked. Drives the "impact" column.
   */
  scores_recomputed_count?: number | null;
}

export interface WeightHistoryProps {
  versions: WeightVersionEntry[];
  /** Cap rows rendered (after sort). Default `12`. */
  maxRows?: number;
  className?: string;
}

// ── Pure helpers (exported for testability) ─────────────────────────────────

/** Count diff entries safely. Returns 0 for null / undefined / empty. */
export function diffCount(
  diff: WeightVersionEntry["weights_diff"],
): number {
  if (diff == null || typeof diff !== "object") return 0;
  return Object.keys(diff).length;
}

/** Human-readable diff summary: "3 weights changed" / "no diff" / "1 weight changed". */
export function diffSummary(
  diff: WeightVersionEntry["weights_diff"],
): string {
  const n = diffCount(diff);
  if (n === 0) return "no diff";
  if (n === 1) return "1 weight changed";
  return `${n} weights changed`;
}

/** Format an ISO datetime as compact "YYYY-MM-DD HH:MM" UTC. Returns "" on failure. */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  // YYYY-MM-DD HH:MM UTC for unambiguous operator-readable timestamps.
  const pad = (n: number) => n.toString().padStart(2, "0");
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`
  );
}

/** Format US-style number (1582 → "1,582"). Returns "" for null/non-finite. */
export function formatRecomputeCount(n: number | null | undefined): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return "";
  return new Intl.NumberFormat("en-US").format(n);
}

/** Sort versions descending by `version_number`. Returns a new array. */
export function sortVersionsDesc(
  versions: WeightVersionEntry[],
): WeightVersionEntry[] {
  return [...versions].sort((a, b) => b.version_number - a.version_number);
}

// ── Component ───────────────────────────────────────────────────────────────

const DEFAULT_MAX_ROWS = 12;

export function WeightHistory({
  versions,
  maxRows = DEFAULT_MAX_ROWS,
  className,
}: WeightHistoryProps): JSX.Element {
  const sorted = sortVersionsDesc(versions);
  const visible = sorted.slice(0, Math.max(0, maxRows));

  if (visible.length === 0) {
    return (
      <section
        className={cn(
          "border border-border bg-card p-4 text-[12px] text-muted-foreground",
          className,
        )}
        data-testid="weight-history-empty"
      >
        <span className="label-eyebrow block mb-1">Weight history</span>
        No weight versions recorded yet.
      </section>
    );
  }

  return (
    <section
      className={cn("border border-border bg-card", className)}
      data-testid="weight-history"
    >
      <header className="flex items-baseline justify-between px-4 py-2 border-b border-border">
        <span className="label-eyebrow">Weight history</span>
        <span className="text-[10px] text-muted-foreground text-mono">
          {visible.length}
          {sorted.length > visible.length && (
            <span className="ml-1">/ {sorted.length}</span>
          )}
        </span>
      </header>

      <ol className="divide-y divide-border">
        {visible.map((v) => {
          const ts = formatTimestamp(v.created_at);
          const recompute = formatRecomputeCount(v.scores_recomputed_count);
          return (
            <li
              key={v.id}
              className="px-4 py-2 grid grid-cols-[auto_1fr_auto] items-baseline gap-3 text-[12px]"
              data-testid={`weight-version-${v.id}`}
            >
              <span className="text-mono text-foreground">
                v{v.version_number}
              </span>
              <span className="text-muted-foreground truncate">
                {ts}
                {v.created_by && (
                  <span className="ml-2 text-foreground/80">
                    {v.created_by}
                  </span>
                )}
                <span className="ml-2">· {diffSummary(v.weights_diff)}</span>
              </span>
              {recompute && (
                <span
                  className="text-[10px] text-mono text-muted-foreground"
                  title="Score rows recomputed"
                >
                  {recompute}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
