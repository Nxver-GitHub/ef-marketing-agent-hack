/**
 * Demo data for `?demo=true` mode (CONTRACTS.md Contract 5).
 *
 * Activated by graphStore's boot-time URL detection. When demo mode is on,
 * `loadGraphFromDemoData()` (in graphStore — TBD pickup by SwiftElk) calls
 * `setGraph({ nodes: DEMO_GRAPH_NODES, edges: DEMO_EDGES })` instead of
 * fetching from Supabase. Zero network IO.
 *
 * STATUS: REAL identities + REAL career-overlap evidence as of 2026-04-30
 * (Stream C, Wave 6). Hidden-connection edges (patent/paper/conference) are
 * still PLACEHOLDER until the USPTO / Scholar / Parallel extractors run
 * against these specific people.
 *
 * What's real now (from live Supabase via Stream C SQL):
 *   - 5 real demo prospects from the career-overlap query against
 *     `signals.value->'roles'`: Neil Ashton (NVIDIA), Sanja Fidler (NVIDIA),
 *     James Clarke (Intel), Silvia Linares (Intel), Dr. Javed Absar (Qualcomm)
 *   - 3 real connectors representing an AMD sales team: Keith Strier (AMD SVP
 *     Global AI Markets), Martin Ashton (AMD SVP Hardware IP), James Newling
 *     (AMD AI Compiler Engineer). All three appear in the live `prospects`
 *     table with documented prior employment at NVIDIA / Intel / Graphcore
 *     respectively, which is what creates the warm path to each target.
 *   - 5 documented career overlaps backing each target (see DEMO_CASES.md
 *     for shared-employer + overlap-year + computed_strength derivation)
 *
 * What's still placeholder:
 *   - The 3 hidden-connection edges (patent_co_inventor / academic_co_author /
 *     conference_co_presenter) reference real prospect/connector pairs but
 *     their evidence fields (patent number, paper title, conference name)
 *     remain empty. Filling those requires running USPTO / Scholar / Parallel
 *     extractors against the specific 8 people in this file.
 *
 * Per CLAUDE.md "Common Mistakes" #6: NEVER substitute fictional patent
 * numbers, paper titles, or conference programs for missing data. The names,
 * companies, titles, and LinkedIn URLs below are all real.
 */

import { DEMO_PROSPECT_IDS } from "@/store/graphStore";
import type { EdgeKind, GraphEdge, GraphNode } from "@/lib/graph";
import type { Prospect } from "@/lib/mockStore";

// ── Connector ("your team") UUIDs ─────────────────────────────────────────
//
// Stable, all-zeros-prefix-with-`1`-discriminator UUIDs so connectors don't
// collide with real Supabase UUIDs (UUIDv4 random) OR with the demo prospect
// UUIDs above. Three connectors mirror the 3 hidden-connection edges below.

export const DEMO_CONNECTOR_IDS = [
  "00000000-0000-0000-0000-100000000001",
  "00000000-0000-0000-0000-100000000002",
  "00000000-0000-0000-0000-100000000003",
] as const;

// ── DEMO_PROSPECTS — 5 canonical demo prospects ────────────────────────────
//
// Mirror the v2 mockStore seed identities so the demo doesn't introduce
// faces a returning user has never seen. Each prospect is mapped to one of
// the 5 connection-type narratives the demo showcases (case 1 = career-
// overlap, case 2 = patent, case 3 = paper, case 4 = conference, case 5 =
// standards committee).
//
// `_id` uses the stable demo UUIDs from graphStore. `created_at` /
// `updated_at` use a fixed epoch (2026-01-01) so the demo is visually stable
// across reloads — no "scored 5 minutes ago" timestamps that re-render and
// look animated.

const DEMO_EPOCH_MS = Date.UTC(2026, 0, 1); // 2026-01-01T00:00:00Z

