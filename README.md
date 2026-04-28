# Credence

A trust-and-fit scoring tool for B2B prospects in sensitive industries
(starting with semiconductors). Triangulates functional scope across multiple
public evidence sources and produces a score with a full evidence trail.

> **Stage:** demo / hackathon. No auth, billing, or multi-tenancy. Optimized
> for live walk-throughs of the scoring loop, not production hardening.

## What's in the box

- `/discover` — force-directed network graph of ~20k prospects with a chat
  agent on the left rail and a node-aware inspector on the right rail. Every
  person, company, role, city, school, and industry is a node; edges are real
  relationships from the underlying signals.
- `/validate` — search a specific lead by name + company + role + keywords
  and get a ranked top-10 with match-fit and overall scores.
- `/prospect/:id` — deep-dive on a single prospect: big score, sub-score
  breakdown, signal evidence, raw blob, and an org-context tab.
- `/settings` — live editor for the per-signal weights that feed the scoring
  formula. Saving recomputes every prospect.

## Stack

- **Frontend** — React 18 + TypeScript + Vite + Tailwind + shadcn/ui.
  Editorial dark UI. Force graph via `react-force-graph-2d`.
- **Backend** — FastAPI + asyncpg + Pydantic v2 in `server/`. Wraps Z.AI's
  GLM model behind `POST /chat` and `POST /validate`. Same Postgres backing
  store as the frontend (Supabase) — no separate write path.
- **Database** — Supabase Postgres. Schema in `supabase/schema.sql`. Five
  tables: `prospects`, `signals`, `scores`, `signal_weights`, `scoring_runs`.
- **Demo mode** — point the frontend at a static JSON snapshot
  (`src/lib/snapshot.json`, ~30 MB) so a recorded demo runs with zero network
  latency. Toggled via `VITE_USE_SNAPSHOT=true`.

## Run locally

```bash
npm install
cp .env.example .env.local        # frontend env
npm run dev                       # http://localhost:8080

# (optional) backend for live LLM-driven chat
cd server
uv run uvicorn main:app --reload  # http://localhost:8000
```

The frontend works **without** the backend (it short-circuits to a canned
response router for the demo prompts). Set `VITE_API_URL=http://localhost:8000`
to wire up the live model.

### Environment variables

```bash
# Frontend (browser)
VITE_SUPABASE_URL=...               # set both → live Supabase reads
VITE_SUPABASE_ANON_KEY=...          # leave both empty → in-memory mock store
VITE_USE_SNAPSHOT=true              # read bulk tables from snapshot.json
VITE_API_URL=http://localhost:8000  # FastAPI base for /chat and /validate
VITE_ENABLE_ORG_CHART=true          # show the org-context tab on /prospect

# Backend (server-only — never the frontend)
DATABASE_URL=postgres://...
ZAI_API_KEY=...
ZAI_BASE_URL=https://api.z.ai/api/paas/v4

# Source-fetch credentials (FastAPI scoring functions only)
APIFY_TOKEN=...
USPTO_API_KEY=...
GITHUB_TOKEN=...
CRUNCHBASE_API_KEY=...
```

## Scripts

```bash
npm run dev          # Vite dev server
npm run build        # production build
npm run lint         # ESLint
npm test             # Vitest (one-shot)
npm run test:watch   # Vitest watch
```

Refresh the offline snapshot:

```bash
node scripts/snapshot-supabase.mjs   # writes src/lib/snapshot.json
```

## Architecture

See `CLAUDE.md` for the signal/score separation, how the dual-mode data layer
works, and the seams for adding new data sources. The high-level summary:

1. **Signals** — one row per source per signal type. Normalized scalar value
   plus raw JSON blob. Confidence and per-signal weight overrides allowed.
2. **Weights** — per-signal contribution to each of the three sub-scores.
   Editable live at `/settings`. Default weights seeded by `schema.sql`.
3. **Scoring** — sigmoid-normalize each signal value, weighted-sum into
   sub-scores, combine `0.4·auth + 0.4·authority + 0.2·warmth → overall`,
   emit falsification notes describing what would invalidate each sub-score.

## Out of scope

Auth, real-time Apify scraping in-app, email outreach, multi-tenant accounts.
