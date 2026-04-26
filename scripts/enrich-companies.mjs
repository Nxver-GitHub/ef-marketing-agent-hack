// One-shot enrichment: pulls every distinct company + industry from the
// prospects table and asks GLM-4.6 for {country, state?, hq_city?, industry,
// employee_count_estimate, partnerships?, description}. Writes two generated
// TS files that graph.ts merges over the hand-curated COMPANY_META.
//
// Usage: node scripts/enrich-companies.mjs
// Reads VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY / VITE_ZAI_API_KEY /
// VITE_ZAI_BASE_URL from .env.local.

import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import OpenAI from "openai";

// ─── env ────────────────────────────────────────────────────────────────────
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
const ZAI_KEY = env.VITE_ZAI_API_KEY;
const ZAI_BASE = env.VITE_ZAI_BASE_URL || "https://api.z.ai/api/paas/v4";
if (!SUPA_URL || !SUPA_KEY || !ZAI_KEY) {
  console.error("Missing env: VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY / VITE_ZAI_API_KEY");
  process.exit(1);
}

const llm = new OpenAI({ apiKey: ZAI_KEY, baseURL: ZAI_BASE });
const MODEL = "glm-4.6";

// ─── pull distinct (company, industry) tuples ────────────────────────────────
async function loadDistinct() {
  const companies = new Map(); // company -> { count, industries: Set }
  for (let offset = 0; offset < 50_000; offset += 1000) {
    const url = `${SUPA_URL}/rest/v1/prospects?select=company,industry&limit=1000&offset=${offset}`;
    const resp = await fetch(url, {
      headers: { apikey: SUPA_KEY, Authorization: `Bearer ${SUPA_KEY}` },
    });
    const rows = await resp.json();
    if (!Array.isArray(rows) || rows.length === 0) break;
    for (const r of rows) {
      const c = (r.company || "").trim();
      if (!c) continue;
      const ind = (r.industry || "").trim();
      if (!companies.has(c)) companies.set(c, { count: 0, industries: new Set() });
      const slot = companies.get(c);
      slot.count++;
      if (ind) slot.industries.add(ind);
    }
    if (rows.length < 1000) break;
  }
  return companies;
}

// GLM-4.6's structured tool-calling on Z.AI returns empty args even when the
// model reasoning contains the right data, so we ask for raw JSON instead and
// parse it ourselves. The first { ... } block in the response is taken.

function extractJson(s) {
  if (!s) return null;
  // Strip ```json fences if present.
  const fenceMatch = s.match(/```(?:json)?\s*([\s\S]*?)```/);
  const body = fenceMatch ? fenceMatch[1] : s;
  // Find the first balanced {...}.
  const start = body.indexOf("{");
  if (start < 0) return null;
  let depth = 0;
  for (let i = start; i < body.length; i++) {
    if (body[i] === "{") depth++;
    else if (body[i] === "}") {
      depth--;
      if (depth === 0) {
        try {
          return JSON.parse(body.slice(start, i + 1));
        } catch {
          return null;
        }
      }
    }
  }
  return null;
}

const COMPANY_BATCH_PROMPT = `For each company below, return a single JSON object whose keys are the company names and whose values match this schema:

{
  "country": string (HQ country, full name),
  "state": string (US state/province, "" if not US/Canada),
  "hq_city": string ("" if unknown),
  "industry": string (best-fit vertical from: Semiconductors, Aerospace, Defense, Health Tech, Quantum, Internet, Consumer Electronics, Industrial, Pharma, AI, Cybersecurity, Energy, Government Lab, National Lab, University, Financial Services, Materials, Robotics, Telecom, Automotive, Biotech, Space, Crypto, etc.),
  "employee_count_estimate": string (one of "<100","100-1k","1k-10k","10k-100k","100k+", or "" if uncertain),
  "partnerships": string[] (<= 3 well-known partners; [] if none),
  "description": string (<= 140 char factual sentence)
}

Output ONE JSON object only — no prose, no markdown fences.
Do not invent partnerships or HQ city; leave them empty if uncertain.

Companies (with candidate industries from our DB):`;

const INDUSTRY_BATCH_PROMPT = `For each industry vertical below, return a single JSON object whose keys are the industry names and whose values match this schema:

{
  "description": string (<= 140 char definition),
  "keywords": string[] (3-5 sub-segments or core technologies),
  "adjacent_industries": string[] (<= 3 closely-related verticals)
}

Output ONE JSON object only — no prose, no markdown fences.

Industries:`;

// Per-call timeout — Z.AI responses occasionally hang, freezing the pool.
async function callLLM(messages, timeoutMs = 120_000) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const resp = await llm.chat.completions.create(
      { model: MODEL, messages, temperature: 0.1 },
      { signal: ac.signal },
    );
    return resp.choices[0].message.content ?? "";
  } finally {
    clearTimeout(timer);
  }
}

