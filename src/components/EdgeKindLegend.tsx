/**
 * EdgeKindLegend — vertical list of every EdgeKind in EDGE_CONFIGS, grouped
 * by relationship category (Warm / Career / Education / Structural / Other).
 * Each row is a color swatch + label, optionally annotated with the
 * baseStrength as `(0.85)`.
 *
 * Pure presentational. Reads `EDGE_CONFIGS` and `ALL_EDGE_KINDS` from
 * `src/lib/graph.ts`. Category groupings mirror `EdgeFilterPills.tsx` so the
 * legend and filter pills stay in lockstep when new edge kinds land.
 */
import type { JSX } from "react"
import { cn } from "@/lib/utils"
import {
  ALL_EDGE_KINDS,
  EDGE_CONFIGS,
  type EdgeKind,
} from "@/lib/graph"

export interface EdgeKindLegendProps {
  className?: string
  showStrength?: boolean
}

// ── Category groupings (mirrors EdgeFilterPills.tsx) ────────────────────────

export type LegendCategory =
  | "Warm"
  | "Career"
  | "Education"
  | "Structural"
  | "Other"

export const LEGEND_CATEGORY_ORDER: ReadonlyArray<LegendCategory> = [
  "Warm",
  "Career",
  "Education",
  "Structural",
  "Other",
]

const CATEGORY_MEMBERS: Record<
  Exclude<LegendCategory, "Other">,
  ReadonlyArray<string>
> = {
  Warm: [
    "patent_co_inventor",
    "academic_co_author",
    "academic_co_author_multi",
    "academic_co_author_single",
    "conference_co_presenter",
    "standards_committee",
    "same_phd_advisor",
    "co_board_member",
    "co_investor",
  ],
  Career: [
    "career_overlap_general",
    "career_overlap_same_team",
    "career_overlap_same_domain",
    "past_employer",
    "colleague",
    "works_at",
    "reports_to",
  ],
  Education: [
    "same_undergrad_cohort",
    "same_mba_cohort",
    "same_phd_program",
    "executive_education",
    "alumni_network",
    "education",
  ],
  Structural: [
    "located_in",
    "partnership",
    "vertical",
    "scope_signal",
    "evidence_cited",
    "conference_co_attendee",
  ],
}

// ── Pure helpers (exported for tests) ───────────────────────────────────────

export function categoryFor(kind: EdgeKind): LegendCategory {
  for (const cat of ["Warm", "Career", "Education", "Structural"] as const) {
    if (CATEGORY_MEMBERS[cat].includes(kind)) return cat
  }
  return "Other"
}

export interface LegendGroup {
  category: LegendCategory
  kinds: EdgeKind[]
}

export function groupEdgeKinds(
  kinds: ReadonlyArray<EdgeKind> = ALL_EDGE_KINDS,
): LegendGroup[] {
  const buckets = new Map<LegendCategory, EdgeKind[]>()
  for (const kind of kinds) {
    const cat = categoryFor(kind)
    const list = buckets.get(cat)
    if (list) list.push(kind)
    else buckets.set(cat, [kind])
  }
  return LEGEND_CATEGORY_ORDER.flatMap((category): LegendGroup[] => {
    const entries = buckets.get(category)
    if (!entries || entries.length === 0) return []
    return [{ category, kinds: entries }]
  })
}

export function formatStrength(strength: number): string {
  return strength.toFixed(2)
}

// ── Component ──────────────────────────────────────────────────────────────

export function EdgeKindLegend({
  className,
  showStrength = true,
}: EdgeKindLegendProps): JSX.Element {
  const groups = groupEdgeKinds()
  return (
    <div
      data-testid="edge-kind-legend"
      role="list"
      className={cn(
        "text-xs text-slate-700 dark:text-slate-200 space-y-3",
        className,
      )}
    >
      {groups.map(({ category, kinds }) => (
        <section
          key={category}
          data-testid={`legend-group-${category}`}
          data-category={category}
          aria-label={`${category} edges`}
        >
          <h4 className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            {category}
          </h4>
          <ul role="list" className="space-y-1">
            {kinds.map((kind) => {
              const cfg = EDGE_CONFIGS[kind]
              return (
                <li
                  key={kind}
                  role="listitem"
                  data-testid={`legend-row-${kind}`}
                  className="flex items-center gap-2"
                >
                  <span
                    data-testid={`legend-swatch-${kind}`}
                    data-css-var={cfg.cssVarName}
                    aria-hidden="true"
                    style={{ backgroundColor: `var(${cfg.cssVarName})` }}
                    className="inline-block h-3 w-3 shrink-0 rounded-full ring-1 ring-slate-300 dark:ring-slate-600"
                  />
                  <span className="flex-1 truncate">{cfg.displayLabel}</span>
                  {showStrength && cfg.baseStrength > 0 ? (
                    <span
                      data-testid={`legend-strength-${kind}`}
                      className="font-mono text-[10px] text-slate-500 dark:text-slate-400"
                    >
                      ({formatStrength(cfg.baseStrength)})
                    </span>
                  ) : null}
                </li>
              )
            })}
          </ul>
        </section>
      ))}
    </div>
  )
}

export default EdgeKindLegend
