# Repo Cleanup & Organization Plan

> **Author:** SwiftElk
> **Date:** 2026-05-02
> **Status:** 📝 Awaiting approval — no destructive actions taken yet
> **Estimated effort:** 30 min execution + 5 min review per category
> **Goal:** Reduce repo-root clutter, consolidate scattered docs, clarify what's
> living vs archived, and codify the rule of where new files belong.

---

## Why now

Repo root currently has **23 top-level files + 8 directories**. A new contributor (human or agent) can't tell at a glance which docs are evergreen, which are deferred plans, and which are stale. CLAUDE.md says to read this whole repo before touching code — but half of it is historical drift.

---

## Inventory (current root)

### ✅ Keep at root (standard project plumbing — don't move)

| File | Reason |
|---|---|
| `package.json`, `bun.lock` | Node manifest |
| `tsconfig.json`, `tsconfig.app.json`, `tsconfig.node.json` | TS config (must be at root) |
| `vite.config.ts`, `vitest.config.ts` | Vite/Vitest config |
| `postcss.config.js`, `tailwind.config.ts` | CSS toolchain |
| `eslint.config.js` | Linter |
| `components.json` | shadcn/ui registry |
| `index.html` | Vite entry |
| `playwright.config.ts` | E2E config |
| `vercel.json` | Deploy config |
| `.gitignore`, `.env.example`, `.env.local`, `.mcp.json` | Standard |
| `README.md` | Project entry doc |
| `CLAUDE.md` | Auto-loaded by Claude Code, must stay at root |

### ✅ Keep at root (load-bearing engineering docs)

| File | Lines | Reason |
|---|---:|---|
| `CONTRACTS.md` | 1,345 | Cross-team interface authority. Referenced from every plan + most files. |
| `DEMO_CASES.md` | 336 | YC demo source-of-truth. Referenced by `CLAUDE.md` "Common Mistakes #6". |
| `ARCHITECTURE.md` | 55 | Short, evergreen high-level overview. |

### 📁 Move into `plans/` (living plan docs)

Already moved 2026-05-02:
- ✅ `COMPANY_ENRICHMENT_PLAN.md`
- ✅ `CHAT_TOOLS_PLAN.md`
- ✅ `MULTITENANT_PLAN.md`
- ✅ `plan.md` → `plans/ORGCHART_REDESIGN_PLAN.md`

Still to move (LP WIP — needs LP coordination first):
- `ICP_ENRICHMENT_PLAN.md` → `plans/ICP_ENRICHMENT_PLAN.md`
- `CUSTOMER_ONBOARDING_PLAN.md` → `plans/CUSTOMER_ONBOARDING_PLAN.md`

Still to move (active task lists treated as plans):
- `FRONTEND_TASKS.md` → `plans/FRONTEND_TASKS.md` (LP-owned; ping LP)
- `PROSPECT_ENRICHMENT_TASK.md` → `plans/PROSPECT_ENRICHMENT_TASK.md` (1,439 lines, active)
- `V3_PT2.md` → `plans/V3_PT2_PLAN.md` (contains the v3.1 org-chart + hidden-connections plan)

### 🗄️ Move into `plans/archive/` (historical, no longer driving work)

Create `plans/archive/` for superseded docs. Keeps history searchable but unclutters the active plan list.

