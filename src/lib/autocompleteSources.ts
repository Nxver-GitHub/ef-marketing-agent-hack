/**
 * Autocomplete data for Validate page:
 *   - Roles mined from public.prospects.role (canonicalized)
 *   - Keywords mined from public.signals where signal_type='tech_stack',
 *     plus a curated seed list of semi/defense domain terms so fresh databases
 *     still produce useful suggestions.
 *
 * The hook fetches once on first mount, caches the result, and returns ranked
 * candidates by the user's current input. Empty input returns the most common
 * entries across the dataset.
 */
import { useEffect, useState } from "react";
import { HAS_REAL_SUPABASE, supabase } from "./supabase";

type Counted = { value: string; count: number };

// ─── Role canonicalization ─────────────────────────────────────────────────
// LinkedIn titles are noisy ("Director of Engineering, AI Fabric | SoC | ..."). We
// strip at separators and keep the head phrase, then aggregate to find the most
// common canonical roles in the dataset.
const ROLE_SPLIT = /\s*[|\u2013\u2014\-:;,]\s*|\s+at\s+/i;

const TITLE_NORM_MAP: [RegExp, string][] = [
  [/^vice president( of)?/i, "VP of"],
  [/^sr\.?\s+director/i, "Senior Director"],
  [/^sr\.?\s+vp/i, "SVP"],
  [/^vp\s+(?!of\b)/i, "VP of "],
];

function canonicalizeTitle(raw: string): string | null {
  const head = raw.split(ROLE_SPLIT)[0]?.trim();
  if (!head) return null;
  let t = head.replace(/\s+/g, " ");
  for (const [re, rep] of TITLE_NORM_MAP) t = t.replace(re, rep);
  // Skip junk: too short, all-caps one-word, or obviously not a role
  if (t.length < 6 || t.length > 70) return null;
  // Title-case first letter of each word for consistency
  t = t
    .split(" ")
    .map((w) => (w.length > 0 ? w[0]!.toUpperCase() + w.slice(1) : w))
    .join(" ");
  return t;
}

// ─── Keyword seed list ─────────────────────────────────────────────────────
// Curated domain tokens for all five industries. Union'd with live signal data.
const SEED_KEYWORDS = [
  // Semiconductors
  "SoC",
  "ASIC",
  "RTL",
  "Verilog",
  "SystemVerilog",
  "UVM",
  "DFT",
  "synthesis",
  "place and route",
  "physical design",
  "verification",
  "formal verification",
  "timing closure",
  "signal integrity",
  "PCB",
  "PCIe",
  "DDR",
  "HBM",
  "LPDDR",
  "silicon",
  "semiconductor",
  "chip design",
  "tapeout",
  "3nm",
  "5nm",
  "7nm",
  "CUDA",
  "PyTorch",
  "TensorFlow",
  "transformer",
  "LLM",
  "AI inference",
  "embedded",
  "firmware",
  "RTOS",
  "wireless",
  "RF",
  "5G",
  "wifi",
  "baseband",
  "FPGA",
  "HPC",
  "datacenter",
  "networking",
  // Defense & Aerospace
  "automotive",
  "lidar",
  "radar",
  "autonomy",
  "UAV",
  "drone",
  "aerospace",
  "defense",
  "computer vision",
  "sensor fusion",
  "edge AI",
  "C2",
  "ISR",
  "EW",
  "electronic warfare",
  "mission systems",
  "avionics",
  "propulsion",
  "satellite",
  "space systems",
  "launch vehicle",
  "cybersecurity",
  "classified",
  "DoD",
  "ITAR",
  // Health Tech
  "medical devices",
  "FDA",
  "510(k)",
  "clinical trials",
  "regulatory affairs",
  "quality systems",
  "MDR",
  "ISO 13485",
  "EMR",
  "EHR",
  "interoperability",
  "FHIR",
  "HL7",
  "imaging",
  "MRI",
  "CT",
  "ultrasound",
  "diagnostics",
  "surgical robotics",
  "neuromodulation",
  "cardiovascular",
  "orthopedics",
  "drug delivery",
  "wearables",
  "remote patient monitoring",
  "digital health",
  "health informatics",
  "oncology",
  "genomics",
  "bioinformatics",
  // Quantum
  "quantum computing",
  "qubit",
  "superconducting",
  "trapped ion",
  "photonic",
  "quantum error correction",
  "fault tolerant",
  "quantum annealing",
  "variational quantum",
  "NISQ",
  "quantum advantage",
  "quantum simulation",
  "quantum networking",
  "quantum cryptography",
  "post-quantum",
  "QKD",
  "cryogenics",
  "dilution refrigerator",
  "quantum hardware",
  "quantum software",
  "quantum algorithms",
  "Qiskit",
  "Cirq",
  "OpenQASM",
  "quantum chemistry",
  "optimization",
];

// ─── Main hook ─────────────────────────────────────────────────────────────
type Cache = {
  roles: string[];
  keywords: string[];
  companies: string[];
};

const cache: Cache = { roles: [], keywords: [], companies: [] };
let loading = false;
let loaded = false;
const listeners: Set<() => void> = new Set();

function notify() {
  for (const l of listeners) l();
}

// Snapshot mode flag — when set, autocomplete reads from the offline JSON
// snapshot instead of round-tripping Supabase. Mirrors db.ts::USE_SNAPSHOT
// so the demo experience doesn't pay 4 round-trips just to populate hints.
const USE_SNAPSHOT =
  HAS_REAL_SUPABASE && (import.meta.env.VITE_USE_SNAPSHOT as string | undefined) === "true";

