/**
 * Unified data layer.
 *
 * When VITE_SUPABASE_URL + VITE_SUPABASE_ANON_KEY are set, every hook and
 * mutation hits the real Supabase tables.  Otherwise the in-memory mock store
 * is used so the app works without any backend credentials.
 *
 * All exported hooks expose the same shape as mockStore so pages import from
 * here and never need to branch themselves.
 */
import { useEffect, useRef, useState, useCallback } from "react";
import { supabase, HAS_REAL_SUPABASE } from "./supabase";
import {
  store,
  useProspects as mockProspects,
  useProspect as mockProspect,
  useSignalsFor as mockSignalsFor,
  useLatestScore as mockLatestScore,
  useLatestRun as mockLatestRun,
  useWeights as mockWeights,
  useScoresFor as mockScoresFor,
} from "./mockStore";
export type { Prospect, Signal, Score, SignalWeight, ScoringRun } from "./mockStore";

// ─── Type normalizers ────────────────────────────────────────────────────────
// Supabase rows use `id` (UUID string) and ISO timestamps.
// The mock store (and pages) use `_id` and millisecond timestamps.
const toP = (r: any) => ({
  ...r,
  _id: r.id,
  created_at: +new Date(r.created_at),
  updated_at: +new Date(r.updated_at),
});
const toSig = (r: any) => ({
  ...r,
  _id: r.id,
  collected_at: +new Date(r.collected_at),
});
const toScore = (r: any) => ({
  ...r,
  _id: r.id,
  computed_at: +new Date(r.computed_at),
});
const toWeight = (r: any) => ({ ...r, _id: r.id });
const toRun = (r: any) => ({
  ...r,
  _id: r.id,
  started_at: +new Date(r.started_at),
  completed_at: r.completed_at ? +new Date(r.completed_at) : undefined,
});

// ─── Supabase hooks ───────────────────────────────────────────────────────────

function useSupaProspects() {
  const [data, setData] = useState<any[]>([]);
  useEffect(() => {
    supabase!
      .from("prospects")
      .select("*")
      .order("created_at", { ascending: false })
      .then(({ data: rows }) => rows && setData(rows.map(toP)));
  }, []);
  return data;
}

function useSupaProspect(id?: string) {
  const [data, setData] = useState<any | null>(null);
  useEffect(() => {
    if (!id) return;
    supabase!
      .from("prospects")
      .select("*")
      .eq("id", id)
      .single()
      .then(({ data: row }) => row && setData(toP(row)));
  }, [id]);
  return data;
}

function useSupaSignalsFor(id?: string) {
  const [data, setData] = useState<any[]>([]);
  useEffect(() => {
    if (!id) return;
    supabase!
      .from("signals")
      .select("*")
      .eq("prospect_id", id)
      .then(({ data: rows }) => rows && setData(rows.map(toSig)));
  }, [id]);
  return data;
}

