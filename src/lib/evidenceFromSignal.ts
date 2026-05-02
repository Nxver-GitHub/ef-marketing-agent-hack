/**
 * Signal → EdgeEvidence bridge.
 *
 * Maps a Contract-1-shaped signal (snake_case `structured_value` from
 * `POST /signals/discover-connections`) into the frontend `EdgeEvidence`
 * discriminated union (camelCase, defined in `graph.ts`).
 *
 * Pure function, no React, no IO. Lives outside `buildGraph` so it can be
 * unit-tested in isolation and reused by any future code path that needs to
 * convert a Supabase signal row into an in-memory edge evidence record.
 *
 * References:
 * - CONTRACTS.md → Contract 1 §"structured_value shapes (by signal_type)"
 * - CLAUDE.md L711-767 (the templates that consume EdgeEvidence)
 * - src/lib/graph.ts (EdgeEvidence + per-kind interfaces)
 *
 * Behavior summary:
 * - Returns the appropriate EdgeEvidence variant when `signal_type` is
 *   recognized AND the required fields can be extracted.
 * - Returns `null` for unknown signal types or when the structured_value is
 *   too sparse to produce a meaningful evidence record. Callers should treat
 *   `null` as "render generic fallback strings" — never fabricate values.
 * - Per CLAUDE.md "Common Mistakes" #6: missing fields propagate as empty
 *   strings or null inside the returned record. The downstream
 *   `generateExplanation` (warmPaths.ts) already handles those gracefully
 *   with documented placeholder strings ("a patent", "year unknown").
 */

import type {
  AcademicCoAuthorEvidence,
  CareerOverlapEvidence,
  ConferenceCoPresenterEvidence,
  EdgeEvidence,
  EducationCohortEvidence,
  PatentCoInventorEvidence,
  StandardsCommitteeEvidence,
} from "./graph";

/**
 * Minimal duck-typed input shape. The frontend's existing `Signal` type from
 * `mockStore.ts` uses `value: unknown` for v2 compatibility — Contract 4's
 * `NormalizedSignal` carries `structured_value`. This interface accepts
 * either by keeping field names aligned with Contract 1 specifically.
 */
export interface SignalLike {
  readonly signal_type: string;
  readonly structured_value: Record<string, unknown>;
}

// ── Field-extraction helpers ─────────────────────────────────────────────────
//
// All helpers tolerate missing or wrong-typed values: they return a sensible
// default (empty string / null / 0) rather than throwing. The downstream
// rendering pipeline treats those as "evidence not yet populated" and falls
// back to documented placeholder strings.

