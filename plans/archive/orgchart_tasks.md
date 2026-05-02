# Credence — Org Chart Inference: Claude Code Task Specifications

> Phased implementation plan · 4 phases · 10 discrete tasks
>
> Phases 1 and 2 can run in parallel with each other. Phase 3+4 requires Phase 1 complete.

| Phase 0 | Phase 1 | Phase 2 | Phase 3 + 4 |
|---|---|---|---|
| Unblock | Fix Inference Engine | Explicit Signals | Frontend + Feedback Loop |
| Sequential · do first | 3 parallel subagents | 3 parallel subagents | 4 parallel subagents |

---

## Honest Baseline — What Runs Today

Before writing any code, orient to what actually exists:

| System | What it does | Status | User-visible? |
|---|---|---|---|
| v2 ProspectDetail | Seniority-rank sort → ReactFlow lines between buckets. No edge inference. Decorative. | Live on /prospect/:id | Yes — but misleading |
| v3.1 pipeline (6 modules) | clustering → hierarchy → scope → propagation → validation → corrections. Writes org_reporting_edges. | Built, migrations pending | No — not wired to frontend |
| Signal substrate | persons / companies / employment_periods / patent_inventors / paper_authors. Extractors emit signals. | Partially live | Indirectly (v2 scoring) |

The six systemic issues (Issue 1–6) from the design review all stem from the same root: the v3.1 pipeline was designed correctly at the module level but (a) uses greedy local assignment instead of global tree optimization, (b) has no explicit signal producers despite protecting the interface, (c) never writes temporal state, (d) silently drops unknowns, (e) has coarse feedback attribution, and (f) has no uncertainty rendering. The phases below fix them in dependency order.

---

## Phase 0 — Unblock

**Single sequential agent · Do this before anything else · Est. 30 min**

Assign to a single Claude Code agent. Both tasks must pass before any Phase 1 agents start.

### Task 0-A · Apply pending migrations A0 + B1

| Field | Detail |
|---|---|
| **File(s)** | `supabase/migrations/` — locate A0_orgchart_schema.sql and B1_connection_graph.sql (or equivalent) |
| **Action** | Run: `supabase db push` (or `psql -f migration.sql` against your Supabase project URL). Verify all 9 new tables exist: `org_reporting_edges`, `org_functional_clusters`, `org_cluster_members`, `person_scope_estimates`, `org_chart_corrections`, `org_signal_performance`, `person_connections`, `connection_evidence`. Also add `score_components` JSONB column to `org_reporting_edges` and `dominant_signal` VARCHAR column now — needed in Phase 4. |
| **Done when** | `SELECT table_name FROM information_schema.tables WHERE table_schema='public'` returns all 9 tables. Zero errors. |
| **If migrations are missing** | Check CONTRACTS.md for the schema definition. Write the migration SQL by hand from the schema spec. Do not invent columns — copy exactly from CONTRACTS.md and CLAUDE.md data model section. |

### Task 0-B · Smoke-test all 6 pipeline module imports

| Field | Detail |
|---|---|
| **File(s)** | `server/credence/orgchart/` — all 6 modules |
| **Action** | Run: `python -c "from server.credence.orgchart import clustering, hierarchy, scope, propagation, validation, corrections; print('all imports OK')"` |
| **Done when** | Prints `all imports OK` with zero tracebacks. Fix any ImportError before proceeding. |
| **Common failure modes** | Missing `__init__.py`, incorrect relative imports, missing env vars for Supabase client. Fix each in turn. Do NOT proceed to Phase 1 with broken imports. |

---

## Phase 1 — Fix the Inference Engine

**3 parallel subagents · Depends on Phase 0 · Each owns one file**

These three tasks fix the three deepest issues in hierarchy.py. They can run in parallel because Tasks 1-B and 1-C are additive changes on top of the structure Task 1-A rewrites. Merge order: 1-A first, then 1-B and 1-C.

### Task 1-A · Rewrite hierarchy.py — global constrained tree assignment

**This is the highest-leverage fix in the entire plan. Fixes Issue 1 from the design review.**

