/**
 * PageSkeleton — animated placeholder for loading-state pages.
 *
 * Three variants tuned for the v3 page shapes:
 *   - `list`   — toolbar + grid of card-shaped rows (Companies, People)
 *   - `chart`  — page header + canvas-shaped block (OrgChart)
 *   - `detail` — left rail + main column + right rail (ProspectDetail)
 *
 * No JS animation — uses Tailwind `animate-pulse` so the shimmer keeps
 * working with reduced-motion preferences (Tailwind disables `pulse` when
 * `prefers-reduced-motion: reduce` is set, which is the right default for
 * loading affordances).
 */
import type { JSX } from "react"
import { cn } from "@/lib/utils"

export type PageSkeletonVariant = "list" | "chart" | "detail"

export interface PageSkeletonProps {
  variant?: PageSkeletonVariant
  /** Number of placeholder rows for the `list` variant. Default 6. */
  rows?: number
  className?: string
}

const SHIMMER = "animate-pulse bg-secondary/60"

export function PageSkeleton({
  variant = "list",
  rows = 6,
  className,
}: PageSkeletonProps): JSX.Element {
  return (
    <div
      className={cn("min-h-screen bg-background p-6 space-y-4", className)}
      role="status"
      aria-busy="true"
      aria-label="Loading"
      data-testid="page-skeleton"
      data-variant={variant}
    >
      {variant === "list" && (
        <>
          {/* Toolbar */}
          <div className={cn("h-14 border border-border", SHIMMER)} />
          {/* Grid */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {Array.from({ length: Math.max(1, rows) }).map((_, i) => (
              <div
                key={i}
                className={cn("h-28 border border-border", SHIMMER)}
                data-testid="page-skeleton-row"
              />
            ))}
          </div>
        </>
      )}

      {variant === "chart" && (
        <>
          {/* Page header strip */}
          <div className={cn("h-16 border border-border", SHIMMER)} />
          {/* Canvas-shaped block */}
          <div
            className={cn("h-[520px] border border-border", SHIMMER)}
            data-testid="page-skeleton-canvas"
          />
        </>
      )}

      {variant === "detail" && (
        <div className="grid grid-cols-1 lg:grid-cols-[240px_1fr_320px] gap-3">
          {/* Left rail */}
          <div
            className={cn("h-[600px] border border-border", SHIMMER)}
            data-testid="page-skeleton-left"
          />
          {/* Main column */}
          <div className="space-y-3">
            <div className={cn("h-24 border border-border", SHIMMER)} />
            <div className={cn("h-[480px] border border-border", SHIMMER)} />
          </div>
          {/* Right rail */}
          <div
            className={cn("h-[600px] border border-border", SHIMMER)}
            data-testid="page-skeleton-right"
          />
        </div>
      )}
    </div>
  )
}
