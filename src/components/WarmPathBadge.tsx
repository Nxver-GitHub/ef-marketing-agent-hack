/**
 * WarmPathBadge — small inline badge for list rows.
 *
 * Renders a count of available warm paths plus a colored dot indicating
 * the strongest path strength. Color bands mirror EdgeInspector:
 *   ≥ 0.70 emerald (strong, single warm hop)
 *   ≥ 0.40 amber (moderate)
 *   <  0.40 slate (weak, mostly cold)
 *
 * Pure presentational. No data fetching, no router. Caller passes paths
 * and (optionally) an onClick — when present, the badge becomes a button
 * that opens the warm-path detail panel.
 */
import type { JSX } from "react"
import { cn } from "@/lib/utils"

export interface WarmPathBadgeProps {
  paths: Array<{ strength: number; hopCount: number; explanation?: string }>
  className?: string
  size?: "sm" | "md"
  onClick?: () => void
}

// ── Pure helpers (exported for tests) ───────────────────────────────────────

export function bestStrength(
  paths: ReadonlyArray<{ strength: number }>,
): number {
  let best = 0
  for (const p of paths) {
    if (p.strength > best) best = p.strength
  }
  return best
}

export type StrengthBand = "strong" | "moderate" | "weak"

export function strengthBand(strength: number): StrengthBand {
  if (strength >= 0.7) return "strong"
  if (strength >= 0.4) return "moderate"
  return "weak"
}

const DOT_BG: Record<StrengthBand, string> = {
  strong: "bg-emerald-500",
  moderate: "bg-amber-500",
  weak: "bg-slate-400",
}

const SIZE_CLASSES: Record<"sm" | "md", { wrap: string; dot: string }> = {
  sm: { wrap: "px-1.5 py-0.5 text-[10px]", dot: "h-1.5 w-1.5" },
  md: { wrap: "px-2 py-1 text-xs", dot: "h-2 w-2" },
}

/**
 * Tooltip text — short summary for hover. Up to 3 paths previewed.
 */
export function buildTooltip(
  paths: ReadonlyArray<{
    strength: number
    hopCount: number
    explanation?: string
  }>,
): string {
  if (paths.length === 0) return "No warm paths"
  const sorted = [...paths].sort((a, b) => b.strength - a.strength)
  const lines = sorted.slice(0, 3).map((p) => {
    const pct = Math.round(p.strength * 100)
    const expl = p.explanation ? ` — ${p.explanation}` : ""
    return `${pct}% · ${p.hopCount} hop${p.hopCount === 1 ? "" : "s"}${expl}`
  })
  if (sorted.length > 3) {
    lines.push(`+${sorted.length - 3} more`)
  }
  return lines.join("\n")
}

// ── Component ──────────────────────────────────────────────────────────────

export function WarmPathBadge({
  paths,
  className,
  size = "sm",
  onClick,
}: WarmPathBadgeProps): JSX.Element | null {
  const count = paths.length
  if (count === 0) return null

  const best = bestStrength(paths)
  const band = strengthBand(best)
  const tooltip = buildTooltip(paths)
  const sz = SIZE_CLASSES[size]
  const label = `${count} warm path${count === 1 ? "" : "s"}`

  const baseClass = cn(
    "inline-flex items-center gap-1 rounded-full border font-medium",
    "border-slate-200 bg-slate-50 text-slate-700 transition-colors",
    "dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100",
    sz.wrap,
    onClick && "cursor-pointer hover:bg-slate-100 dark:hover:bg-slate-700",
    className,
  )

  const inner = (
    <>
      <span
        data-testid="warm-path-badge-dot"
        data-band={band}
        className={cn("rounded-full", sz.dot, DOT_BG[band])}
        aria-hidden="true"
      />
      <span data-testid="warm-path-badge-count">{count}</span>
    </>
  )

  if (onClick) {
    return (
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation()
          onClick()
        }}
        title={tooltip}
        aria-label={label}
        data-testid="warm-path-badge"
        data-strength-band={band}
        className={baseClass}
      >
        {inner}
      </button>
    )
  }

  return (
    <span
      title={tooltip}
      aria-label={label}
      data-testid="warm-path-badge"
      data-strength-band={band}
      className={baseClass}
    >
      {inner}
    </span>
  )
}

export default WarmPathBadge
