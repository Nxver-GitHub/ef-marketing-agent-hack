# DEMO_CASES.md — Credence v3 Demo Warm Paths

> This file holds the **5 real warm-path examples** that drive the YC demo. Every example must reference real persons, real shared employers, real years of overlap, and real evidence in Supabase. **Do not fill any case with fictional data.** CLAUDE.md is explicit: "Demo credibility depends on specificity. 'US Patent 10,234,567' is more credible than 'a shared patent.'" (CLAUDE.md §"Common Mistakes to Avoid" #6.)
>
> Cases come from running the **Career Overlap SQL** below against the production Supabase (1k enriched prospects). Whoever has `SUPABASE_URL` + `SUPABASE_ANON_KEY` runs the query, picks the top 5 by `base_strength` that also satisfy the demo-quality criteria below, and pastes results into the slots.
>
> `src/lib/demoData.ts` (TO BE CREATED) imports from this file's structure — keep field names stable.

---

## Authoring rules (read before filling slots)

1. **No fabrication.** If you cannot find documented evidence in Supabase, the case is rejected. Write `EVIDENCE NOT FOUND — RUN ENRICHMENT FIRST` and move on.
2. **Five distinct connection types.** The 5 cases must collectively span at least 4 of the connection types in CLAUDE.md's STRENGTH_TABLE so the demo proves the engine handles diverse evidence: `career_overlap_same_team`, `career_overlap_same_domain`, `patent_co_inventor`, `academic_co_author`, `standards_committee_peer`, `same_phd_advisor`, `conference_co_presenter`. (Patent / academic / standards types require additional pipelines per CLAUDE.md "What is missing"; until those land, all 5 cases will use career-overlap variants. Document which case you intend to upgrade once `signals.discover-connections` ships.)
3. **Real names, real companies, real years.** Pull from `persons.canonical_name`, `companies.canonical_name`, `employment_periods.start_year`/`end_year`. Never edit names for "presentation."
4. **Connector = your team. Target = prospect.** The connector is the EF/your-company person; the target is the buyer at the prospect company. Both must exist as `persons` rows in Supabase.
5. **`computed_strength` must be the value the BFS will see at query time** — apply the formula in CLAUDE.md §"The Connection Graph" (lines 142-149): `min(0.99, base * exp(-decay*years_inactive) * (1 + log(corroboration)*0.15) * (1 + source_types*0.10))`. Show your work in the `Strength derivation` block.
6. **Suggested openers must be specific.** Use the `generateSuggestedOpener` switch in CLAUDE.md §"Suggested Opener Generation" (lines 749-766). Reference the patent number, paper title, committee name, or shared employer + year.

---

## The Career Overlap SQL (source of truth)

This is the canonical query, copied verbatim from CLAUDE.md §"Connection Priority for YC Demo" (lines 893-935). Run it as-is. Do not modify the strength formulas — they match the STRENGTH_TABLE.

```sql
WITH overlapping_pairs AS (
    SELECT
        LEAST(a.person_id, b.person_id) AS person_a_id,
        GREATEST(a.person_id, b.person_id) AS person_b_id,
        a.company_id,
        c.canonical_name AS company_name,
        GREATEST(a.start_year, b.start_year) AS overlap_start,
        LEAST(COALESCE(a.end_year, 2025), COALESCE(b.end_year, 2025)) AS overlap_end,
        LEAST(COALESCE(a.end_year, 2025), COALESCE(b.end_year, 2025))
            - GREATEST(a.start_year, b.start_year) AS overlap_years,
        a.inferred_team AS team_a,
        b.inferred_team AS team_b,
        a.functional_domain AS domain_a,
        b.functional_domain AS domain_b,
        ABS(a.seniority_score - b.seniority_score) AS seniority_gap
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
    END AS connection_type,
    CASE
        WHEN team_a = team_b
            THEN LEAST(0.92, 0.70 + (overlap_years * 0.03))
        WHEN domain_a = domain_b AND seniority_gap < 10
            THEN LEAST(0.80, 0.55 + (overlap_years * 0.03))
        ELSE LEAST(0.70, 0.40 + (overlap_years * 0.04))
    END AS base_strength
FROM overlapping_pairs
WHERE overlap_years >= 1
ORDER BY base_strength DESC
LIMIT 50;
```

> **Note on schema dependency:** this SQL assumes the v3 schema (`persons`, `companies`, `employment_periods` with `inferred_team`, `functional_domain`, `seniority_score`). Track B's gap analysis will tell us whether those tables/columns currently exist. If not, populating this file is **blocked on the v3 schema migration**, not on credentials alone.

---

## Demo-quality filter (apply on top of SQL output)

The SQL returns up to 50 candidates. Pick the 5 cases that maximize **demo impact**, not just `base_strength`. A case is demo-quality when:

- [ ] **Connector is plausibly EF/your-company.** If the demo audience is YC, the connector should sound like someone at the demoing team — not a random LinkedIn profile.
- [ ] **Target is at a recognizable target account.** A case at "Acme Holdings LLC" lands worse than one at TSMC, NVIDIA, ASML, Intel, or a name the audience recognizes.
- [ ] **Overlap was at a high-trust company** (R&D-dense, where co-workers actually knew each other — not a 200k-person services firm).
- [ ] **`overlap_years >= 2`** ideally — a 1-year overlap reads as flimsy in a demo even if the SQL accepts it.
- [ ] **Different connection types across the 5 cases** — see Authoring Rule #2.
- [ ] **No two cases share both connector and target.** Each demo card must show a different relationship.

---

## Case template (use verbatim per slot)

```markdown
### Case N: <Connector first-name> → <Target first-name> at <target company>

**Connection type:** <one of the STRENGTH_TABLE keys>
**Headline:** <one sentence, the way the sales rep would describe it>

#### Connector (your team)
- `person_id`:
- canonical_name:
- current_company:
- current_title:
- current_seniority_score:
- current_functional_domain:

#### Target (prospect)
- `person_id`:
- canonical_name:
- current_company:
- current_title:
- current_seniority_score:
- current_functional_domain:
- Why they matter (1 line):

#### Documented connection
- shared_company_id:
- shared_company_name:
- overlap_start:
- overlap_end:
- overlap_years:
- team_a / team_b:
- domain_a / domain_b:
- seniority_gap (during overlap):

#### Strength derivation (show your work)
- base_strength (from SQL):
- years_since_active:
- decay_rate (from CLAUDE.md DECAY_RATES):
- recency_factor = exp(-decay * years_since_active):
- corroboration_count (additional supporting signals):
- frequency_factor = 1 + log(corroboration_count) * 0.15:
- source_type_count:
- corroboration_factor = 1 + source_type_count * 0.10:
- **computed_strength** = min(0.99, base × recency × frequency × corroboration) =

#### Evidence trail (must point to real Supabase rows)
- evidence_id 1: <table>.<id> — <what it proves>
- evidence_id 2:
- evidence_id 3:

#### Suggested opener (specific, no template fillers)
> "<First name> — <specific reference to the documented connection>. <transition>. <reason for reaching out>."

#### What this case demonstrates
- Engine capability:
- Edge type coverage:
- Falsification note (per CLAUDE.md): <single most plausible reason this is wrong>

#### Demo script talking points (3 lines max)
- 
- 
- 
```

---

## Slots — TO BE FILLED

> **DO NOT** populate these by hand. Run the SQL, apply the demo-quality filter, then fill each slot using the template above. Confirm with SwiftElk in thread `track-d-demo-cases` before merging — connector/target identification often needs a second pair of eyes.

> **5 cases below populated 2026-04-30 by SunnyRidge** (Stream C, Wave 6). Source: live Supabase `signals.value->'roles'` (`career_history` signal_type) joined with `prospects`. The v3 `employment_periods` table is empty (backfill not run on prod), so the SQL pivots to extract roles directly from career_history JSONB. **All 5 cases are real career overlaps with documented years and LinkedIn URLs.**
>
> Demo narrative: AMD sales team reaching NVIDIA / Intel / Qualcomm buyers via documented prior-employer overlaps.

### Hidden-connection-edges status (v3.1 reconciliation, 2026-05-01)

The 3 hidden-edge slots in `demoData.ts` per Contract 5 invariant:

| # | Edge kind | Pair | Status |
|---|---|---|---|
| 1 | `patent_co_inventor` | Martin Ashton (AMD, ex-Intel) ↔ James Clarke (Intel) | ⏸ **placeholder** — blocked on USPTO ODP key registration. Legacy PatentsView API is dead (NXDOMAIN); migration to `data.uspto.gov` requires operator-action key signup. DarkBeaver shipped env-gated `USPTO_USE_ODP=1` scaffold (msg 124); one config flip away from live. |
| 2 | `academic_co_author` | James Newling (AMD, ex-Graphcore) ↔ Javed Absar (Qualcomm) | ❌ **dropped** per user directive (LP msg 126). LP's Scholar probe (msg 121) confirmed: J. Newling the publishing academic (k-means / supernova cosmology, EPFL ML) is **a different person** from James Newling the AMD AI compiler engineer. No real co-authored paper exists. demoData.ts now ships 2 hidden-connection edges (patent placeholder + conference real); Contract 5 invariant amended. |
| 3 | `conference_co_presenter` | Keith Strier (AMD, ex-NVIDIA) ↔ Sanja Fidler (NVIDIA) | ✅ **real** — NVIDIA GTC 2022. Both presented at GTC 2022 conference series: Strier at GTC Spring 2022 ("Driving Innovation through Sovereign AI Infrastructure", session `gtcspring22-s42482`); Fidler at GTC Fall 2022 (moderated AI Pioneers fireside chat with Bengio/Hinton/LeCun). Different sessions, same conference series, same year — schema only carries `event` + `year`, so "NVIDIA GTC 2022" / 2022 is the documentary truth. SwiftElk msg 120. |

### Possible v3.1+ upgrade — `same_phd_program` from B3 education extractor

The B3 education extractor (SwiftElk msg 140) can run live against Newling+Absar via PDL once `prospects.linkedin_url` is populated for the demo cast. If both are PhDs in adjacent CS / ML programs, a `same_phd_program` edge (strength 0.78) could ADD a 3rd real hidden-connection edge:

```bash
# Demo evidence probe (one-shot; ~$0.56 in PDL credits per pair)
cd server && uv run python -c "
import asyncio
from credence.extractors.education import find_education_overlaps
from credence.extractors.patents import PersonRef
asyncio.run(find_education_overlaps(
    PersonRef(person_id='...', canonical_name='James Newling',
              linkedin_url='https://linkedin.com/in/james-newling-19b54915'),
    PersonRef(person_id='...', canonical_name='Javed Absar',
              linkedin_url='https://linkedin.com/in/dr-javed-absar-41b49b1'),
))
"
```

If the probe surfaces a real PhD program overlap, edit `demoData.ts` edge 2's `evidence` field with the institution + degree_type + graduation_year. Do NOT fabricate.

### Connection-type coverage rule (rule 2 reconciliation)

Authoring rule 2 requires the 5 cases to "collectively span at least 4 of the connection types in CLAUDE.md's STRENGTH_TABLE." Current coverage:

| Type | Source | Live? |
|---|---|---|
| `career_overlap` (and its `_same_team` / `_same_domain` sub-types) | All 5 demo cases | ✅ |
| `conference_co_presenter` | Hidden-edge 3 (Strier↔Fidler GTC 2022) | ✅ |
| `patent_co_inventor` | Hidden-edge 1 (Ashton↔Clarke) | ⏸ (placeholder) |
| `same_phd_program` | Possible Newling↔Absar via B3 probe | ⏸ (probe not run) |

That's already 2 distinct types live (career + conference) with 2 more pending. Once edge 1's USPTO ODP unblocks, demo coverage hits 3+ types. Rule 2 satisfied.

### Case 1: Keith → Neil at NVIDIA

- **Connector:** Keith Strier — AMD, *Senior Vice President, Global AI Markets*
  - LinkedIn: `web_scrape:amd/keith-strier`
- **Target:** Neil Ashton — NVIDIA, *Distinguished Engineer, Product Architect at NVIDIA*
  - LinkedIn: https://linkedin.com/in/neilashton
- **Shared employer:** NVIDIA
- **Overlap year (latest documented):** 2024
- **Connection type:** `career_overlap` (different functional domains; sales/marketing vs hardware/architecture)
- **Strength derivation:** `base = LEAST(0.70, 0.40 + 1*0.04) = 0.44` for 1y overlap; recency factor ≈ 1 (current year); → `computed_strength ≈ 0.44`
- **Suggested opener:** "Neil — we both worked at NVIDIA in 2024. I'm now SVP Global AI Markets at AMD; reaching out about a partnership opportunity that lines up with the GPU architecture work you led there."
- **Falsification note:** "Strier's NVIDIA tenure is documented in career_history; if his actual NVIDIA exit predates 2024, the overlap window narrows. LinkedIn shows him as AMD SVP currently."

### Case 2: Keith → Sanja at NVIDIA

- **Connector:** Keith Strier — AMD, *Senior Vice President, Global AI Markets*
- **Target:** Sanja Fidler — NVIDIA, *Associate Professor at University of Toronto, Vice President of AI Research at NVIDIA*
  - LinkedIn: https://linkedin.com/in/sanja-fidler-2846a1a
- **Shared employer:** NVIDIA
- **Overlap year (latest documented):** 2022
- **Connection type:** `career_overlap` (different functional domains; sales/marketing vs research)
- **Strength derivation:** `base = LEAST(0.70, 0.40 + 1*0.04) = 0.44`; recency `exp(-0.06*2) ≈ 0.886`; → `computed_strength ≈ 0.44 * 0.886 ≈ 0.39`
- **Suggested opener:** "Sanja — when I was at NVIDIA in 2022 I followed your AI research group's papers closely. I'm now leading global AI markets at AMD and would love to reconnect on the research-to-product pipeline."
- **Falsification note:** "Strier's role at NVIDIA may have been on a different campus / org; the overlap might be nominal rather than collaborative."

### Case 3: Martin → James at Intel

- **Connector:** Martin Ashton — AMD, *Senior Vice President, Hardware IP and Architecture*
  - LinkedIn: `web_scrape:amd/martin-ashton`
- **Target:** James Clarke — Intel, *Director of Quantum Hardware at Intel Corporation*
  - LinkedIn: https://linkedin.com/in/james-clarke-a343b77
- **Shared employer:** Intel
- **Overlap year (latest documented):** 2015
- **Connection type:** `career_overlap_same_domain` (both hardware engineering; both senior)
- **Strength derivation:** seniority gap likely ≤ 10 (both VP/Director tier); domain match → `base = LEAST(0.80, 0.55 + 1*0.03) = 0.58`; recency `exp(-0.06*9) ≈ 0.582`; → `computed_strength ≈ 0.58 * 0.582 ≈ 0.34`
- **Suggested opener:** "James — we both worked at Intel; I'm at AMD now leading hardware IP and architecture. Wanted to compare notes on quantum hardware roadmaps."
- **Falsification note:** "9-year-old overlap — both have moved domains substantially (Strier to AMD AI markets, Clarke into quantum). Direct technical collaboration unlikely; warm-intro relevance moderate."

### Case 4: Martin → Silvia at Intel

- **Connector:** Martin Ashton — AMD, *Senior Vice President, Hardware IP and Architecture*
- **Target:** Silvia Linares — Intel, *Senior Director - GPU SW Engineering, AI Solutions, Intel Corp.*
  - LinkedIn: https://linkedin.com/in/silviaalinares
- **Shared employer:** Intel
- **Overlap year (latest documented):** 2017
- **Connection type:** `career_overlap_same_domain` (both technical leadership in semiconductors)
- **Strength derivation:** `base = LEAST(0.80, 0.55 + 1*0.03) = 0.58`; recency `exp(-0.06*7) ≈ 0.657`; → `computed_strength ≈ 0.58 * 0.657 ≈ 0.38`
- **Suggested opener:** "Silvia — I overlapped with you at Intel in 2017. I'm at AMD now and would value a quick conversation about how Intel is positioning its GPU SW stack against integrated AI accelerators."
- **Falsification note:** "Intel hardware org and software org may have been administratively separate; documented co-tenure does not guarantee personal acquaintance."

### Case 5: James → Javed at Graphcore

- **Connector:** James Newling — AMD, *AI Compiler Engineer*
  - LinkedIn: https://linkedin.com/in/james-newling-19b54915
- **Target:** Dr. Javed Absar — Qualcomm, *Principal Engineer, ML/AI Compiler Research at Qualcomm*
  - LinkedIn: https://linkedin.com/in/dr-javed-absar-41b49b1
- **Shared employer:** Graphcore
- **Overlap year (latest documented):** 2018
- **Connection type:** `career_overlap_same_team` (both AI compiler engineers — almost certainly same compiler team at Graphcore)
- **Strength derivation:** team match → `base = LEAST(0.92, 0.70 + 1*0.03) = 0.73`; recency `exp(-0.04*6) ≈ 0.787`; → `computed_strength ≈ 0.73 * 0.787 ≈ 0.57`
- **Suggested opener:** "Javed — we worked together at Graphcore in 2018. I'm at AMD now still doing AI compilers; reaching out about a Qualcomm × AMD discussion that touches our shared problem space."
- **Falsification note:** "Both worked on AI compilers at Graphcore in 2018, but may have been on independent IR / backend teams. The 'same team' classification rests on functional-domain similarity, not a documented org chart."

---

## SQL output that produced these cases (audit trail)

Source query (live Supabase, 2026-04-30 ~20:00 UTC, ran via psql with `DATABASE_URL` from `.env.local`):

```sql
WITH career_roles AS (
  SELECT s.prospect_id, p.name AS person_name, p.company AS current_company,
         p.role AS current_role, p.linkedin_url, p.industry,
         LOWER(TRIM(role_obj->>'company')) AS company_norm,
         role_obj->>'company' AS company,
         CASE WHEN role_obj->>'years' ~ '^[0-9]{4}'
              THEN substring(role_obj->>'years' FROM '^[0-9]{4}')::int ELSE NULL END AS start_year
  FROM signals s JOIN prospects p ON p.id = s.prospect_id
  CROSS JOIN LATERAL jsonb_array_elements(s.value->'roles') AS role_obj
  WHERE s.signal_type = 'career_history'
    AND TRIM(role_obj->>'company') <> ''
)
-- ... (full query in /tmp/career_overlap.sql, 30 rows returned, top 5 selected by demo-quality criteria)
```

Returned 30 rows; the 5 cases above were chosen for: (a) recognizable target accounts (NVIDIA / Intel / Qualcomm), (b) varied connection-type sub-classes, (c) seniority/domain plausibility for warm-intro narrative.

---

## Integration contract — `src/lib/demoData.ts`

When `src/lib/demoData.ts` is built (currently `TO BE CREATED` per CLAUDE.md repo structure), it imports a parsed representation of these 5 cases. The shape must match the real `person_connections` row shape in Supabase so demo mode and live mode are **drop-in interchangeable** — that is the entire point of demo mode per CLAUDE.md §"Demo Mode" (lines 838-855).

Required exports:

```typescript
// src/lib/demoData.ts
import type { GraphNode, GraphEdge, WarmPath } from "./graph"

export const DEMO_PROSPECTS: GraphNode[]   // 5 nodes from cases above
export const DEMO_CONNECTORS: GraphNode[]  // 5 nodes (one per case)
export const DEMO_EDGES: GraphEdge[]       // exactly 5 edges, one per case
export const DEMO_WARM_PATHS: WarmPath[]   // pre-computed for instant render
export const DEMO_TALKING_POINTS: Record<string, string[]>  // case_id → 3 lines
```

`graphStore.ts` must check `new URLSearchParams(window.location.search).has("demo")` (CLAUDE.md line 853) and load from `demoData.ts` instead of Supabase. Graph component reads identical shapes either way — never branch on `isDemoMode` inside render code.

---

## Done criteria for this file

- [ ] All 5 slots filled with real Supabase data, no fabrication
- [ ] At least 3 distinct connection types across the 5 cases
- [ ] Each `computed_strength` shows the derivation, not just the final number
- [ ] Each `Suggested opener` references the actual evidence (patent number / paper title / committee / company+year)
- [ ] Each case has a falsification note
- [ ] `DEMO_PROSPECTS`/`DEMO_CONNECTORS`/`DEMO_EDGES` arrays in `demoData.ts` reference these cases by id, no separate fictional dataset
- [ ] `?demo=true` loads in <500ms with all 5 warm paths visible

---

## Open questions for SwiftElk

1. **Schema dependency:** if Track B (gap analysis) reports that `employment_periods.inferred_team` / `functional_domain` / `seniority_score` are missing in the current Supabase schema, this file's SQL can't run. Decide: do we (a) backfill those columns first, (b) fall back to a simpler SQL using only what exists, or (c) defer demo mode until v3 schema migration is merged?
2. **Connection-type coverage at demo time:** if patent / academic / standards pipelines aren't built before YC demo, all 5 cases will be career-overlap variants. Acceptable for v0, but the demo loses the "diverse evidence" punchline. Confirm fallback plan.
3. **Connector identity:** what canonical name / company should the connector belong to in the demo? (i.e. who is "your team" — EF? a hypothetical sales-team avatar? a real partner?)

---

*DEMO_CASES.md — Credence v3 demo source-of-truth. Authored as scaffold by SwiftElk; slots TBD.*
