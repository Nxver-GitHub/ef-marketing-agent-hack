# Org Chart Inference — State, Gaps, and Redesign Plan

> Owner: DarkBeaver. Initiative: 2026-05-01 → through Phase D. See Agent Mail thread `orgchart-redesign` (msg 164) for the active reservation list and progress broadcasts.

## Status (live — update on every milestone)

| Phase | Status | Owner |
|---|---|---|
| **Final unblockers** persons.is_unresolved_target ALTER + per-company refresh script + REPORTING_PATTERNS verbatim alignment | ✅ shipped this iteration | DarkBeaver |
| **Phase A.1** Apply A0 + B1 migrations | ⏸ blocked on operator | LavenderPrairie |
| **Phase A.2-A.4** Frontend rewire | ⏸ pending Phase A.5/A.6 backend | SunnyRidge (when ready) |
| **Phase A.5** Unresolved-target schema | 🟢 migration drafted (307L), needs LP apply; writer support deferred | DarkBeaver |
| **Phase A.6** LinkedIn reports_to ingestion | ✅ shipped — 9 specs, 581/581 green | DarkBeaver |
| **Wave 2 / Task 0-A** score_components migration | 🟢 drafted (additive ALTER), needs LP apply | DarkBeaver |
| **Wave 2 / Task 0-B** import smoke test | ✅ pytest validates end-to-end (636/636) | DarkBeaver |
| **Wave 2 / Task 1-A** hierarchy global tree assignment | ✅ shipped — global candidate-edge sort + union-find + inline span | subagent A1 |
| **Wave 3 / Task 1-B** temporal model on edge writes | ✅ shipped — UPDATE old → INSERT new + skip-write check, valid_from/valid_to populated | subagent W3-A |
| **Wave 3 / Task 1-C** unknown node stub generation | ✅ shipped — `ingest_explicit_edge` accepts manager_title, creates `[Unknown <title>]` person stub, edge confidence × 0.7 | subagent W3-A |
| **Wave 2 / Task 2-A** Apollo reports_to | ✅ already shipped via Phase A.6 | DarkBeaver |
| **Wave 2 / Task 2-B** Job posting extractor | ✅ shipped — 12 specs, REPORTING_PATTERNS verbatim from CLAUDE.md | subagent A2 |
| **Wave 2 / Task 2-C** Press release LLM extractor | ✅ shipped — 8 specs, Z.AI client mirrored from chat.py | subagent A3 |
| **Wave 2 / Task 3-A+B+C** Frontend rewire | ✅ shipped — ProspectDetail v3 path + StubNode + StubInspector + confidence slider, tsc clean, 133 vitest pass | subagent A4 |
| **Wave 3 / Task 4-A** Per-component EdgeScore | ✅ shipped — EdgeScore dataclass with 7-key components, score_components JSONB + dominant_signal persisted | subagent W3-A |
| **Wave 3 / Task 4-B** Per-component optimizer | ✅ shipped — `optimize_account_weights_per_component`, dominant component nudged 3× harder, 5-correction min threshold | subagent W3-B |
| **Phase A.7** Run pipeline live | ⏸ blocked on A.1 | DarkBeaver |
| **Phase B** Better signal sources | ⏸ pending Phase A | DarkBeaver |
| **Phase C** Constrained optimization + temporal | ⏸ pending Phase B | DarkBeaver |
| **Phase D** Active learning loop | ⏸ pending Phase C | DarkBeaver |

---

## I. What actually runs today

Three systems coexist at different states.

**The v2 production path** is what users see when they hit `/prospect/:id`. `ProspectDetail.tsx` imports ReactFlow (L17, 678–689) and renders a tree. The Track A audit (msg 14) described it as "reportingTree reconstructed from signals... seniority-rank binning" — we sort employees by `seniority_score` and bucket them into approximate tiers. There's no edge inference at all. We're not asserting "X reports to Y" — we're showing "here's the org sorted by seniority, draw lines down." This is essentially **decorative**, not inferred.

