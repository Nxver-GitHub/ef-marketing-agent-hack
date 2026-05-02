# CLAUDE.md — Credence v3

> This file is the authoritative reference for any AI agent (Claude Code, subagents, or otherwise) working on the Credence codebase. Read this entire file before touching any code. Do not skip sections. When in doubt about intent, re-read this file rather than inferring.

---

## What Credence Is

Credence is the warm outbound layer the B2B GTM stack is missing.

Every B2B sales organization is sitting on a massive, completely unmapped relationship asset. The collective relationship graph of their employees — co-inventors, co-authors, former colleagues, PhD labmates, standards committee peers — connects them to thousands of people at their target accounts through documented, real relationships. Nobody has ever mapped this. The data has always existed. The tools to assemble it have not.

Credence maps that asset and tells the sales team how to use it.

The two core features:

1. **Hidden Connections Network** — surfaces non-obvious, documented relationships between your team and your buyers. Patent co-inventions, academic co-authorship, conference co-presentations, standards committee peers, shared PhD advisors, career overlaps. Relationships that exist in public records but have never been cross-referenced against a sales contact list.

2. **Inferred Org Charts** — probabilistic reconstruction of company reporting hierarchies from job postings, press releases, SEC filings, conference speaker bios, patent inventor clusters, and GitHub org structures. Tells you who owns the budget before you make the first call.

**The product is not**: a contact database, an enrichment tool, or a cold email tool. Credence does not compete with Apollo or Clay on coverage. It is the layer above those tools that turns contact data into warm introductions.

---

## Current State

- Frontend deployed at ef-marketing-agent-hack.vercel.app
- 20k prospects in Supabase, 1k fully enriched with real signals
- Force-directed graph on /discover using react-force-graph-2d
- GraphChat left rail (Z.AI agent, 4 tools: focus_node, filter, explain, expand_node)
- NodeInspector right rail (identity card, sub-scores, evidence trail)
- 8 edge types: works_at, reports_to, located_in, evidence_cited, scope_signal, partnership, past_employer, vertical
- Scoring pipeline: Authenticity 40% + Authority 40% + Warmth 20%
- Falsification notes on every score
- FastAPI backend: /chat and /validate endpoints
- /validate, /prospect/:id, /settings all functional

**What is missing and must be built:**
- patent_co_inventor edge type
- academic_co_author edge type
- conference_co_presenter edge type
- standards_committee edge type
- Warm path engine (BFS over connection graph)
- Warm path UI panel in NodeInspector
- USPTO and Semantic Scholar data pipelines in FastAPI
- Demo mode (?demo=true) for YC video

---

## Repository Structure

```
credence/
├── src/
│   ├── components/
│   │   ├── TopBar.tsx          -- edge type filter pills + global controls
│   │   ├── NodeInspector.tsx   -- right rail: identity, scores, evidence, warm paths
│   │   ├── GraphChat.tsx       -- left rail: agent chat interface
│   │   └── GraphCanvas.tsx     -- react-force-graph-2d wrapper
│   ├── pages/
│   │   ├── Discover.tsx        -- main graph page
│   │   ├── Validate.tsx        -- search and ranking
│   │   ├── ProspectDetail.tsx  -- /prospect/:id deep dive
│   │   └── Settings.tsx        -- live weight editing
│   ├── lib/
│   │   ├── graph.ts            -- EdgeKind type system, graph types, edge configs
│   │   ├── scoring.ts          -- score computation (currently client-side)
│   │   ├── mockStore.ts        -- hardcoded demo data, mock graph state
│   │   ├── warmPaths.ts        -- TO BE CREATED: BFS warm path engine
│   │   └── demoData.ts         -- TO BE CREATED: demo mode data
│   ├── store/
│   │   └── graphStore.ts       -- Zustand store for graph state
│   └── types/
│       └── index.ts            -- shared TypeScript types
├── server/
│   ├── main.py                 -- FastAPI app entry point
│   ├── routes/
│   │   ├── chat.py             -- POST /chat
│   │   ├── validate.py         -- POST /validate
│   │   └── signals.py          -- TO BE CREATED: POST /signals/discover-connections
│   ├── lib/
│   │   ├── supabase.py         -- Supabase client
│   │   ├── scoring.py          -- server-side scoring logic
│   │   └── extractors/         -- TO BE CREATED: connection extractors
│   │       ├── patents.py      -- PatentsView API extractor
│   │       ├── scholar.py      -- Semantic Scholar extractor
│   │       └── career.py       -- career overlap extractor
│   └── requirements.txt
├── CONTRACTS.md                -- interface contracts between sessions/modules
├── DEMO_CASES.md               -- 5 real warm path examples from career overlap SQL
└── CLAUDE.md                   -- this file
```

---

## The Data Model

Understand this before writing any code that touches data. Every feature is built on this schema.

### Core Entities

**persons** — canonical person record. One row per real human. Multiple source records may map to one person via entity resolution.

Key fields: id, canonical_name, name_variants[], linkedin_url, orcid, uspto_inventor_id, current_company_id, current_title, current_seniority_score, current_functional_domain, enrichment_tier (0-3), blocking_keys[]

**companies** — canonical company record. org_chart_confidence, org_chart_last_built, org_chart_signal_count track the org chart build state per company.

