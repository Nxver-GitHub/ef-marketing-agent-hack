# V3.1 Implementation Plans — Credence

> Two detailed plans for v3.1 work: org chart optimization pipeline and the expanded hidden-connections
> network (education, conference attendance, cohort bonds). Both plans are written for Claude Code
> subagents — each section is self-contained and references exact file paths, contracts, and data model
> fields from CLAUDE.md and CONTRACTS.md.

---

## Plan A: Org Chart Pipeline + Optimization Loop

### Context

Six schema tables are live in Supabase and empty: `org_reporting_edges`, `org_functional_clusters`,
`org_cluster_members`, `person_scope_estimates`, `org_chart_corrections`, `org_signal_performance`.
No population code exists anywhere. CLAUDE.md L182–251 is the authoritative spec. The entry point
package is `server/credence/orgchart/` (does not yet exist).

The plan is divided into three stages: **population**, **quality measurement**, and **optimization loop**.
Do not start Stage 2 until Stage 1 produces populated rows. Do not start Stage 3 until Stage 2 produces
real correction data.

---

### Stage 1: Pipeline — Populate the Org Chart Tables

#### 1.1 Functional Clustering (`org_functional_clusters` + `org_cluster_members`)

**File:** `server/credence/orgchart/clustering.py`

Cluster people by `functional_domain` before any hierarchy work. This is CLAUDE.md Decision 2 — it is
not optional. A seniority-sort ladder is wrong.

```python
# Algorithm
# For each company C with > 3 persons in employment_periods:
#   1. Pull all current persons at C (is_current=True in employment_periods)
#   2. Group by functional_domain — one cluster per domain key
#   3. Within each cluster, sub-cluster by inferred_team where inferred_team is not null
#   4. Assign membership_confidence:
#      - functional_domain exact match: 0.95
#      - inferred_team match (within domain): 0.90
#      - functional_domain inferred from title NLP: 0.70
#   5. Insert into org_functional_clusters (one row per cluster) and
#      org_cluster_members (one row per person per cluster)
#   6. Handle IC track: anyone with title matching
#      (Distinguished Engineer|Principal Engineer|Staff Engineer|Fellow|Architect)
#      gets flag is_ic_track=True — they do NOT get hierarchy assigned across
#      domain boundaries

# IC track check — add is_ic_track column to org_cluster_members if not present
IC_TRACK_PATTERNS = re.compile(
    r'\b(Distinguished Engineer|Principal Engineer|Staff Engineer|'
    r'Fellow|Chief Architect|Principal Architect|Principal Scientist)\b',
    re.IGNORECASE
)
```

Functional domain keys and title → domain mapping come from CLAUDE.md's Functional Domain Taxonomy.
That mapping lives in `server/lib/taxonomy.py` (create if it does not exist). The clustering code
imports from it — never hardcodes strings.

**Input:** `employment_periods` joined with `persons`, filtered `is_current=True`.
**Output:** rows in `org_functional_clusters` and `org_cluster_members`.
**Done when:** every company with ≥ 3 enriched persons has at least one cluster row.

---

#### 1.2 Hierarchy Inference (`org_reporting_edges`)

**File:** `server/credence/orgchart/hierarchy.py`

Runs after clustering. For each cluster, assign reporting edges within that cluster only. Never assign
edges across domain boundaries at a lower seniority level (a hardware engineer does not report to a
finance director).

**Step 1 — Explicit signal pass (CLAUDE.md Decision 3):**

Pull all signals where `signal_type IN ('job_posting', 'press_mention', 'sec_filing')` and
`structured_value` contains a REPORTING_PATTERN match (CLAUDE.md NLP Extraction Patterns). If an
explicit signal exists for a (manager, report) pair, write the edge with:
```python
{
    "confidence": <signal-specific confidence>,
    "inference_method": "explicit_<signal_type>",
    "is_current": True,
}
```
Return immediately. Do not run implicit scoring for this pair.

**Step 2 — Implicit scoring pass (only for pairs with no explicit signal):**