**The v3.1 backend pipeline** is what the team just shipped. Six modules in `server/credence/orgchart/`:
- `clustering.py` (A1, SwiftElk) — groups current employees into `(company, functional_domain[, sub_domain])` clusters with confidence 0.70/0.90/0.95
- `hierarchy.py` (A2, DarkBeaver) — implicit scoring + explicit-edge ingestion → `org_reporting_edges`
- `scope.py` (A3, DarkBeaver) — `owns_*`, `team_size_min/max`, `budget_authority_level`
- `propagation.py` (A8, DarkBeaver) — `path_confidence` BFS post-pass
- `validation.py` (A7, DarkBeaver) — span / cycle / IC misclassification audit
- `corrections.py` + `optimizer.py` + `performance.py` (A4-A6, SwiftElk) — feedback loop

This pipeline produces a real materialized chart. **It is not yet rendered to users.** No frontend reads from `org_reporting_edges` / `person_scope_estimates` yet, and the migrations (A0 + B1) still await operator apply.

**The signal substrate** sits underneath both. We have `prospects` (denormalized v2), `persons`/`companies`/`employment_periods` (v3 normalized), `patent_inventors`, plus paper/conference/standards/education extractors (B3-B5) that emit signals into the connection graph. None of these signals currently feed hierarchy inference — `hierarchy.py` only reads cluster membership + per-pair patent counts.

So the honest answer to "how do we infer org charts today" is: **we don't really.** v2 is a seniority-sorted list with lines drawn between buckets. v3.1 is a heuristic edge writer that hasn't been turned on. There's no working inference path live.

---

## II. Six systemic issues with the v3.1 design

### 1. Greedy local optimization, no tree-level coherence

`hierarchy.py` picks each report's manager independently — for each report, score every other cluster member, take the highest. This is locally rational but globally weird:

- The result isn't guaranteed to be a tree. Two cluster members can pick each other as their best manager → cycle. We catch this in `validation.py` after the fact, but the writer never tries to prevent it.
- A high-seniority person (say a VP) with no reasonable manager candidate above them gets dropped (`edges_skipped_no_candidate`). They're orphaned even when there's clearly an SVP a tier up — but we never look up the cluster.
- Span-cap reroute is single-pass within cluster only. If a Director hits cap=10 and the 11th best-scored report has no plausible peer manager, they become orphan.

**A proper system would solve the assignment globally** as a constrained optimization: maximize ∑ confidence subject to (each person has ≤1 manager, no cycles, span limits, IC track preserved, manager.seniority > report.seniority, edges within cluster). Even simple Hungarian-style assignment beats independent greedy.

### 2. Weak signal model — heuristic, no ground truth

The implicit scoring in `_score_pair` is essentially a hand-tuned linear combination of seven heuristic features. The weights (`0.30 / 0.25 / 0.15 / 0.10 / 0.05 / 0.15 / 0.08`) come straight from V3_PT2.md. They're plausible but **never validated against real org charts.** We have no ground-truth oracle.

The real explicit-edge sources we'd want — LinkedIn `reports_to`, SEC proxy filings for public companies, job-posting "reports to <Title>" extraction, press-release CTO-announcements — **none are wired.** `ingest_explicit_edge()` is a public API with no producer. Decision 3 (explicit > implicit) is theoretically protected but practically dormant.

### 3. No temporal model — everything is "now"

The schema has `valid_from` / `valid_to` / `is_current` columns. The code only ever writes `is_current=TRUE`, never sets the date range. When a person changes role or leaves a company:

- Old edges don't get historicized.
- New edges write over old ones (ON CONFLICT DO UPDATE) — we lose the history.
- A scrape from 2024 and a scrape from 2026 both produce "current" edges and one silently overwrites the other.

**Org charts are time-varying.** Promotions, reorgs, departures happen monthly. A serious system models edges with explicit validity intervals and treats "current" as a derived view.

### 4. The feedback loop is real but the attribution is global

SwiftElk's A4 corrections route + A5 performance tracker + A6 optimizer is genuinely good architecture. Users correct edges; we count accuracy per `inference_method`; the optimizer nudges weights toward methods that are right.

But: **per-component attribution doesn't exist.** When a user reports an edge wrong, we know the `inference_method` (e.g., `implicit_scoring`) but not WHICH of the seven scoring components drove the decision. A6 has a frank comment: "the optimizer dials all 7 components by the same delta. That degrades to a global confidence multiplier on `implicit_scoring`."

So we're learning "implicit_scoring is X% accurate" but not "the patent-cluster bonus is over-firing." We can't tune the model to its actual failure modes.