**employment_periods** — every job a person has held. Backbone of both warm path and org chart features. Key fields: person_id, company_id, title, functional_domain, seniority_score, start_year, end_year, is_current, inferred_team, inferred_team_confidence.

**education_periods** — every academic institution. advisor_person_id links to another person record if the advisor is in the database.

**events** — conferences, workshops, standards meetings, award ceremonies.

**event_appearances** — who appeared at each event, in what role (presenter, panelist, session_chair, attendee, keynote).

**patents** — USPTO patent records. Linked to companies via assignee_company_id.

**patent_inventors** — junction: patent_id + person_id. The source of patent_co_inventor connections.

**papers** — Semantic Scholar paper records.

**paper_authors** — junction: paper_id + person_id. The source of academic_co_author connections.

**standards_memberships** — JEDEC, IEEE SA, SEMI committee membership records.

### The Connection Graph

**person_connections** — pre-computed pairwise connections. This table is the core of the product. Never query it at build time; always query it at read time with indexes.

```
person_a_id < person_b_id  (always — enforced by CHECK constraint)
connection_type            (patent_co_inventor, academic_co_author, etc.)
base_strength              (type-specific baseline, see STRENGTH_TABLE below)
recency_factor             (exp decay from last active year)
frequency_factor           (log boost from corroboration_count)
corroboration_factor       (boost from source_type_count)
computed_strength          (the indexed value: base * recency * frequency * corroboration)
```

The warm path BFS queries computed_strength. The strength model:

```python
computed_strength = min(0.99,
    base_strength
    * exp(-DECAY_RATE[type] * years_since_active)
    * (1.0 + log(corroboration_count) * 0.15)
    * (1.0 + source_type_count * 0.10)
)
```

STRENGTH_TABLE (base values):
```
patent_co_inventor:       0.95
same_phd_advisor:         0.92
co_board_member:          0.90
academic_co_author_multi: 0.90  (3+ papers)
academic_co_author_single:0.85
career_overlap_same_team: 0.88
standards_committee_peer: 0.82
conference_co_presenter:  0.80
co_investor:              0.78
career_overlap_same_domain:0.72
career_overlap_general:   0.60
alumni_network:           0.25
conference_co_attendee:   0.20
```

DECAY_RATES per year inactive:
```
patent_co_inventor:       0.01
same_phd_advisor:         0.01
co_board_member:          0.02
academic_co_author:       0.02
standards_committee_peer: 0.03
career_overlap_same_team: 0.04
co_investor:              0.04
conference_co_presenter:  0.05
career_overlap_general:   0.06
alumni_network:           0.08
conference_co_attendee:   0.20
```

**connection_evidence** — what exactly supports each connection record. Linked by person_connections.evidence_ids[].

### Org Chart Tables

**org_reporting_edges** — inferred reporting relationships. Every edge has: confidence (local), path_confidence (propagated from root), inference_method, valid_from, valid_to, is_current.

**org_functional_clusters** — people grouped by functional domain before hierarchy is assigned. This is the key structural insight: cluster by domain first, assign hierarchy within clusters second. Never assign hierarchy across domain boundaries at a lower level.

**org_cluster_members** — junction: cluster_id + person_id + membership_confidence.

**person_scope_estimates** — what each person owns: owns_products[], owns_technologies[], owns_functions[], owns_regions[], team_size_min/max, budget_authority_level.

**org_chart_corrections** — user-submitted corrections. Every row is a training signal for the scoring model.

**org_signal_performance** — per-signal-type error_count and success_count, updated by the correction analysis pipeline. Used to tune edge scoring weights over time.

---

## Architecture Decisions — Do Not Reverse Without Discussion

### Decision 1: person_a_id < person_b_id is always enforced

Every row in person_connections has person_a_id < person_b_id as a UUID comparison. This prevents duplicate edges (A→B and B→A). The warm path BFS must query both directions: `WHERE person_a_id = $id OR person_b_id = $id`. Do not add directional edges to this table.

### Decision 2: Functional clustering before hierarchy assignment

The org chart pipeline clusters people by functional_domain + sub_domain before any hierarchy edges are assigned. This is not optional. Assigning hierarchy by seniority sort alone produces a ladder. The product produces trees with functional branches.

The IC track (Distinguished Engineer, Principal Engineer, Staff Engineer) runs parallel to the management track at the same seniority level. A Distinguished Engineer (seniority 55) does not report to a Director (seniority 60) just because of the seniority gap. They are peers. The functional clustering + management track filter handles this.

### Decision 3: Explicit signals override implicit scoring

In org chart edge scoring, any explicit signal (job posting reporting clause, press release, SEC proxy, LinkedIn Reports-To field) returns immediately with its own confidence score. The implicit scoring model (seniority gap + domain match + patent cluster + etc.) only runs when no explicit signal exists. Never combine explicit and implicit — explicit wins.

### Decision 4: Unknown nodes are rendered, not omitted

When a job posting references a role that cannot be mapped to a known person, render an `[Unknown Role Title]` node with distinct visual treatment. This is honest and useful. Omitting unknown nodes creates a false confidence that the org chart is complete.

### Decision 5: raw_data stays out of Postgres

The signals table stores structured_value JSONB capped at 4KB. Raw API responses go to S3 at raw_data_uri. This is essential for scale. Do not put multi-KB API blobs in structured_value.

### Decision 6: Scores are always versioned

