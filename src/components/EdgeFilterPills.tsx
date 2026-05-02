/**
 * EdgeFilterPills — horizontal toggle pills for edge-kind visibility.
 *
 * Reads `visibleEdgeKinds` from the global graphStore and exposes one toggle
 * pill per `EdgeKind` declared in `EDGE_CONFIGS` (the Contract 3 single
 * source of truth in `src/lib/graph.ts`). Clicking a pill calls
 * `toggleEdgeKind` on the store. Three bulk-action buttons (Show all / Hide
 * all / Reset) drive `setVisibleEdgeKinds`.
 *
 * Pure presentational: no fetching, no async, no local state. The store IS
 * the state — this component is just a view onto it.
 */
import type { CSSProperties, JSX } from "react";
import { useMemo } from "react";
import {
  ALL_EDGE_KINDS,
  EDGE_CONFIGS,
  type EdgeKind,
} from "@/lib/graph";
import { useGraphStore } from "@/store/graphStore";
import { cn } from "@/lib/utils";

export interface EdgeFilterPillsProps {
  className?: string;
}

// ── Category mapping ────────────────────────────────────────────────────────
//
// Per task spec — categories grouped by relationship type. Any EdgeKind in
// `EDGE_CONFIGS` that is NOT listed below falls into "Other". We intentionally
// list MANY potential kinds (some not currently in EDGE_CONFIGS, e.g.
// same_phd_advisor, co_investor, alumni_network) so that when those edge kinds
// are added to graph.ts in the future they categorize automatically without
// touching this file.

type CategoryKey = "Warm" | "Career" | "Education" | "Structural" | "Other";

const CATEGORY_ORDER: ReadonlyArray<CategoryKey> = [
  "Warm",
  "Career",
  "Education",
  "Structural",
  "Other",
];

const CATEGORY_MEMBERS: Record<Exclude<CategoryKey, "Other">, ReadonlyArray<string>> = {
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
};

function categoryFor(kind: EdgeKind): CategoryKey {
  for (const cat of ["Warm", "Career", "Education", "Structural"] as const) {
    if (CATEGORY_MEMBERS[cat].includes(kind)) return cat;
  }
  return "Other";
}

// ── Helpers ─────────────────────────────────────────────────────────────────

interface GroupedKinds {
  category: CategoryKey;
  kinds: EdgeKind[];
}

function groupKinds(kinds: ReadonlyArray<EdgeKind>): GroupedKinds[] {
  const buckets = new Map<CategoryKey, EdgeKind[]>();
  for (const kind of kinds) {
    const cat = categoryFor(kind);
    const list = buckets.get(cat);
    if (list) list.push(kind);
    else buckets.set(cat, [kind]);
  }
  // Preserve declared category order, drop empty buckets.
  return CATEGORY_ORDER.flatMap((category): GroupedKinds[] => {
    const entries = buckets.get(category);
    if (!entries || entries.length === 0) return [];
    return [{ category, kinds: entries }];
  });
}

// ── Pill ────────────────────────────────────────────────────────────────────

interface EdgePillProps {
  kind: EdgeKind;
  visible: boolean;
  count: number;
  onToggle: (kind: EdgeKind) => void;
}

function EdgePill({ kind, visible, count, onToggle }: EdgePillProps): JSX.Element {
  const cfg = EDGE_CONFIGS[kind];
  // The dynamic per-kind color must be inlined as a CSS variable — Tailwind
  // can't compose a class from an arbitrary `var(--edge-…)` token. The
  // swatch element below reads `--swatch` from this style and uses it as
  // its background. The CSS vars in src/index.css are stored as raw HSL
  // triples (e.g. "199 85% 72%") so they have to be wrapped in `hsl()`.
  const style = {
    "--swatch": `hsl(var(${cfg.cssVarName}))`,
  } as CSSProperties;
  return (
    <button
      type="button"
      role="switch"
      aria-pressed={visible}
      aria-label={`${cfg.displayLabel} filter, ${visible ? "visible" : "hidden"}`}
      onClick={() => onToggle(kind)}
      data-edge-kind={kind}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border border-border bg-background px-2 py-0.5 text-xs",
        "transition-colors hover:border-foreground/40 focus:outline-none focus-visible:ring-1 focus-visible:ring-foreground/40",
        visible ? "opacity-100" : "opacity-40",
      )}
    >
      <span
        aria-hidden="true"
        data-testid={`edge-pill-swatch-${kind}`}
        style={{ ...style, background: "var(--swatch)" }}
        className="inline-block h-[10px] w-[10px] rounded-sm"
      />
      <span
        className={cn(
          "leading-none",
          visible ? "" : "line-through",
        )}
      >
        {cfg.displayLabel}
      </span>
      <span className="text-[10px] tabular-nums text-muted-foreground">
        {count}
      </span>
    </button>
  );
}