async function enrichCompanyBatch(batch) {
  const lines = batch.map((c) => `- ${c.name} (DB industries: ${[...c.candidateIndustries].join(", ") || "(none)"})`).join("\n");
  const user = `${COMPANY_BATCH_PROMPT}\n${lines}`;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const text = await callLLM([{ role: "user", content: user }]);
      const obj = extractJson(text);
      if (!obj || typeof obj !== "object") throw new Error("no json object");
      // Map back to batch entries by best-effort lookup (case-insensitive,
      // stripped of common suffixes).
      const norm = (s) => s.toLowerCase().replace(/\b(corp|corporation|inc|incorporated|ltd|llc|technologies|technology|systems?)\b/g, "").replace(/[^a-z0-9]/g, "");
      const byKey = new Map();
      for (const k of Object.keys(obj)) byKey.set(norm(k), obj[k]);
      const out = batch.map((c) => {
        const m = byKey.get(norm(c.name)) ?? obj[c.name];
        if (m && typeof m.country === "string" && m.country.trim()
            && typeof m.industry === "string" && m.industry.trim()) {
          return { ...c, meta: m };
        }
        return { ...c, meta: null };
      });
      return out;
    } catch (err) {
      if (attempt === 2) {
        console.error(`  ✗ batch [${batch[0].name}…]: ${err.message}`);
        return batch.map((c) => ({ ...c, meta: null }));
      }
      await new Promise((r) => setTimeout(r, 800 * (attempt + 1)));
    }
  }
}

async function enrichIndustryBatch(batch) {
  const lines = batch.map((i) => `- ${i.name}`).join("\n");
  const user = `${INDUSTRY_BATCH_PROMPT}\n${lines}`;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const text = await callLLM([{ role: "user", content: user }]);
      const obj = extractJson(text);
      if (!obj || typeof obj !== "object") throw new Error("no json object");
      const out = batch.map((i) => {
        const m = obj[i.name];
        if (m && typeof m.description === "string" && m.description.trim()) {
          return { ...i, meta: m };
        }
        return { ...i, meta: null };
      });
      return out;
    } catch (err) {
      if (attempt === 2) {
        console.error(`  ✗ industry batch [${batch[0].name}…]: ${err.message}`);
        return batch.map((i) => ({ ...i, meta: null }));
      }
      await new Promise((r) => setTimeout(r, 800 * (attempt + 1)));
    }
  }
}

function chunk(arr, n) {
  const out = [];
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n));
  return out;
}

// ─── concurrency limiter ────────────────────────────────────────────────────
async function pmap(items, n, fn) {
  const results = new Array(items.length);
  let i = 0;
  let done = 0;
  const total = items.length;
  await Promise.all(
    Array.from({ length: n }).map(async () => {
      while (true) {
        const idx = i++;
        if (idx >= items.length) return;
        results[idx] = await fn(items[idx], idx);
        done++;
        if (done % 10 === 0 || done === total) {
          process.stdout.write(`\r  progress: ${done}/${total}`);
        }
      }
    }),
  );
  process.stdout.write("\n");
  return results;
}

// ─── codegen ────────────────────────────────────────────────────────────────
function tsString(s) {
  return JSON.stringify(s ?? "");
}
function tsArray(arr) {
  return `[${(arr ?? []).map(tsString).join(", ")}]`;
}

function emitCompanies(rows) {
  const lines = [
    "// Auto-generated by scripts/enrich-companies.mjs. Do not edit by hand.",
    "// Re-run the script when the prospects table gets new companies.",
    "",
    "export interface GeneratedCompanyMeta {",
    "  country: string;",
    "  state?: string;",
    "  hq_city?: string;",
    "  industry: string;",
    "  employee_count_estimate?: string;",
    "  partnerships?: string[];",
    "  description?: string;",
    "  prospect_count: number;",
    "}",
    "",
    "export const GENERATED_COMPANY_META: Record<string, GeneratedCompanyMeta> = {",
  ];
  for (const r of rows) {
    if (!r.meta) continue;
    const m = r.meta;
    const parts = [
      `country: ${tsString(m.country)}`,
      m.state ? `state: ${tsString(m.state)}` : null,
      m.hq_city ? `hq_city: ${tsString(m.hq_city)}` : null,
      `industry: ${tsString(m.industry)}`,
      m.employee_count_estimate ? `employee_count_estimate: ${tsString(m.employee_count_estimate)}` : null,
      m.partnerships?.length ? `partnerships: ${tsArray(m.partnerships)}` : null,
      m.description ? `description: ${tsString(m.description)}` : null,
      `prospect_count: ${r.count}`,
    ].filter(Boolean);
    lines.push(`  ${tsString(r.name)}: { ${parts.join(", ")} },`);
  }
  lines.push("};", "");
  return lines.join("\n");
}

