# Architecture

Credence's backend is built around three principles:

1. **Source-agnostic signals.** Every external data point becomes a row in the `signals` table tagged with its `source` and `signal_type`. Adding a new data source never requires a schema migration.
2. **Runtime-tunable weights.** The `signal_weights` table maps each signal type to its contribution to the three sub-scores (Authenticity / Authority / Warmth). Scoring code reads weights at runtime — no hardcoded multipliers.
3. **Falsifiable scores.** Every score writes 2–4 `falsification_notes` describing what evidence would invalidate it. This is the product's transparency contract.

## Tables (`convex/schema.ts`)

| Table | Purpose |
|---|---|
| `prospects` | A person we're evaluating. |
| `signals` | Normalized data points from any source. |
| `scores` | Computed sub-scores + overall + falsification notes. |
| `signal_weights` | Per-signal contribution to each sub-score. Editable at `/settings`. |
| `scoring_runs` | Live progress for the `/validate` UX. |

## Service layer (`convex/services/dataSources.ts`)

One function per source. All return a normalized `NormalizedSignal[]` payload:

```ts
{ source, signal_type, value, raw_data, weight, confidence }
```

Currently mocked. Each function carries a `TODO` comment pointing at the exact actor / API to wire.

## Scoring engine (`convex/scoring.ts`)

`computeTrustScore(prospect_id)`:

1. Creates a `scoring_runs` row for live progress.
2. Iterates `SOURCE_FETCHERS`, runs each, and writes returned signals to `signals`.
3. Loads weights from `signal_weights` and signals for the prospect.
4. Normalizes each signal value into 0–100 (sigmoid-ish), multiplies by `weight × confidence × per-signal sub-score weight`, sums, divides by total weight.
5. Combines sub-scores via `OVERALL_WEIGHTS` (`convex/constants.ts`) into `overall_score`.
6. Generates falsification notes based on which sources succeeded and which scores are high.
7. Writes the result to `scores` and marks the run complete.

## Adding a new data source — under 15 minutes

1. Add a new fetcher to `convex/services/dataSources.ts` returning `NormalizedSignal[]`. Use a fresh `signal_type` string for each new signal.
2. Register the fetcher in `SOURCE_FETCHERS` inside `convex/scoring.ts`.
3. Add default weights for any new `signal_type`s to `DEFAULT_SIGNAL_WEIGHTS` in `convex/constants.ts`, then call `signalWeights.seedDefaults` (or just edit them in `/settings`).

That's the entire surface. No schema changes, no UI changes — the `/validate` and `/discover` views automatically reflect the new signals.

## Frontend ↔ backend contract

In production the UI calls Convex via `useQuery` / `useMutation` / `useAction`. The preview build uses `src/lib/mockStore.ts` with the same data shapes; swap by importing from `convex/react` once `VITE_CONVEX_URL` is set.

## Feature flags

- `VITE_ENABLE_ORG_CHART` — shows the placeholder org graph tab on `/prospect/:id`. Real org-fetching is a marked TODO.