### 5. Decision 4 is honored in spec, violated in practice

CLAUDE.md L188: "Unknown nodes are rendered, not omitted." Schema has `is_unresolved_target=TRUE` field. Reality:

- `clustering.py` drops persons whose title can't be classified — they don't enter the cluster.
- `hierarchy.py` orphans reports who can't find a manager with `min_confidence ≥ 0.45`.
- No code ever writes an unresolved-target row.
- The UI has no rendering path for "[Unknown VP of Manufacturing]" placeholder boxes.

The product loses honesty — instead of saying "we don't know who runs the Verification team here, but the team exists," we just don't show that team at all. For sales, this is exactly backward: knowing a function exists matters even when we can't name the head.

### 6. No honest uncertainty rendering

A 0.95-confidence edge and a 0.55-confidence edge look identical to the user. There's no:

- Confidence-weighted edge thickness or color
- Multi-candidate visualization ("70% A, 25% B, 5% C")
- Provenance hover ("inferred from 3 signals: SEC proxy, LinkedIn, patent cluster")
- Last-validated date
- Filter slider ("show only edges ≥ 0.80")

For a B2B sales tool where the cost of acting on a wrong relationship is real (cold-pitching the wrong VP), honest uncertainty matters more than aspirational completeness.

---

## III. First-principles redesign — what a proper org chart system looks like

The core insight: **the org chart is a Bayesian inference problem on a tree-structured graph.** We have noisy partial observations from heterogeneous sources; we want to estimate the most-probable tree subject to known structural constraints (each person has ≤1 manager, no cycles, manager more senior, parallel IC ladders, bounded span).

This frames everything else.

### Three classes of signal, three confidence regimes

| Class | Examples | Baseline confidence | Decay | Properties |
|---|---|---|---|---|
| **Direct** | SEC proxy "X is CFO and reports to CEO Y", LinkedIn `reports_to` field, job-posting "this role reports to <Title>" | 0.85–0.99 | slow (years) | One signal can assert an edge with high certainty; corroboration boosts |
| **Structural** | Same patent cluster, same paper coauthorship, same team string in employment_periods, same conference committee | 0.30–0.70 | medium | Indicates working relationship, not direction; needs combination with seniority to direction-ify |
| **Behavioral** | Career follows-the-leader (X always changes companies after Y), email signature mentions, LinkedIn endorsements | 0.20–0.50 | fast | Weak prior; useful as tiebreaker only |

A signal model that knows these classes can:
- Materialize a Direct edge immediately, no scoring
- Combine Structural signals into a directional edge candidate via the seniority gap
- Use Behavioral signals only to break ties between equally-scored candidates

### The tree-finding problem as constrained optimization

Given persons P at company C, signals S = {(p_a, p_b, type, confidence, valid_window)}, find edges E that maximize:

```
∑(p_a → p_b) ∈ E   confidence(signals supporting p_a → p_b) × decay(time) × consistency_bonus(direction matches seniority)

subject to:
  ∀p ∈ P:  |{e ∈ E : e.report = p}| ≤ 1            // single manager
  ∀cycle in E:  forbidden                          // no cycles
  ∀m ∈ P:  |{e ∈ E : e.manager = m}| ≤ span_cap(seniority(m))
  ∀(m,r) ∈ E:  same_or_parent_cluster(m, r) ∧ seniority(m) > seniority(r) ∧ ic_compat(m, r)
```

This is an integer programming problem in general, but reasonable approximations (weighted maximum spanning forest with span constraints, or simulated annealing from a greedy start) get you 95% of the way. Hungarian-with-capacity is a clean fit and lands in pure Python in ~150 lines.

### Honest uncertainty as a first-class output

For every edge written, store:
- Point estimate confidence
- Top-2 alternatives with probabilities
- Provenance: which signals contributed, with weights
- Last refresh timestamp
- Stability score: how often has this edge changed in the last N inferences?

For every node, store:
- Resolution status: known person, unresolved target ("Unknown VP of Manufacturing"), or stub
- Confidence in identity
- Sources that established the role

---

## IV. The proper 7-stage pipeline

Here's the architecture I'd build if starting clean. It maps to incremental improvements over what we have.

### Stage 0 — Signal acquisition (broaden the inputs)

Today's inputs: enriched LinkedIn employment + patents + papers (when extractors run). Missing:

- **LinkedIn `reports_to`** — Apollo / PDL sometimes have this on their person enrichment payload. We currently don't parse it. **Highest-leverage missing signal.** Probably 30% of executive enrichments include it.
- **SEC proxy filings** — for every public company on the prospect list, the DEF 14A proxy lists named executive officers + their reporting structure. Parseable with NLP. Best ground truth available.
- **Job posting reports-to mining** — when a company posts "Senior PM, ML Compiler — reports to Director of Compiler Engineering," that's a directional edge. Apify / scraping pipelines exist. This is the V3_PT2 plan but never wired.
- **Press release named-officer announcements** — "Acme appoints Alice Kim as VP Engineering, reporting to CTO Bob Chen." LLM-extract.
- **Crunchbase executive-team data** — explicit titles, sometimes reporting structure.
- **Conference speaker hierarchy** — keynotes are senior to panelists are senior to attendees. Weak but real.
- **Email signatures** when scrapeable — explicit titles + sometimes "manager: <name>".

Each new signal source is a small extractor module (`server/credence/extractors/<source>.py`) following the existing pattern (`apollo.py`, `scholar.py`, etc.) — defensive HTTP, doc-driven, mock-transport tested, opt-in via env var or feature flag.

### Stage 1 — Signal canonicalization

Before scoring, every signal lands in a single normalized form:

```python
@dataclass(frozen=True)
class OrgSignal:
    source_type: Literal["sec_proxy", "linkedin_reports_to", "job_posting", ...]
    asserts: tuple[UUID, UUID, str]  # (manager_id, report_id, "reports_to" | "peer" | "skip_level" | "in_cluster")
    raw_confidence: float            # source-baseline
    valid_from: date | None
    valid_to: date | None
    extracted_at: datetime
    structured_evidence: dict        # source-specific details (≤4KB)
    raw_uri: str | None              # S3 pointer to full payload
```

This sits in a new `org_signals` table. The orchestrator reads from it; producers (extractors) write to it. Signals are append-only, never deleted — the historical record matters for temporal modeling.

### Stage 2 — Cluster + anchor (have this — A1, evolved)

Keep the existing clustering. Add:

- **Cluster-head identification**: for each cluster, name the most-likely manager (highest seniority + manager-title + tenure). This anchors the tree.
- **Cross-cluster parent**: every functional cluster has a parent cluster (Hardware → Engineering → Office of CTO → CEO). Encode this as a `parent_cluster_id` on `org_functional_clusters`.

### Stage 3 — Confident island assertion (NEW — currently missing)

Walk every Direct signal. If confidence ≥ 0.85 AND no contradicting Direct signal exists, assert the edge immediately with `inference_method = "explicit_<source>"`. These are the **load-bearing edges** the rest of the tree hangs from.

Output: a sparse partial tree — typically the c-suite + some VPs + scattered named-manager pairs.

### Stage 4 — Constrained tree filling (UPGRADE A2)

Now with the partial tree as a fixed skeleton, fill the rest. Three improvements over greedy:

**a) Joint scoring within cluster**: instead of "for each report, pick best manager," score the entire cluster's manager-assignment as a joint optimization. Even bipartite Hungarian gives better results than greedy.

**b) Cross-cluster bridges**: cluster heads need managers from a parent cluster (or are roots). Currently `hierarchy.py` doesn't traverse this. Add a "cross-cluster step" that connects each cluster head to a candidate in `parent_cluster.members`.

**c) Span-aware initial assignment**: instead of pick-then-trim, allocate manager-slots up front. A Director with cap=10 has 10 slots; assign each report to its highest-scoring available slot. Hungarian-with-capacity solves this.

**d) Iterate until stable**: run the assignment, validate, re-pick conflicts, repeat. Converges in ≤3 passes for typical org sizes.

### Stage 5 — Tree coherence + temporal stitching (NEW)

After Stage 4, run validation (we have this — A7). For violations:

- **Span over-cap**: surface for human review; do NOT auto-fix. The cap is heuristic — sometimes a real director has 14 reports.
- **Cycles**: hard error, refuse to write. Pick the lowest-confidence edge in the cycle, demote it.
- **IC misclassification**: hard error, demote.

For temporal stitching:

- New edges go in with `valid_from = max(extraction_dates)`.
- Old edges with the same `report_id` get `valid_to = new_edge.valid_from - 1 day`, `is_current = FALSE`.
- Schedule a weekly job: re-walk all signals, detect stale edges (no Direct signal in 90 days, no recent enrichment), set `valid_to = now()` and `is_current = FALSE`.

### Stage 6 — Confidence propagation + UX rendering (UPGRADE A8 + frontend)

Keep A8 propagation (path_confidence as multiplicative product). Add to the response shape:

- **Top-2 alternatives** for each edge (computed during Stage 4 by tracking the second-best assignment).
- **Provenance**: list of contributing signals.
- **Stability score**: how many of the last 3 inference passes agreed on this edge.

Frontend changes:

- Edge thickness ∝ confidence; edge color ∈ {green ≥0.8, yellow 0.5-0.8, red <0.5}
- Hover shows provenance: "SEC proxy 2023 + LinkedIn reports_to + 2 patent co-inventions"
- Unresolved-target nodes render as dashed boxes with placeholder labels ("Unknown VP of Manufacturing")
- A "show only confidence ≥ X" filter on the chart
- "Last refreshed N days ago" footnote

### Stage 7 — Active learning + feedback loop (UPGRADE A4-A6)

Three changes to the existing feedback loop:

**a) Per-component attribution at correction time**: when `org_chart_corrections` captures a correction, also store which scoring components fired for the wrong edge:

```sql
ALTER TABLE org_chart_corrections ADD COLUMN component_attributions JSONB;
-- e.g., {"seniority_gap": 0.30, "domain_match": 0.25, "patent_cluster": 0.15, ...}
```

**b) Per-component optimization**: A6 currently nudges all components uniformly. With attribution, nudge components that fire most often on wrong edges down, components that fire most often on right edges up. This is the actual learning loop V3_PT2.md L262 specified.

**c) Active sampling**: surface low-confidence + high-degree edges for user verification ("you'll see this org chart for prospect X — does Y → Z look right?"). One correction at the right edge has ~10x the information value of a correction on a confident leaf.

---

## V. Phased implementation plan

Four phases. Each builds on the prior, each is independently shippable.

### Phase A — Honest rendering + Direct signal ingestion (2 weeks)

Goal: turn the v3.1 backend on, and make the chart honest about what it knows.

| ID | Task | Owner | Status |
|---|---|---|---|
| A.1 | Apply A0 + B1 migrations (operator action) | LavenderPrairie | ⏸ |
| A.2 | Rewire `ProspectDetail.tsx` to read from `org_reporting_edges` + `person_scope_estimates` | SunnyRidge | ⏸ |
| A.3 | Add edge-confidence visual encoding (thickness + color) | SunnyRidge | ⏸ |
| A.4 | Add hover-provenance tooltip | SunnyRidge | ⏸ |
| A.5 | Render unresolved-target nodes (schema flag + writer) | DarkBeaver | 🟡 |
| A.6 | Wire LinkedIn `reports_to` from Apollo + PDL | DarkBeaver | 🟡 |
| A.7 | Run live pipeline against 20k prospects | DarkBeaver (ops) | ⏸ |

After Phase A: real edges in the DB, real chart in the UI, ~30% of edges explicit (LinkedIn-derived).

### Phase B — Better signal sources (3-4 weeks)

Goal: feed the system real ground truth.

| ID | Task | Owner | Status |
|---|---|---|---|
| B.1 | SEC proxy parser — DEF 14A → executive officers + reporting | DarkBeaver | ⏸ |
| B.2 | Job-posting `reports_to` extractor (Firecrawl + LLM) | DarkBeaver | ⏸ |
| B.3 | Press-release named-officer parser | DarkBeaver | ⏸ |
| B.4 | Crunchbase executive-team scraper | DarkBeaver | ⏸ |
| B.5 | Land all 4 via existing extractor pattern + new `org_signals` table | DarkBeaver | ⏸ |

After Phase B: signal coverage goes from ~30% Direct to ~70% Direct on US public companies, ~50% on private-but-VC-funded.

### Phase C — Constrained optimization + temporal model (next month after B)

Goal: replace greedy with proper joint inference.