function emitIndustries(rows) {
  const lines = [
    "// Auto-generated by scripts/enrich-companies.mjs. Do not edit by hand.",
    "",
    "export interface GeneratedIndustryMeta {",
    "  description: string;",
    "  keywords?: string[];",
    "  adjacent_industries?: string[];",
    "  prospect_count: number;",
    "}",
    "",
    "export const GENERATED_INDUSTRY_META: Record<string, GeneratedIndustryMeta> = {",
  ];
  for (const r of rows) {
    if (!r.meta) continue;
    const m = r.meta;
    const parts = [
      `description: ${tsString(m.description)}`,
      m.keywords?.length ? `keywords: ${tsArray(m.keywords)}` : null,
      m.adjacent_industries?.length ? `adjacent_industries: ${tsArray(m.adjacent_industries)}` : null,
      `prospect_count: ${r.count}`,
    ].filter(Boolean);
    lines.push(`  ${tsString(r.name)}: { ${parts.join(", ")} },`);
  }
  lines.push("};", "");
  return lines.join("\n");
}

// ─── main ───────────────────────────────────────────────────────────────────
console.log("→ loading distinct companies from Supabase…");
const companyMap = await loadDistinct();
const companyList = [...companyMap.entries()].map(([name, v]) => ({
  name,
  count: v.count,
  candidateIndustries: v.industries,
}));
companyList.sort((a, b) => b.count - a.count);
console.log(`  ${companyList.length} unique companies`);

const industryCounter = new Map();
for (const v of companyMap.values()) {
  for (const ind of v.industries) {
    industryCounter.set(ind, (industryCounter.get(ind) ?? 0) + v.count);
  }
}
const industryList = [...industryCounter.entries()].map(([name, count]) => ({ name, count }));
industryList.sort((a, b) => b.count - a.count);
console.log(`  ${industryList.length} unique industries`);

// Resume mode: load existing generated files and skip companies/industries
// that already have non-empty meta. Lets us re-run after a partial failure
// without re-querying the LLM for already-enriched entries.
async function loadExisting(path, key) {
  try {
    const txt = readFileSync(path, "utf8");
    const m = txt.match(new RegExp(`export const ${key}[^=]*=\\s*({[\\s\\S]*?});\\s*$`, "m"));
    if (!m) return new Map();
    // Lightly evaluate the object literal — safe because we generated it.
    const obj = new Function(`return ${m[1]}`)();
    return new Map(Object.entries(obj));
  } catch {
    return new Map();
  }
}

const existingCompanies = await loadExisting(resolve("src/lib/company-meta.generated.ts"), "GENERATED_COMPANY_META");
const existingIndustries = await loadExisting(resolve("src/lib/industry-meta.generated.ts"), "GENERATED_INDUSTRY_META");

const todoCompanies = companyList.filter((c) => {
  const e = existingCompanies.get(c.name);
  return !(e && e.country && e.industry);
});
const todoIndustries = industryList.filter((i) => {
  const e = existingIndustries.get(i.name);
  return !(e && e.description);
});
console.log(`  resume: ${companyList.length - todoCompanies.length} companies + ${industryList.length - todoIndustries.length} industries already enriched`);

console.log("\n→ enriching companies (batches of 5, concurrency 2, 120s/call)…");
const companyBatches = chunk(todoCompanies, 5);
const enrichedBatches = todoCompanies.length > 0
  ? await pmap(companyBatches, 2, async (b) => enrichCompanyBatch(b))
  : [];
const newlyEnriched = enrichedBatches.flat();
const enrichedCompanies = companyList.map((c) => {
  const fresh = newlyEnriched.find((n) => n.name === c.name);
  if (fresh && fresh.meta) return fresh;
  const cached = existingCompanies.get(c.name);
  if (cached && cached.country && cached.industry) {
    // Drop prospect_count from cached object — we'll re-emit it from current scan.
    const { prospect_count: _drop, ...m } = cached;
    return { ...c, meta: m };
  }
  return { ...c, meta: null };
});

console.log("\n→ enriching industries…");
const enrichedIndustryBatches = todoIndustries.length > 0
  ? await pmap(chunk(todoIndustries, 12), 2, async (b) => enrichIndustryBatch(b))
  : [];
const newIndustries = enrichedIndustryBatches.flat();
const enrichedIndustries = industryList.map((i) => {
  const fresh = newIndustries.find((n) => n.name === i.name);
  if (fresh && fresh.meta) return fresh;
  const cached = existingIndustries.get(i.name);
  if (cached && cached.description) {
    const { prospect_count: _drop, ...m } = cached;
    return { ...i, meta: m };
  }
  return { ...i, meta: null };
});

const outCo = resolve("src/lib/company-meta.generated.ts");
const outInd = resolve("src/lib/industry-meta.generated.ts");
writeFileSync(outCo, emitCompanies(enrichedCompanies));
writeFileSync(outInd, emitIndustries(enrichedIndustries));
const okCo = enrichedCompanies.filter((c) => c.meta).length;
const okInd = enrichedIndustries.filter((i) => i.meta).length;
console.log(`\n✓ wrote ${outCo} (${okCo}/${companyList.length})`);
console.log(`✓ wrote ${outInd} (${okInd}/${industryList.length})`);
