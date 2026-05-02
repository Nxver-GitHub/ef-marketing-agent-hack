# Plans Index

Compiled 2026-05-02 by SwiftElk. All future plan documents land here, not the
repo root. Use `UPPER_SNAKE_CASE_PLAN.md` for the filename.

## Active plans

| Plan | Author | Date | Status | Effort | Topic |
|---|---|---|---|---:|---|
| [CHAT_TOOLS_PLAN.md](CHAT_TOOLS_PLAN.md) | LavenderPrairie | 2026-05-02 | ✅ Shipped | 14 hr | Add `find_warm_paths` + `get_org_context` chat tools to surface BFS warm paths and org-chart context. |
| [COMPANY_ENRICHMENT_PLAN.md](COMPANY_ENRICHMENT_PLAN.md) | LavenderPrairie | 2026-05-01 | 🚧 In flight (DarkBeaver) | 18 hr | Real company context for the chat agent — descriptions, executives, recent press, Firecrawl extraction. |
| `CUSTOMER_ONBOARDING_PLAN.md` (still at repo root, LP WIP) | LavenderPrairie | 2026-05-02 | 📝 Approved | 28 hr | Auto-discover the rep's team and seed warm-path coverage on signup, zero manual config. Move into `plans/` once LP commits at root. |
| `ICP_ENRICHMENT_PLAN.md` (still at repo root, LP WIP) | LavenderPrairie | 2026-05-02 | 📝 Approved | 20 hr | Give the chat agent a full picture of who the rep is and what they sell — every recommendation filtered through their ICP. Move into `plans/` once LP commits at root. |
| [MULTITENANT_PLAN.md](MULTITENANT_PLAN.md) | LavenderPrairie | (in progress) | 🚧 Phases shipping | — | Phased roadmap to make Credence multi-tenant production-ready. M1.5 RLS applied; M2 + M3 shipped. |
| [ORGCHART_REDESIGN_PLAN.md](ORGCHART_REDESIGN_PLAN.md) | DarkBeaver | 2026-05-01 | 🚧 Phase D in flight | — | Org-chart inference engine — current state, gaps, redesign across phases. |
| [PROSPECT_ENRICHMENT_TASK.md](PROSPECT_ENRICHMENT_TASK.md) | LavenderPrairie | 2026-05-01 | 🚧 Active | — | Expand prospect coverage from 20k records (1k enriched) to full org-chart coverage across semiconductor/defense/aerospace verticals. |
| [V3_PT2_PLAN.md](V3_PT2_PLAN.md) | (multi-author) | 2026-05-01 | 🚧 In flight | — | V3.1 work — org-chart optimization pipeline + expanded hidden-connections (education, conference, cohort bonds). |
| [FRONTEND_TASKS.md](FRONTEND_TASKS.md) | LavenderPrairie | 2026-05-02 | 🚧 Active task list | — | Frontend overhaul task list, coordinated across SwiftElk/SunnyRidge/DarkBeaver. |

## Meta

| Plan | Description |
|---|---|
| [CLEANUP_PLAN.md](CLEANUP_PLAN.md) | Repo-root reorganization proposal — most of it executed 2026-05-02 in commit `chore(repo): consolidate plans + archive superseded docs`. |

## Archived

See [`archive/INDEX.md`](archive/INDEX.md) for the index of superseded plans
that are kept for audit trail but no longer drive work.

## Conventions

- **One plan per discrete initiative.** No catch-all roadmaps. If you need a
  cross-cutting tracker, link multiple plans from a section in `CONTRACTS.md`.
- **Header block on every plan** with `Author`, `Date`, `Status`, `Estimated effort`
  fields. Status is one of: `📝 Approved · 🚧 In flight · ✅ Shipped · ❌ Cancelled`.
- **Definition of Done section.** Every plan ends with a checklist of testable
  criteria. Plans without a DoD are aspirational, not actionable.
- **Update status in-place.** When a plan ships, edit the `Status` field;
  don't move/delete the file. The historical record matters for audits.

## Why a subfolder

Repo root was getting noisy — 6 plan files mixed with `CLAUDE.md`,
`CONTRACTS.md`, `DEMO_CASES.md`, `HANDOVER.md`, `SKILL.md`, etc. Easier to
spot a new plan when they all live together. `git log --follow plans/` shows
plan-history in one query.
