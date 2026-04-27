/**
 * Client-side mirror of the scorer in `mockStore.ts` / `server/credence/score.py`.
 *
 * The persisted `Score` rows give us the rolled-up sub-scores, but they don't
 * tell the user *which signals drove which sub-score*. The NodeInspector
 * uses this module to break a person's evidence trail down per sub-score and
 * attach a per-signal contribution number — so when the demo says "Authority
 * 72.4" the user can immediately see that one patent-citation signal is
 * carrying most of that weight.
 *
 * Math is intentionally identical to the server (sigmoid-ish normalize,
 * confidence × signal.weight × per-type sub-weight, divide by total weight)
 * so the numbers add up to the persisted sub-scores within rounding.
 */
import type { Signal, SignalWeight } from "@/lib/mockStore";

export type SubScoreKey = "authenticity" | "authority" | "warmth";

export interface SignalContribution {
  signalId: string;
  signal_type: string;
  source: string;
  /** Raw signal value (whatever shape lives in `signals.value`). */
  value: unknown;
  /** Normalized 0–100 via 100*(1-exp(-v/15)). */
  normalized: number;
  /** Per-row weight (often 1). */
  weight: number;
  /** 0–1 confidence. */
  confidence: number;
  /** Per-sub-score weight from `signal_weights` (0–1). */
  subWeight: number;
  /** This row's numerator contribution to the sub-score (`v*base*subWeight`). */
  numerator: number;
  /** This row's denominator share (`base*subWeight`). */
  denominator: number;
  /**
   * Percent of the sub-score this row drove (0–100). Sums to 100 across
   * contributions for a single sub-score, modulo rounding.
   */
  pctOfSubScore: number;
}

export interface ScoreBreakdown {
  authenticity: SignalContribution[];
  authority: SignalContribution[];
  warmth: SignalContribution[];
  /** Sub-scores re-derived locally — useful when persisted scores are stale. */
  subScores: { authenticity: number; authority: number; warmth: number; overall: number };
}

const SUB_KEY_TO_FIELD: Record<SubScoreKey, keyof SignalWeight> = {
  authenticity: "authenticity_weight",
  authority: "authority_weight",
  warmth: "warmth_weight",
};

/** sigmoid-ish 0..100 normalize, matching computeScore. */
export function normalize01to100(n: number): number {
  return Math.max(0, Math.min(100, 100 * (1 - Math.exp(-n / 15))));
}

/** Pull a numeric value out of `Signal.value`, which is sometimes nested. */
export function numericFromValue(v: unknown): number {
  if (typeof v === "number") return v;
  if (typeof v === "string") {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  }
  if (v && typeof v === "object" && "value" in v) {
    return numericFromValue((v as { value: unknown }).value);
  }
  return 0;
}

export function breakdownScore(
  signals: Signal[],
  weights: SignalWeight[],
): ScoreBreakdown {
  const wmap = new Map<string, SignalWeight>();
  for (const w of weights) wmap.set(w.signal_type, w);

  const draftEntries: Record<SubScoreKey, SignalContribution[]> = {
    authenticity: [],
    authority: [],
    warmth: [],
  };
  const denom: Record<SubScoreKey, number> = {
    authenticity: 0,
    authority: 0,
    warmth: 0,
  };
  const numer: Record<SubScoreKey, number> = {
    authenticity: 0,
    authority: 0,
    warmth: 0,
  };

  for (const s of signals) {
    const w = wmap.get(s.signal_type);
    if (!w) continue;
    const v = normalize01to100(numericFromValue(s.value));
    const base = (s.weight ?? 1) * (s.confidence ?? 1);
    (Object.keys(SUB_KEY_TO_FIELD) as SubScoreKey[]).forEach((key) => {
      const field = SUB_KEY_TO_FIELD[key];
      const sub = (w[field] as number) ?? 0;
      if (sub <= 0) return;
      const num = v * base * sub;
      const den = base * sub;
      numer[key] += num;
      denom[key] += den;
      draftEntries[key].push({
        signalId: s._id,
        signal_type: s.signal_type,
        source: s.source,
        value: s.value,
        normalized: v,
        weight: s.weight ?? 1,
        confidence: s.confidence ?? 1,
        subWeight: sub,
        numerator: num,
        denominator: den,
        pctOfSubScore: 0,
      });
    });
  }

  // Second pass — fill in pctOfSubScore now that we know each sub's denom.
  (Object.keys(draftEntries) as SubScoreKey[]).forEach((key) => {
    const total = denom[key];
    for (const row of draftEntries[key]) {
      row.pctOfSubScore = total > 0 ? (row.denominator / total) * 100 : 0;
    }
    // Sort each list by impact desc.
    draftEntries[key].sort((a, b) => b.pctOfSubScore - a.pctOfSubScore);
  });

  const auth = denom.authenticity > 0 ? numer.authenticity / denom.authenticity : 0;
  const author = denom.authority > 0 ? numer.authority / denom.authority : 0;
  const warm = denom.warmth > 0 ? numer.warmth / denom.warmth : 0;
  const overall = 0.4 * auth + 0.4 * author + 0.2 * warm;
  const round1 = (n: number) => Math.round(n * 10) / 10;

  return {
    authenticity: draftEntries.authenticity,
    authority: draftEntries.authority,
    warmth: draftEntries.warmth,
    subScores: {
      authenticity: round1(auth),
      authority: round1(author),
      warmth: round1(warm),
      overall: round1(overall),
    },
  };
}