| File | Why archive |
|---|---|
| `credence_2.0.md` | "v2 graph chat pivot" — superseded by v3 (the current Credence). |
| `orgchart_tasks.md` | Superseded by `plans/ORGCHART_REDESIGN_PLAN.md` (DB's redesign). |
| `SURYA.md` | One-time handoff doc to a contractor named Surya. Keep as audit trail, not as active reference. |

### 🗑️ Delete

| File | Reason |
|---|---|
| `orgchart_tasks.docx` | Word duplicate of `orgchart_tasks.md`. Markdown is the source-of-truth. |

(No other deletions — when in doubt, archive instead of delete.)

### 🔍 Verify (before touching)

| Path | What to check |
|---|---|
| `dist/` | Should NOT be tracked. `.gitignore` excludes `dist`. Run `git ls-files dist/` — if any tracked files leak, remove them with `git rm --cached`. |
| `tests/` vs `server/tests/` | Frontend has `tests/` (1 file: `tests/orgchart/data_quality/test_no_cycles.py` — Python?), backend has `server/tests/` (~50 Python suites). The `tests/` at root is mis-located if it's Python; consolidate into `server/tests/`. |
| `e2e/` | Confirm Playwright suite is still maintained / referenced from CI. |

---

## Proposed final root layout

```
credence/
├── CLAUDE.md                  # Claude Code context (must stay at root)
├── README.md                  # Project entry doc
├── CONTRACTS.md               # Cross-team interface authority
├── ARCHITECTURE.md            # Evergreen high-level overview
├── DEMO_CASES.md              # YC demo source-of-truth
│
├── package.json + bun.lock
├── tsconfig*.json
├── vite.config.ts + vitest.config.ts + playwright.config.ts
├── postcss.config.js + tailwind.config.ts + eslint.config.js
├── components.json
├── index.html
├── vercel.json
├── .gitignore + .env.example + .env.local + .mcp.json
│
├── src/                       # Frontend (React + Vite)
├── server/                    # Backend (FastAPI + asyncpg)
├── supabase/                  # Migrations
├── public/                    # Vite static assets
├── e2e/                       # Playwright tests
├── tests/                     # Move root-level tests into server/tests/
├── scripts/                   # CI / one-off scripts
│
├── plans/
│   ├── INDEX.md
│   ├── CLEANUP_PLAN.md         # this doc
│   ├── CHAT_TOOLS_PLAN.md
│   ├── COMPANY_ENRICHMENT_PLAN.md
│   ├── CUSTOMER_ONBOARDING_PLAN.md
│   ├── FRONTEND_TASKS.md
│   ├── ICP_ENRICHMENT_PLAN.md
│   ├── MULTITENANT_PLAN.md
│   ├── ORGCHART_REDESIGN_PLAN.md
│   ├── PROSPECT_ENRICHMENT_TASK.md
│   ├── V3_PT2_PLAN.md
│   └── archive/
│       ├── credence_2.0.md
│       ├── orgchart_tasks.md
│       └── SURYA.md
│
├── node_modules/              # gitignored
└── dist/                      # gitignored
```

---

## Execution order (when approved)

1. **Verify dist/ + tests/ + e2e/** (read-only probes; no changes)
2. **Coordinate via Agent Mail** — broadcast intent, give LP/DB 24h to flag conflicts
3. **Create `plans/archive/`** + git mv the 3 archived files
4. **git mv** `FRONTEND_TASKS.md`, `PROSPECT_ENRICHMENT_TASK.md`, `V3_PT2.md` → `plans/`
5. **git rm** `orgchart_tasks.docx`
6. **Update `plans/INDEX.md`** with the newly moved plans + archive section
7. **Coordinate with LP** to git mv her 2 untracked plans (or she does it herself)
8. **Single squash commit:** `chore(repo): consolidate plan + task docs into /plans, archive superseded docs`

---

## Out of scope (intentionally)

- **Renaming files for casing consistency** beyond what we've already done. The mix of `UPPER_CASE.md` and `Title_Case.md` is annoying but not a real problem.
- **Splitting `CLAUDE.md`** (1,060 lines). It's auto-loaded; splitting forces `@import` chains. Live with the size.
- **Cleaning `node_modules` / regenerating `bun.lock`** — out of scope, already gitignored.
- **Reorganizing `src/components/`** (~50 files, well-grouped already).
- **Reorganizing `server/credence/`** subpackages — recently restructured by DB, still settling.

---

## Definition of Done

- [ ] `ls *.md` at root returns ≤6 files (CLAUDE, README, CONTRACTS, ARCHITECTURE, DEMO_CASES, plus this PR's commit message-only docs if any).
- [ ] All plan docs are inside `plans/` and listed in `plans/INDEX.md`.
- [ ] Archived docs are inside `plans/archive/` with a one-line note in the archive index explaining what superseded them.
- [ ] `git ls-files dist/` returns nothing.
- [ ] One commit with `git mv` (history preserved) and one for `git rm` if any.
- [ ] Broadcast notification on `agent-mail` so other sessions don't hunt for the moved files.
