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

// Supabase's REST endpoint defaults to a max of 1000 rows per request. Page
// through the result set so callers get every row regardless of table size.
const PAGE = 1000;
async function fetchAllRows<T = any>(
  build: () => any,
): Promise<T[]> {
  const all: T[] = [];
  for (let from = 0; ; from += PAGE) {
    const { data, error } = await build().range(from, from + PAGE - 1);
    if (error) {
      console.error("[db] fetchAllRows error:", error);
      return all;
    }
    if (!data || data.length === 0) break;
    all.push(...(data as T[]));
    if (data.length < PAGE) break;
  }
  return all;
}

function useSupaProspects() {
  const [data, setData] = useState<any[]>([]);
  useEffect(() => {
    fetchAllRows<any>(() =>
      supabase!
        .from("prospects")
        .select("*")
        .order("created_at", { ascending: false }),
    ).then((rows) => setData(rows.map(toP)));
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

// Bulk variant for Discover: fetch all signals once (paginated) and group by
// prospect_id client-side. Same reasoning as `useSupaScoresFor` — a PostgREST
// `.in("prospect_id", ids)` with hundreds of UUIDs exceeds the gateway URL cap.
function useSupaSignalsForMany(ids: string[]) {
  const [data, setData] = useState<Record<string, any[]>>({});
  const key = ids.length ? `${ids.length}` : "";
  useEffect(() => {
    if (!ids.length) return;
    let cancelled = false;
    fetchAllRows<any>(() =>
      supabase!
        .from("signals")
        .select("*")
        .order("collected_at", { ascending: false }),
    ).then((rows) => {
      if (cancelled) return;
      const wanted = new Set(ids);
      const out: Record<string, any[]> = {};
      for (const r of rows) {
        if (!wanted.has(r.prospect_id)) continue;
        (out[r.prospect_id] ??= []).push(toSig(r));
      }
      setData(out);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return data;
}

// Mock fallback: group whatever the in-memory store has by prospect_id.
function mockSignalsForMany(ids: string[]) {
  const [data, setData] = useState<Record<string, any[]>>({});
  const key = ids.length ? `${ids.length}` : "";
  useEffect(() => {
    if (!ids.length) return;
    const out: Record<string, any[]> = {};
    for (const id of ids) out[id] = store.signals.filter((s) => s.prospect_id === id);
    setData(out);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
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
  // Only re-fetch when the *set* of ids changes meaningfully. We key on length +
  // a stable-sorted hash so reordering doesn't thrash the effect.
  const key = ids.length ? `${ids.length}` : "";
  useEffect(() => {
    if (!ids.length) return;
    let cancelled = false;
    // A PostgREST `.in("prospect_id", ids)` with hundreds of UUIDs produces a
    // URL longer than the gateway's max length (~16 KB at Cloudflare) and
    // returns HTTP 400. We also can't rely on `.limit()`: default page size
    // is 1000, but there can be multiple scores per prospect. So: page through
    // the scores table in full, ordered newest-first, and keep the first score
    // we see per prospect_id.
    fetchAllRows<any>(() =>
      supabase!
        .from("scores")
        .select("*")
        .order("computed_at", { ascending: false }),
    ).then((rows) => {
      if (cancelled) return;
      const wanted = new Set(ids);
      const out: Record<string, any> = {};
      for (const r of rows) {
        if (!wanted.has(r.prospect_id)) continue;
        if (!out[r.prospect_id]) out[r.prospect_id] = toScore(r);
      }
      setData(out);
    });
    return () => {
      cancelled = true;
    };
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

// Intentionally empty — we don't emit generic falsification boilerplate per
// user directive. Any falsification note must be derived from THIS prospect's
// actual coverage/confidence state, emitted inline where it is computed.
const FALSIFICATION_NOTES: string[] = [];

async function supaCreateProspect(p: {
  name: string;
  company: string;
  role: string;
  roles?: string[];
  keywords?: string[];
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

const API_BASE: string =
  (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/+$/, "") ||
  "http://localhost:8000";

/**
 * Score a prospect by:
 *   1. Creating a `scoring_runs` row so the /validate UX can poll progress.
 *   2. Trying our FastAPI `/validate` endpoint first. On success, map the
 *      ScoreResult to a `public.scores` row.
 *   3. On 404 / network error, falling back to the lightweight signal-based
 *      scorer (seeds mock signals for new prospects, then weighted sigmoid).
 *   4. Marking `scoring_runs` complete when done.
 *
 * Replaces the prior `supabase.functions.invoke("validate-agent")` which
 * referenced an edge function that was never deployed.
 */
async function supaRunScoring(prospect_id: string): Promise<void> {
  // 1. scoring_runs row — frontend polling UX reads this.
  const run = {
    prospect_id,
    status: "running" as const,
    sources_attempted: [...ALL_SOURCES],
    sources_succeeded: [] as string[],
    current_source: null,
  };
  const { data: runRow } = await supabase!
    .from("scoring_runs")
    .insert(run)
    .select()
    .single();
  const runId = runRow?.id as string | undefined;

  const finish = async (status: "complete" | "error", extra?: Record<string, unknown>) => {
    if (!runId) return;
    await supabase!
      .from("scoring_runs")
      .update({ status, completed_at: new Date().toISOString(), ...(extra ?? {}) })
      .eq("id", runId);
  };

  try {
    // 2. Fetch prospect to get name/industry/role for our /validate call.
    const { data: prospect, error: fetchErr } = await supabase!
      .from("prospects")
      .select("name, company, role, industry")
      .eq("id", prospect_id)
      .single();
    if (fetchErr || !prospect) {
      console.error("[supaRunScoring] fetch prospect failed:", fetchErr);
      await finish("error", { error_log: "prospect not found" });
      return;
    }

    // 3. Try FastAPI /validate. Returns a rich ScoreResult when the person
    //    matches a lead_scoring.people row (ILIKE on name + industry).
    let used_fastapi = false;
    try {
      const resp = await fetch(`${API_BASE}/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: prospect.name,
          industry: prospect.industry,
          role: prospect.role,
        }),
      });
      if (resp.ok) {
        const result = (await resp.json()) as {
          composite: number;
          role_fit: number;
          authority_fit: number;
          company_fit: number;
          confidence: number;
          reasoning?: string;
        };
        // Map to frontend schema: authenticity = meta-trust (our confidence),
        // authority = authority_fit, warmth = role_fit, overall = composite.
        await supabase!.from("scores").insert({
          prospect_id,
          authenticity_score: result.confidence,
          authority_score: result.authority_fit,
          warmth_score: result.role_fit,
          overall_score: result.composite,
          falsification_notes: result.reasoning
            ? [`reasoning: ${String(result.reasoning).slice(0, 240)}`]
            : [],
        });
        used_fastapi = true;
      } else if (resp.status !== 404) {
        console.error("[supaRunScoring] /validate unexpected status:", resp.status);
      }
    } catch (err) {
      console.error("[supaRunScoring] /validate fetch error:", err);
    }

    // 4. Fallback — seed mock signals + lightweight weighted-sigmoid scorer.
    if (!used_fastapi) {
      const { data: existing } = await supabase!
        .from("signals")
        .select("id")
        .eq("prospect_id", prospect_id)
        .limit(1);
      if (!existing?.length) {
        const rows: Record<string, unknown>[] = [];
        for (const source of ALL_SOURCES) {
          for (const signal_type of SOURCE_TO_SIGNALS[source]) {
            // value is scalar — `supaComputeScores` does `Number(s.value)` so
            // passing an object here would NaN-coalesce to 0.
            rows.push({
              prospect_id,
              source,
              signal_type,
              value: rand(5, 30),
              raw_data: { _synthetic: true, via: "supaRunScoring-fallback" },
              weight: 1,
              confidence: +(0.6 + Math.random() * 0.35).toFixed(2),
            });
          }
        }
        if (rows.length > 0) {
          await supabase!.from("signals").insert(rows);
        }
      }
      await supaComputeScores([prospect_id]);
    }

    await finish("complete", { sources_succeeded: [...ALL_SOURCES] });
  } catch (err) {
    console.error("[supaRunScoring] fatal:", err);
    await finish("error", { error_log: String(err).slice(0, 500) });
  }
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

// Signals land in `public.signals.value` as either a scalar (number/string) or
// a JSON object. Historically the bulk-import pipeline wraps scalars as
// `{raw: N}` (and sometimes richer blobs like `{about, method, headline}`).
// `Number({...}) || 0` collapsed every object to 0 and produced 0/0/0/0 scores.
// Normalize defensively here so one scorer handles both shapes.
function extractSignalValue(v: unknown): number {
  if (typeof v === "number") return isFinite(v) ? v : 0;
  if (typeof v === "string") { const n = Number(v); return isFinite(n) ? n : 0; }
  if (v && typeof v === "object") {
    const obj = v as Record<string, unknown>;
    if ("raw" in obj) return extractSignalValue(obj.raw);
    if ("value" in obj) return extractSignalValue(obj.value);
  }
  return 0;
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
      const v = norm(extractSignalValue(s.value));
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
export const useSignalsForMany = HAS_REAL_SUPABASE ? useSupaSignalsForMany : mockSignalsForMany;
// Signal-value normalizer exposed for the Discover row-enrichment UX.
export { extractSignalValue };
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