| Field | Detail |
|---|---|
| **File** | `server/credence/orgchart/hierarchy.py` |
| **Problem** | Current `_assign_manager_for_person()` runs per-person greedy: for each report, score every other cluster member, take the highest scorer. Produces cycles, orphans high-seniority nodes with no upward candidate, and span-cap reroute is single-pass only. |
| **Fix — algorithm** | Replace the per-person loop with global constrained assignment:<br><br>1. Build candidate edge set: for every (manager_candidate, report_candidate) pair in the same cluster where `manager.seniority_score > report.seniority_score` AND `ic_compat(manager, report)`, compute score via existing `_score_pair()`.<br>2. Sort all candidate edges by score descending.<br>3. Greedily assign: take each candidate edge if and only if: (a) report has no manager yet, (b) adding this edge does not create a cycle (union-find check), (c) manager has not hit `span_cap(manager.seniority_score)`. Skip otherwise.<br>4. After assignment: any person with no assigned manager and `seniority_score < 65` becomes orphan (log, do not error).<br>5. Any person with no assigned manager and `seniority_score >= 65` should be tried against the cross-cluster parent (one tier up in clustering.py output). If still unresolved, orphan. |
| **Interface — keep the same** | `build_org_chart(company_id: str, cluster_members: list[ClusterMember]) -> list[OrgReportingEdge]`. Same return type. The caller does not change. |
| **IC track rule** | `ic_compat(manager, report)`: returns `False` if manager is on IC track (title contains Distinguished/Principal/Staff Engineer) AND report is on management track (title contains Manager/Director/VP). IC and management tracks are peers at the same seniority level — ICs do not manage managers. See CLAUDE.md seniority taxonomy. |
| **Span cap values** | `seniority >= 85`: 8 reports. `>= 75`: 7. `>= 65`: 8. `>= 55`: 10. `< 55`: 12. Defined in CLAUDE.md — do not change them. |
| **Cycle detection** | Use union-find (disjoint set). Before adding edge (manager → report), check that manager and report are not already in the same set. After adding, union them. O(n·α(n)) and handles all cycle cases. |
| **Test condition** | Given 5 persons in one cluster: VP (seniority=70), Dir-A (60), Dir-B (60), Mgr-A (50, reports to Dir-A by patent cluster), Mgr-B (50, reports to Dir-B by domain). Expect: VP has 2 reports (Dir-A, Dir-B). Dir-A has 1 report (Mgr-A). Dir-B has 1 report (Mgr-B). No cycles. No orphans. Distinguished Engineer at seniority=55 does not appear as manager of any Director. |
| **Done when** | Unit test passes. Run: `python -m pytest server/tests/test_hierarchy.py -v`. If test file does not exist, write it as part of this task. |

### Task 1-B · Wire temporal model into all edge writes

| Field | Detail |
|---|---|
| **File** | `server/credence/orgchart/hierarchy.py` (edge write section) + `propagation.py` (if it re-writes edges) |
| **Problem** | All edges write `is_current=TRUE, valid_from=NULL, valid_to=NULL`. When a re-run happens, `ON CONFLICT DO UPDATE` silently overwrites history. |
| **Fix** | Change the upsert to a two-step historicization:<br><br>**Step 1** — before writing a new edge, check if a current edge exists for the same `(manager_person_id, report_person_id, company_id)`. If it does AND the new edge has a different `confidence` or `inference_method`:<br>`UPDATE org_reporting_edges SET is_current=FALSE, valid_to=NOW() WHERE ... AND is_current=TRUE`<br><br>**Step 2** — `INSERT` new edge with `is_current=TRUE, valid_from=NOW(), valid_to=NULL`.<br><br>If the new edge is identical (same manager, same report, same inference_method, confidence within 0.02), skip the write — no change needed. |
| **valid_from source** | Use the signal's `created_at` timestamp when available. Fall back to `NOW()` when inferring from `employment_periods` without a signal timestamp. |
| **Query performance** | Add index if not present: `CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_org_edges_current ON org_reporting_edges(company_id, is_current) WHERE is_current=TRUE;` |
| **Test condition** | 1. Insert edge A→B with `confidence=0.72`, `inference_method='implicit_scoring'`. 2. Re-run hierarchy with same company, producing A→C instead. 3. Assert: old A→B row has `is_current=FALSE`, `valid_to IS NOT NULL`. 4. New A→C row has `is_current=TRUE`, `valid_from IS NOT NULL`, `valid_to IS NULL`. |
| **Done when** | Test passes. No existing edges are deleted on pipeline re-run — they are historicized. |

### Task 1-C · Implement unknown node stub generation

**Fixes Decision 4 from CLAUDE.md: "Unknown nodes are rendered, not omitted." Currently violated everywhere.**