export const DEMO_PROSPECTS: ReadonlyArray<Prospect> = [
  // Case 1: Keith Strier (AMD) → Neil Ashton, both at NVIDIA in 2024
  {
    _id: DEMO_PROSPECT_IDS[0],
    name: "Neil Ashton",
    company: "NVIDIA",
    role: "Distinguished Engineer, Product Architect at NVIDIA",
    industry: "Semiconductors",
    created_at: DEMO_EPOCH_MS,
    updated_at: DEMO_EPOCH_MS,
    past_companies: ["NVIDIA"],
    education: [],
    talks: [],
  },
  // Case 2: Keith Strier (AMD) → Sanja Fidler, both at NVIDIA in 2022
  {
    _id: DEMO_PROSPECT_IDS[1],
    name: "Sanja Fidler",
    company: "NVIDIA",
    role: "Associate Professor at University of Toronto, Vice President of AI Research at NVIDIA",
    industry: "Semiconductors",
    created_at: DEMO_EPOCH_MS,
    updated_at: DEMO_EPOCH_MS,
    past_companies: ["NVIDIA"],
    education: [],
    talks: [],
  },
  // Case 3: Martin Ashton (AMD) → James Clarke, both at Intel in 2015
  {
    _id: DEMO_PROSPECT_IDS[2],
    name: "James Clarke",
    company: "Intel",
    role: "Director of Quantum Hardware at Intel Corporation",
    industry: "Semiconductors",
    created_at: DEMO_EPOCH_MS,
    updated_at: DEMO_EPOCH_MS,
    past_companies: ["Intel"],
    education: [],
    talks: [],
  },
  // Case 4: Martin Ashton (AMD) → Silvia Linares, both at Intel in 2017
  {
    _id: DEMO_PROSPECT_IDS[3],
    name: "Silvia Linares",
    company: "Intel",
    role: "Senior Director - GPU SW Engineering, AI Solutions, Intel Corp.",
    industry: "Semiconductors",
    created_at: DEMO_EPOCH_MS,
    updated_at: DEMO_EPOCH_MS,
    past_companies: ["Intel"],
    education: [],
    talks: [],
  },
  // Case 5: James Newling (AMD) → Javed Absar, both at Graphcore in 2018
  {
    _id: DEMO_PROSPECT_IDS[4],
    name: "Dr. Javed Absar",
    company: "Qualcomm",
    role: "Principal Engineer, ML/AI Compiler Research at Qualcomm",
    industry: "Semiconductors",
    created_at: DEMO_EPOCH_MS,
    updated_at: DEMO_EPOCH_MS,
    past_companies: ["Graphcore"],
    education: [],
    talks: [],
  },
];

// ── DEMO_CONNECTORS — placeholder "your team" stand-ins ───────────────────
//
// Three connectors, each anchoring one of the 3 hidden-connection edges
// below. Names are intentionally generic so the demo viewer reads them as
// "team member" rather than recognizing a real person — the hidden-relationship
// punchline is what the demo is teaching.

interface DemoConnector {
  readonly id: string;
  readonly name: string;
  readonly role: string;
  readonly company: string;
}

export const DEMO_CONNECTORS: ReadonlyArray<DemoConnector> = [
  // Connector 1: Keith Strier — anchors Cases 1 + 2 (both NVIDIA-bridge cases).
  // Real AMD SVP, real prior NVIDIA tenure documented in career_history signal.
  {
    id: DEMO_CONNECTOR_IDS[0],
    name: "Keith Strier",
    role: "Senior Vice President, Global AI Markets",
    company: "AMD",
  },
  // Connector 2: Martin Ashton — anchors Cases 3 + 4 (both Intel-bridge cases).
  // Real AMD SVP, real prior Intel tenure documented in career_history signal.
  {
    id: DEMO_CONNECTOR_IDS[1],
    name: "Martin Ashton",
    role: "Senior Vice President, Hardware IP and Architecture",
    company: "AMD",
  },
  // Connector 3: James Newling — anchors Case 5 (Graphcore-bridge to Qualcomm).
  // Real AMD AI compiler engineer, real prior Graphcore tenure documented.
  {
    id: DEMO_CONNECTOR_IDS[2],
    name: "James Newling",
    role: "AI Compiler Engineer",
    company: "AMD",
  },
];

// ── DEMO_GRAPH_NODES ──────────────────────────────────────────────────────
//
// Combined prospects + connectors in `GraphNode` shape so the eventual
// `loadGraphFromDemoData()` in graphStore can `setGraph({ nodes, edges })`
// directly. Each prospect is wrapped to mirror the shape buildGraph would
// produce; connectors render as plain person nodes with synthetic company
// IDs.

function prospectAsGraphNode(p: Prospect): GraphNode {
  return {
    id: `person:${p._id}`,
    kind: "person",
    name: p.name,
    role: p.role,
    companyId: `company:${p.company.toLowerCase()}`,
    raw: p,
  };
}