For every candidate (A reports to B) pair within the same functional cluster:

```python
score = 0.0

# Seniority gap naturalness (max 0.30)
gap = B.seniority_score - A.seniority_score
if 8 <= gap <= 15:   score += 0.30
elif 5 <= gap < 8:   score += 0.18
elif 15 < gap <= 25: score += 0.12
# gap < 5 or > 25: score += 0.00

# Same functional domain (max 0.25)
if A.functional_domain == B.functional_domain: score += 0.25

# Same sub-domain / inferred_team (max 0.15, on top of domain)
if A.inferred_team and A.inferred_team == B.inferred_team: score += 0.15

# Manager title signal (max 0.10)
MANAGER_TITLES = re.compile(r'\b(manager|director|VP|head of|lead)\b', re.IGNORECASE)
if MANAGER_TITLES.search(B.current_title): score += 0.10

# Span of control capacity (max 0.05)
# B must have room for another report given seniority-based span limits
max_reports = SPAN_LIMITS[B.seniority_tier]
current_reports = count of current A-reports-to-B candidate edges so far
if current_reports < max_reports: score += 0.05

# Patent cluster membership (max 0.15)
shared_patents = count of patents co-invented by A and B
score += min(0.15, shared_patents / 3 * 0.15)

# Geographic scope compatibility (max 0.08)
if A.scope == B.scope or B.scope == 'global': score += 0.08

score = min(0.95, score)
```

Span-of-control limits (from CLAUDE.md):
```python
SPAN_LIMITS = {
    'c_suite':  8,   # seniority >= 85
    'svp':      7,   # seniority >= 75
    'vp':       8,   # seniority >= 65
    'director': 10,  # seniority >= 55
    'manager':  12,  # seniority < 55
}
```

Only write an edge if `score >= 0.55`. Below that, the edge is implausible — leave the node
unparented rather than asserting a wrong relationship.

**Unknown node rendering (CLAUDE.md Decision 4):** If a job posting mentions a role that cannot be
resolved to a known person, insert a stub person row with `canonical_name = '[VP of <X>]'` and
`enrichment_tier = 0`. Render with a distinct "unresolved" visual in the UI. Do not omit.

**Output:** rows in `org_reporting_edges` with `confidence`, `inference_method`, `is_current`.
**Done when:** every company with ≥ 5 persons has a non-empty set of reporting edges.

---

#### 1.3 Scope Estimation (`person_scope_estimates`)

**File:** `server/credence/orgchart/scope.py`

For each person in `org_reporting_edges` as a manager node:

```python
{
    "owns_functions": [cluster.functional_domain for cluster in their_subtree],
    "owns_technologies": extract from patents (assignee_fields) + job_posting signals,
    "team_size_min": count of direct reports in org_reporting_edges,
    "team_size_max": count of all persons in their subtree,
    "budget_authority_level": derive from seniority_score:
        # >= 85: "company"
        # >= 70: "division"
        # >= 60: "department"
        # >= 50: "team"
        # < 50:  "individual"
}
```

This feeds directly into the **Authority** component of the scoring model (CLAUDE.md Scoring Model),
specifically the "team size estimates" sub-factor. Once populated, Authority scores will be more
accurate than the current title-only proxy.

---

### Stage 2: Quality Measurement

#### 2.1 Correction Capture (`org_chart_corrections`)

**File:** `server/credence/orgchart/corrections.py` + UI component

The UI needs a **"Report wrong relationship"** button on every reporting edge in the org chart view.
When clicked: opens a small form with options:
- "This person does not report to that person"
- "This person reports to someone else" → free text for who
- "These two people are peers, not manager/report"
- "This person's team is wrong"

Submits to `POST /orgchart/correction` which writes to `org_chart_corrections`:
```python
{
    "person_a_id": ...,
    "person_b_id": ...,
    "edge_id": ...,         # FK into org_reporting_edges
    "correction_type": ..., # one of the four above
    "correct_value": ...,   # free text or structured
    "submitted_by": ...,    # user email
    "inference_method": ..., # copied from the edge being corrected
}
```

