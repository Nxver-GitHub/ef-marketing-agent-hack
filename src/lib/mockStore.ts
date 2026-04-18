/**
 * In-memory mock data layer that mirrors the Convex API surface used in
 * the UI. This keeps the Lovable preview functional even when no Convex
 * deployment is connected. Replace by wiring ConvexProvider once
 * VITE_CONVEX_URL is set.
 */
import { useEffect, useState, useSyncExternalStore } from "react";

type Prospect = {
  _id: string;
  name: string;
  company: string;
  role: string;
  industry: string;
  linkedin_url?: string;
  created_at: number;
  updated_at: number;
};
type Signal = {
  _id: string;
  prospect_id: string;
  source: string;
  signal_type: string;
  value: any;
  raw_data: any;
  weight: number;
  confidence: number;
  collected_at: number;
};
type Score = {
  _id: string;
  prospect_id: string;
  authenticity_score: number;
  authority_score: number;
  warmth_score: number;
  overall_score: number;
  falsification_notes: string[];
  computed_at: number;
};
type SignalWeight = {
  _id: string;
  signal_type: string;
  authenticity_weight: number;
  authority_weight: number;
  warmth_weight: number;
};
type ScoringRun = {
  _id: string;
  prospect_id: string;
  status: "pending" | "running" | "complete" | "error";
  sources_attempted: string[];
  sources_succeeded: string[];
  current_source?: string;
  error_log?: string;
  started_at: number;
  completed_at?: number;
};

const DEFAULT_WEIGHTS: Record<string, [number, number, number]> = {
  tenure_years: [0.8, 0.6, 0.0],
  post_activity: [0.5, 0.2, 0.3],
  recommendations: [0.7, 0.3, 0.2],
  patent_count: [0.6, 0.9, 0.0],
  patent_citations: [0.4, 0.8, 0.0],
  github_commits: [0.5, 0.6, 0.1],
  conference_talks: [0.6, 0.8, 0.2],
  hiring_signal: [0.2, 0.7, 0.1],
  mutual_connections: [0.1, 0.1, 0.9],
  crunchbase_role: [0.6, 0.7, 0.0],
};

const ALL_SOURCES = [
  "linkedin_profile",
  "linkedin_posts",
  "uspto",
  "github",
  "conference",
  "company_hiring",
  "crunchbase",
  "mutual_connections",
] as const;

const SOURCE_TO_SIGNALS: Record<string, string[]> = {
  linkedin_profile: ["tenure_years", "recommendations"],
  linkedin_posts: ["post_activity"],
  uspto: ["patent_count", "patent_citations"],
  github: ["github_commits"],
  conference: ["conference_talks"],
  company_hiring: ["hiring_signal"],
  crunchbase: ["crunchbase_role"],
  mutual_connections: ["mutual_connections"],
};

const rid = () => Math.random().toString(36).slice(2, 11);
const rand = (min: number, max: number) => Math.floor(Math.random() * (max - min + 1)) + min;

class Store {
  prospects: Prospect[] = [];
  signals: Signal[] = [];
  scores: Score[] = [];
  signal_weights: SignalWeight[] = [];
  scoring_runs: ScoringRun[] = [];
  private listeners = new Set<() => void>();

  constructor() {
    this.seedWeights();
    this.seedDemo();
  }
  subscribe = (cb: () => void) => {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  };
  emit = () => this.listeners.forEach((l) => l());

  seedWeights() {
    if (this.signal_weights.length) return;
    for (const [signal_type, [a, au, w]] of Object.entries(DEFAULT_WEIGHTS)) {
      this.signal_weights.push({
        _id: rid(),
        signal_type,
        authenticity_weight: a,
        authority_weight: au,
        warmth_weight: w,
      });
    }
  }

  seedDemo() {
    if (this.prospects.length) return;
    const seed = [
      { name: "Lin Wei", company: "TSMC", role: "VP Process Engineering" },
      { name: "Ana Souza", company: "ASML", role: "Director Lithography" },
      { name: "Marcus Hale", company: "Intel", role: "Principal Engineer" },
      { name: "Priya Raman", company: "NVIDIA", role: "Director of HW" },
      { name: "Jonas Berg", company: "Infineon", role: "Head of Power" },
    ];
    for (const p of seed) {
      const id = this.createProspect({ ...p, industry: "Semiconductors" });
      this.runScoringSync(id);
    }
  }

  createProspect(p: Omit<Prospect, "_id" | "created_at" | "updated_at">): string {
    const now = Date.now();
    const _id = rid();
    this.prospects.push({ _id, ...p, created_at: now, updated_at: now });
    this.emit();
    return _id;
  }

  /** Synchronous variant for seeding. */
  runScoringSync(prospect_id: string) {
    for (const source of ALL_SOURCES) {
      for (const signal_type of SOURCE_TO_SIGNALS[source]) {
        const value = rand(0, 30);
        this.signals.push({
          _id: rid(),
          prospect_id,
          source,
          signal_type,
          value,
          raw_data: { _mock: true, value, source },
          weight: 1,
          confidence: +(0.6 + Math.random() * 0.35).toFixed(2),
          collected_at: Date.now(),
        });
      }
    }
    this.computeScore(prospect_id);
  }