score computations reference a weight_version_id. When weights change, a new version row is inserted. Existing scores are NOT invalidated — they are recomputed lazily on read and replaced by background job. The UI shows "Score computed with previous weights, updated X minutes ago" during the catchup window.

### Decision 7: Warm paths are pre-materialized, not computed at query time

The person_connections table is the materialized warm path graph. Adding new connection extractors means writing to this table, not computing on the fly at query time. Warm path BFS over pre-materialized edges targets <50ms at 2M persons. Computing on-the-fly at query time will not scale.

---

## Edge Scoring Detail — Org Chart

For reference when modifying the org chart pipeline. These weights are the current defaults and are tuned by the optimization loop:

```
Signal                          Max contribution    Notes
-----------------------------------------------------------------------
Seniority gap naturalness       0.30                Natural = 8-15 point gap
Same functional domain          0.25
Same sub-domain                 0.15                On top of domain match
Manager title signal            0.10                "manager/director/VP" in title
Team size capacity              0.05                Manager has room for more reports
Patent cluster membership       0.15                Scaled by shared_patents / 3
Geographic scope compat         0.08                Same scope or manager is global
-----------------------------------------------------------------------
Total possible implicit:        0.95 (capped)
```

Gap naturalness scoring:
- gap 8-15 → +0.30 (natural manager-report gap)
- gap 5-8 → +0.18 (peer-ish, possible)
- gap 15-25 → +0.12 (skip-level, unusual but real)
- gap <5 or >25 → +0.00 (implausible)

Span of control limits by seniority:
```
seniority >= 85 (C-suite):  max 8 direct reports
seniority >= 75 (SVP):      max 7
seniority >= 65 (VP):       max 8
seniority >= 55 (Director): max 10
seniority < 55 (Manager):   max 12
```

---

## Seniority Taxonomy

Any code that classifies titles must use this taxonomy consistently:

```
CEO=100, President=95, COO/CTO/CFO/CPO/CRO=88-90
EVP=82, SVP=80
VP=70, Group VP=72
Senior Director=62, Director=60, Principal Director=63
Senior Manager=52, Engineering Manager=50, Group Manager=52
Distinguished Engineer=55, Principal Engineer=48, Staff Engineer=45
Senior Engineer=40, Engineer=35
```

IC tracks parallel management tracks at the same seniority level. This is the fundamental insight that most org chart tools miss.

---

## Functional Domain Taxonomy

Any code that classifies functional domains must use these keys consistently:

```
hardware_engineering    -- chip design, RTL, verification, physical design, analog,
                           mixed signal, memory design, SoC, architecture
software_engineering    -- software, firmware, embedded, SDK, driver, BSP
product_management      -- product, program, PM, TPM, roadmap
manufacturing_ops       -- manufacturing, operations, supply chain, yield, process,
                           fab, foundry, quality, reliability
sales_marketing         -- sales, marketing, BD, GTM, account management, partnerships
research                -- research, advanced development, pathfinding, exploratory
finance_legal           -- finance, legal, compliance, accounting, tax
people_ops              -- HR, recruiting, people operations, culture
general_management      -- GM, general manager, business unit, P&L owner, president
```

---

## NLP Extraction Patterns — Org Chart Signals

Use these exact patterns for job posting and press release parsing. Do not invent new patterns without testing against real postings.

```python
REPORTING_PATTERNS = [
    r"reports\s+(?:directly\s+)?to\s+(?:the\s+)?([A-Z][^,.]+(?:Officer|President|Director|VP|Head|Manager|Lead))",
    r"reporting\s+line\s+to\s+([A-Z][^,.]+)",
    r"will\s+work\s+(?:closely\s+)?(?:with|under)\s+([A-Z][^,.]+)\s*,\s*(?:the\s+)?(?:VP|SVP|Director|Head)",
    r"dotted\s+line\s+to\s+([A-Z][^,.]+)",
    r"(?:direct|indirect)\s+report\s+to\s+([A-Z][^,.]+)",
    r"management\s+chain.*?(?:VP|SVP|Director|Head)\s+of\s+([^,.]+)",
]

SCOPE_PATTERNS = [
    r"(?:manage|lead|oversee|responsible\s+for)\s+(?:a\s+team\s+of\s+)?(\d+)\+?\s+(?:engineers?|people|employees|reports?)",
    r"team\s+(?:of|size)\s+(\d+)",
    r"(\d+)\s+direct\s+reports?",
    r"growing\s+team\s+of\s+(\d+)",
]

LEADERSHIP_VERBS = [
    "leads", "heads", "manages", "oversees", "directs",
    "runs", "is responsible for", "spearheads", "drives"
]
```

---

## External APIs

### PatentsView (USPTO) — Free, No Auth Required

```
Base URL: https://search.patentsview.org/api/v1/
Find patents by inventor name:
  GET /patent/?q={"_and":[{"_contains":{"inventor_name_first":"Wei"}},
                           {"_contains":{"inventor_name_last":"Chen"}}]}
Find co-inventors for a patent:
  GET /inventor/?q={"patent_id":"10234567"}
Rate limit: 45 requests/minute on free tier
```

### Semantic Scholar — Free, Rate Limited