// ── Bulk-action buttons ─────────────────────────────────────────────────────

interface BulkActionsProps {
  onShowAll: () => void;
  onHideAll: () => void;
  onReset: () => void;
}

function BulkActions({ onShowAll, onHideAll, onReset }: BulkActionsProps): JSX.Element {
  return (
    <div className="flex items-center gap-1">
      <BulkButton onClick={onShowAll}>Show all</BulkButton>
      <BulkButton onClick={onHideAll}>Hide all</BulkButton>
      <BulkButton onClick={onReset}>Reset</BulkButton>
    </div>
  );
}

function BulkButton({
  onClick,
  children,
}: {
  onClick: () => void;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-full border border-border bg-background px-2 py-0.5 text-[10px] uppercase tracking-wide",
        "text-muted-foreground transition-colors hover:border-foreground/40 hover:text-foreground",
        "focus:outline-none focus-visible:ring-1 focus-visible:ring-foreground/40",
      )}
    >
      {children}
    </button>
  );
}

// ── Component ───────────────────────────────────────────────────────────────

export function EdgeFilterPills(props: EdgeFilterPillsProps): JSX.Element {
  const { className } = props;

  // Subscribe to just the slices we need — avoid re-rendering on every
  // unrelated graph-store change (e.g., selection updates).
  const visibleEdgeKinds = useGraphStore((s) => s.visibleEdgeKinds);
  const edges = useGraphStore((s) => s.edges);
  const toggleEdgeKind = useGraphStore((s) => s.toggleEdgeKind);
  const setVisibleEdgeKinds = useGraphStore((s) => s.setVisibleEdgeKinds);

  // Live counts per EdgeKind, recomputed whenever the edges array reference
  // changes (which is on every setGraph call — buildGraph emits new arrays).
  const countsByKind = useMemo(() => {
    const counts = new Map<EdgeKind, number>();
    for (const e of edges) {
      counts.set(e.kind, (counts.get(e.kind) ?? 0) + 1);
    }
    return counts;
  }, [edges]);

  const groups = useMemo(() => groupKinds(ALL_EDGE_KINDS), []);
  const totalKinds = ALL_EDGE_KINDS.length;
  const visibleCount = ALL_EDGE_KINDS.filter((k) => visibleEdgeKinds.has(k)).length;

  const handleShowAll = (): void => {
    setVisibleEdgeKinds(new Set<EdgeKind>(ALL_EDGE_KINDS));
  };
  const handleHideAll = (): void => {
    setVisibleEdgeKinds(new Set<EdgeKind>());
  };
  const handleReset = (): void => {
    const defaults = new Set<EdgeKind>(
      ALL_EDGE_KINDS.filter((k) => EDGE_CONFIGS[k].defaultVisible),
    );
    setVisibleEdgeKinds(defaults);
  };

  return (
    <div
      className={cn(
        "flex flex-col gap-1.5 border-b border-border bg-background/80 px-3 py-2",
        className,
      )}
      data-testid="edge-filter-pills"
    >
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
          Edge filters · {visibleCount} of {totalKinds} visible
        </span>
        <BulkActions
          onShowAll={handleShowAll}
          onHideAll={handleHideAll}
          onReset={handleReset}
        />
      </div>
      <div className="flex flex-col gap-1">
        {groups.map(({ category, kinds }) => (
          <div
            key={category}
            data-category={category}
            className="flex flex-wrap items-center gap-1.5"
          >
            <span className="text-[10px] uppercase tracking-wide text-muted-foreground/70 w-16 shrink-0">
              {category}
            </span>
            {kinds.map((kind) => (
              <EdgePill
                key={kind}
                kind={kind}
                visible={visibleEdgeKinds.has(kind)}
                count={countsByKind.get(kind) ?? 0}
                onToggle={toggleEdgeKind}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