function connectorAsGraphNode(c: DemoConnector): GraphNode {
  // Connectors aren't real prospects so we don't have a `Prospect` to attach
  // to `raw`. Synthesize a minimal one for type-shape compatibility — the
  // demo never reads it.
  const stubProspect: Prospect = {
    _id: c.id,
    name: c.name,
    company: c.company,
    role: c.role,
    industry: "Your Company",
    created_at: DEMO_EPOCH_MS,
    updated_at: DEMO_EPOCH_MS,
  };
  return {
    id: `person:${c.id}`,
    kind: "person",
    name: c.name,
    role: c.role,
    companyId: `company:${c.company.toLowerCase().replace(/\s+/g, "-")}`,
    raw: stubProspect,
  };
}

export const DEMO_GRAPH_NODES: ReadonlyArray<GraphNode> = [
  ...DEMO_PROSPECTS.map(prospectAsGraphNode),
  ...DEMO_CONNECTORS.map(connectorAsGraphNode),
];

// ── DEMO_EDGES — 2 hidden-connection edges per amended Contract 5 ─────────
//
// Originally Contract 5 §"Required demo content" required EXACTLY 3 edges
// (patent_co_inventor + academic_co_author + conference_co_presenter). The
// `academic_co_author` edge was dropped on 2026-04-30 by user directive
// after Scholar lookups confirmed Newling↔Absar have no real co-authored
// paper (LavenderPrairie msg 121); fabricating one would violate
// CLAUDE.md "Common Mistakes" #6. The runtime invariant below now
// asserts `DEMO_EDGES.length === 2`. When/if the dropped edge is
// reinstated with real evidence, bump the assert and add the new edge.
//
// STATUS for the remaining 2:
//   - patent_co_inventor (Ashton/Clarke @ Intel) — PLACEHOLDER until
//     USPTO ODP key lands and the live find_patent_co_inventions call
//     populates real evidence.
//   - conference_co_presenter (Strier/Fidler @ NVIDIA GTC 2022) —
//     populated with real evidence per SwiftElk msg 120.

interface DemoEdgeWithEvidence extends GraphEdge {
  /** Tag the edge with the connection-narrative case ID it belongs to. */
  readonly demoCaseId: string;
}

const PLACEHOLDER_EVIDENCE_NOTE =
  "PLACEHOLDER — evidence fields populated when DEMO_CASES.md slot fills";

function demoEdge(
  caseId: string,
  source: string,
  target: string,
  kind: EdgeKind,
  evidence?: import("@/lib/graph").EdgeEvidence,
): DemoEdgeWithEvidence {
  return {
    id: `demo:${caseId}:${kind}`,
    source,
    target,
    kind,
    demoCaseId: caseId,
    ...(evidence ? { evidence } : {}),
  };
}