```
Base URL: https://api.semanticscholar.org/graph/v1/
Find author by name:
  GET /author/search?query=Wei+Chen&fields=name,affiliations,paperCount
Get author's papers:
  GET /author/{authorId}/papers?fields=title,year,authors,venue,citationCount
Rate limit: 100 requests/5min unauthenticated; register for 1 req/sec
```

### Supabase Client (Python)

```python
from supabase import create_client
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"])

# Read
result = supabase.table("prospects").select("*").eq("id", prospect_id).single().execute()

# Upsert signal
supabase.table("signals").upsert({
    "prospect_id": prospect_id,
    "signal_type": "patent_co_inventor",
    "structured_value": {...},
    "confidence": 0.95,
}).execute()
```

### Supabase Client (TypeScript/Frontend)

```typescript
import { createClient } from '@supabase/supabase-js'
const supabase = createClient(
  import.meta.env.VITE_SUPABASE_URL,
  import.meta.env.VITE_SUPABASE_ANON_KEY
)
```

---

## Environment Variables

```
# Frontend (.env.local)
VITE_SUPABASE_URL=
VITE_SUPABASE_ANON_KEY=
VITE_API_BASE_URL=http://localhost:8000

# Backend (.env)
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
Z_AI_API_KEY=
```

---

## How to Work on This Codebase

### Always Read CONTRACTS.md First

CONTRACTS.md defines the interfaces between all modules. If you are adding a new endpoint, a new TypeScript function, or a new data pipeline, check CONTRACTS.md first. If the interface you are implementing is already defined there, implement it exactly as specified. If you need a new interface, add it to CONTRACTS.md before writing any implementation code.

### Always Read DEMO_CASES.md Before Touching mockStore.ts or demoData.ts

DEMO_CASES.md contains 5 real warm path examples derived from the career overlap SQL query. These are the concrete examples that power the demo. Any hardcoded demo data must reference these real cases. Do not invent fictional patent numbers, paper titles, or career overlaps.

### File Modification Rules

**graph.ts** — the EdgeKind type union is the source of truth for all edge types. Any new edge type must be added here first, then propagated to TopBar.tsx, index.css, and Discover.tsx. Never hardcode edge type strings outside of graph.ts.

**mockStore.ts** — only modify to add demo data from DEMO_CASES.md. Do not add real API calls here. The mock store is for offline demo and testing only.

**scoring.ts** — the client-side scoring implementation. Will eventually be deprecated in favor of the FastAPI backend, but do not remove it until the backend scoring endpoint is live and tested.

**graphStore.ts** — Zustand store. Any new global state for the graph must go here. Do not create local component state for anything that needs to be shared between GraphChat, GraphCanvas, and NodeInspector.

**server/main.py** — do not add route handlers directly to this file. Routes go in server/routes/ as separate modules registered with app.include_router().

---

## How to Use Subagents

For any task that involves multiple distinct concerns (e.g., "add the warm path engine AND update the UI to show it"), decompose into subagent tasks and run them in parallel or in sequence depending on dependencies.

### When to Spawn a Subagent

Spawn a subagent when:
- The task can be clearly scoped to a single file or a small set of tightly related files
- The task has a clear interface contract (defined in CONTRACTS.md) that the subagent can implement without needing full codebase context
- The task is parallelizable — it does not depend on the output of another concurrent task
- The task is iterative and benefits from a focused context window

Do NOT spawn a subagent when:
- The task requires understanding the full data flow from frontend to backend
- The task modifies a shared interface that other tasks depend on
- You are not sure what the task is yet (explore first, then decompose)

### Subagent Task Template

When spawning a subagent, give it:

```
1. The specific file(s) it is responsible for
2. The interface contract it must satisfy (from CONTRACTS.md)
3. The exact function/component signature to implement
4. The data it will receive and must return
5. Any constants or type definitions it must use (from this CLAUDE.md)
6. The test condition: how you will verify it is correct
```

Example well-formed subagent task:

```
Subagent task: implement src/lib/warmPaths.ts

You are implementing the warm path BFS engine. This file is self-contained.
It takes a graph (nodes + edges) and returns ranked warm paths.

Interface (from CONTRACTS.md):
  export function findWarmPaths(
    targetNodeId: string,
    sourceNodeIds: string[],
    graph: { nodes: GraphNode[], edges: GraphEdge[] },
    options: WarmPathOptions
  ): WarmPath[]

  interface WarmPathOptions {
    maxHops: number      // default 3
    minStrength: number  // default 0.30
    warmEdgeKinds: EdgeKind[]
  }

  interface WarmPath {
    nodes: GraphNode[]
    edges: GraphEdge[]
    strength: number          // product of all edge strengths
    explanation: string       // "Sarah Kim co-invented US Patent 10,234,567 with Wei Chen at Intel (2018)"
    suggested_opener: string  // first line of outreach email
  }

Edge strength defaults (use these exactly, from CLAUDE.md STRENGTH_TABLE):
  patent_co_inventor: 0.95
  academic_co_author: 0.85
  standards_committee: 0.82
  conference_co_presenter: 0.75
  education: 0.65
  career_overlap: 0.60
  colleague: 0.40

Algorithm: BFS from each source node. Only traverse edges with
kind in warmEdgeKinds. Path strength = product of edge strengths.
Naturally deprioritizes longer paths. Return paths sorted by
path strength descending.

Test condition: given a graph with a patent_co_inventor edge
between node A and node C (via node B), findWarmPaths(C, [A], graph)
should return a path with strength 0.95 and a generated explanation.
```

