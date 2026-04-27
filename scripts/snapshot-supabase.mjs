// One-shot snapshot: pages every row from prospects / scores / signals /
// signal_weights and writes src/lib/snapshot.json. The frontend can read
// from this file instead of hitting Supabase at runtime — kills the ~12MB
// per-load fan-out the audit flagged.
//
// Usage: node scripts/snapshot-supabase.mjs
// Reads VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY from .env.local.

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { gzipSync } from "node:zlib";

const env = Object.fromEntries(
  readFileSync(resolve(".env.local"), "utf8")
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith("#") && l.includes("="))
    .map((l) => {
      const i = l.indexOf("=");
      return [l.slice(0, i), l.slice(i + 1).replace(/^["']|["']$/g, "")];
    }),
);
const SUPA_URL = env.VITE_SUPABASE_URL;
const SUPA_KEY = env.VITE_SUPABASE_ANON_KEY;
if (!SUPA_URL || !SUPA_KEY) {
  console.error("Missing VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY in .env.local");
  process.exit(1);
}

const headers = { apikey: SUPA_KEY, Authorization: `Bearer ${SUPA_KEY}` };

async function pageAll(table, opts = {}) {
  const { order, select = "*" } = opts;
  const all = [];
  for (let offset = 0; ; offset += 1000) {
    const params = new URLSearchParams({ select, limit: "1000", offset: String(offset) });
    if (order) params.set("order", order);
    const url = `${SUPA_URL}/rest/v1/${table}?${params}`;
    const resp = await fetch(url, { headers });
    if (!resp.ok) {
      throw new Error(`${table} @ offset=${offset} → HTTP ${resp.status}`);
    }
    const rows = await resp.json();
    if (!Array.isArray(rows) || rows.length === 0) break;
    all.push(...rows);
    process.stdout.write(`\r  ${table}: ${all.length} rows`);
    if (rows.length < 1000) break;
  }
  process.stdout.write("\n");
  return all;
}

// ─── pull ────────────────────────────────────────────────────────────────────
console.log("→ snapshotting Supabase…");
const prospects = await pageAll("prospects", { order: "created_at.desc,id.asc" });
const signal_weights = await pageAll("signal_weights");
const allScores = await pageAll("scores", { order: "computed_at.desc" });

// Lite signal projection — `value` and `raw_data` JSONB blobs are 40MB+
// combined and only the per-prospect inspector reads them. The graph builder
// just needs (id, prospect_id, signal_type, confidence, weight, collected_at)
// to count evidence per prospect and emit evidence_cited edges. The inspector
// keeps fetching full signals from live Supabase via useSupaSignalsFor(id).
const allSignals = await pageAll("signals", {
  order: "collected_at.desc",
  select: "id,prospect_id,source,signal_type,confidence,weight,collected_at",
});

// ─── shrink scores: pick best per prospect (mirrors db.ts MODEL_PRECEDENCE) ──
const MODEL_PRECEDENCE = {
  chartreuse_llm_v1: 1,
  chartreuse_deterministic_v1: 2,
  lightweight_v1: 3,
};
const scoreByProspect = new Map();
for (const r of allScores) {
  const cur = scoreByProspect.get(r.prospect_id);
  if (!cur) {
    scoreByProspect.set(r.prospect_id, r);
    continue;
  }
  const ta = MODEL_PRECEDENCE[r.model_version] ?? 9;
  const tb = MODEL_PRECEDENCE[cur.model_version] ?? 9;
  if (ta < tb) scoreByProspect.set(r.prospect_id, r);
  else if (ta === tb && +new Date(r.computed_at) > +new Date(cur.computed_at)) {
    scoreByProspect.set(r.prospect_id, r);
  }
}
const scores = [...scoreByProspect.values()];

// ─── write ───────────────────────────────────────────────────────────────────
const out = {
  version: 1,
  generated_at: new Date().toISOString(),
  prospects,
  scores,
  signals: allSignals,
  signal_weights,
};
const outPath = resolve("src/lib/snapshot.json");
mkdirSync(dirname(outPath), { recursive: true });
const json = JSON.stringify(out);
writeFileSync(outPath, json);
const gz = gzipSync(json);

const fmtMB = (n) => (n / 1024 / 1024).toFixed(1);
console.log(`\n✓ wrote ${outPath}`);
console.log(`  prospects:      ${prospects.length}`);
console.log(`  scores (best):  ${scores.length}  (from ${allScores.length} total rows)`);
console.log(`  signals:        ${allSignals.length}`);
console.log(`  signal_weights: ${signal_weights.length}`);
console.log(`  raw size:       ${fmtMB(json.length)} MB`);
console.log(`  gzipped:        ${fmtMB(gz.length)} MB`);