Every correction row is a labeled training example: `(inference_method, confidence) → correct/wrong`.

---

#### 2.2 Performance Tracking (`org_signal_performance`)

**File:** `server/credence/orgchart/performance.py` — runs as a scheduled job

After N corrections accumulate (threshold: 20), run:

```python
# For each inference_method in org_chart_corrections:
method = "explicit_job_posting"  # or "implicit_scoring", etc.

correct = COUNT WHERE correction_type IS NULL  # edge not corrected = assumed correct
wrong   = COUNT WHERE correction_type IS NOT NULL

# Update org_signal_performance
supabase.table("org_signal_performance").upsert({
    "inference_method": method,
    "success_count": correct,
    "error_count": wrong,
    "accuracy": correct / (correct + wrong),
    "last_computed_at": now(),
})
```

Target: this job runs nightly. After 2 weeks of live usage, every inference_method has a real accuracy
estimate.

---

### Stage 3: Optimization Loop

#### 3.1 Weight Tuning from `org_signal_performance`

**File:** `server/credence/orgchart/optimizer.py`

The seven implicit scoring components in CLAUDE.md each have a `max_contribution`. The optimizer
adjusts those contributions using the performance data.

```python
# Current defaults (from CLAUDE.md, must match exactly)
COMPONENT_WEIGHTS = {
    "seniority_gap":      0.30,
    "same_domain":        0.25,
    "same_sub_domain":    0.15,
    "manager_title":      0.10,
    "team_capacity":      0.05,
    "patent_cluster":     0.15,
    "geographic_scope":   0.08,
}
# Sum = 1.08; max_contribution is capped at 0.95 in the scoring formula.

# Optimization approach: Bayesian update per component
# For each correction where we know WHICH component contributed the decisive score:
# - If the edge was wrong despite high component score → decrease that component's weight
# - If the edge was right when only that component scored high → increase weight
# Learning rate: 0.05 per update, bounded [0.01, 0.40] per component
# Weights are re-normalized so sum = 1.08 after each update
```

This is lightweight Bayesian weight adjustment, not gradient descent. The dataset will be small (dozens
of corrections, not thousands) so full ML is overkill. The optimizer runs when new corrections arrive.

Key constraint: the seven components' weights must be written to `score_weights.sub_weights` (Contract
6) so the scoring versioning system tracks them. Each optimizer run that changes any weight by > 0.02
inserts a new `score_weights` row.

---

#### 3.2 Span-of-Control Validation (Automated QA)

**File:** `server/credence/orgchart/validation.py`

Run after every hierarchy inference pass. Flag edges that violate structural constraints:

```python
violations = []

for manager in org_reporting_edges.all_managers():
    direct_reports = manager.direct_reports()
    max_span = SPAN_LIMITS[manager.seniority_tier]

    if len(direct_reports) > max_span:
        violations.append({
            "type": "span_exceeded",
            "manager_id": manager.id,
            "actual_span": len(direct_reports),
            "max_span": max_span,
            "excess_reports": direct_reports[max_span:],  # weakest-confidence ones
        })

    # Cycle detection (should be impossible given our insertion logic, but verify)
    if has_cycle(manager):
        violations.append({"type": "cycle", "manager_id": manager.id})

    # IC-track misclassification: IC should never appear as manager of non-IC
    if manager.is_ic_track:
        non_ic_reports = [r for r in direct_reports if not r.is_ic_track]
        if non_ic_reports:
            violations.append({
                "type": "ic_managing_non_ic",
                "manager_id": manager.id,
                "wrong_reports": non_ic_reports,
            })
```

Violations are written to a `org_chart_validation_log` table (add to schema) and surfaced in the
admin UI as "needs review" flags. They are NOT auto-corrected — they surface for human review.

---

#### 3.3 Confidence Propagation