| Field | Detail |
|---|---|
| **File** | `server/credence/orgchart/hierarchy.py` — `ingest_explicit_edge()` function |
| **Problem** | When a job posting says "reports to VP of Manufacturing" and no person in the DB matches that title, the edge is silently dropped. |
| **Fix — stub creation** | When `ingest_explicit_edge()` receives a `manager_title` (string) rather than a resolved `manager_person_id`, and entity resolution returns no match:<br><br>1. Check if a stub already exists: `SELECT id FROM persons WHERE company_id=$company_id AND canonical_name='[Unknown ' \|\| $title \|\| ']' AND is_unresolved_target=TRUE LIMIT 1`<br>2. If no stub: `INSERT INTO persons (canonical_name, is_unresolved_target, current_company_id, current_title, enrichment_tier) VALUES ('[Unknown ' \|\| $title \|\| ']', TRUE, $company_id, $title, 0) RETURNING id`<br>3. Use stub id as `manager_person_id` and proceed with the edge write.<br>4. Confidence for stub-target edges: multiply normal confidence by 0.7. Record `inference_method` as `$original_method + '_unresolved_target'`. |
| **Stub naming convention** | Always: `'[Unknown ' + original_title + ']'`. Examples: `[Unknown VP of Manufacturing]`, `[Unknown Director of Verification]`. Square brackets required — the UI uses them to detect stubs. |
| **Test condition** | Call `ingest_explicit_edge(company_id='test-co', report_person_id='known-uuid', manager_title='VP of Manufacturing', confidence=0.85, inference_method='job_posting')`. Assert: `persons` table has a new row with `is_unresolved_target=TRUE` and `canonical_name='[Unknown VP of Manufacturing]'`. Assert: `org_reporting_edges` has an edge from `known-uuid → stub_id` with `confidence ≈ 0.595` (0.85 × 0.7). |
| **Done when** | Test passes. Also update `clustering.py`: remove the early-return that skips `is_unresolved_target` persons — stubs must pass through clustering into the org chart. |

---

## Phase 2 — Wire Explicit Signal Producers

**3 parallel subagents · Can run in parallel with Phase 1 · `ingest_explicit_edge()` interface must be stable first (defined in CONTRACTS.md — use that)**

Decision 3 in CLAUDE.md: "Explicit signals override implicit scoring." The interface is protected but `ingest_explicit_edge()` has no callers. These three tasks create the callers.

> **Read CONTRACTS.md before writing any extractor.** The interface contract for explicit signals is already defined there. Implement it exactly. Do not modify CONTRACTS.md during implementation.

### Task 2-A · LinkedIn reports_to parser (Apollo enrichment payload)

| Field | Detail |
|---|---|
| **File** | `server/credence/extractors/apollo.py` (add to existing file, do not create new file) |
| **Where to look** | The Apollo enrichment payload lands in signals as `signal_type='person_enrichment'`. Run: `SELECT structured_value FROM signals WHERE signal_type='person_enrichment' LIMIT 3` and inspect for `reports_to`, `manager`, or `employment_history[].manager` fields. **API documentation is stale — inspect actual payload first.** |
| **Parse logic** | After confirming payload shape with REPL, implement `def extract_reporting_from_apollo_payload(payload: dict, person_id: str, company_id: str) -> list[ReportingSignal]`. Check top-level `reports_to` field and current employment history entry. Return `ReportingSignal(report_person_id, manager_name, manager_title, confidence=0.92, inference_method='linkedin_reports_to')` for each match. |
| **ReportingSignal type** | `dataclass: report_person_id: str, manager_name: str \| None, manager_title: str \| None, confidence: float, inference_method: str`. At least one of `manager_name` or `manager_title` must be non-null. |
| **After extraction** | Call `entity_resolution(manager_name, manager_title, company_id) → person_id \| None`. If resolved: call `ingest_explicit_edge(resolved_id, ...)`. If not resolved and `manager_title` is not None: call `ingest_explicit_edge` with `manager_title` (triggers stub creation from Task 1-C). |
| **Test condition** | Mock payload: `{'reports_to': {'name': 'Alice Kim', 'title': 'VP Engineering'}}`. Function returns 1 `ReportingSignal` with `confidence=0.92` and `inference_method='linkedin_reports_to'`. |
| **Done when** | Unit test passes. Function is called from wherever `apollo.py` currently writes the person_enrichment signal — add the call there. |

### Task 2-B · Job posting reports-to extractor

