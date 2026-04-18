# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**Credence** — a trust-and-fit scoring tool for B2B prospects in sensitive industries (starting with semiconductors). Triangulates functional scope across multiple evidence sources and produces a score with a full evidence trail.

**Current stage:** demo-only. No auth, billing, or multi-tenancy. Optimize for demo credibility, not production hardening.

## Commands

```bash
npm install
npm run dev          # Start Vite frontend (http://localhost:5173)
npm run build        # Production build
npm run lint         # ESLint
npm test             # Vitest (run once)
npm run test:watch   # Vitest (watch mode)
```

Run a single test file:
```bash
npx vitest run src/path/to/file.test.ts
```

Tests live in `src/**/*.{test,spec}.{ts,tsx}` and use jsdom + @testing-library/react.

## Architecture

### Dual-mode data layer

The app runs in two modes controlled by `VITE_SUPABASE_URL` + `VITE_SUPABASE_ANON_KEY`:

- **With credentials set**: real Supabase backend via `@supabase/supabase-js`. The `supabase` client is exported from `src/lib/supabase.ts`.
- **Without credentials** (demo/offline): in-memory mock store at `src/lib/mockStore.ts` with seeded demo prospects. Same data shapes as Supabase. All pages currently use this path.

The switch is `HAS_REAL_SUPABASE` exported from `src/lib/supabase.ts`.

### Database (`supabase/schema.sql`)

Apply once via Supabase SQL editor or `supabase db push`. Five tables:

| Table | Purpose |
|---|---|
| `prospects` | Person being evaluated |
| `signals` | Normalized data points — one row per source per signal type |
| `scores` | Computed sub-scores (Authenticity / Authority / Warmth) + overall |
| `signal_weights` | Per-signal contribution to each sub-score; editable live at `/settings` |
| `scoring_runs` | Live progress tracking for the `/validate` real-time UX |

Default weights are seeded by the `INSERT … ON CONFLICT DO NOTHING` block at the bottom of `schema.sql`.

### Scoring logic (lives in `src/lib/mockStore.ts` until server-side wiring)

`computeScore(prospect_id)` pipeline:
1. Loads signals for the prospect and weights from `signal_weights`
2. Normalizes each signal value 0–100 via sigmoid: `100 * (1 - exp(-v/15))`
3. Weighted sum per sub-score: `Σ(normalized × weight × confidence × sub_score_weight)`
4. Combines sub-scores: Authenticity 40% + Authority 40% + Warmth 20%
5. Appends four falsification notes describing what would invalidate each sub-score

### Adding a new data source

1. Add the source name to `ALL_SOURCES` and `SOURCE_TO_SIGNALS` in `src/lib/mockStore.ts`
2. Add default weights for any new `signal_type` to the `signal_weights` seed block in `supabase/schema.sql`
3. When wiring real fetchers server-side, mirror the `NormalizedSignal` shape: `{ source, signal_type, value, raw_data, weight, confidence }`

### Frontend (`src/`)

- `src/pages/` — `Validate.tsx`, `Discover.tsx`, `ProspectDetail.tsx`, `Settings.tsx`
- `src/components/` — layout (`PageShell`, `TopBar`) and domain primitives (`ScoreBar`, `HeroMark`)
- `src/components/ui/` — shadcn/ui primitives (generated; don't hand-edit)
- `src/lib/supabase.ts` — Supabase client + `HAS_REAL_SUPABASE` + `ENABLE_ORG_CHART` flags
- `src/lib/mockStore.ts` — in-memory store with seeded data for demo mode
- `src/lib/database.types.ts` — TypeScript types mirroring the Supabase schema (regenerate with `npx supabase gen types typescript`)

Router is `react-router-dom` v6 in `src/App.tsx`.

## Feature Flags

```
VITE_SUPABASE_URL        # Empty = mock mode; set = real Supabase
VITE_SUPABASE_ANON_KEY   # Required alongside VITE_SUPABASE_URL
VITE_ENABLE_ORG_CHART    # Shows placeholder org graph tab on /prospect/:id (default: true)
```

External source credentials (server-side scoring functions only, never the frontend):
```
APIFY_TOKEN
USPTO_API_KEY
GITHUB_TOKEN
CRUNCHBASE_API_KEY
```

Copy `.env.example` → `.env.local` to start.