After edge confidence scores are assigned per node, propagate them up the tree to get
`path_confidence` (already a column in `org_reporting_edges`):

```python
# path_confidence = product of all edge confidences from root to this node
# Root nodes get path_confidence = their own edge confidence
# This means a VP (conf 0.80) → Director (conf 0.75) → Manager (conf 0.70) path
# gives the Manager a path_confidence of 0.80 * 0.75 * 0.70 = 0.42
# Low path_confidence nodes should render with visual uncertainty treatment in UI
```

The UI should show low-path-confidence nodes with a dashed border or greyed color.

---

### Done Criteria for Org Chart (v3.1)

- [ ] Every company with ≥ 5 enriched persons has at least one `org_functional_clusters` row
- [ ] No hierarchy edges cross functional domain boundaries at the same or lower seniority
- [ ] IC track persons (DE, PE, SE, Fellow, Architect) are never assigned as managers of non-IC persons
- [ ] `person_scope_estimates` populated for every manager-level node
- [ ] `org_chart_corrections` capture works end-to-end: user submits → row appears in Supabase
- [ ] `org_signal_performance` updates nightly from corrections
- [ ] Weight optimizer runs and produces a new `score_weights` row when any component shifts > 0.02
- [ ] Span-of-control violations are flagged, not silently accepted
- [ ] Unknown-role nodes render with distinct visual treatment, not omitted
- [ ] `tsc --noEmit` and `python -m py_compile` pass on all new files

---

## Plan B: Expanded Hidden Connections — Education, Cohort, Conference

### Why This Matters More Than Career Overlap

Career overlap is the weakest warm signal because everyone already knows about it. LinkedIn Sales
Navigator shows shared employers. What Credence uniquely surfaces:

- MBA cohort bonds: two years of case studies, recruiting hell, and living in the same dorms create
  relationships that outlast any job. HBS section bonds are tighter than most marriages.
- PhD program cohort: five to seven years in the same department. You attend each other's quals,
  share advisors in adjacent labs, work on adjacent problems.
- Conference co-presentation: you prepared a talk together. That's real collaboration.
- Executive education: HBS AMP, Kellogg EMBA, Wharton Executive program — these are intense 3-8 week
  residential programs. Participants bond with their cohort the way soldiers bond in training.

These are the connections that make a cold email read as a warm one. "We were in the same HBS
section" is worth 10 career overlaps.

---

### New EdgeKind Additions

Add to `src/lib/graph.ts` EdgeKind union (following Contract 3 protocol — 4 file updates):

```typescript
// Add to EdgeKind union
| "same_mba_cohort"          // same school, graduation year ±1, MBA/EMBA degree
| "same_phd_program"         // same school, same dept, overlapping enrollment years
| "executive_education"      // same HBS/Kellogg/Wharton executive program, same cohort year
| "same_undergrad_cohort"    // same school, same graduation year, small school (<5k) or same dept

// Note: conference_co_attendee (0.20) and alumni_network (0.25) already exist in STRENGTH_TABLE
// conference_co_presenter (0.80) already exists
// same_phd_advisor (0.92) already exists in STRENGTH_TABLE
```

Add to `EDGE_CONFIGS` in graph.ts:

```typescript
same_mba_cohort: {
    displayLabel: "MBA Cohort",
    cssVarName: "--edge-same-mba-cohort",
    defaultVisible: true,
    baseStrength: 0.85,
    decayRate: 0.02,
    isWarmByDefault: true,
},
same_phd_program: {
    displayLabel: "PhD Program",
    cssVarName: "--edge-same-phd-program",
    defaultVisible: true,
    baseStrength: 0.78,
    decayRate: 0.02,
    isWarmByDefault: true,
},
executive_education: {
    displayLabel: "Executive Education",
    cssVarName: "--edge-executive-education",
    defaultVisible: true,
    baseStrength: 0.70,
    decayRate: 0.03,
    isWarmByDefault: true,
},
same_undergrad_cohort: {
    displayLabel: "Undergrad Cohort",
    cssVarName: "--edge-same-undergrad-cohort",
    defaultVisible: true,
    baseStrength: 0.62,
    decayRate: 0.04,
    isWarmByDefault: false,  // only warm at small schools or same-major
},
```

