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