function asString(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function asNullableString(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function asNumber(v: unknown): number {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const parsed = Number(v);
    if (Number.isFinite(parsed)) return parsed;
  }
  return Number.NaN;
}

function asInteger(v: unknown): number {
  const n = asNumber(v);
  return Number.isFinite(n) ? Math.trunc(n) : Number.NaN;
}

function asNullableInteger(v: unknown): number | null {
  const n = asInteger(v);
  return Number.isFinite(n) ? n : null;
}

// ── Per-signal-type adapters ─────────────────────────────────────────────────

function patentEvidence(
  sv: Record<string, unknown>,
): PatentCoInventorEvidence {
  return {
    kind: "patent_co_inventor",
    patentNumber: asString(sv.patent_number),
    patentTitle: asString(sv.patent_title),
    filingDate: asString(sv.filing_date),
    grantDate: asNullableString(sv.grant_date),
    assignee: asString(sv.assignee),
    usptoUrl: asNullableString(sv.uspto_url),
  };
}

function paperEvidence(
  sv: Record<string, unknown>,
): AcademicCoAuthorEvidence {
  return {
    kind: "academic_co_author",
    paperTitle: asString(sv.paper_title),
    venue: asString(sv.venue),
    year: asInteger(sv.year),
    citationCount: asInteger(sv.citation_count),
    semanticScholarId: asNullableString(sv.semantic_scholar_id),
    doi: asNullableString(sv.doi),
  };
}

function conferenceEvidence(
  sv: Record<string, unknown>,
): ConferenceCoPresenterEvidence {
  return {
    kind: "conference_co_presenter",
    event: asString(sv.event),
    year: asInteger(sv.year),
  };
}

function standardsEvidence(
  sv: Record<string, unknown>,
): StandardsCommitteeEvidence {
  return {
    kind: "standards_committee",
    committee: asString(sv.committee),
    years: asString(sv.years),
  };
}

function educationCohortEvidence(
  sv: Record<string, unknown>,
): EducationCohortEvidence {
  return {
    kind: "education_cohort",
    institution: asString(sv.institution),
    program: asNullableString(sv.program),
    overlapStartYear: asNullableInteger(sv.overlap_start_year),
    overlapEndYear: asNullableInteger(sv.overlap_end_year),
  };
}

function careerOverlapEvidence(
  sv: Record<string, unknown>,
): CareerOverlapEvidence {
  return {
    kind: "career_overlap",
    companyName: asString(sv.company_name),
    overlapStartYear: asInteger(sv.overlap_start_year),
    overlapEndYear: asInteger(sv.overlap_end_year),
    overlapYears: asInteger(sv.overlap_years),
    teamA: asNullableString(sv.team_a),
    teamB: asNullableString(sv.team_b),
    domainA: asNullableString(sv.domain_a),
    domainB: asNullableString(sv.domain_b),
    seniorityGap: asNullableInteger(sv.seniority_gap),
  };
}

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Connection-record signal types that have an EdgeEvidence variant.
 * Per CONTRACTS.md Contract 1 — the union of recognized `signal_type`
 * values that this bridge can map.
 */
export const RECOGNIZED_CONNECTION_SIGNAL_TYPES: ReadonlyArray<string> = Object.freeze([
  "patent_co_inventor",
  "academic_co_author",
  "academic_co_author_multi",   // v3 schema variant (3+ shared papers)
  "academic_co_author_single",  // v3 schema variant (1-2 shared papers)
  "conference_co_presenter",
  "standards_committee",
  "standards_committee_peer", // backend signal_type variant
  "career_overlap_same_team",
  "career_overlap_same_domain",
  "career_overlap_general",
  // SwiftElk's `bulk_career_history_signals.py` (Wave 5 Job A) emits
  // ~2,000+ rows of signal_type='past_employer' with structured_value
  // {connected_to, company_name, company_canonical, role_a, role_b}.
  // Without this entry the fifth pass silently drops them, and warm
  // paths fall back to generic text. Same evidence shape as
  // career_overlap (no overlap years for the bare past_employer path).
  "past_employer",
  // Education-cohort kinds (V3_PT2.md L376-381) — emitted by
  // server/credence/extractors/education.py, 1:1 with EdgeKind.
  "same_mba_cohort",
  "same_phd_program",
  "executive_education",
  "same_undergrad_cohort",
]);

/**
 * Map a Contract-1-shaped signal record into an EdgeEvidence value.
 *
 * Returns `null` when:
 *   - `signal_type` is not in the recognized set above
 *   - `structured_value` is missing entirely
 *   - The signal payload is malformed (non-object structured_value, etc.)
 *
 * Callers should treat `null` as "no rich evidence available" — the
 * rendering layer (warmPaths.ts) falls back to generic strings.
 *
 * Sparse evidence (recognized signal_type but with empty / missing
 * sub-fields) returns a populated EdgeEvidence with empty strings / NaN /
 * null in the missing slots. The downstream renderers already handle
 * those gracefully with documented placeholder strings.
 */
export function evidenceFromSignal(sig: SignalLike): EdgeEvidence | null {
  if (!sig || typeof sig !== "object") return null;
  if (typeof sig.signal_type !== "string" || sig.signal_type.length === 0) {
    return null;
  }
  const sv = sig.structured_value;
  if (!sv || typeof sv !== "object" || Array.isArray(sv)) return null;

  switch (sig.signal_type) {
    case "patent_co_inventor":
      return patentEvidence(sv);

    case "academic_co_author":
    case "academic_co_author_multi":
    case "academic_co_author_single":
      return paperEvidence(sv);

    case "conference_co_presenter":
      return conferenceEvidence(sv);

    case "standards_committee":
    case "standards_committee_peer":
      return standardsEvidence(sv);

    case "career_overlap_same_team":
    case "career_overlap_same_domain":
    case "career_overlap_general":
    case "past_employer":
      // past_employer shares the career-overlap evidence shape: company_name
      // is the load-bearing field, year fields default to NaN/null when
      // absent (the bare past_employer path doesn't carry overlap years).
      return careerOverlapEvidence(sv);

    // Education-cohort kinds (V3_PT2.md L376-381) — emitted by
    // server/credence/extractors/education.py.
    case "same_mba_cohort":
    case "same_phd_program":
    case "executive_education":
    case "same_undergrad_cohort":
      return educationCohortEvidence(sv);

    default:
      return null;
  }
}