### Parallelizable Task Sets

These tasks can run as parallel subagents because they do not share output files:

**Set 1 (schema + mock data layer):**
- Subagent A: extend graph.ts with 4 new EdgeKinds
- Subagent B: update index.css with new edge color variables
- These can merge cleanly since they edit different files

**Set 2 (engine + UI):**
- After Set 1 is merged:
- Subagent A: implement src/lib/warmPaths.ts
- Subagent B: implement WarmPathPanel section in NodeInspector.tsx
- Subagent B should import from warmPaths.ts but only needs the interface contract, not the implementation

**Set 3 (backend pipeline):**
- Independent of Set 2:
- Subagent A: server/routes/signals.py (the endpoint)
- Subagent B: server/lib/extractors/patents.py (PatentsView extractor)
- Subagent C: server/lib/extractors/scholar.py (Semantic Scholar extractor)
- These are fully independent; the endpoint calls the extractors via asyncio.gather

---

## How to Use REPL Loops

Use REPL loops (bash tool in Claude Code, or iterative Python/TS execution) for any task that involves:
- Data exploration before writing code
- Iterative debugging of API responses
- SQL query development and validation
- Verifying a regex pattern against real text
- Checking that a transformation produces the expected shape

### REPL Loop Patterns

**Pattern 1: Explore before implement**

Before writing the PatentsView extractor, run this in the REPL first:

```python
import httpx
import json

# Explore what the API actually returns for a known inventor
response = httpx.get(
    "https://search.patentsview.org/api/v1/patent/",
    params={
        "q": json.dumps({"_and": [
            {"_contains": {"inventor_name_first": "Wei"}},
            {"_contains": {"inventor_name_last": "Chen"}}
        ]}),
        "f": json.dumps(["patent_number", "patent_title",
                         "inventor_name_first", "inventor_name_last",
                         "assignee_organization"]),
        "o": json.dumps({"per_page": 5})
    }
)
print(json.dumps(response.json(), indent=2))
```

Look at the actual response shape before writing any parsing code. The API may return a different structure than you expect.

**Pattern 2: SQL iteration against real data**

Before writing the career overlap extractor, iterate on the SQL in a REPL with your actual Supabase data:

```python
from supabase import create_client
import os

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"])

# First: understand what the signals table actually contains
result = supabase.table("signals")\
    .select("signal_type, value")\
    .eq("signal_type", "career_history")\
    .limit(3)\
    .execute()

for row in result.data:
    print(row["signal_type"])
    print(json.dumps(row["value"], indent=2)[:500])
    print("---")
```

Only after you understand the actual data shape should you write the career overlap extraction logic.

**Pattern 3: Regex validation**

Before adding a new REPORTING_PATTERN, test it against 10 real job posting sentences:

```python
import re

# Real sentences from TSMC, Intel, ASML job postings
test_sentences = [
    "The role reports directly to the VP of Process Engineering",
    "You will work closely under Dr. Wei Chen, the SVP of R&D",
    "This position has a dotted line to the Chief Technology Officer",
    "Reporting line to the Head of Memory Architecture",
    "The successful candidate will have 3 direct reports",
    "Oversee a team of 12 engineers across 3 locations",
]

pattern = r"reports\s+(?:directly\s+)?to\s+(?:the\s+)?([A-Z][^,.]+(?:Officer|President|Director|VP|Head|Manager|Lead))"

for sentence in test_sentences:
    matches = re.findall(pattern, sentence, re.IGNORECASE)
    print(f"Input: {sentence[:60]}")
    print(f"Match: {matches}")
    print()
```

If the pattern does not match what you expect, fix it in the REPL before putting it in source code.

**Pattern 4: Transform verification**

Before writing the edge strength computation, verify the math is correct:

```python
from math import exp, log

def compute_strength(base, decay_rate, years_inactive, corroboration_count, source_type_count):
    recency = exp(-decay_rate * years_inactive)
    frequency = 1.0 + log(max(1, corroboration_count)) * 0.15
    corroboration = 1.0 + source_type_count * 0.10
    return min(0.99, base * recency * frequency * corroboration)

# Verify: patent co-invention from 2018 (7 years ago), 2 co-inventions, 2 source types
strength = compute_strength(0.95, 0.01, 7, 2, 2)
print(f"Patent strength (2018, 2 patents, 2 sources): {strength:.3f}")
# Expected: ~0.95 * 0.932 * 1.104 * 1.20 = ~1.17 -> capped at 0.99
# If this is wrong, the model needs adjustment before writing it into source
```

**Pattern 5: API shape discovery**

For any external API that is new to you, always run at least one exploratory call in the REPL before writing a parser. Never assume the response shape from documentation alone — documentation is often stale.

---

## Iterative Development — The Right Loop

For any significant feature, the correct development loop is:

```
1. Read CONTRACTS.md and DEMO_CASES.md
2. Run REPL exploration to understand actual data shape
3. Write the minimum implementation that satisfies the contract
4. Test against the test condition defined in the task
5. Iterate in the REPL on any failures before touching source files
6. Only commit code that passes the test condition
7. Update CONTRACTS.md if the interface changed during implementation
```

Do not skip step 2. Do not skip step 5. Code written without REPL validation takes longer total even if it feels faster in the moment.