Add to `src/index.css`:
```css
--edge-same-mba-cohort:       #7c3aed;   /* violet */
--edge-same-phd-program:      #2563eb;   /* blue */
--edge-executive-education:   #0891b2;   /* cyan */
--edge-same-undergrad-cohort: #64748b;   /* slate */
```

---

### New Supabase Tables

```sql
-- Already in schema: education_periods, events, event_appearances
-- Add:

-- Canonical institution name registry (school name normalization)
create table institutions (
  id              uuid primary key default gen_random_uuid(),
  canonical_name  text not null unique,  -- "Harvard Business School"
  short_name      text,                  -- "HBS"
  aliases         text[] not null default '{}', -- all known variants
  institution_type text not null,        -- 'mba' | 'phd' | 'undergrad' | 'exec_ed'
  prestige_tier   int not null default 3,-- 1=top10 2=top50 3=other
  typical_cohort_size int,               -- smaller = stronger connections
);

-- Cohort overlap pre-computed (mirrors person_connections pattern)
-- This is populated by the education extractor, not by person_connections directly
-- person_connections rows are DERIVED from this table by the cohort_strength_job
create table education_overlaps (
  id              uuid primary key default gen_random_uuid(),
  person_a_id     uuid not null references persons(id),
  person_b_id     uuid not null references persons(id),
  institution_id  uuid not null references institutions(id),
  degree_type     text not null,  -- 'mba' | 'phd' | 'emba' | 'bs' | 'ms' | 'exec_ed'
  graduation_year_a int,
  graduation_year_b int,
  same_program    bool not null default false,  -- same department/section
  source          text not null,  -- 'pdl' | 'apollo' | 'linkedin_scrape' | 'manual'
  constraint a_lt_b check (person_a_id < person_b_id),
  unique (person_a_id, person_b_id, institution_id, degree_type)
);

-- Conference attendance (non-presenting; presenting goes via event_appearances)
create table conference_attendances (
  id              uuid primary key default gen_random_uuid(),
  person_id       uuid not null references persons(id),
  event_id        uuid not null references events(id),
  role            text not null default 'attendee', -- 'attendee' | 'panelist' | 'speaker'
  year            int not null,
  source          text not null,  -- 'firecrawl' | 'parallel' | 'manual'
  confidence      float not null,
  unique (person_id, event_id)
);
```

---

### New Extractor: `server/lib/extractors/education.py`

**Primary data source: PDL (People Data Labs)** — `PDL_API_KEY` is already in `.env.local`.

PDL returns `education` as an array on the person record:
```python
# PDL education field shape (verify with REPL exploration first):
{
    "school": {
        "name": "Harvard Business School",
        "type": "MBA",
        "linkedin_id": "...",
    },
    "degrees": ["MBA"],
    "majors": ["Business Administration"],
    "start_date": "2010-01",
    "end_date": "2012-05",
    "gpa": None,
    "raw": "Harvard Business School, MBA, 2012"
}
```

**Always run REPL exploration before writing the parser:**
```python
import httpx, os, json
r = httpx.post(
    "https://api.peopledatalabs.com/v5/person/enrich",
    headers={"X-Api-Key": os.environ["PDL_API_KEY"]},
    json={"linkedin_url": "https://linkedin.com/in/sanja-fidler-2846a1a"}
)
print(json.dumps(r.json().get("education", []), indent=2))
# Inspect actual shape before writing any parser
```

**School normalization — the hardest part:**