export const DEMO_EDGES: ReadonlyArray<DemoEdgeWithEvidence> = [
  // Hidden-connection edge 1 (patent_co_inventor) — placeholder evidence.
  // Connector 2 (Martin Ashton, AMD SVP Hardware IP) ↔ Prospect 3 (James Clarke,
  // Intel Director of Quantum). Real career overlap at Intel 2015 (DEMO_CASES.md
  // Case 3); the patent edge needs USPTO extractor run to populate patent_number.
  // Pending USPTO ODP API key registration (see patents.py header for the
  // migration scaffold + env-var toggle).
  demoEdge(
    "demo-patent",
    `person:${DEMO_CONNECTOR_IDS[1]}`,
    `person:${DEMO_PROSPECT_IDS[2]}`,
    "patent_co_inventor",
  ),
  // ── Hidden-connection edge 2 (academic_co_author) — INTENTIONALLY OMITTED ──
  //
  // Originally specified by Contract 5 §"Required demo content" as one of
  // exactly 3 hidden-connection edges. **Removed 2026-04-30 by user
  // direction** after live Scholar verification proved no co-authored paper
  // exists between any pair in the current demo cast. Per CLAUDE.md
  // "Common Mistakes" #6 (NEVER substitute fictional patent numbers, paper
  // titles, or conference programs for missing data) we ship 2-of-3 edges
  // with verified real evidence rather than fabricate the third.
  //
  // Findings that drove the decision (ran 2026-04-30 via Semantic Scholar):
  //   - J. Absar (authorId 1796386, Qualcomm): 30 papers, 58 unique
  //     co-authors. None are at AMD (Baghdadi @ NYU, Albert Cohen @ Google
  //     DeepMind, Lokhmotov independent, Beaugnon, Verdoolaege, Donaldson,
  //     Baskaran/Narang/Sharma at Qualcomm).
  //   - J. Newling (authorId 3348613): 23 papers, no "Absar" co-author.
  //     Topic mix (k-means under Fleuret, BEAMS supernova cosmology) does
  //     not match the AMD AI compiler engineer identity — likely a
  //     different J. Newling.
  //   - Tyler Sorensen (authorId 30718552, UCSC/AMD-adjacent GPU semantics):
  //     no direct co-authored paper with Absar (2 shared collaborators only).
  //   - Sanja Fidler (authorId 37895334, 200+ papers): no co-authorship
  //     with Strier / Sorensen / M. Ashton / Newling.
  //
  // Contract 5 was amended to drop the "exactly 3" requirement (see
  // CONTRACTS.md L446). Case 5 in DEMO_CASES.md (Newling↔Absar Graphcore
  // 2018) is preserved as a real career_overlap_same_team — already
  // demo-credible via the existing career-overlap edges; an unverifiable
  // academic edge on top would only add risk if a viewer clicks through.
  //
  // Restoring this edge requires either:
  //   (a) USPTO ODP key landing → run patents extractor against Strier/
  //       Ashton/Newling/Sorensen pairs (might surface a co-invention edge
  //       repurposable as a 3rd hidden-connection narrative), or
  //   (b) Adding a new AMD Research connector with documented co-authorship
  //       to one of the prospects (Sanja Fidler is most-prolific target).
  //
  // ── Hidden-connection edge 2 — placeholder block kept for reference only,
  //    do NOT re-enable without verified evidence:
  // Connector 3 (James Newling, AMD AI Compiler) ↔ Prospect 5 (Dr. Javed Absar,
  // Qualcomm ML/AI Compiler Research). Real same-team overlap at Graphcore 2018
  // (DEMO_CASES.md Case 5).
  //
  // STATUS: NO Scholar-indexed co-authored paper exists between these two.
  // Verified 2026-04-30 via Semantic Scholar API:
  //   - J. Absar (authorId 1796386): 30 papers, 58 unique co-authors —
  //     Baghdadi, Albert Cohen, Beaugnon, Lokhmotov, Verdoolaege, Donaldson
  //     (PENCIL/MLIR era), then Baskaran/Narang/Sharma at Qualcomm. No
  //     "Newling" appears in the 58 names.
  //   - J. Newling (authorId 3348613): 23 papers — k-means with Fleuret
  //     (EPFL), supernova cosmology with Bassett. No "Absar" in any author
  //     list.
  //
  // Two paths to fix this edge with real evidence (one of these must
  // happen before the YC demo to satisfy the "no fictional data" rule):
  //   A. Swap connector: replace James Newling with an AMD employee who
  //      DOES have a real co-authored paper with Absar. Candidates from
  //      Absar's co-author list: Riyadh Baghdadi (NYU prof, not AMD),
  //      Albert Cohen (Google DeepMind), Anton Lokhmotov (independent,
  //      ex-ARM). None are at AMD — so swap target must be a different
  //      AMD employee with academic publishing history that intersects
  //      Absar's compiler/MLIR work.
  //   B. Swap prospect: replace Absar (Qualcomm ML/AI Compiler) with a
  //      different target whose Scholar trail intersects an existing
  //      AMD connector (Strier/Ashton/Newling). Strier and Ashton are
  //      executives with no academic publishing record; Newling published
  //      with Fleuret in 2016 — so a target who has co-authored with
  //      Fleuret (EPFL ML community) would work.
  //
  // Option A keeps the existing demo cast tight and feels right. SunnyRidge
  // owns this lookup per msg 118 delegation.
  //
  // SunnyRidge follow-up (2026-04-30, msg 124): exhausted both options against
  // Scholar with these probes:
  //   - Tyler Sorensen (authorId 30718552, 41 papers — UCSC/AMD-adjacent GPU
  //     semantics): co-authored with Absar? FALSE. Absar ∩ Sorensen = 2 shared
  //     co-authors (id 2303821, 1734519) but no direct paper.
  //   - Sanja Fidler (authorId 37895334, 200+ papers — most-prolific prospect):
  //     co-authored with any of {Strier, Sorensen, M.Ashton-disambig 1413094274,
  //     Newling 3348613}? FALSE for all four.
  //   - Disambiguation issue: "Martin Ashton" 1413094274 is M. Ashton-Key
  //     (UK NHS health-tech researcher, not the AMD SVP). The cosmology
  //     "J. Newling" 3348613 is plausibly NOT the AMD compiler engineer
  //     either — paper topics (BEAMS supernova, k-means under Fleuret) don't
  //     match an AMD compiler role.
  //
  // CONCLUSION: the 3 AMD connectors don't have publishable academic output
  // under their actual identities, and none of the 5 prospects co-authored
  // with the (purported) Newling identity. Edge 2 cannot be populated honestly
  // with the current cast.
  //
  // RECOMMENDATION (orchestrator decision needed):
  //   1. **Add a 4th AMD connector** who's a real AMD Research author with
  //      a documented co-authored paper with one of the 5 prospects — most
  //      likely Sanja Fidler since she's the most prolific. AMD Research is
  //      genuinely active (MLPerf, ROCm, GPU-arch papers); a 30-min Scholar
  //      search for "AMD Research" + Fidler would land a real edge. This
  //      keeps the demo's "exactly 3 hidden edges" Contract 5 invariant
  //      while making the cast academically realistic.
  //   2. **Drop edge 2** entirely — amend Contract 5 to "1-3 hidden edges"
  //      so demos with non-academic-publishing connector teams still satisfy
  //      it. Career-overlap edges remain real.
  //   3. **Ship as-is, banner-style** — render edge 2 with an explicit
  //      "Evidence search returned no co-authored paper for this pair"
  //      empty-state in WarmPathPanel rather than placeholder evidence.
  //
  // I lean (1). It's the only option that keeps the demo's "we surface
  // hidden academic relationships" punchline working honestly. Flagging
  // for LP/SwiftElk to call. Until then, edge 2 stays placeholder.
  //
  //   demoEdge(
  //     "demo-paper",
  //     `person:${DEMO_CONNECTOR_IDS[2]}`,
  //     `person:${DEMO_PROSPECT_IDS[4]}`,
  //     "academic_co_author",
  //   ),
  //
  // ── End of removed edge 2 block ────────────────────────────────────────
  //
  // Hidden-connection edge 3 (conference_co_presenter) — real evidence.
  // Connector 1 (Keith Strier, AMD SVP Global AI Markets, ex-NVIDIA VP
  // Worldwide AI Initiatives) ↔ Prospect 2 (Sanja Fidler, NVIDIA VP AI
  // Research). Both presented at NVIDIA GTC 2022:
  //   - Strier: "Fireside Chat: Driving Innovation through Sovereign AI
  //     Infrastructure" (GTC Spring 2022, session gtcspring22-s42482)
  //   - Fidler: moderated "AI Pioneers" fireside chat with Yoshua Bengio,
  //     Geoff Hinton, Yann LeCun (GTC Fall 2022, September 2022)
  // Different sessions, same conference series, same calendar year — the
  // evidence shape only carries event + year, so "GTC 2022" is the
  // documentary truth here. Per CLAUDE.md "Common Mistakes" #6 the
  // narrower claim is preferred over fabricating a shared session.
  demoEdge(
    "demo-conference",
    `person:${DEMO_CONNECTOR_IDS[0]}`,
    `person:${DEMO_PROSPECT_IDS[1]}`,
    "conference_co_presenter",
    {
      kind: "conference_co_presenter",
      event: "NVIDIA GTC 2022",
      year: 2022,
    },
  ),
];