---

## The Warm Path Engine — Implementation Detail

`src/lib/warmPaths.ts` — the client-side warm path BFS. This is the most important new piece of frontend code.

### Algorithm

```
Input:
  targetNodeId: the person being evaluated (the prospect)
  sourceNodeIds: all persons with edges in the current graph view
                 (these represent your company's people)
  graph: current graph state from graphStore
  options: maxHops, minStrength, warmEdgeKinds

Algorithm:
  For each source in sourceNodeIds:
    BFS from source, traversing only warmEdgeKinds
    At each hop: path_strength *= edge.strength
    If path_strength < minStrength: prune this path
    If current node == targetNodeId: record as a completed path
    If hop count == maxHops: stop this branch

  Deduplicate paths (same set of nodes, different traversal order)
  Sort by path_strength descending
  For each path: generate explanation string and suggested_opener
  Return top 10 paths

Path strength = product of edge strengths:
  1-hop path: 0.95 (direct patent co-invention)
  2-hop path: 0.95 * 0.80 = 0.76
  3-hop path: 0.95 * 0.80 * 0.70 = 0.53

This naturally penalizes longer paths without an arbitrary cutoff.
```

### Explanation Generation

The explanation string must be specific, not generic. It tells the sales rep exactly what the connection is.

```typescript
function generateExplanation(path: WarmPath, graph: Graph): string {
  const firstEdge = path.edges[0]

  switch (firstEdge.kind) {
    case "patent_co_inventor":
      return `${path.nodes[0].name} co-invented ${firstEdge.evidence?.patentTitle ?? "a patent"}
              with ${path.nodes[1].name} at ${firstEdge.evidence?.assignee ?? "their shared employer"}
              (${firstEdge.evidence?.year ?? "year unknown"})`

    case "academic_co_author":
      return `${path.nodes[0].name} co-authored "${firstEdge.evidence?.paperTitle ?? "a paper"}"
              with ${path.nodes[1].name} at ${firstEdge.evidence?.venue ?? "a conference"}
              (${firstEdge.evidence?.year ?? "year unknown"},
               ${firstEdge.evidence?.citationCount ?? 0} citations)`

    case "standards_committee":
      return `${path.nodes[0].name} and ${path.nodes[1].name} served on
              the ${firstEdge.evidence?.committee ?? "standards committee"}
              together (${firstEdge.evidence?.years ?? "active period unknown"})`

    case "conference_co_presenter":
      return `${path.nodes[0].name} and ${path.nodes[1].name} co-presented
              at ${firstEdge.evidence?.event ?? "a conference"}
              (${firstEdge.evidence?.year ?? "year unknown"})`

    default:
      return `${path.nodes[0].name} and ${path.nodes[1].name} have
              a ${firstEdge.kind.replace(/_/g, " ")} connection`
  }
}
```

### Suggested Opener Generation

The opener is the first line of the outreach email. It must be specific and reference the actual connection.

```typescript
function generateSuggestedOpener(path: WarmPath): string {
  const connector = path.nodes[0]     // the person at your company
  const target = path.nodes.at(-1)!   // the prospect being reached
  const firstEdge = path.edges[0]

  switch (firstEdge.kind) {
    case "patent_co_inventor":
      return `${connector.name} — I noticed we co-invented ${firstEdge.evidence?.patentTitle ?? "a patent"} together at ${firstEdge.evidence?.assignee ?? "our shared employer"} back in ${firstEdge.evidence?.year ?? "years past"}. I'm now at [Company] and would love to reconnect.`

    case "same_phd_advisor":
      return `${connector.name} — we both worked under ${firstEdge.evidence?.advisorName ?? "the same advisor"} at ${firstEdge.evidence?.institution ?? "grad school"}. I'm now at [Company] working on [relevant problem] and thought it was worth reaching out.`

    case "standards_committee":
      return `${connector.name} — we sat on the ${firstEdge.evidence?.committee ?? "standards committee"} together for ${firstEdge.evidence?.years ?? "several years"}. I'm now at [Company] and wanted to reconnect.`

    default:
      return `${connector.name} — we crossed paths at ${firstEdge.evidence?.venue ?? "a shared event or employer"} and I'm now at [Company] working on something I think would be relevant to you.`
  }
}
```

---

## The FastAPI Signals Endpoint — Implementation Detail

`server/routes/signals.py` — the backend warm path discovery endpoint.

### POST /signals/discover-connections

```python
@router.post("/signals/discover-connections")
async def discover_connections(request: DiscoverConnectionsRequest):
    """
    Given two prospect IDs, find documented connections between them
    via USPTO (patents) and Semantic Scholar (papers).
    Run both searches in parallel. Write results to signals table.
    Target: under 5 seconds total.
    """

    # Step 1: Load both prospects
    person_a = await load_prospect(request.prospect_a_id)
    person_b = await load_prospect(request.prospect_b_id)

    # Step 2: Parallel discovery
    patents_task = find_patent_co_inventions(person_a, person_b)
    papers_task = find_paper_co_authorships(person_a, person_b)

    patents, papers = await asyncio.gather(patents_task, papers_task)

    # Step 3: Write to signals table
    connections = []
    for patent in patents:
        signal = {
            "prospect_id": request.prospect_a_id,
            "signal_type": "patent_co_inventor",
            "structured_value": {
                "connected_to": request.prospect_b_id,
                "patent_number": patent.number,
                "patent_title": patent.title,
                "filing_date": patent.filing_date,
                "grant_date": patent.grant_date,
                "assignee": patent.assignee,
            },
            "confidence": 0.95,
        }
        await write_signal(signal)
        connections.append(signal)

    for paper in papers:
        signal = {
            "prospect_id": request.prospect_a_id,
            "signal_type": "academic_co_author",
            "structured_value": {
                "connected_to": request.prospect_b_id,
                "paper_title": paper.title,
                "venue": paper.venue,
                "year": paper.year,
                "citation_count": paper.citation_count,
                "semantic_scholar_id": paper.semantic_scholar_id,
            },
            "confidence": 0.90 if paper.author_count <= 5 else 0.75,
        }
        await write_signal(signal)
        connections.append(signal)

    return {"connections_found": len(connections), "connections": connections}