```python
# server/lib/extractors/education.py

MBA_SCHOOL_ALIASES = {
    "Harvard Business School": ["HBS", "Harvard University", "Harvard Business",
                                 "Harvard Univ Business School"],
    "Wharton School": ["Wharton", "University of Pennsylvania Wharton",
                        "UPenn Wharton", "Penn Wharton"],
    "Stanford Graduate School of Business": ["Stanford GSB", "Stanford Business",
                                              "Stanford University GSB"],
    "MIT Sloan School of Management": ["MIT Sloan", "Sloan MIT", "Sloan School",
                                        "Massachusetts Institute of Technology Sloan"],
    "Kellogg School of Management": ["Kellogg", "Northwestern Kellogg",
                                      "Northwestern University Kellogg"],
    "Booth School of Business": ["Booth", "Chicago Booth", "University of Chicago Booth"],
    "Columbia Business School": ["Columbia Business", "CBS"],
    "Haas School of Business": ["Haas", "UC Berkeley Haas", "Berkeley Haas"],
    "Tuck School of Business": ["Tuck", "Dartmouth Tuck"],
    "Fuqua School of Business": ["Fuqua", "Duke Fuqua"],
    # PhD programs that produce semiconductor/AI talent:
    "MIT EECS": ["MIT Electrical Engineering", "MIT Computer Science",
                  "Massachusetts Institute of Technology EECS"],
    "Stanford EE": ["Stanford Electrical Engineering", "Stanford CS"],
    "Carnegie Mellon CS": ["CMU CS", "Carnegie Mellon Computer Science"],
    "UC Berkeley EECS": ["Berkeley EECS", "UC Berkeley Computer Science"],
    "Caltech": ["California Institute of Technology"],
    # Executive education
    "Harvard Business School (Executive)": ["HBS AMP", "HBS Executive Education",
                                              "Harvard Advanced Management Program"],
    "Kellogg Executive Education": ["Kellogg EMBA", "Northwestern Executive Education"],
}

def normalize_school(raw_name: str) -> Optional[str]:
    """Returns canonical institution name, or None if unrecognized."""
    raw_lower = raw_name.lower().strip()
    for canonical, aliases in MBA_SCHOOL_ALIASES.items():
        if raw_lower == canonical.lower():
            return canonical
        for alias in aliases:
            if raw_lower == alias.lower():
                return canonical
    # Fuzzy match as fallback (use rapidfuzz, threshold 0.88)
    ...
```

**Cohort strength scoring:**

```python
def compute_cohort_strength(overlap: EducationOverlap, institution: Institution) -> float:
    """
    Cohort bonds decay with graduation year gap and grow with program intensity.
    """
    base = EDGE_CONFIGS["same_mba_cohort"].base_strength  # 0.85

    # Year gap penalty: same year = full strength; 1 year apart = 80%; 2 years = 50%
    year_gap = abs(overlap.graduation_year_a - overlap.graduation_year_b)
    if year_gap == 0:
        year_factor = 1.0
    elif year_gap == 1:
        year_factor = 0.80
    else:
        year_factor = 0.50  # 2+ years = same school, different cohort = alumni_network tier

    # Cohort size: smaller = tighter bonds
    # HBS section (~90 people) is stronger than "went to HBS" (~1800/year)
    if institution.typical_cohort_size and institution.typical_cohort_size <= 100:
        size_factor = 1.10
    elif institution.typical_cohort_size and institution.typical_cohort_size <= 500:
        size_factor = 1.00
    else:
        size_factor = 0.85

    # Same program/section (e.g., both in HBS MBA not just HBS generally)
    program_factor = 1.05 if overlap.same_program else 1.00

    return min(0.99, base * year_factor * size_factor * program_factor)
```

**Writing to person_connections:**

After computing all education overlaps for a batch of prospects, write them to `person_connections`
using the standard `ON CONFLICT DO UPDATE` merge pattern from Contract 7. Connection type is
`same_mba_cohort`, `same_phd_program`, `executive_education`, or `same_undergrad_cohort` depending
on degree type. Strength is the `computed_cohort_strength` above.

---

### New Extractor: `server/lib/extractors/conference.py`

**Goal:** surface conference_co_attendee and conference_co_presenter edges beyond what Scholar/USPTO
provide. These are the warm connections hiding in plain sight in public conference programs.