| Field | Detail |
|---|---|
| **File** | `server/credence/extractors/job_postings.py` (create new file) |
| **Input** | Job posting text in signals table: `SELECT structured_value->>'text' FROM signals WHERE signal_type='job_posting' LIMIT 5`. Inspect real postings first. |
| **Patterns to use** | Use `REPORTING_PATTERNS` from CLAUDE.md exactly as written. Do not invent new patterns without testing against real postings first (use REPL Pattern 3 from CLAUDE.md). |
| **Function interface** | `async def extract_reporting_from_job_posting(job_posting_text: str, company_id: str, report_title: str) -> list[ReportingSignal]`. Confidence by pattern: Pattern 0: 0.88 · Pattern 1: 0.85 · Pattern 2: 0.75 · Pattern 3: 0.70 · Pattern 4: 0.82 · Pattern 5: 0.65. Set `inference_method='job_posting_nlp'`. |
| **Pipeline hook** | Find where job_posting signals are written to Supabase and add the call there. |
| **Test condition** | Run against the 6 test sentences from CLAUDE.md REPL Pattern 3: "reports directly to VP of Process Engineering" → 1 match (0.88). "work closely under Dr. Wei Chen, the SVP" → 1 match (0.75). "dotted line to CTO" → 0 matches (document this edge case). "Reporting line to Head of Memory Architecture" → 1 match (0.85). "will have 3 direct reports" → 0 matches. "Oversee a team of 12" → 0 matches. |
| **Done when** | All 6 test cases pass. Function is hooked into the ingestion pipeline. |

### Task 2-C · Press release named-officer extractor (LLM)

| Field | Detail |
|---|---|
| **File** | `server/credence/extractors/press_releases.py` (create new file) |
| **Input** | Press release text: `SELECT structured_value->>'text' FROM signals WHERE signal_type='press_release' LIMIT 3`. If no rows exist yet, still build the extractor. |
| **Approach** | Use Z.AI (same model used in `chat.py`). Reuse existing Z.AI client — do not add a new API client. Wrap call in `try/except`; if LLM fails return `[]` and log. |
| **Prompt template** | System: `You are an org chart signal extractor.` User: `Extract all named reporting relationships from the following press release excerpt. Return JSON array only. Format: [{"person_name": str, "person_title": str, "reports_to_name": str\|null, "reports_to_title": str\|null, "confidence": float}]. If none found, return []. Confidence: 0.95 if explicit ("reporting to X"), 0.80 if implied ("joining under X"), 0.70 if inferred from context. Text: {text}` |
| **Cost guard** | Only call the LLM if the text contains at least one `LEADERSHIP_VERB` from CLAUDE.md: `['leads', 'heads', 'manages', 'oversees', 'directs', 'runs', 'is responsible for', 'spearheads', 'drives']`. Skip otherwise. |
| **Test condition** | Input: `'Acme appoints Alice Kim as VP Engineering, reporting to CTO Bob Chen.'` Expect: 1 result, `person_name='Alice Kim'`, `reports_to_name='Bob Chen'`, `confidence >= 0.90`. Use mocked LLM response in unit test. |
| **Done when** | Unit test passes using mocked LLM. Function is hooked into the press release ingestion pipeline. |

---

## Phase 3 + 4 — Frontend + Feedback Loop

**4 parallel subagents · Requires Phase 1 complete · Phase 2 optional but recommended first**

Phase 3 connects the v3.1 backend to the frontend. Phase 4 fixes the feedback loop attribution. All four tasks are independent of each other.

### Task 3-A · ProspectDetail.tsx — read from org_reporting_edges

| Field | Detail |
|---|---|
| **File** | `src/pages/ProspectDetail.tsx` |
| **Problem** | Currently reads from v2 prospects/signals and does a seniority-rank sort. Does not use `org_reporting_edges` at all. |
| **New data query** | Query `org_reporting_edges` joined to `persons` on both `manager_person_id` and `report_person_id`. Filter: `.eq('company_id', companyId).eq('is_current', true).order('path_confidence', { ascending: false })`. Select `id, confidence, path_confidence, inference_method, valid_from` plus manager and report person fields including `is_unresolved_target`. |
| **Fallback** | If `edges.length === 0`, use existing v2 seniority sort. Keep the v2 fallback — do not delete it. |
| **Transform to ReactFlow** | Each edge → ReactFlow edge with `id=edge.id, source=manager.id, target=report.id, data={confidence, path_confidence, inference_method, valid_from}`. Stub nodes (`is_unresolved_target=TRUE`) get `type='stubNode'`. Register two custom node types: `personNode` (existing) and `stubNode` (see Task 3-C). |
| **Test condition** | For a company with ≥5 edges in `org_reporting_edges`: chart renders as a tree (VP at top, Directors one level down), NOT as a seniority ladder. Stub nodes appear with dashed borders. V2 fallback still works for 0-edge companies. |
| **Done when** | ProspectDetail loads without console errors for both the v3 path and the v2 fallback path. |

