/**
 * Connection strength model — canonical source for the warm-path graph.
 *
 * Pure functions and constants, no React, no IO. Mirrors the tables in
 * CLAUDE.md (`STRENGTH_TABLE`, `DECAY_RATES`) and the strength formula
 * documented there.
 *
 * Track H deliverable. Per the proposed amendment to CONTRACTS.md Contract 3,
 * this file is the canonical home for the numeric model; `graph.ts`'s future
 * `EDGE_CONFIGS` registry imports `STRENGTH_TABLE` and `DECAY_RATES` from
 * here and layers UI metadata on top (display label, CSS var, default
 * visibility). One source of truth for the math; one for the UI.
 *
 * Formula (CLAUDE.md, "Connection Graph"):
 *
 *   computed_strength = min(0.99,
 *       base
 *     * exp(-decay_rate * years_since_active)
 *     * (1 + log(corroboration_count) * 0.15)
 *     * (1 + source_type_count * 0.10)
 *   )
 */

// ── ConnectionType union ─────────────────────────────────────────────────────
//
// Keys taken verbatim from CLAUDE.md `STRENGTH_TABLE`. CLAUDE.md splits
// `academic_co_author` into `_multi` (3+ shared papers) and `_single`
// (1–2 papers); we preserve that distinction.

export type ConnectionType =
  | "patent_co_inventor"
  | "same_phd_advisor"
  | "co_board_member"
  | "academic_co_author_multi"
  | "academic_co_author_single"
  | "career_overlap_same_team"
  | "standards_committee_peer"
  | "conference_co_presenter"
  | "co_investor"
  | "career_overlap_same_domain"
  | "career_overlap_general"
  | "alumni_network"
  | "conference_co_attendee"
  // Education-cohort kinds (V3_PT2.md §"New Edge Kinds" L376-381). Sourced
  // from PDL `education[]` arrays; cohort detection requires school + dept
  // + graduation-year-window matching. Strengths reflect the typical
  // closeness of these cohorts in real life.
  | "same_mba_cohort"
  | "same_phd_program"
  | "executive_education"
  | "same_undergrad_cohort";

// ── STRENGTH_TABLE ───────────────────────────────────────────────────────────
//
// Base strength per connection type. Verbatim from CLAUDE.md.

export const STRENGTH_TABLE: Readonly<Record<ConnectionType, number>> = Object.freeze({
  patent_co_inventor: 0.95,
  same_phd_advisor: 0.92,
  co_board_member: 0.9,
  academic_co_author_multi: 0.9,
  academic_co_author_single: 0.85,
  career_overlap_same_team: 0.88,
  standards_committee_peer: 0.82,
  conference_co_presenter: 0.8,
  co_investor: 0.78,
  career_overlap_same_domain: 0.72,
  career_overlap_general: 0.6,
  alumni_network: 0.25,
  conference_co_attendee: 0.2,
  // Education-cohort kinds — V3_PT2.md L378-381 + L391-422.
  same_mba_cohort: 0.85,
  same_phd_program: 0.78,
  executive_education: 0.7,
  same_undergrad_cohort: 0.62,
});

// ── DECAY_RATES ──────────────────────────────────────────────────────────────
//
// Exponential decay per year of inactivity. Verbatim from CLAUDE.md, with two
// derived entries flagged below.

export const DECAY_RATES: Readonly<Record<ConnectionType, number>> = Object.freeze({
  patent_co_inventor: 0.01,
  same_phd_advisor: 0.01,
  co_board_member: 0.02,

  // CLAUDE.md DECAY_RATES uses a single `academic_co_author` key (0.02). We
  // apply that rate to both the multi- and single-paper variants — the decay
  // semantics don't change with shared-paper count, only the base strength does.
  academic_co_author_multi: 0.02,
  academic_co_author_single: 0.02,

  career_overlap_same_team: 0.04,
  standards_committee_peer: 0.03,
  conference_co_presenter: 0.05,
  co_investor: 0.04,

  // CLAUDE.md DECAY_RATES does not list `career_overlap_same_domain`. We
  // interpolate between `same_team` (0.04) and `general` (0.06) → 0.05.
  // Flagged for SwiftElk in [REPORT H]; trivial to update if overridden.
  career_overlap_same_domain: 0.05,

  career_overlap_general: 0.06,
  alumni_network: 0.08,
  conference_co_attendee: 0.2,
  // Education-cohort kinds — V3_PT2.md L391-422.
  same_mba_cohort: 0.02,
  same_phd_program: 0.02,
  executive_education: 0.03,
  same_undergrad_cohort: 0.04,
});