**Target conferences by vertical** (the current 20k prospects skew semiconductor/AI):

```python
TARGET_CONFERENCES = {
    # AI/ML
    "NeurIPS": ["neurips.cc", "papers.nips.cc"],
    "ICML":    ["icml.cc"],
    "ICLR":    ["iclr.cc"],
    "CVPR":    ["cvpr.thecvf.com"],
    "NVIDIA GTC": ["www.nvidia.com/gtc"],  # Strier/Fidler edge lives here
    # Semiconductor
    "ISSCC":   ["isscc.org"],
    "Hot Chips": ["hotchips.org"],
    "DAC":     ["dac.com"],
    "IEDM":    ["ieee.org/conferences/iedm"],
    "Linley Tech": ["linleygroup.com/events"],
    # Enterprise / GTM
    "Gartner IT Symposium": ["gartner.com/events"],
    "Dreamforce": ["salesforce.com/dreamforce"],
    "SaaStr Annual": ["saastr.com/annual"],
}
```

**Extraction strategy using Firecrawl** (`FIRECRAWL_API_KEY` already in `.env.local`):

```python
import httpx, os

async def crawl_conference_speakers(conference_url: str, year: int) -> list[dict]:
    """
    Firecrawl the conference speakers/program page and extract structured names.
    """
    r = await httpx.AsyncClient().post(
        "https://api.firecrawl.dev/v0/scrape",
        headers={"Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}"},
        json={
            "url": conference_url,
            "pageOptions": {"includeHtml": False},
            "extractorOptions": {
                "mode": "llm-extraction",
                "extractionPrompt": (
                    "Extract all speaker names and their affiliations from this "
                    "conference program page. Return a JSON array of "
                    "{name: string, title: string, company: string, session: string}."
                ),
                "extractionSchema": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "title": {"type": "string"},
                            "company": {"type": "string"},
                            "session": {"type": "string"},
                        }
                    }
                }
            }
        }
    )
    return r.json().get("data", {}).get("llm_extraction", [])
```

After extraction, entity-resolve each speaker name against `persons.canonical_name` and
`persons.name_variants[]` using fuzzy matching (rapidfuzz, threshold 0.85). Only write an edge if
confidence ≥ 0.75 (to avoid false positives from name disambiguation).

For speaker pairs at the same conference in the same year:
- If they co-presented the same session → `conference_co_presenter` (strength 0.80)
- If they both appeared (different sessions) → `conference_co_attendee` (strength 0.20)

This is the difference between a warm intro ("we presented together at NeurIPS 2022") and a weak
one ("we were both at NeurIPS 2022").

---

### New Extractor: `server/lib/extractors/standards.py`

**Goal:** populate `standards_memberships` and derive `standards_committee_peer` edges.

Standards bodies with public membership rosters:

```python
STANDARDS_SOURCES = {
    "JEDEC": "https://www.jedec.org/committees",
    "IEEE SA": "https://standards.ieee.org/about/get-involved/join/",
    "SEMI": "https://www.semi.org/en/standards/standards-committees",
    "Wi-Fi Alliance": "https://www.wi-fi.org/membership",
    "RISC-V International": "https://riscv.org/members/",
    "MLCommons": "https://mlcommons.org/en/members/",
}
```

Crawl each roster with Firecrawl, extract company + representative names, entity-resolve against
`persons`, write to `standards_memberships`. Then derive `standards_committee_peer` edges in
`person_connections` for all pairs at the same committee.

---

### Signal Priority and Demo Cast Application

Run these extractors against the 5 demo prospects first, in this order:

1. **Education extractor on all 5 people via PDL** — pull education, normalize schools, find overlaps.
   The James Newling ↔ Javed Absar pair: both AI compiler engineers who attended conferences in the
   same years (2017–2019); pull their education history and check for PhD program overlap.

2. **Conference extractor on NeurIPS/ICML 2018–2022** — check if Newling or Absar appear in speaker
   lists (both published ML papers at that time).