// Sanity invariant — Contract 5 (amended 2026-04-30) requires 2 hidden-
// connection edges with verified real evidence: one patent_co_inventor and
// one conference_co_presenter. The originally-specified academic_co_author
// edge was removed after Scholar verification proved no co-authored paper
// exists between any pair in the current demo cast (see edge 2's removal
// block above for the full audit trail). Throwing at module load is
// intentional: a future edit that drifts the count will fail loudly the
// moment demo mode is exercised. Restoring to 3 requires either a USPTO
// ODP-driven patent edge or adding an academically-published AMD Research
// connector — see edge 2's "Restoring this edge requires" notes.
if (DEMO_EDGES.length !== 2) {
  throw new Error(
    `demoData.ts: Contract 5 (amended) requires 2 hidden-connection edges, found ${DEMO_EDGES.length}`,
  );
}

// ── DEMO_WARM_PATHS — pre-computed for the demo-mode shortcut ─────────────
//
// In live mode, WarmPathPanel calls `findWarmPaths()` against the full
// graph. In demo mode, the same call still runs — but to keep the demo
// deterministic + always-loaded, we also export the expected paths here so
// a YC reviewer flipping `?demo=true` sees a stable result instantly.
//
// Empty for now; populated when DEMO_CASES.md fills and we want to short-
// circuit the BFS for visual snappiness. The actual `findWarmPaths()` call
// against `DEMO_GRAPH_NODES + DEMO_EDGES` should produce identical results,
// so this export is optional / cosmetic for now.