```

---

## The Demo Mode — Implementation Detail

`src/lib/demoData.ts` — hardcoded demo data for the ?demo=true URL parameter.

Demo mode must:
1. Pre-load exactly the 5 prospects from DEMO_CASES.md (real names, real connections)
2. Pre-load exactly 3 hidden connection edges (patent_co_inventor, academic_co_author, conference_co_presenter)
3. Show WarmPathPanel immediately when any of these persons is clicked
4. Require zero external API calls — works completely offline
5. Have a floating "Demo Script" panel with talking points
6. Show a subtle "DEMO MODE" banner in the corner

Demo mode is detected with:
```typescript
const isDemoMode = new URLSearchParams(window.location.search).has("demo")
```

When isDemoMode is true, graphStore.ts must load from demoData.ts instead of from Supabase.

---

## Scoring Model

The current scoring model weights (adjustable via /settings):

```
OVERALL SCORE = Authenticity * 0.40 + Authority * 0.40 + Warmth * 0.20

Authenticity (0-100): How real and specific is the evidence?
  - Executive profile depth: named role, tenure, verifiable employer
  - Patent/paper evidence: formal documentary evidence
  - Award/recognition: third-party validation
  - Conference record: technical community participation

Authority (0-100): How senior and influential is this person?
  - Seniority score from title taxonomy
  - Team size estimates (from scope signals)
  - Budget authority level
  - Publication/patent count as proxy for domain influence

Warmth (0-100): How likely is this person to engage?
  - Career overlap with your team (past employer connections)
  - Educational overlap
  - Conference co-appearance
  - Standards committee co-participation
```

Falsification notes appear on every score. They describe the single most plausible reason the score is wrong. Example: "Authority score is 0.78 but this person may have left the company since this data was collected — no employment end date confirmation exists."

---

## Connection Priority for YC Demo

The career overlap connection type is what you have data for right now, in your 1k enriched prospects. Run this SQL to find warm paths before writing any new data pipelines:

```sql
WITH overlapping_pairs AS (
    SELECT
        LEAST(a.person_id, b.person_id) as person_a_id,
        GREATEST(a.person_id, b.person_id) as person_b_id,
        a.company_id,
        c.canonical_name as company_name,
        GREATEST(a.start_year, b.start_year) as overlap_start,
        LEAST(COALESCE(a.end_year, 2025), COALESCE(b.end_year, 2025)) as overlap_end,
        LEAST(COALESCE(a.end_year, 2025), COALESCE(b.end_year, 2025))
            - GREATEST(a.start_year, b.start_year) as overlap_years,
        a.inferred_team as team_a,
        b.inferred_team as team_b,
        a.functional_domain as domain_a,
        b.functional_domain as domain_b,
        ABS(a.seniority_score - b.seniority_score) as seniority_gap
    FROM employment_periods a
    JOIN employment_periods b
        ON a.company_id = b.company_id
        AND a.person_id < b.person_id
        AND a.start_year <= COALESCE(b.end_year, 2025)
        AND b.start_year <= COALESCE(a.end_year, 2025)
    JOIN companies c ON c.id = a.company_id
    WHERE a.start_year IS NOT NULL AND b.start_year IS NOT NULL
)
SELECT *,
    CASE
        WHEN team_a IS NOT NULL AND team_a = team_b THEN 'career_overlap_same_team'
        WHEN domain_a = domain_b AND seniority_gap < 10 THEN 'career_overlap_same_domain'
        ELSE 'career_overlap'
    END as connection_type,
    CASE
        WHEN team_a = team_b
            THEN LEAST(0.92, 0.70 + (overlap_years * 0.03))
        WHEN domain_a = domain_b AND seniority_gap < 10
            THEN LEAST(0.80, 0.55 + (overlap_years * 0.03))
        ELSE LEAST(0.70, 0.40 + (overlap_years * 0.04))
    END as base_strength