function useSupaLatestScore(id?: string) {
  const [data, setData] = useState<any | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetch = useCallback(async () => {
    if (!id) return false;
    const { data: rows } = await supabase!
      .from("scores")
      .select("*")
      .eq("prospect_id", id)
      .order("computed_at", { ascending: false })
      .limit(1);
    if (rows?.length) {
      setData(toScore(rows[0]));
      return true;
    }
    return false;
  }, [id]);

  useEffect(() => {
    if (!id) return;
    let active = true;
    fetch().then((found) => {
      if (!found && active) {
        // Poll until the scoring run writes a score (up to 90 s)
        let attempts = 0;
        timerRef.current = setInterval(async () => {
          attempts++;
          const ok = await fetch();
          if (ok || attempts >= 90) clearInterval(timerRef.current!);
        }, 1000);
      }
    });
    return () => {
      active = false;
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [id, fetch]);

  return data;
}

function useSupaLatestRun(id?: string) {
  const [data, setData] = useState<any | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetch = useCallback(async () => {
    if (!id) return null;
    const { data: rows } = await supabase!
      .from("scoring_runs")
      .select("*")
      .eq("prospect_id", id)
      .order("started_at", { ascending: false })
      .limit(1);
    if (rows?.length) {
      setData(toRun(rows[0]));
      return rows[0];
    }
    return null;
  }, [id]);

  useEffect(() => {
    if (!id) return;
    fetch().then((run) => {
      if (run && (run.status === "running" || run.status === "pending")) {
        timerRef.current = setInterval(async () => {
          const r = await fetch();
          if (r?.status === "complete" || r?.status === "error") {
            clearInterval(timerRef.current!);
          }
        }, 500);
      }
    });
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [id, fetch]);

  return data;
}

function useSupaWeights() {
  const [data, setData] = useState<any[]>([]);
  useEffect(() => {
    supabase!
      .from("signal_weights")
      .select("*")
      .then(({ data: rows }) => rows && setData(rows.map(toWeight)));
  }, []);
  return data;
}

function useSupaScoresFor(ids: string[]) {
  const [data, setData] = useState<Record<string, any>>({});
  const key = ids.join(",");
  useEffect(() => {
    if (!ids.length) return;
    supabase!
      .from("scores")
      .select("*")
      .in("prospect_id", ids)
      .order("computed_at", { ascending: false })
      .then(({ data: rows }) => {
        if (!rows) return;
        const out: Record<string, any> = {};
        for (const r of rows) {
          if (!out[r.prospect_id]) out[r.prospect_id] = toScore(r);
        }
        setData(out);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return data;
}

// ─── Supabase mutations ───────────────────────────────────────────────────────

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

const rand = (min: number, max: number) =>
  Math.floor(Math.random() * (max - min + 1)) + min;
const norm = (n: number) => Math.max(0, Math.min(100, 100 * (1 - Math.exp(-n / 15))));

const FALSIFICATION_NOTES = [
  "Authenticity assumes LinkedIn tenure is accurate — re-verify if profile was edited in the last 60 days.",
  "Authority cross-checks USPTO patents — invalid if patent attribution is wrong.",
  "Warmth depends on a fresh mutual-connections graph — re-sync if data is >7 days old.",
  "Role not cross-checked against Crunchbase — re-verify if prospect changed jobs in the last 30 days.",
];

async function supaCreateProspect(p: {
  name: string;
  company: string;
  role: string;
  industry: string;
  linkedin_url?: string;
}) {
  const { data, error } = await supabase!
    .from("prospects")
    .insert(p)
    .select()
    .single();
  if (error) throw error;
  return data.id as string;
}

async function supaRunScoring(prospect_id: string) {
  const { data: runRow, error: runErr } = await supabase!
    .from("scoring_runs")
    .insert({
      prospect_id,
      status: "running",
      sources_attempted: [...ALL_SOURCES],
      sources_succeeded: [],
    })
    .select()
    .single();
  if (runErr) throw runErr;
  const runId = runRow!.id;

  for (const source of ALL_SOURCES) {
    await supabase!
      .from("scoring_runs")
      .update({ current_source: source })
      .eq("id", runId);

    await new Promise((r) => setTimeout(r, 350 + Math.random() * 350));

    const signalRows = SOURCE_TO_SIGNALS[source].map((signal_type) => ({
      prospect_id,
      source,
      signal_type,
      value: rand(0, 30),
      raw_data: { _mock: true, source },
      weight: 1,
      confidence: +(0.6 + Math.random() * 0.35).toFixed(2),
    }));
    await supabase!.from("signals").insert(signalRows);

    const succIdx = ALL_SOURCES.indexOf(source as any);
    await supabase!
      .from("scoring_runs")
      .update({ sources_succeeded: [...ALL_SOURCES].slice(0, succIdx + 1) })
      .eq("id", runId);
  }

  // Compute score from stored signals + weights
  const { data: signals } = await supabase!
    .from("signals")
    .select("*")
    .eq("prospect_id", prospect_id);
  const { data: weights } = await supabase!.from("signal_weights").select("*");

  const wmap = new Map(
    (weights ?? []).map((w: any) => [
      w.signal_type,
      { a: w.authenticity_weight, au: w.authority_weight, w: w.warmth_weight },
    ])
  );
  let aN = 0, aD = 0, auN = 0, auD = 0, wN = 0, wD = 0;
  for (const s of signals ?? []) {
    const w = wmap.get(s.signal_type);
    if (!w) continue;
    const v = norm(Number(s.value) || 0);
    const base = (s.weight ?? 1) * (s.confidence ?? 1);
    aN += v * base * w.a; aD += base * w.a;
    auN += v * base * w.au; auD += base * w.au;
    wN += v * base * w.w; wD += base * w.w;
  }
  const round = (n: number) => Math.round(n * 10) / 10;
  await supabase!.from("scores").insert({
    prospect_id,
    authenticity_score: round(aD ? aN / aD : 0),
    authority_score: round(auD ? auN / auD : 0),
    warmth_score: round(wD ? wN / wD : 0),
    overall_score: round(
      0.4 * (aD ? aN / aD : 0) +
      0.4 * (auD ? auN / auD : 0) +
      0.2 * (wD ? wN / wD : 0)
    ),
    falsification_notes: FALSIFICATION_NOTES,
  });

  await supabase!
    .from("scoring_runs")
    .update({
      status: "complete",
      current_source: null,
      completed_at: new Date().toISOString(),
    })
    .eq("id", runId);
}

async function supaUpsertWeight(
  signal_type: string,
  a: number,
  au: number,
  w: number
) {
  await supabase!.from("signal_weights").upsert(
    { signal_type, authenticity_weight: a, authority_weight: au, warmth_weight: w },
    { onConflict: "signal_type" }
  );
}

async function supaComputeScores(prospectIds: string[]) {
  const { data: weights } = await supabase!.from("signal_weights").select("*");
  const wmap = new Map(
    (weights ?? []).map((w: any) => [
      w.signal_type,
      { a: w.authenticity_weight, au: w.authority_weight, w: w.warmth_weight },
    ])
  );
  for (const pid of prospectIds) {
    const { data: signals } = await supabase!
      .from("signals")
      .select("*")
      .eq("prospect_id", pid);
    if (!signals?.length) continue;
    let aN = 0, aD = 0, auN = 0, auD = 0, wN = 0, wD = 0;
    for (const s of signals) {
      const w = wmap.get(s.signal_type);
      if (!w) continue;
      const v = norm(Number(s.value) || 0);
      const base = (s.weight ?? 1) * (s.confidence ?? 1);
      aN += v * base * w.a; aD += base * w.a;
      auN += v * base * w.au; auD += base * w.au;
      wN += v * base * w.w; wD += base * w.w;
    }
    const round = (n: number) => Math.round(n * 10) / 10;
    await supabase!.from("scores").insert({
      prospect_id: pid,
      authenticity_score: round(aD ? aN / aD : 0),
      authority_score: round(auD ? auN / auD : 0),
      warmth_score: round(wD ? wN / wD : 0),
      overall_score: round(
        0.4 * (aD ? aN / aD : 0) +
        0.4 * (auD ? auN / auD : 0) +
        0.2 * (wD ? wN / wD : 0)
      ),
      falsification_notes: FALSIFICATION_NOTES,
    });
  }
}

// ─── Exported unified hooks ───────────────────────────────────────────────────

export const useProspects = HAS_REAL_SUPABASE ? useSupaProspects : mockProspects;
export const useProspect = HAS_REAL_SUPABASE ? useSupaProspect : mockProspect;
export const useSignalsFor = HAS_REAL_SUPABASE ? useSupaSignalsFor : mockSignalsFor;
export const useLatestScore = HAS_REAL_SUPABASE ? useSupaLatestScore : mockLatestScore;
export const useLatestRun = HAS_REAL_SUPABASE ? useSupaLatestRun : mockLatestRun;
export const useWeights = HAS_REAL_SUPABASE ? useSupaWeights : mockWeights;
export const useScoresFor = HAS_REAL_SUPABASE ? useSupaScoresFor : mockScoresFor;

// ─── Exported unified mutations ───────────────────────────────────────────────

export const db = HAS_REAL_SUPABASE
  ? {
      createProspect: supaCreateProspect,
      runScoring: supaRunScoring,
      upsertWeight: supaUpsertWeight,
      computeScores: supaComputeScores,
    }
  : {
      createProspect: (p: any) => Promise.resolve(store.createProspect(p)),
      runScoring: (id: string) => store.runScoring(id),
      upsertWeight: (st: string, a: number, au: number, w: number) => {
        store.upsertWeight(st, a, au, w);
        return Promise.resolve();
      },
      computeScores: (ids: string[]) => {
        ids.forEach((id) => store.computeScore(id));
        return Promise.resolve();
      },
    };
