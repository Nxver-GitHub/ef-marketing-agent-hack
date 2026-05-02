/**
 * SignalSourceBadge — small chip identifying where a signal/edge came from.
 *
 * Used by NodeInspector evidence rows + EdgeInspector evidence rows. Pure
 * presentational; the source string maps to (icon, label, color) via the
 * SOURCE_REGISTRY below. Unknown source strings render as the "unknown"
 * variant.
 */
import type { JSX } from "react"
import { cn } from "@/lib/utils"

export type SignalSource =
  | "apify_apimaestro"
  | "apify_harvestapi"
  | "apollo"
  | "pdl"
  | "firecrawl"
  | "semantic_scholar"
  | "uspto"
  | "github"
  | "crunchbase"
  | "unknown"

export interface SignalSourceBadgeProps {
  source: SignalSource | string
  className?: string
}

interface SourceMeta {
  icon: string
  label: string
  // Tailwind tinted classes — kept together so a colorblind reviewer can
  // see the full palette in one place.
  className: string
}

export const SOURCE_REGISTRY: Readonly<Record<SignalSource, SourceMeta>> =
  Object.freeze({
    apify_apimaestro: {
      icon: "🔗",
      label: "LinkedIn",
      className:
        "bg-sky-50 text-sky-800 ring-sky-200 dark:bg-sky-900/40 dark:text-sky-100 dark:ring-sky-800",
    },
    apify_harvestapi: {
      icon: "🔗",
      label: "LinkedIn (Harvest)",
      className:
        "bg-sky-50 text-sky-800 ring-sky-200 dark:bg-sky-900/40 dark:text-sky-100 dark:ring-sky-800",
    },
    apollo: {
      icon: "📧",
      label: "Apollo",
      className:
        "bg-emerald-50 text-emerald-800 ring-emerald-200 dark:bg-emerald-900/40 dark:text-emerald-100 dark:ring-emerald-800",
    },
    pdl: {
      icon: "🧬",
      label: "PDL",
      className:
        "bg-violet-50 text-violet-800 ring-violet-200 dark:bg-violet-900/40 dark:text-violet-100 dark:ring-violet-800",
    },
    firecrawl: {
      icon: "🔥",
      label: "Firecrawl",
      className:
        "bg-orange-50 text-orange-800 ring-orange-200 dark:bg-orange-900/40 dark:text-orange-100 dark:ring-orange-800",
    },
    semantic_scholar: {
      icon: "📄",
      label: "Semantic Scholar",
      className:
        "bg-indigo-50 text-indigo-800 ring-indigo-200 dark:bg-indigo-900/40 dark:text-indigo-100 dark:ring-indigo-800",
    },
    uspto: {
      icon: "📜",
      label: "USPTO",
      className:
        "bg-amber-50 text-amber-800 ring-amber-200 dark:bg-amber-900/40 dark:text-amber-100 dark:ring-amber-800",
    },
    github: {
      icon: "🐙",
      label: "GitHub",
      className:
        "bg-slate-100 text-slate-800 ring-slate-300 dark:bg-slate-800 dark:text-slate-100 dark:ring-slate-700",
    },
    crunchbase: {
      icon: "💰",
      label: "Crunchbase",
      className:
        "bg-cyan-50 text-cyan-800 ring-cyan-200 dark:bg-cyan-900/40 dark:text-cyan-100 dark:ring-cyan-800",
    },
    unknown: {
      icon: "❓",
      label: "Unknown",
      className:
        "bg-slate-50 text-slate-600 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700",
    },
  })

const KNOWN_SOURCES = new Set<string>(Object.keys(SOURCE_REGISTRY))

// ── Pure helpers (exported for tests) ───────────────────────────────────────

export function resolveSource(source: string): SignalSource {
  if (KNOWN_SOURCES.has(source)) return source as SignalSource
  return "unknown"
}

// ── Component ──────────────────────────────────────────────────────────────

export function SignalSourceBadge({
  source,
  className,
}: SignalSourceBadgeProps): JSX.Element {
  const key = resolveSource(source)
  const meta = SOURCE_REGISTRY[key]
  return (
    <span
      data-testid="signal-source-badge"
      data-source={key}
      aria-label={`Source: ${meta.label}`}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset",
        meta.className,
        className,
      )}
    >
      <span aria-hidden="true">{meta.icon}</span>
      <span>{meta.label}</span>
    </span>
  )
}

export default SignalSourceBadge