| ID | Task | Owner | Status |
|---|---|---|---|
| C.1 | Refactor `hierarchy.py`: split scoring / assignment / persistence | DarkBeaver | ⏸ |
| C.2 | Implement Hungarian-with-capacity manager assignment | DarkBeaver | ⏸ |
| C.3 | Add cross-cluster bridge step (parent cluster traversal) | DarkBeaver | ⏸ |
| C.4 | Wire `valid_from`/`valid_to`/`is_current` properly + weekly refresh job | DarkBeaver | ⏸ |
| C.5 | Top-2 alternative tracking in `org_edge_alternatives` table | DarkBeaver | ⏸ |

After Phase C: the chart is structurally coherent (real tree, no orphans where parent clusters exist) and time-aware (departures, promotions tracked).

### Phase D — Active learning loop (ongoing)

Goal: make the system self-improving.

| ID | Task | Owner | Status |
|---|---|---|---|
| D.1 | Add `component_attributions` JSONB to `org_chart_corrections` | DarkBeaver | ⏸ |
| D.2 | Upgrade A6 optimizer: per-component nudging | DarkBeaver / SwiftElk | ⏸ |
| D.3 | Active sampling — surface uncertain edges in UI | SunnyRidge | ⏸ |
| D.4 | Quarterly ground-truth check vs SEC filings, compute precision/recall | DarkBeaver | ⏸ |

After Phase D: the model improves from real usage. We can show "model precision improved from 78% to 84% over the last quarter."

---

## VI. Open user decisions

A handful of choices need explicit calls before the team can execute:

1. **Phase A blocker — LP apply A0 + B1.** No amount of code work matters until these run. Worth confirming whether LP has DB access tonight or whether we need to schedule.

2. **SEC proxy ingestion has free + paid tiers.** sec.gov is free but XBRL parsing is gnarly; Sentieo / IntellizenceAI are $1k-5k/mo and pre-parse. Default to free + LLM parser, paid if accuracy disappoints.

3. **Apify / Firecrawl spend cap for job-posting mining.** Probably $50-200/mo for ongoing crawls of target accounts' career pages. Set a per-tenant budget.

4. **Crunchbase API access.** Has org-tree data for VC-backed companies. ~$500/mo basic plan. Worth it after Phase B is wired and we know the signal coverage gap.

5. **How aggressively to surface unresolved-target nodes.** A chart with 50% "Unknown" boxes feels broken; a chart with 0% feels false. Probably gate on cluster confidence — show unresolved targets only for clusters with ≥3 known members.

6. **Temporal model: how far back?** Edges valid in 2022 are mostly noise for a current sales workflow. Probably trim to "last 24 months" by default with a "show historical" toggle.

7. **Active sampling consent.** Asking users to verify edges in the middle of their flow has UX cost. Probably opt-in via "improve org chart accuracy" Settings toggle.

---

## VII. What this gets us

Concretely, after Phases A–C ship:

- **For sales**: a chart per prospect with real reporting lines, confidence bands, and provenance. Sales rep can hover an edge and see "this comes from Q2 SEC proxy + 2 LinkedIn reports_to entries."
- **For product**: an honest chart that says "we have 3 confirmed VPs, 1 unresolved Director slot under Manufacturing" rather than a beautiful but partly-fictional tree.
- **For Credence the differentiator**: nobody else does this. Apollo/Clay sell flat contact data. ZoomInfo has org charts but they're hand-curated for the F500 only and stale within a quarter. A live, signal-grounded chart with confidence per edge is a real product moat for the warm-introduction wedge.

Estimated effort: Phase A ≈ 1 week of solid work (mostly frontend + Apollo/PDL re-parse). Phase B ≈ 3-4 weeks (each extractor is ~1 week). Phase C ≈ 2 weeks. Phase D ongoing.

---

## VIII. Coordination & ownership

This document lives at `plan.md` at the repo root. The Agent Mail thread is `orgchart-redesign`.

**DarkBeaver owns** the backend pipeline + extractors + plan.md updates.
**SunnyRidge owns** the frontend rewire + active-sampling UI when the time comes.
**LavenderPrairie owns** migration applies + cross-stack debugging support.
**SwiftElk owns** the orchestrator hat + A4-A6 optimizer evolution + cost-budget plumbing for new signal sources.

Update this status table on every milestone landing. When in doubt about scope or sequencing, post to the `orgchart-redesign` thread.