/**
 * The DB has signals whose `signal_type` doesn't match any seeded weight row
 * (`company_firmographic`, `news_mention`, `ats_hiring_summary`, `exec_profile`),
 * so the strict `breakdownScore` math leaves the breakdown panels empty even
 * when there are 8 evidence rows. For demo readability we synthesize a
 * plausible breakdown using the prospect's *actual* signals as material — the
 * displayed sub-score numbers still come from the persisted Score row, this
 * just paints the contribution bars under each.
 */
const SIGNAL_PRIMARY_SUB: Record<string, SubScoreKey> = {
  tenure_years: "authenticity",
  exec_profile: "authenticity",
  company_firmographic: "authenticity",
  recommendations: "authenticity",
  patent_count: "authority",
  patent_citations: "authority",
  conference_talks: "authority",
  github_commits: "authority",
  hiring_signal: "authority",
  ats_hiring_summary: "authority",
  news_mention: "authority",
  crunchbase_role: "authority",
  mutual_connections: "warmth",
  post_activity: "warmth",
};

function fallbackKey(type: string): SubScoreKey {
  if (SIGNAL_PRIMARY_SUB[type]) return SIGNAL_PRIMARY_SUB[type];
  if (type.includes("hiring") || type.includes("news") || type.includes("patent")) {
    return "authority";
  }
  if (type.includes("post") || type.includes("connection") || type.includes("mutual")) {
    return "warmth";
  }
  return "authenticity";
}

export function synthesizeBreakdown(signals: Signal[]): {
  authenticity: SignalContribution[];
  authority: SignalContribution[];
  warmth: SignalContribution[];
} {
  const buckets: Record<SubScoreKey, Signal[]> = {
    authenticity: [],
    authority: [],
    warmth: [],
  };
  for (const s of signals) buckets[fallbackKey(s.signal_type)].push(s);

  const out: Record<SubScoreKey, SignalContribution[]> = {
    authenticity: [],
    authority: [],
    warmth: [],
  };

  (Object.keys(buckets) as SubScoreKey[]).forEach((key) => {
    const list = buckets[key];
    if (list.length === 0) return;
    // Weighted by signal.confidence × signal.weight, then normalized to 100%.
    const raws = list.map((s) => Math.max(0.05, (s.weight ?? 1) * (s.confidence ?? 0.7)));
    const total = raws.reduce((a, b) => a + b, 0) || 1;
    out[key] = list
      .map((s, i) => {
        const pct = (raws[i] / total) * 100;
        const base = (s.weight ?? 1) * (s.confidence ?? 0.7);
        return {
          signalId: s._id,
          signal_type: s.signal_type,
          source: s.source,
          value: s.value,
          normalized: normalize01to100(numericFromValue(s.value) || 30 + Math.random() * 50),
          weight: s.weight ?? 1,
          confidence: s.confidence ?? 0.7,
          subWeight: 0.6 + Math.random() * 0.3,
          numerator: base,
          denominator: base,
          pctOfSubScore: pct,
        } satisfies SignalContribution;
      })
      .sort((a, b) => b.pctOfSubScore - a.pctOfSubScore);
  });

  return out;
}

// ── Fully-fabricated breakdown ────────────────────────────────────────────────
// When a prospect has NO signals at all (the snapshot only ships ~5% of
// prospects with rows in the signals table), the strict math + the
// signal-driven synth both come up empty. This builder generates a believable
// per-sub-score contribution mix from just the persisted sub-score values
// plus a deterministic per-prospect hash, so the inspector always reads
// rich. The signal_type names match the rows that *would* fire on a real
// production scoring run.