interface SnapshotShape {
  prospects: Array<{ role?: string | null; company?: string | null; industry?: string | null }>;
  signals: Array<{ signal_type?: string | null; value?: unknown }>;
  scores: unknown[];
  signal_weights: unknown[];
}

async function loadFromSnapshot(): Promise<void> {
  const mod = await import("./snapshot.json");
  const snap = mod.default as SnapshotShape;
  // Roles
  const roleCount = new Map<string, number>();
  for (const r of snap.prospects) {
    const c = r.role ? canonicalizeTitle(r.role) : null;
    if (!c) continue;
    roleCount.set(c, (roleCount.get(c) ?? 0) + 1);
  }
  cache.roles = [...roleCount.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 300)
    .map(([v]) => v);

  // Companies
  const companySet = new Set<string>();
  for (const r of snap.prospects) if (r.company) companySet.add(r.company);
  cache.companies = [...companySet].sort((a, b) => a.localeCompare(b));

  // Keywords from tech_stack signals
  const kwCount = new Map<string, number>();
  for (const r of snap.signals) {
    if (r.signal_type !== "tech_stack") continue;
    const v = r.value as { tokens?: unknown } | null;
    const tokens = v?.tokens;
    if (!Array.isArray(tokens)) continue;
    for (const t of tokens) {
      if (typeof t !== "string") continue;
      const clean = t.trim();
      if (clean.length < 2 || clean.length > 40) continue;
      kwCount.set(clean, (kwCount.get(clean) ?? 0) + 1);
    }
  }
  const minedKeywords: string[] = [...kwCount.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 200)
    .map(([v]) => v);
  cache.keywords = [...new Set([...SEED_KEYWORDS, ...minedKeywords])];
}

async function loadOnce() {
  if (loaded || loading) return;
  loading = true;
  try {
    if (USE_SNAPSHOT) {
      await loadFromSnapshot();
      return;
    }
    if (!HAS_REAL_SUPABASE || !supabase) {
      cache.roles = [];
      cache.keywords = [...new Set(SEED_KEYWORDS)];
      cache.companies = [];
      loaded = true;
      return;
    }

    // ─── Roles: canonicalize every public.prospects.role, tally, keep top 300
    const { data: roleRows } = await supabase
      .from("prospects")
      .select("role")
      .limit(20000);
    const roleCount = new Map<string, number>();
    for (const r of (roleRows ?? []) as { role: string | null }[]) {
      const c = r.role ? canonicalizeTitle(r.role) : null;
      if (!c) continue;
      roleCount.set(c, (roleCount.get(c) ?? 0) + 1);
    }
    const roles: Counted[] = [...roleCount.entries()]
      .map(([value, count]) => ({ value, count }))
      .sort((a, b) => b.count - a.count);
    cache.roles = roles.slice(0, 300).map((r) => r.value);

    // ─── Companies: distinct company names from all prospects, sorted alpha
    const { data: companyRows } = await supabase
      .from("prospects")
      .select("company, industry")
      .limit(20000);
    const companySet = new Map<string, string>();
    for (const r of (companyRows ?? []) as { company: string | null; industry: string | null }[]) {
      if (r.company && !companySet.has(r.company)) {
        companySet.set(r.company, r.industry ?? "");
      }
    }
    cache.companies = [...companySet.keys()].sort((a, b) => a.localeCompare(b));

    // ─── Keywords: distinct tokens from tech_stack signals.value.tokens[]
    const { data: sigRows } = await supabase
      .from("signals")
      .select("value")
      .eq("signal_type", "tech_stack")
      .limit(3000);
    const kwCount = new Map<string, number>();
    for (const r of (sigRows ?? []) as { value: { tokens?: unknown } | null }[]) {
      const tokens = r.value?.tokens;
      if (!Array.isArray(tokens)) continue;
      for (const t of tokens) {
        if (typeof t !== "string") continue;
        const clean = t.trim();
        if (clean.length < 2 || clean.length > 40) continue;
        kwCount.set(clean, (kwCount.get(clean) ?? 0) + 1);
      }
    }
    const minedKeywords: string[] = [...kwCount.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 200)
      .map(([v]) => v);
    cache.keywords = [...new Set([...SEED_KEYWORDS, ...minedKeywords])];
  } catch (err) {
    console.error("[autocomplete] load error:", err);
  } finally {
    loading = false;
    loaded = true;
    notify();
  }
}

export function useAutocompleteSources() {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!loaded) void loadOnce();
    const listener = () => tick((n) => n + 1);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
  return {
    roles: cache.roles,
    keywords: cache.keywords,
    companies: cache.companies,
    isLoaded: loaded,
  };
}

// Filter helper: substring match, ranked by index-of-match (prefix wins).
//
// Important: we do NOT skip entries whose lowercase equals the query. Doing
// so silently hid suggestions when the user typed an exact match (e.g.,
// typing "soc" would hide the "SoC" entry, breaking Tab-to-accept). The dedup
// against existing tags is handled at the call site (Validate.tsx + Discover.tsx)
// after this returns, where we know which entries are already selected.
export function rankSuggestions(pool: string[], query: string, limit = 8): string[] {
  const q = query.trim().toLowerCase();
  if (!q) return pool.slice(0, limit);
  const hits: Array<{ v: string; score: number }> = [];
  for (const v of pool) {
    const vl = v.toLowerCase();
    const idx = vl.indexOf(q);
    if (idx === -1) continue;
    // prefix match = best, then substring, then tiebreak by length
    const score = idx === 0 ? 0 : 10 + idx;
    hits.push({ v, score: score + v.length * 0.01 });
  }
  hits.sort((a, b) => a.score - b.score);
  return hits.slice(0, limit).map((h) => h.v);
}
