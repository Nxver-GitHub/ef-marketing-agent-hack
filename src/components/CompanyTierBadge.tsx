/**
 * CompanyTierBadge — colored chip identifying a company's vertical tier.
 *
 * Pure presentational. Unrecognised tier strings render as the "other"
 * variant so callers don't need to gate on the literal union upstream.
 */
import type { JSX } from "react"
import { cn } from "@/lib/utils"

export type CompanyTier =
  | "semiconductor"
  | "defense"
  | "aerospace"
  | "research_lab"
  | "other"

export interface CompanyTierBadgeProps {
  tier: CompanyTier | string
  className?: string
}

interface TierMeta {
  label: string
  className: string
}

export const TIER_REGISTRY: Readonly<Record<CompanyTier, TierMeta>> =
  Object.freeze({
    semiconductor: {
      label: "Semiconductor",
      className:
        "bg-blue-50 text-blue-800 ring-blue-200 dark:bg-blue-900/40 dark:text-blue-100 dark:ring-blue-800",
    },
    defense: {
      label: "Defense",
      className:
        "bg-red-50 text-red-800 ring-red-200 dark:bg-red-900/40 dark:text-red-100 dark:ring-red-800",
    },
    aerospace: {
      label: "Aerospace",
      className:
        "bg-indigo-50 text-indigo-800 ring-indigo-200 dark:bg-indigo-900/40 dark:text-indigo-100 dark:ring-indigo-800",
    },
    research_lab: {
      label: "Research Lab",
      className:
        "bg-green-50 text-green-800 ring-green-200 dark:bg-green-900/40 dark:text-green-100 dark:ring-green-800",
    },
    other: {
      label: "Other",
      className:
        "bg-slate-100 text-slate-700 ring-slate-200 dark:bg-slate-800 dark:text-slate-200 dark:ring-slate-700",
    },
  })

const KNOWN_TIERS = new Set<string>(Object.keys(TIER_REGISTRY))

// ── Pure helpers (exported for tests) ───────────────────────────────────────

export function resolveTier(tier: string): CompanyTier {
  if (KNOWN_TIERS.has(tier)) return tier as CompanyTier
  return "other"
}

// ── Component ──────────────────────────────────────────────────────────────

export function CompanyTierBadge({
  tier,
  className,
}: CompanyTierBadgeProps): JSX.Element {
  const key = resolveTier(tier)
  const meta = TIER_REGISTRY[key]
  return (
    <span
      data-testid="company-tier-badge"
      data-tier={key}
      aria-label={`Tier: ${meta.label}`}
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset",
        meta.className,
        className,
      )}
    >
      {meta.label}
    </span>
  )
}

export default CompanyTierBadge