  /** Async variant with simulated source-by-source progress for /validate UX. */
  async runScoring(prospect_id: string) {
    const _id = rid();
    const run: ScoringRun = {
      _id,
      prospect_id,
      status: "running",
      sources_attempted: [...ALL_SOURCES],
      sources_succeeded: [],
      started_at: Date.now(),
    };
    this.scoring_runs.push(run);
    this.emit();

    for (const source of ALL_SOURCES) {
      run.current_source = source;
      this.emit();
      await new Promise((r) => setTimeout(r, 350 + Math.random() * 350));
      for (const signal_type of SOURCE_TO_SIGNALS[source]) {
        const value = rand(0, 30);
        this.signals.push({
          _id: rid(),
          prospect_id,
          source,
          signal_type,
          value,
          raw_data: { _mock: true, value, source },
          weight: 1,
          confidence: +(0.6 + Math.random() * 0.35).toFixed(2),
          collected_at: Date.now(),
        });
      }
      run.sources_succeeded.push(source);
      this.emit();
    }
    run.current_source = undefined;
    run.status = "complete";
    run.completed_at = Date.now();
    this.computeScore(prospect_id);
    this.emit();
    return _id;
  }

  computeScore(prospect_id: string) {
    const sigs = this.signals.filter((s) => s.prospect_id === prospect_id);
    const wmap = new Map(
      this.signal_weights.map((w) => [
        w.signal_type,
        { a: w.authenticity_weight, au: w.authority_weight, w: w.warmth_weight },
      ])
    );
    const norm = (n: number) => Math.max(0, Math.min(100, 100 * (1 - Math.exp(-n / 15))));
    let aN = 0, aD = 0, auN = 0, auD = 0, wN = 0, wD = 0;
    for (const s of sigs) {
      const w = wmap.get(s.signal_type);
      if (!w) continue;
      const v = norm(Number(s.value) || 0);
      const base = (s.weight ?? 1) * (s.confidence ?? 1);
      aN += v * base * w.a; aD += base * w.a;
      auN += v * base * w.au; auD += base * w.au;
      wN += v * base * w.w; wD += base * w.w;
    }
    const a = aD ? aN / aD : 0;
    const au = auD ? auN / auD : 0;
    const wm = wD ? wN / wD : 0;
    const overall = 0.4 * a + 0.4 * au + 0.2 * wm;
    const round = (n: number) => Math.round(n * 10) / 10;
    const notes = [
      "Authenticity assumes LinkedIn tenure is accurate — re-verify if profile was edited in the last 60 days.",
      "Authority cross-checks USPTO patents — invalid if patent attribution is wrong.",
      "Warmth depends on a fresh mutual-connections graph — re-sync if data is >7 days old.",
      "Role not cross-checked against Crunchbase — re-verify if prospect changed jobs in the last 30 days.",
    ];
    this.scores.push({
      _id: rid(),
      prospect_id,
      authenticity_score: round(a),
      authority_score: round(au),
      warmth_score: round(wm),
      overall_score: round(overall),
      falsification_notes: notes,
      computed_at: Date.now(),
    });
    this.emit();
  }

  upsertWeight(signal_type: string, a: number, au: number, w: number) {
    const existing = this.signal_weights.find((x) => x.signal_type === signal_type);
    if (existing) {
      existing.authenticity_weight = a;
      existing.authority_weight = au;
      existing.warmth_weight = w;
    } else {
      this.signal_weights.push({
        _id: rid(),
        signal_type,
        authenticity_weight: a,
        authority_weight: au,
        warmth_weight: w,
      });
    }
    this.emit();
  }
}

export const store = new Store();

function useStore<T>(selector: (s: Store) => T): T {
  return useSyncExternalStore(
    store.subscribe,
    () => selector(store),
    () => selector(store)
  );
}

export const useProspects = () => useStore((s) => [...s.prospects]);
export const useProspect = (id?: string) =>
  useStore((s) => (id ? s.prospects.find((p) => p._id === id) ?? null : null));
export const useSignalsFor = (id?: string) =>
  useStore((s) => (id ? s.signals.filter((x) => x.prospect_id === id) : []));
export const useLatestScore = (id?: string) =>
  useStore((s) => {
    if (!id) return null;
    const list = s.scores.filter((x) => x.prospect_id === id);
    return list.length ? list[list.length - 1] : null;
  });
export const useLatestRun = (id?: string) =>
  useStore((s) => {
    if (!id) return null;
    const list = s.scoring_runs.filter((x) => x.prospect_id === id);
    return list.length ? list[list.length - 1] : null;
  });
export const useWeights = () => useStore((s) => [...s.signal_weights]);
export const useScoresFor = (ids: string[]) =>
  useStore((s) => {
    const out: Record<string, Score> = {};
    for (const id of ids) {
      const arr = s.scores.filter((x) => x.prospect_id === id);
      if (arr.length) out[id] = arr[arr.length - 1];
    }
    return out;
  });

export type { Prospect, Signal, Score, SignalWeight, ScoringRun };
