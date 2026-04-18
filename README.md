# Credence

A trust-and-fit scoring tool for B2B prospects in sensitive industries (starting with semiconductors).

## Stack

- **Frontend** — React + Vite + Tailwind + shadcn/ui. Studio—BA-inspired editorial dark UI.
- **Backend** — Convex (`/convex` folder). Schema, queries, mutations, and actions are defined and runnable with `npx convex dev`.
- **Preview fallback** — when no `VITE_CONVEX_URL` is configured (e.g. inside Lovable preview), the app uses an in-memory mock store (`src/lib/mockStore.ts`) that mirrors the Convex API shape. Swap to real Convex by adding the env var; the UI stays the same.

## Two flows

1. **`/validate`** — Validate a specific person. Submit name/company/role/industry, watch sources query in real-time, get a transparent breakdown.
2. **`/discover`** — Find ICP matches. Filter by industry/company/role, get a ranked list with sub-scores. Click a row → same detail view as Flow 01.

Plus:

- **`/settings`** — Tune signal weights live. Saving recomputes every prospect.
- **Org context tab** (feature-flagged via `VITE_ENABLE_ORG_CHART`) — placeholder org graph on the prospect detail view.

## Run locally

```bash
npm install
cp .env.example .env.local
# (optional) start Convex backend in another terminal
npx convex dev
# start frontend
npm run dev
```

If `VITE_CONVEX_URL` is unset, the app runs entirely against the in-browser mock store with seeded demo prospects. Useful for design iteration and hackathon demos without needing a Convex deployment.

## Architecture

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the signal/score separation and how to add a new data source in <15 minutes.

## Out of scope

Auth, real Apify wiring, email outreach, persistent user accounts.
