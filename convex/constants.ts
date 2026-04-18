/**
 * Tunable scoring constants. Edit here without touching scoring logic.
 * Per-signal weights live in the `signal_weights` table (runtime-tunable
 * from /settings).
 */
export const OVERALL_WEIGHTS = {
  authenticity: 0.4,
  authority: 0.4,
  warmth: 0.2,
};

export const DEFAULT_SIGNAL_WEIGHTS: Record<
  string,
  { authenticity: number; authority: number; warmth: number }
> = {
  tenure_years: { authenticity: 0.8, authority: 0.6, warmth: 0.0 },
  post_activity: { authenticity: 0.5, authority: 0.2, warmth: 0.3 },
  recommendations: { authenticity: 0.7, authority: 0.3, warmth: 0.2 },
  patent_count: { authenticity: 0.6, authority: 0.9, warmth: 0.0 },
  patent_citations: { authenticity: 0.4, authority: 0.8, warmth: 0.0 },
  github_commits: { authenticity: 0.5, authority: 0.6, warmth: 0.1 },
  conference_talks: { authenticity: 0.6, authority: 0.8, warmth: 0.2 },
  hiring_signal: { authenticity: 0.2, authority: 0.7, warmth: 0.1 },
  mutual_connections: { authenticity: 0.1, authority: 0.1, warmth: 0.9 },
  crunchbase_role: { authenticity: 0.6, authority: 0.7, warmth: 0.0 },
};

export const ALL_SOURCES = [
  "linkedin_profile",
  "linkedin_posts",
  "uspto",
  "github",
  "conference",
  "company_hiring",
  "crunchbase",
  "mutual_connections",
] as const;
export type Source = (typeof ALL_SOURCES)[number];