3. **Standards extractor on MLCommons** — check if any of the 5 demo people served on committees.

If either step 1 or step 2 finds a real connection for Newling ↔ Absar, that edge can replace the
dropped `academic_co_author` placeholder in `demoData.ts`. Per CLAUDE.md: only real evidence, specific.

---

### API Extension: `POST /signals/discover-connections`

Add `"education"` and `"conference"` as valid values in `sources` (Contract 1's
`Literal["uspto", "scholar", "career"]` → extend to include `"education"`, `"conference"`,
`"standards"`). Add corresponding `signal_type` values to `SignalType` union in Contract 4:
- `"same_mba_cohort"`
- `"same_phd_program"`
- `"executive_education"`
- `"same_undergrad_cohort"`
- `"conference_co_attendee"`
- `"standards_committee_peer"`

---

### Warm Path Explanation Templates

Add to `generateExplanation()` in `warmPaths.ts`:

```typescript
case "same_mba_cohort":
    return `${path.nodes[0].name} and ${path.nodes[1].name} were in the same MBA cohort at
            ${firstEdge.evidence?.institution ?? "business school"}
            (Class of ${firstEdge.evidence?.graduationYear ?? "year unknown"})`

case "same_phd_program":
    return `${path.nodes[0].name} and ${path.nodes[1].name} overlapped in the
            ${firstEdge.evidence?.department ?? "PhD program"} at
            ${firstEdge.evidence?.institution ?? "the same university"}
            (${firstEdge.evidence?.years ?? "years unknown"})`

case "executive_education":
    return `${path.nodes[0].name} and ${path.nodes[1].name} attended
            ${firstEdge.evidence?.program ?? "the same executive education program"}
            at ${firstEdge.evidence?.institution ?? "the same institution"}
            (${firstEdge.evidence?.year ?? "year unknown"})`

case "conference_co_attendee":
    return `${path.nodes[0].name} and ${path.nodes[1].name} both attended
            ${firstEdge.evidence?.event ?? "the same conference"}
            (${firstEdge.evidence?.year ?? "year unknown"})`
```

Add suggested openers:

```typescript
case "same_mba_cohort":
    return `${connector.name} — we were in the same MBA cohort at ${firstEdge.evidence?.institution} 
            (Class of ${firstEdge.evidence?.graduationYear}). I'm now at [Company] and wanted to reconnect.`

case "same_phd_program":
    return `${connector.name} — we overlapped in the ${firstEdge.evidence?.department} PhD program 
            at ${firstEdge.evidence?.institution}. I'm at [Company] now and working on something 
            in your space.`
```

---

### Done Criteria for Education + Conference Hidden Connections

- [ ] `institutions` table populated with canonical names + aliases for top 30 MBA/PhD/exec-ed programs
- [ ] PDL education extractor runs against all 20k prospects and populates `education_overlaps`
- [ ] School normalization handles all major variants without fuzzy-match fallback for top-tier schools
- [ ] `same_mba_cohort` edges appear in `person_connections` for at least 20 real pairs in the 20k dataset
- [ ] Conference crawl runs against NeurIPS, ICML, ISSCC, Hot Chips, NVIDIA GTC for years 2018–2024
- [ ] Entity resolution achieves ≥ 0.85 precision on speaker name → person match (spot-check 20 matches)
- [ ] No `conference_co_presenter` edge written with confidence < 0.75
- [ ] New EdgeKinds render correctly in TopBar filter pills and graph canvas
- [ ] New explanation strings are specific (contain institution name + year), not generic
- [ ] `tsc --noEmit` clean; `grep -r '"same_mba_cohort"' src/` matches only `graph.ts`
- [ ] DEMO_CASES.md updated if education/conference evidence is found for any demo pair

---

*V31_PLANS.md — authored 2026-04-30. Reference CLAUDE.md and CONTRACTS.md as primary specs.*
*This document is implementation guidance, not a contract. Contracts live in CONTRACTS.md.*
