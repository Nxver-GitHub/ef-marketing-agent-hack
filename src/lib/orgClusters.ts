/**
 * orgClusters — pure utilities for the 9-key functional-domain taxonomy.
 *
 * Mirrors the canonical Python `credence.taxonomy.FUNCTIONAL_DOMAINS` keyset
 * (see CLAUDE.md "Functional Domain Taxonomy" L297-313). The frontend uses
 * these helpers for cluster color/label/sort decisions on the upcoming
 * `/org/:companyId` page.
 *
 * Design rules:
 * - Pure functions only. No React, no fetching, no global state.
 * - Null/undefined-tolerant on every public input — UI rendering should never
 *   throw on partially-enriched persons.
 * - Unknown domain strings degrade gracefully to a neutral fallback rather
 *   than throwing, so a backend taxonomy patch (e.g. a 10th domain key)
 *   doesn't crash the frontend before frontend can ship a matching update.
 */

// ── Canonical keyspace ──────────────────────────────────────────────────────

/**
 * The 9 functional-domain keys. Matches the Postgres CHECK constraint on
 * `org_functional_clusters.functional_domain` exactly. Adding a key here
 * without updating that CHECK (and the Python taxonomy module) will cause
 * a downstream INSERT failure.
 */
export const FUNCTIONAL_DOMAINS = [
  "hardware_engineering",
  "software_engineering",
  "product_management",
  "manufacturing_ops",
  "sales_marketing",
  "research",
  "finance_legal",
  "people_ops",
  "general_management",
] as const;

export type FunctionalDomain = (typeof FUNCTIONAL_DOMAINS)[number];

/** True when `s` is one of the 9 canonical keys. */
export function isFunctionalDomain(s: string | null | undefined): s is FunctionalDomain {
  if (typeof s !== "string") return false;
  return (FUNCTIONAL_DOMAINS as readonly string[]).includes(s);
}

// ── Display labels ──────────────────────────────────────────────────────────

const DOMAIN_LABELS: Record<FunctionalDomain, string> = {
  hardware_engineering: "Hardware Engineering",
  software_engineering: "Software Engineering",
  product_management: "Product",
  manufacturing_ops: "Manufacturing & Ops",
  sales_marketing: "Sales & Marketing",
  research: "Research",
  finance_legal: "Finance & Legal",
  people_ops: "People Ops",
  general_management: "General Management",
};

/** Human-readable label. Unknown / null inputs return `"Other"`. */
export function domainLabel(
  domain: FunctionalDomain | string | null | undefined,
): string {
  if (isFunctionalDomain(domain)) {
    return DOMAIN_LABELS[domain];
  }
  return "Other";
}

// ── Color tokens (HSL strings) ──────────────────────────────────────────────

// Per-domain HSL hue values. Matches the new `--domain-*` CSS variables in
// `src/index.css` so consumers can use either `domainColor()` for inline
// styles or `domainCssVar()` for Tailwind/className-driven styling.
//
// Hue choices spread across the wheel for visual differentiation while
// staying within the project's muted palette (S=60-75%, L=45-55%):
//   hardware_engineering  → 12  (warm orange-red)
//   software_engineering  → 217 (steel blue, matches --accent)
//   product_management    → 262 (purple)
//   manufacturing_ops     →  35 (amber)
//   sales_marketing       → 142 (green)
//   research              → 295 (magenta)
//   finance_legal         → 178 (teal)
//   people_ops            →  90 (yellow-green)
//   general_management    → 215 (slate)
const DOMAIN_HSL: Record<FunctionalDomain, string> = {
  hardware_engineering: "hsl(12, 70%, 50%)",
  software_engineering: "hsl(217, 75%, 56%)",
  product_management: "hsl(262, 60%, 55%)",
  manufacturing_ops: "hsl(35, 80%, 50%)",
  sales_marketing: "hsl(142, 55%, 45%)",
  research: "hsl(295, 60%, 55%)",
  finance_legal: "hsl(178, 60%, 42%)",
  people_ops: "hsl(90, 50%, 45%)",
  general_management: "hsl(215, 14%, 47%)",
};

const FALLBACK_DOMAIN_COLOR = "hsl(215, 14%, 47%)"; // matches general_management slate

/**
 * Returns an `hsl(...)` string suitable for inline styles. Unknown / null
 * inputs return a neutral slate matching the `general_management` color
 * to keep the cluster legend visually consistent.
 */
export function domainColor(
  domain: FunctionalDomain | string | null | undefined,
): string {
  if (isFunctionalDomain(domain)) {
    return DOMAIN_HSL[domain];
  }
  return FALLBACK_DOMAIN_COLOR;
}

/**
 * Returns the matching CSS variable reference (`hsl(var(--domain-...))`)
 * for use in Tailwind `style={{ color: domainCssVar(...) }}` or via
 * `className` with arbitrary-value syntax. Maps to the variables
 * declared in `src/index.css`.
 */
export function domainCssVar(
  domain: FunctionalDomain | string | null | undefined,
): string {
  if (isFunctionalDomain(domain)) {
    return `hsl(var(--domain-${domain.replace(/_/g, "-")}))`;
  }
  return "hsl(var(--domain-fallback))";
}

// ── Sorting ────────────────────────────────────────────────────────────────

/**
 * Stable sort by `seniority_score` descending. Items without a
 * `seniority_score` (null / undefined / NaN / wrong type) sort to the end
 * in their input order. Pure: returns a new array, does not mutate input.
 *
 * Tied seniority scores preserve input order — important for the "alphabetical
 * tiebreak inside a cluster" rendering rule.
 */
export function sortBySeniority<T extends { seniority_score?: number | null }>(
  items: T[],
): T[] {
  // Pair each item with its original index so we can break ties stably.
  const decorated: { item: T; idx: number; score: number | null }[] = items.map(
    (item, idx) => {
      const raw = item.seniority_score;
      const score =
        typeof raw === "number" && Number.isFinite(raw) ? raw : null;
      return { item, idx, score };
    },
  );
  decorated.sort((a, b) => {
    // Nulls always last.
    if (a.score === null && b.score === null) return a.idx - b.idx;
    if (a.score === null) return 1;
    if (b.score === null) return -1;
    if (a.score !== b.score) return b.score - a.score;
    // Equal scores → input order.
    return a.idx - b.idx;
  });
  return decorated.map((d) => d.item);
}