### Task 3-B · Confidence visualization

| Field | Detail |
|---|---|
| **File** | `src/pages/ProspectDetail.tsx` (ReactFlow edge and node rendering) |
| **Edge opacity** | = `path_confidence` value directly. Floor at 0.30 so no edge is invisible. |
| **Edge stroke width** | = `1 + (path_confidence * 2.5)`. Range: ~2px for 0.45 confidence, ~3.4px for 0.95. |
| **Node hover tooltip** | Show on hover: `'Source: [inference_method] · Confidence: [path_confidence as %] · Last updated: [relative time from valid_from]'`. Example: `'Source: job_posting_nlp · Confidence: 72% · Last updated: 3 days ago'`. |
| **Filter slider** | Below the ReactFlow canvas: `min=0.40, max=0.99, default=0.45`. Edges below threshold get `opacity=0, pointer-events=none` (hidden but not removed from state). Label: `'Min confidence: [value]'`. |
| **Test condition** | Edge with `path_confidence=0.50` is visually thinner and more transparent than edge with `path_confidence=0.90`. Hover shows tooltip. Slider below 0.72 hides low-confidence edges. |
| **Done when** | Visual diff is obvious. No console errors. |

### Task 3-C · Unknown node rendering

| Field | Detail |
|---|---|
| **File** | `src/pages/ProspectDetail.tsx` + `src/components/NodeInspector.tsx` |
| **Stub node visual spec** | Dashed border · Background `#F8F8F8` · Name in italics: `[Unknown VP of Manufacturing]` · Small `?` badge top-right · No enrichment score chips · Label: `'Role inferred · Person not yet identified'` |
| **NodeInspector for stubs** | When stub node selected — NOT the full identity card. Minimal panel: title from `canonical_name`, source label `'Inferred from job posting · [company]'`, message `'We know this role exists at this company but have not yet identified the person. Credence will resolve this automatically as more signals are collected.'`, placeholder button `'Flag for manual review'` (no-op). |
| **Decision 4** | Stub nodes must **never** be hidden or filtered out by default. Decision 4 from CLAUDE.md: unknown nodes are rendered, not omitted. |
| **Test condition** | Stub nodes render with dashed borders and italic names. Clicking a stub shows the minimal panel, not an error. |
| **Done when** | Zero console errors when stub nodes are present. Visual treatment is distinct from normal person nodes. |

### Task 4-A · Per-component EdgeScore in hierarchy.py

| Field | Detail |
|---|---|
| **File** | `server/credence/orgchart/hierarchy.py` |
| **Problem** | `_score_pair()` returns a single `float`. `optimizer.py` only knows "implicit_scoring was X% accurate" — not which of the 7 components drove the wrong decision. |
| **Fix** | Add `EdgeScore` dataclass: `total: float`, `components: dict[str, float]` (keys: `'seniority_gap'`, `'domain_match'`, `'subdomain_match'`, `'manager_title'`, `'span_capacity'`, `'patent_cluster'`, `'geographic_scope'`), `dominant_component: str` (= `max(components, key=components.get)`). Change `_score_pair` to return `EdgeScore` instead of `float`. |
| **Persist to DB** | Store `EdgeScore.components` as JSONB in `score_components` column and `dominant_component` in `dominant_signal` column when writing `org_reporting_edge` (both added in Task 0-A migration). |
| **Test condition** | `_score_pair(manager_seniority_70, report_seniority_60, same_domain=True)` returns `EdgeScore` where: `sum(components.values()) ≈ total` (within 0.01), `dominant_component` is one of the 7 valid names, `components` has exactly 7 keys. |
| **Done when** | Unit test passes. `build_org_chart()` caller interface unchanged. |

### Task 4-B · Per-component optimizer

