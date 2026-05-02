/**
 * warmPathsForCompany — pure aggregator for the People / Companies pages.
 *
 * Takes a flat `WarmPath[]` (output of `findWarmPaths` from `warmPaths.ts`)
 * and rolls them up by terminal-company. Used by the People page row badge
 * to show "N warm paths from your team to this company" and by the
 * Companies page to surface companies with the most warm-path coverage.
 *
 * Pure: no I/O, no React, no async. Caller owns the
 * `personIdToCompanyId` map (resolved via the persons + employment_periods
 * tables upstream).
 */
import type { WarmPath } from "./warmPaths"

export interface CompanyWarmPathStats {
  company_id: string
  /** Total number of paths whose terminal node lands at this company. */
  total_paths: number
  /** Number of distinct terminal persons at this company. */
  unique_targets: number
  /** Highest path strength across all paths to this company. */
  best_strength: number
  /** Median hop count across all paths (rounded with even-length avg). */
  median_hop_count: number
  /** Per-target breakdown (target_person_id → stats). */
  per_target: Map<
    string,
    { paths: number; bestStrength: number; bestHops: number }
  >
}

/**
 * Compute the median of a numeric array. Pure helper — exported for
 * test reuse. Even-length arrays return the average of the two middles
 * rounded to the nearest integer (hop counts are always whole numbers).
 */
export function medianHopCount(values: number[]): number {
  if (values.length === 0) return 0
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  if (sorted.length % 2 === 1) {
    return sorted[mid]
  }
  return Math.round((sorted[mid - 1] + sorted[mid]) / 2)
}

/**
 * Aggregate warm paths by terminal company.
 *
 * The terminal node of a `WarmPath` is `path.nodes[path.nodes.length - 1]`
 * — that's the prospect being reached. We look up its company via the
 * `personIdToCompanyId` map; paths whose terminal person isn't in the map
 * are silently dropped (caller's responsibility to provide a complete
 * map for the persons they care about).
 *
 * Returns an empty Map for an empty `paths` input.
 */
export function aggregateWarmPathsByCompany(
  paths: WarmPath[],
  personIdToCompanyId: Map<string, string>,
): Map<string, CompanyWarmPathStats> {
  const out = new Map<string, CompanyWarmPathStats>()
  // Per-company hop-count arrays so we can compute median once at the end.
  const hopsBuckets = new Map<string, number[]>()

  for (const path of paths) {
    if (!path.nodes || path.nodes.length === 0) continue
    const terminal = path.nodes[path.nodes.length - 1]
    if (!terminal || typeof terminal.id !== "string") continue
    const companyId = personIdToCompanyId.get(terminal.id)
    if (!companyId) continue

    let stats = out.get(companyId)
    if (!stats) {
      stats = {
        company_id: companyId,
        total_paths: 0,
        unique_targets: 0,
        best_strength: 0,
        median_hop_count: 0,
        per_target: new Map(),
      }
      out.set(companyId, stats)
      hopsBuckets.set(companyId, [])
    }

    stats.total_paths += 1
    if (path.strength > stats.best_strength) {
      stats.best_strength = path.strength
    }
    hopsBuckets.get(companyId)!.push(path.hopCount)

    let target = stats.per_target.get(terminal.id)
    if (!target) {
      target = { paths: 0, bestStrength: 0, bestHops: path.hopCount }
      stats.per_target.set(terminal.id, target)
    }
    target.paths += 1
    if (path.strength > target.bestStrength) {
      target.bestStrength = path.strength
      // bestHops tracks hops of the strongest path (tie-break by lower hops).
      target.bestHops = path.hopCount
    } else if (
      path.strength === target.bestStrength &&
      path.hopCount < target.bestHops
    ) {
      target.bestHops = path.hopCount
    }
  }

  // Final pass: median + unique_targets count.
  for (const [companyId, stats] of out) {
    stats.unique_targets = stats.per_target.size
    stats.median_hop_count = medianHopCount(hopsBuckets.get(companyId) ?? [])
  }
  return out
}