FROM overlapping_pairs
WHERE overlap_years >= 1
ORDER BY base_strength DESC
LIMIT 50;
```

Take the top 5 results and put them in DEMO_CASES.md. These are your demo. Do not make up fictional connections.

---

## Scale Thresholds — Know When to Refactor

Do not prematurely optimize. Do not delay necessary optimization. These are the actual breaking points:

| Threshold | What breaks |
|---|---|
| 100k prospects | buildGraph() in JS memory, client-side filter/sort, JSONB scans |
| 500k prospects | Postgres full-text search, full rescore on weight change, force graph render |
| 2M prospects | Single Postgres instance, batch enrichment scripts, synchronous scoring |

Current state is 20k. Current architecture handles 200k with no changes. Do not build for 2M until you have 200k paying customers.

---

## What "Done" Means for Each Task

### Done: new edge type added to graph.ts

- EdgeKind union includes the new type
- CSS variable exists in index.css for the edge color
- Filter pill appears in TopBar.tsx
- Discover.tsx applies the filter correctly
- At least 1 test edge of this type exists in mockStore.ts or demoData.ts
- The edge renders correctly on the graph canvas

### Done: warmPaths.ts engine

- findWarmPaths returns correctly sorted paths
- Path strength is the product of edge strengths, not a sum or average
- Paths with strength below minStrength are not returned
- Explanation string is specific, not generic ("co-invented US Patent X" not "have a connection")
- suggested_opener is a complete first sentence of an email
- Works correctly with 0 paths found (returns empty array, not null or error)

### Done: WarmPathPanel in NodeInspector

- Panel appears when a person node with warm edges is selected
- Each path shows: person chain, edge type pills, strength bar (0-100%), evidence text, "Use This Path" button
- "Use This Path" copies the suggested_opener to clipboard
- Panel shows "No warm paths in current view. Expand graph." when 0 paths found
- Panel does not appear for company or location nodes, only person nodes

### Done: POST /signals/discover-connections

- Returns under 5 seconds for any input
- Handles USPTO API timeout gracefully (returns partial results, does not throw)
- Handles Semantic Scholar rate limit gracefully (exponential backoff, max 3 retries)
- Writes found connections to Supabase signals table
- Returns structured response matching Contract 1 in CONTRACTS.md
- Does not crash if either person has no USPTO or Scholar records

### Done: demo mode

- Activated by ?demo=true in URL
- Graph loads without any Supabase queries (offline safe)
- All 5 DEMO_CASES.md persons are present in the graph
- All 3 hardcoded edges are present and visible with correct colors
- Clicking any DEMO_CASES.md person shows WarmPathPanel immediately
- "DEMO MODE" banner is visible
- Demo Script floating button opens talking points panel
- No console errors in demo mode

---

## Common Mistakes to Avoid

**1. Don't put direction into person_connections.**
Edges are bidirectional. person_a_id < person_b_id is always enforced. The BFS queries both sides: `WHERE person_a_id = $id OR person_b_id = $id`.

**2. Don't compute warm paths at query time.**
The person_connections table is pre-materialized. The BFS traverses it. It does not compute connection strength on the fly. Strength is stored in computed_strength and indexed.

**3. Don't assign org chart hierarchy before clustering by domain.**
This is the most common org chart mistake. Seniority sort alone produces a ladder. Always cluster by functional_domain first, then assign hierarchy within each cluster.

**4. Don't put raw API responses in structured_value.**
The JSONB field is capped at 4KB. Raw API responses from USPTO, Semantic Scholar, or Proxycurl can be 50-500KB. They go to S3. structured_value gets the extracted, structured subset.

**5. Don't skip REPL exploration on new external APIs.**
API documentation is frequently stale. Always make an exploratory call and inspect the actual response shape before writing a parser.

**6. Don't use fictional data in demo mode.**
DEMO_CASES.md has real warm paths. Use those. Demo credibility depends on specificity. "US Patent 10,234,567" is more credible than "a shared patent."

**7. Don't modify CONTRACTS.md during implementation.**
CONTRACTS.md is the interface agreement. If implementation reveals a needed change, discuss it explicitly before modifying the contract. Changing the contract mid-implementation breaks parallel subagent tasks.

**8. Don't add edge color hardcodes outside index.css.**
All edge colors are CSS variables defined in index.css. graph.ts references them by variable name. Components use the CSS variable. Never hardcode a color hex for an edge type in a component file.

---

## Session Checklist

Before ending any session, verify:

- [ ] All new files are in the correct directories per the repo structure above
- [ ] Any new interfaces are reflected in CONTRACTS.md
- [ ] Any new demo data references DEMO_CASES.md examples, not invented data
- [ ] No raw API responses are stored in Postgres JSONB fields
- [ ] No edge types are hardcoded outside of graph.ts
- [ ] The code you wrote satisfies the "Done" criteria for your task (see above)
- [ ] TypeScript files have no type errors (run `tsc --noEmit`)
- [ ] Python files have no import errors (run `python -m py_compile server/routes/signals.py`)
- [ ] You ran at least one REPL test of any new parsing or transformation logic

---

## Why This Matters

Every B2B sales team is fighting over the 1% reply rate on cold outbound. Credence finds the hidden connections that turn 1% into 40%. The patent co-inventor relationship between your CTO and a VP at a target account is sitting in a USPTO database right now. Nobody has mapped it. The academic co-authorship is in Semantic Scholar. The conference co-presentation is in an archived program PDF. The standards committee co-participation is in a JEDEC roster.

Credence is the first tool that assembles all of it and tells the sales rep: here is the person on your team who knows the buyer, here is the documented relationship that proves it, here is the first line of the email they should send.

The code in this repository is the thing that makes that happen. Write it well.

---

*CLAUDE.md — Credence v3*  
*Read this entire file before touching any code. When in doubt, re-read it.*