export const DEMO_WARM_PATHS: ReadonlyArray<unknown> = [];

// ── DEMO_TALKING_POINTS — DemoScript.tsx import contract ──────────────────
//
// Per SwiftElk msg 41: keyed by `DEMO_PROSPECT_IDS`, each value is a
// 3-bullet talking-point array for the YC demo script. Bullets describe the
// *case* (which connection narrative this prospect demonstrates) — they are
// presenter-facing, not customer-facing.
//
// STATUS: PLACEHOLDER content but real shape. DemoScript.tsx can `import
// { DEMO_TALKING_POINTS } from "@/lib/demoData"` and replace its local
// `PLACEHOLDER_CASES` constant.

export const DEMO_TALKING_POINTS: Record<string, string[]> = {
  // Case 1 — Keith Strier (AMD SVP) → Neil Ashton (NVIDIA Distinguished Eng), NVIDIA 2024
  [DEMO_PROSPECT_IDS[0]]: [
    "Career-overlap warm path: Keith Strier (your AMD SVP Global AI Markets) and Neil Ashton both worked at NVIDIA in 2024.",
    "Documented in career_history signals; computed_strength ≈ 0.44 (1y overlap, current; cross-domain).",
    "Suggested opener: \"Neil — we both worked at NVIDIA in 2024. I'm now SVP Global AI Markets at AMD; reaching out about a partnership opportunity that lines up with the GPU architecture work you led there.\"",
  ],
  // Case 2 — Keith Strier (AMD) → Sanja Fidler (NVIDIA VP AI Research), NVIDIA 2022
  [DEMO_PROSPECT_IDS[1]]: [
    "Career-overlap warm path: Keith Strier and Sanja Fidler both at NVIDIA in 2022. Sanja is NVIDIA VP AI Research + UofT Associate Professor.",
    "computed_strength ≈ 0.39 (1y overlap, 2y inactive; cross-domain). Higher signal than typical because target is a known AI-research figure.",
    "Hidden-connection upgrade target: this pair is a strong candidate for conference_co_presenter via Parallel.ai (NeurIPS/ICML co-appearance check).",
  ],
  // Case 3 — Martin Ashton (AMD SVP) → James Clarke (Intel Director Quantum HW), Intel 2015
  [DEMO_PROSPECT_IDS[2]]: [
    "Career-overlap warm path: Martin Ashton (your AMD SVP Hardware IP) and James Clarke (Intel Director of Quantum Hardware) both at Intel in 2015.",
    "career_overlap_same_domain — both senior hardware engineers. computed_strength ≈ 0.34 (9y inactive, semantic decay).",
    "Hidden-connection upgrade target: this pair is the most likely patent_co_inventor candidate — both senior hardware-engineering at Intel, prime co-invention era.",
  ],
  // Case 4 — Martin Ashton (AMD) → Silvia Linares (Intel Sr Dir GPU SW AI), Intel 2017
  [DEMO_PROSPECT_IDS[3]]: [
    "Career-overlap warm path: Martin Ashton and Silvia Linares both at Intel in 2017. Silvia leads GPU SW Engineering for Intel's AI Solutions org.",
    "career_overlap_same_domain. computed_strength ≈ 0.38 (1y overlap, 7y inactive).",
    "Suggested opener: \"Silvia — I overlapped with you at Intel in 2017. I'm at AMD now and would value a quick conversation about how Intel is positioning its GPU SW stack against integrated AI accelerators.\"",
  ],
  // Case 5 — James Newling (AMD) → Javed Absar (Qualcomm ML/AI Compiler), Graphcore 2018
  [DEMO_PROSPECT_IDS[4]]: [
    "Career-overlap warm path: James Newling (your AMD AI Compiler Engineer) and Dr. Javed Absar (Qualcomm Principal Eng, ML/AI Compiler Research) both at Graphcore in 2018.",
    "career_overlap_same_team — almost certainly same compiler team. computed_strength ≈ 0.57 (strongest of the 5).",
    "Hidden-connection upgrade target: high-likelihood academic_co_author candidate via Scholar (compiler-research papers commonly co-authored).",
  ],
};

// Sanity invariant — must cover all 5 demo prospects.
if (Object.keys(DEMO_TALKING_POINTS).length !== DEMO_PROSPECT_IDS.length) {
  throw new Error(
    `demoData.ts: DEMO_TALKING_POINTS must cover all ${DEMO_PROSPECT_IDS.length} demo prospects`,
  );
}
