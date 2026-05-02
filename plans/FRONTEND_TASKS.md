# Frontend Overhaul — Task List

> Generated 2026-05-02. Owner: LavenderPrairie. Coordinated with SwiftElk (clustering surface), SunnyRidge (frontend surface), DarkBeaver (org-chart pipeline).

## Status legend
- 🟢 done
- 🟡 in-flight (subagent or active session)
- ⚪ blocked / waiting on coordination
- ⏳ queued
- ❌ rejected / out of scope

---

## Coordination contract

Before any task touches **SunnyRidge-owned files**, send a coordination message + `file_reservation_paths` request and wait for ack:
- `src/lib/db.ts`
- `src/lib/graph.ts`
- `src/lib/strength.ts` (likely SR)
- `src/store/graphStore.ts`
- `src/components/GraphCanvas.tsx`
- `src/components/GraphChat.tsx`
- `src/lib/warmPaths.ts`
- `useSupaPersonConnections` and other custom hooks

Files that **LavenderPrairie can edit unilaterally** (per session-history ownership):
- `src/components/NodeInspector.tsx` (verified open — last touched by SR but is shared)
- New components under `src/components/` that don't currently exist
- New pages under `src/pages/` that don't currently exist
- New libs under `src/lib/` that don't currently exist
- `src/App.tsx` (small additive route changes only — coordinate before structural)
- `FRONTEND_TASKS.md` (this file)

---

## Phase A — Make the graph usable + surface enrichment

### A1 · TopBar filter pills · ⚪ blocked on SR
- Touches `src/components/TopBar.tsx` (likely SR-owned)
- New: `src/components/EdgeFilterPills.tsx`
- One toggle pill per `EDGE_CONFIGS` entry, grouped by category (Warm / Career / Education / Structural)
- Per-pill: edge color swatch, label, live-count of edges of that type, on/off toggle bound to graphStore.toggleEdgeKind
- Buttons: "Show all" / "Hide all" / "Reset"
- ETA: 4 hr

### A2 · PersonProfileCard component · ⏳ green-field, parallelizable
- New: `src/components/PersonProfileCard.tsx`
- Sections:
  - Identity: avatar, name, headline, role + company, location + country flag
  - Badges: Premium / Verified / Open to Work / Hiring (from `persons` columns)
  - Reach: connections_count, followers_count, registered_at
  - Contact: email + email_status pill (verified/unverified/guessed), LinkedIn URL → external link
- Pure presentational; no data fetching
- ETA: 4 hr

### A3 · CareerTimeline + EducationTimeline components · ⏳ green-field, parallelizable
- New: `src/components/CareerTimeline.tsx`
- New: `src/components/EducationTimeline.tsx`
- CareerTimeline: vertical sorted-desc by start_year, title + company + dates + duration + is_current badge
- EducationTimeline: school + degree + dates
- Both take props (employment_periods[] / education_periods[]) — no data fetching inside
- ETA: 4 hr

### A4 · SkillsChipCloud component · ⏳ green-field, parallelizable
- New: `src/components/SkillsChipCloud.tsx`
- Top N skills as chips, "+N more" expand to full list
- Pure presentational
- ETA: 2 hr

### A5 · NodeInspector person variant integration · ⚪ depends on A2/A3/A4
- Touches: `src/components/NodeInspector.tsx` (shared)
- Wire PersonProfileCard, CareerTimeline, EducationTimeline, SkillsChipCloud into the person variant
- Add `useEmploymentEducation(personId)` hook (touches db.ts → coordinate with SR)
- Add `useSkillsFor(personId)` hook
- ETA: 4 hr

### A6 · ProspectDetail full mirror · ⚪ depends on A2/A3/A4
- Touches: `src/pages/ProspectDetail.tsx`
- Same components as A5 but on the standalone detail page
- ETA: 3 hr

---

## Phase B — Org chart as first-class view

### B1 · OrgChart page · ⏳ green-field
- New: `src/pages/OrgChart.tsx`
- Route: `/org/:companyId`
- ReactFlow rendering of org_reporting_edges for the company
- Layout: dagre top-down with seniority on Y axis
- Click person node → navigate to `/prospect/:id`
- ETA: 6 hr

### B2 · Functional cluster overlay · ⏳ green-field
- New: `src/lib/orgClusters.ts`
- Color-code person nodes by org_functional_clusters.functional_domain
- Legend in corner; toggle to collapse cluster as super-node
- ETA: 4 hr

### B3 · Confidence shading + correction affordance · ⏳
- Edges: solid for path_confidence ≥ 0.7, dashed for 0.4–0.7, dotted+gray for <0.4
- Edges: thicker for is_current, thinner for historical
- Per-edge hover: confidence %, inference_method, valid_from/valid_to
- Per-edge click: opens existing OrgCorrectionDialog
- ETA: 4 hr

### B4 · CompanyHeaderCard · ⏳ green-field
- New: `src/components/CompanyHeaderCard.tsx`
- At top of /org/:companyId — name, logo, total persons, % to 500, top 5 highest-scoring people
- ETA: 2 hr

### B5 · App.tsx route addition · ⚪ small additive change
- Add `/org/:companyId` route to lazy router
- ETA: 30 min

---

## Phase C — Edge-level evidence drilldown

### C1 · EdgeInspector component · ⏳ green-field
- New: `src/components/EdgeInspector.tsx`
- Shows source person → target person, edge kind, base_strength, recency_factor, frequency_factor, corroboration_factor, computed_strength
- Bottom panel: list of connection_evidence rows backing this edge with structured fields per source_type
- "Use this connection" copies opener
- ETA: 5 hr

### C2 · Wire edge-click on canvas · ⚪ blocked on SR (graphStore + GraphCanvas)
- Touches: `src/store/graphStore.ts`, `src/components/GraphCanvas.tsx`
- react-force-graph-2d's onLinkClick → setSelectedEdgeId
- Inspector switches between person/aggregation/edge variants based on selection
- ETA: 2 hr

---

## Phase D — Coverage / discovery surfaces

### D1 · /companies page · ⏳ green-field
- New: `src/pages/Companies.tsx`
- Table of all 59 target cos + the ~110 with ≥50 enriched persons
- Columns: name, tier, enriched_count, % to 500, distinct edge types, # warm paths, last enriched
- Sort + filter; click row → /org/:companyId
- ETA: 6 hr

### D2 · /people page upgrade · ⚪ depends on data layer
- Touches: existing Validate or new People.tsx
- Filters: company multi-select, seniority range, functional domain, has-email, country
- Bulk actions: send to validation, export CSV
- ETA: 6 hr

### D3 · WeightHistory · ⏳ green-field
- New: `src/components/WeightHistory.tsx`
- Timeline of last N weight versions + their scoring impact
- ETA: 3 hr

---

## Phase E — Polish

### E1 · Demo mode reskin · ⏳
- Update `src/lib/demoData.ts` to seed from real graph snapshots
- ETA: 3 hr

### E2 · Loading / error states · ⏳
- All new pages need skeleton + error fallback
- ETA: 2 hr

### E3 · Tests · ⏳
- warmPathsForCompany, useCompanyCoverage, EdgeInspector
- ETA: 4 hr

---

## Total estimate: ~55 hr of focused work

## Active subagents

(Updated each cron tick.)

---

## Status log

(Updated each cron tick. Tail of recent broadcasts.)