// ── computeStrength — pure function ──────────────────────────────────────────

export interface ComputeStrengthInput {
  /** Base strength for the connection type. Range [0, 1]. */
  base: number;
  /** Per-year exponential decay rate. Range [0, 1] in practice. */
  decayRate: number;
  /** Years since the connection was last active (e.g., last shared employment). */
  yearsSinceActive: number;
  /** Number of independent confirmations of this connection (e.g., # patents). Default 1. */
  corroborationCount?: number;
  /** Number of distinct source kinds (USPTO, Scholar, …) that confirm. Default 1. */
  sourceTypeCount?: number;
}

/** Hard cap on computed strength, per CLAUDE.md "min(0.99, …)". */
export const STRENGTH_CAP = 0.99;

const FREQUENCY_COEFF = 0.15;
const CORROBORATION_COEFF = 0.1;

/**
 * Compute connection strength from raw inputs.
 *
 * Formula (CLAUDE.md):
 *   min(0.99,
 *     base
 *     * exp(-decayRate * yearsSinceActive)
 *     * (1 + log(corroborationCount) * 0.15)
 *     * (1 + sourceTypeCount * 0.10)
 *   )
 *
 * Throws on out-of-range inputs (the model is meaningless on negative years
 * or counts < 1).
 */
export function computeStrength(input: ComputeStrengthInput): number {
  const {
    base,
    decayRate,
    yearsSinceActive,
    corroborationCount = 1,
    sourceTypeCount = 1,
  } = input;

  if (!Number.isFinite(base) || base < 0 || base > 1) {
    throw new RangeError(`computeStrength: base must be in [0, 1], got ${base}`);
  }
  if (!Number.isFinite(decayRate) || decayRate < 0) {
    throw new RangeError(`computeStrength: decayRate must be >= 0, got ${decayRate}`);
  }
  if (!Number.isFinite(yearsSinceActive) || yearsSinceActive < 0) {
    throw new RangeError(
      `computeStrength: yearsSinceActive must be >= 0, got ${yearsSinceActive}`,
    );
  }
  if (!Number.isInteger(corroborationCount) || corroborationCount < 1) {
    throw new RangeError(
      `computeStrength: corroborationCount must be an integer >= 1, got ${corroborationCount}`,
    );
  }
  if (!Number.isInteger(sourceTypeCount) || sourceTypeCount < 1) {
    throw new RangeError(
      `computeStrength: sourceTypeCount must be an integer >= 1, got ${sourceTypeCount}`,
    );
  }

  const recency = Math.exp(-decayRate * yearsSinceActive);
  const frequency = 1 + Math.log(corroborationCount) * FREQUENCY_COEFF;
  const corroboration = 1 + sourceTypeCount * CORROBORATION_COEFF;

  return Math.min(STRENGTH_CAP, base * recency * frequency * corroboration);
}

/**
 * Convenience wrapper: compute strength for a known `ConnectionType` using
 * the canonical `STRENGTH_TABLE` and `DECAY_RATES` lookups.
 */
export function computeStrengthForType(
  type: ConnectionType,
  yearsSinceActive: number,
  corroborationCount = 1,
  sourceTypeCount = 1,
): number {
  return computeStrength({
    base: STRENGTH_TABLE[type],
    decayRate: DECAY_RATES[type],
    yearsSinceActive,
    corroborationCount,
    sourceTypeCount,
  });
}

/**
 * List of all supported connection types, in the order they appear in
 * `STRENGTH_TABLE`. Stable order is useful for tests, UI listings, and
 * deterministic sort tiebreakers.
 */
export const ALL_CONNECTION_TYPES: readonly ConnectionType[] = Object.freeze(
  Object.keys(STRENGTH_TABLE) as ConnectionType[],
);