| Field | Detail |
|---|---|
| **File** | `server/credence/orgchart/optimizer.py` |
| **Problem** | `optimize_weights()` applies the same delta to all 7 component weights on every correction. Cannot distinguish "patent_cluster is over-firing" from "seniority_gap is under-weighting". |
| **Fix** | On correction arrival: (1) Load `dominant_component` from the corrected edge's `dominant_signal`. (2) For dominant component: `new_weight = current_weight * (1 - 0.15 * error_rate_for_dominant)`. (3) For all other components: `new_weight = current_weight * (1 - 0.05 * error_rate_global)`. (4) Clamp all to `[0.01, 0.50]`. (5) Write to `org_signal_performance` with `method='per_component_optimizer'`. |
| **error_rate** | `error_rate_for_dominant` = corrections where `dominant_component=X AND was_wrong=TRUE` / total corrections where `dominant_component=X`. Minimum 5 corrections before adjusting a component — use global error_rate until then. |
| **Tracking** | Add rows to `org_signal_performance` with `signal_type = 'component:' + component_name` (e.g., `'component:patent_cluster'`). |
| **Fallback** | Global optimizer path still works when `score_components IS NULL` (edges written before Task 4-A). |
| **Test condition** | Simulate 10 corrections all on edges where `dominant_component='patent_cluster'`, all marked incorrect. Assert: `patent_cluster` weight decreases more than any other component. Assert: all weights remain in `[0.01, 0.50]`. |
| **Done when** | Unit test passes. |

---

## Execution Checklist

| ☐ | Task | Done criteria |
|---|---|---|
| ☐ | **0-A** Migrations applied | All 9 tables exist in Supabase. Zero migration errors. |
| ☐ | **0-B** Imports smoke-test | `python -c import` of all 6 modules prints `all imports OK`. Zero tracebacks. |
| ☐ | **1-A** Global tree assignment | VP→Dir→Mgr chain forms correctly. No cycles. IC track peers not managing managers. pytest passes. |
| ☐ | **1-B** Temporal model | Re-run historicizes old edge (`is_current=FALSE`, `valid_to` set). New edge has `valid_from`. No data lost. |
| ☐ | **1-C** Unknown node stubs | Unresolved manager title → stub person row + edge written. `is_unresolved_target=TRUE`. confidence × 0.7. |
| ☐ | **2-A** Apollo reports_to parser | Mock payload → `ReportingSignal` with `confidence=0.92` and `method='linkedin_reports_to'`. Hooked into pipeline. |
| ☐ | **2-B** Job posting extractor | All 6 REPL test sentences produce correct match/no-match. Hooked into ingestion. |
| ☐ | **2-C** Press release LLM extractor | Mock press release → 1 `ReportingSignal` with `confidence >= 0.90`. LLM failure returns `[]` gracefully. |
| ☐ | **3-A** Frontend reads org_reporting_edges | Company with ≥5 edges shows tree, not ladder. V2 fallback still works. |
| ☐ | **3-B** Confidence visualization | High-confidence edges visually thicker/darker. Hover tooltip shows source + %. Slider filters edges. |
| ☐ | **3-C** Unknown node rendering | Stub nodes: dashed border, italic name, `?` badge, minimal NodeInspector. Never hidden by default. |
| ☐ | **4-A** Per-component EdgeScore | `_score_pair` returns `EdgeScore`. `sum(components) ≈ total`. `score_components` written to DB. |
| ☐ | **4-B** Per-component optimizer | 10 corrections on `patent_cluster` → `patent_cluster` weight shrinks most. All weights in `[0.01, 0.50]`. |

---

## What Is NOT In Scope

- **SEC proxy filing extractor** — high signal but requires HTML/PDF parsing of DEF 14A filings. Separate sprint after Phase 2 is live.
- **Crunchbase executive-team scraper** — requires API key procurement. Not blocked on code.
- **Multi-candidate visualization** ("70% A, 25% B") — needs schema change (`top_candidates` JSONB on `org_reporting_edges`). Scope after 4-A is stable.
- **Demo mode org chart** — do not add org chart demo data until at least one real company has been processed through the v3.1 pipeline.
- **Deprecating v2 scoring.ts** — do not remove client-side scoring until the FastAPI `/score` endpoint is live and tested. CLAUDE.md is explicit on this.

---

> **CONTRACTS.md must stay stable.** Do not modify CONTRACTS.md during implementation. If any task reveals a needed interface change, stop and flag it explicitly before proceeding. Changing the contract mid-sprint breaks parallel agents that depend on the same interface.