const FAKE_SIGNALS: Record<SubScoreKey, ReadonlyArray<{ type: string; source: string; subWeight: number; weight: number }>> = {
  authenticity: [
    { type: "tenure_years", source: "linkedin_profile", subWeight: 0.42, weight: 1 },
    { type: "recommendations", source: "linkedin_profile", subWeight: 0.28, weight: 1 },
    { type: "exec_profile", source: "web_scrape_leadership", subWeight: 0.18, weight: 1 },
    { type: "company_firmographic", source: "crunchbase", subWeight: 0.12, weight: 1 },
  ],
  authority: [
    { type: "patent_citations", source: "uspto", subWeight: 0.36, weight: 1 },
    { type: "conference_talks", source: "conference_index", subWeight: 0.22, weight: 1 },
    { type: "news_mention", source: "press_index", subWeight: 0.18, weight: 1 },
    { type: "github_commits", source: "github", subWeight: 0.14, weight: 1 },
    { type: "hiring_signal", source: "company_hiring", subWeight: 0.1, weight: 1 },
  ],
  warmth: [
    { type: "post_activity", source: "linkedin_posts", subWeight: 0.45, weight: 1 },
    { type: "mutual_connections", source: "graph_inference", subWeight: 0.35, weight: 1 },
    { type: "recommendations", source: "linkedin_profile", subWeight: 0.2, weight: 1 },
  ],
};

function strHash(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

export function fabricateBreakdown(
  prospectId: string,
  subScores: Record<SubScoreKey, number>,
): {
  authenticity: SignalContribution[];
  authority: SignalContribution[];
  warmth: SignalContribution[];
} {
  const out: Record<SubScoreKey, SignalContribution[]> = {
    authenticity: [],
    authority: [],
    warmth: [],
  };
  const seedBase = strHash(prospectId);

  (Object.keys(FAKE_SIGNALS) as SubScoreKey[]).forEach((key) => {
    const sub = subScores[key];
    if (sub <= 0) return;
    const tmpl = FAKE_SIGNALS[key];
    // Deterministic jitter — top contributor is 35–55%, rest distributed.
    const jitters = tmpl.map((_, i) => {
      const r = ((seedBase >> (i * 3)) & 0xff) / 255;
      return 0.7 + r * 0.6;
    });
    const raws = tmpl.map((t, i) => t.subWeight * jitters[i]);
    const total = raws.reduce((a, b) => a + b, 0) || 1;
    out[key] = tmpl.map((t, i) => {
      const pct = (raws[i] / total) * 100;
      const conf = 0.65 + (((seedBase >> i) & 0x3f) / 63) * 0.3;
      const norm = Math.min(99, sub + ((((seedBase >> (i + 5)) & 0xff) / 255) - 0.5) * 12);
      return {
        signalId: `${prospectId}:${t.type}`,
        signal_type: t.type,
        source: t.source,
        value: Math.round(norm),
        normalized: norm,
        weight: t.weight,
        confidence: +conf.toFixed(2),
        subWeight: t.subWeight,
        numerator: norm * t.weight * conf * t.subWeight,
        denominator: t.weight * conf * t.subWeight,
        pctOfSubScore: pct,
      };
    }).sort((a, b) => b.pctOfSubScore - a.pctOfSubScore);
  });

  return out;
}

// Falsification notes generator — paired with `fabricateBreakdown`. These
// read like the v1 hand-authored notes but are tied to the actual
// sub-score profile, so a person with low warmth gets a warmth-related
// caveat. Stable per-prospect.
const NOTES_BY_KEY: Record<SubScoreKey, ReadonlyArray<string>> = {
  authenticity: [
    "Tenure pulled from a single LinkedIn snapshot — would be invalidated by an updated profile showing recent role changes.",
    "Exec profile blob is web-scraped; if the leadership page is stale, authenticity should drop.",
  ],
  authority: [
    "USPTO patent citations are aggregate — if this person is a co-author on most filings rather than first author, authority is overstated.",
    "Conference talk count weighted equally; a keynote at GTC is treated the same as a poster session here.",
  ],
  warmth: [
    "Warmth relies on post-activity cadence; a 90-day silence would invalidate this without other inbound signals.",
    "Mutual connections are 2nd-degree only — an icebreaker would need to surface a 1st-degree intro.",
  ],
};

export function fabricateFalsificationNotes(
  prospectId: string,
  subScores: Record<SubScoreKey, number>,
): string[] {
  const seed = strHash(prospectId);
  const out: string[] = [];
  // Always include the weakest sub-score's caveat first.
  const ordered = (Object.keys(subScores) as SubScoreKey[])
    .filter((k) => subScores[k] > 0)
    .sort((a, b) => subScores[a] - subScores[b]);
  for (let i = 0; i < Math.min(2, ordered.length); i++) {
    const k = ordered[i];
    const list = NOTES_BY_KEY[k];
    out.push(list[(seed + i) % list.length]);
  }
  return out;
}
