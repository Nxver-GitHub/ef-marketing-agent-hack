# Prospect Enrichment Task — Semiconductor, Defense & Aerospace Org Charts

> **Goal:** Expand the Supabase prospect database from 20k records (1k enriched) to full org chart
> coverage across the three verticals Credence targets first: semiconductor, defense, and aerospace.
> "Full org chart coverage" means enough people at each target company — across all functional domains
> and seniority levels — that the org chart pipeline (V3_PT2.md Plan A) can produce a real hierarchy
> when it runs. Thin coverage produces ladders; dense coverage produces trees.
>
> This task is a data pipeline task, not a UI task. All output goes to Supabase. No frontend changes.
> Read CLAUDE.md in full before starting.

---

## Coverage Targets

### What "full org chart coverage" requires per company

To run the functional-clustering + hierarchy-inference pipeline from V3_PT2.md, each company needs:

| Seniority tier | Minimum needed | Why |
|---|---|---|
| C-suite / President (85–100) | 3–8 | Roots of the hierarchy tree |
| EVP / SVP (78–85) | 5–15 | First branch level |
| VP / Group VP (65–75) | 10–30 | Functional domain owners |
| Director / Sr Director (55–65) | 20–60 | Sub-domain cluster heads |
| Manager / Sr Manager (45–55) | 30–80 | Leaf-level managers |
| IC track (DE / PE / SE / Staff) (40–55) | 15–40 | Parallel IC track peers |

**Minimum viable org chart: 500 people per company.**
**Full org chart: 500–1,000 people per company.**

For companies above 10k employees (Intel, NVIDIA, Lockheed), 500–1,000 well-chosen people across all
functional domains gives a structurally complete and detailed tree. You do not need every employee.
You need enough density at every seniority level and functional domain that the clustering algorithm
produces real branches — not just a top-heavy leadership layer with a handful of ICs underneath.

---

## Target Company List

### Tier 1 — Semiconductor (highest GTM priority)

These are the accounts where the warm path product is most valuable. Semiconductor is a small world:
people patent together, publish together, sit on the same standards committees. The relationship graph
is dense.

| Company | HQ | ~Employees | Priority | Why |
|---|---|---|---|---|
| NVIDIA | Santa Clara, CA | 36k | P0 | GPU AI dominance; every enterprise AI team has a NVIDIA buyer |
| Intel | Santa Clara, CA | 124k | P0 | Largest semiconductor employer; quantum, foundry, CPU, GPU divisions |
| AMD | Santa Clara, CA | 26k | P0 | Direct Credence team company; connector-side org chart needed |
| Qualcomm | San Diego, CA | 51k | P1 | Mobile + automotive AI; strong patent co-inventor graph |
| TSMC | Hsinchu + San Jose | 73k | P1 | Foundry; US expansions in Arizona create new reachable contacts |
| ASML | Veldhoven + Wilton | 42k | P1 | EUV monopoly; small world, tight standards committee participation |
| Broadcom | San Jose, CA | 20k | P1 | Networking + storage silicon; strong M&A-driven org complexity |
| Marvell Technology | Santa Clara, CA | 6k | P1 | Data infrastructure silicon; smaller, easier to get full coverage |
| Micron Technology | Boise, ID | 48k | P2 | DRAM/NAND; manufacturing-heavy org chart |
| Applied Materials | Santa Clara, CA | 34k | P2 | Semiconductor equipment; ISSCC/IEDM conference presence |
| Lam Research | Fremont, CA | 18k | P2 | Etch/deposition equipment |
| KLA Corporation | Milpitas, CA | 15k | P2 | Inspection equipment |
| Synopsys | Sunnyvale, CA | 20k | P2 | EDA tools; strong academic co-author network |
| Cadence Design Systems | San Jose, CA | 12k | P2 | EDA tools |
| Texas Instruments | Dallas, TX | 35k | P3 | Analog + embedded; slower GTM cycle |
| NXP Semiconductors | Eindhoven | 34k | P3 | Automotive + IoT |
| Infineon Technologies | Munich | 58k | P3 | Power + automotive |
| Arm Holdings | Cambridge + San Jose | 6k | P3 | IP licensor; small headcount, high influence |
| SK Hynix | Icheon, Korea | 30k | P3 | Memory; US presence via Purdue fab |
| Samsung Semiconductor | Suwon + San Jose | 50k | P3 | Memory + foundry; US R&D center reachable |

### Tier 2 — Defense

Defense has longer sales cycles but enormous budgets and concentrated buying authority. The key insight:
defense buyers care deeply about clearances and prior work, which makes career overlap signals
especially meaningful (if you both worked at a cleared contractor, you know each other).

| Company | HQ | ~Employees | Priority | Why |
|---|---|---|---|---|
| Lockheed Martin | Bethesda, MD | 122k | P0 | Largest defense contractor; space, missiles, aeronautics divisions |
| Raytheon (RTX) | Arlington, VA | 185k | P0 | Missiles and defense electronics; strong semiconductor buyer |
| Northrop Grumman | Falls Church, VA | 101k | P0 | Space + autonomous systems + cyber |
| General Dynamics | Reston, VA | 106k | P1 | IT + combat systems + marine |
| L3Harris Technologies | Melbourne, FL | 47k | P1 | Communication systems + space |
| BAE Systems | Falls Church, VA | 90k | P1 | Electronic systems + platforms |
| Leidos | Reston, VA | 47k | P1 | IT services + national security |
| SAIC | Reston, VA | 26k | P2 | Government IT + engineering |
| Booz Allen Hamilton | McLean, VA | 34k | P2 | Defense consulting + analytics |
| Palantir Technologies | Denver, CO | 3.5k | P2 | Defense AI; small, influential, strong engineering culture |
| Anduril Industries | Costa Mesa, CA | 3k | P2 | Autonomous defense; fast-growing, strong engineering talent |
| Shield AI | San Diego, CA | 1k | P3 | AI fighter pilot; smaller but visible |
| MITRE Corporation | McLean, VA | 10k | P3 | FFRDC; important standards and research influence |

### Tier 3 — Aerospace

Aerospace overlaps heavily with defense (same contractors) but has distinct commercial buyers in
satellite, launch, and avionics.

| Company | HQ | ~Employees | Priority | Why |
|---|---|---|---|---|
| Boeing | Arlington, VA | 172k | P0 | Commercial + defense + space; massive org chart complexity |
| Airbus | Toulouse + Herndon | 134k | P0 | Commercial aviation; US presence is reachable |
| SpaceX | Hawthorne, CA | 13k | P1 | Launch + Starlink; strong engineering culture, public profiles |
| Rocket Lab | Long Beach, CA | 2k | P1 | Small launch + spacecraft; small world |
| Aerojet Rocketdyne | Sacramento, CA | 15k | P2 | Propulsion; strong patent record |
| Textron Aviation | Wichita, KS | 40k | P2 | General aviation + defense |
| Honeywell Aerospace | Phoenix, AZ | 36k | P2 | Avionics + propulsion; strong standards committee presence |
| Collins Aerospace (RTX) | Charlotte, NC | 73k | P2 | Already under RTX umbrella |
| GE Aerospace | Cincinnati, OH | 52k | P2 | Jet engines; strong patent record |
| Joby Aviation | Santa Cruz, CA | 1.5k | P3 | eVTOL; growing fast |
| Archer Aviation | San Jose, CA | 0.6k | P3 | eVTOL |

---

## Data Sources and APIs

All keys are already in `.env.local`. Do not add new paid APIs without user confirmation.

### Source 1: Apollo (`APOLLO_API_KEY`)

Best for: current title, company, verified email, LinkedIn URL. Poor for: historical employment,
education, patents.

```python
# Apollo people search endpoint
POST https://api.apollo.io/v1/mixed_people/search
{
    "organization_ids": ["<apollo_org_id>"],   # get org ID first via /organizations/search
    "page": 1,
    "per_page": 100,
    "person_seniorities": ["c_suite", "vp", "director", "manager", "senior"],
    "contact_email_status": ["verified", "likely to engage"],
}
# Rate limit: 50 req/min on paid plan; 1 req/sec safe default
# Returns up to 10k results per org with pagination
# Key fields: id, name, title, organization_name, linkedin_url, email,
#             employment_history (array of past jobs)
```

Apollo is the **primary source for org-chart-tier targeting** — it lets you filter by seniority and
returns employment_history, which is the backbone of career overlap signals.

**Extraction order per company:**
1. Look up company by name → get `apollo_org_id`
2. Pull all persons at org, filtered to seniorities: `c_suite, vp, director, manager, senior`
3. For each person: extract current title, current company, LinkedIn URL, employment_history
4. Write to `prospects` (current role) and `employment_periods` (history)

### Source 2: People Data Labs (`PDL_API_KEY`)

Best for: education history, full employment timeline with dates, skills. PDL often has better date
ranges than Apollo for historical roles.

```python
# PDL person enrich endpoint (by LinkedIn URL)
POST https://api.peopledatalabs.com/v5/person/enrich
{
    "linkedin_url": "https://linkedin.com/in/...",
    "pretty": True,
}
# Returns: education[], experience[], skills[], certifications[]
# Rate limit: 1 req/sec on standard plan; check your tier
# Cost: credits per call — check account balance before running bulk
```

Use PDL as the **secondary enrichment pass** after Apollo has populated the prospect list. PDL fills in
education history (for the education extractor in V3_PT2.md) and precise employment date ranges.

**Do not use PDL as the primary discovery source** — it's more expensive per call and doesn't have
the org-level bulk search Apollo supports. Use Apollo to find people, PDL to enrich them.

### Source 3: Firecrawl (`FIRECRAWL_API_KEY`)

Best for: company leadership pages, press releases, org announcements, conference speaker lists.
Free-form HTML → structured JSON via LLM extraction.

```python
# Firecrawl scrape + extract
POST https://api.firecrawl.dev/v0/scrape
{
    "url": "https://www.nvidia.com/en-us/about-nvidia/leadership/",
    "extractorOptions": {
        "mode": "llm-extraction",
        "extractionPrompt": "Extract all named executives with their titles. Return JSON array of {name, title, bio_snippet}.",
    }
}
```

Use Firecrawl for:
- Company `About > Leadership` pages (public C-suite and VP lists)
- Press releases announcing new hires or promotions
- Conference speaker bios (per V3_PT2.md Plan B)
- SEC filings (proxy statements list named executives with compensation — public)

**Firecrawl gives you the leadership layer for free**, without using Apollo credits. Always crawl
leadership pages first before running Apollo people search.

### Source 4: Parallel Web Systems (`PARALLEL_API_KEY`)

Best for: async deep research tasks where you want structured output from multiple public sources
simultaneously. Use for complex extractions that Firecrawl can't handle in a single page scrape
(e.g., "find all VPs of Engineering at Lockheed Martin across their divisions").

### Source 5: SEC EDGAR (no key required)

SEC proxy statements (`DEF 14A` filings) list all named executive officers with compensation. This is
the most authoritative source for C-suite and SVP-level people at any public US company.

```python
# EDGAR full-text search (free, no auth)
GET https://efts.sec.gov/LATEST/search-index?q="Lockheed+Martin"&dateRange=custom&startdt=2024-01-01&enddt=2025-01-01&forms=DEF+14A
# Returns links to DEF 14A filings
# Then Firecrawl the filing URL to extract named executives
```

Always run EDGAR scrape before Apollo for any public company. It costs nothing and gives you the
ground-truth executive roster with titles.

### Source 6: Apify LinkedIn Scrapers (`APIFY_TOKEN`)

Apify hosts maintained LinkedIn scraper actors that handle session rotation, pagination, and
anti-bot countermeasures. This is the **highest-fidelity source for LinkedIn profile data** —
richer than PDL's LinkedIn-backed data and more current than Apollo's cache.

Three actors to use, in order of priority:

#### 6a. Company Employee Scraper — bulk discovery

```python
# Apify actor: "curious_coder/linkedin-company-employees-scraper"
# or: "anchor/linkedin-company-employees-scraper"
# Pulls the full employee list for a company LinkedIn page

POST https://api.apify.com/v2/acts/curious_coder~linkedin-company-employees-scraper/runs
Headers: {"Authorization": f"Bearer {APIFY_TOKEN}"}
Body: {
    "companyUrl": "https://www.linkedin.com/company/nvidia/",
    "maxResults": 1000,          # set to 1000 to hit the 500-person target
    "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
}
# Returns: [{firstName, lastName, headline, title, location, profileUrl, connectionDegree}]
# Cost: ~$0.50–2.00 per 1,000 profiles depending on actor
# This replaces the Firecrawl LinkedIn scrape — Apify handles pagination and session properly
```

Use this as the **primary bulk discovery pass** for every company, run in parallel with or before
the Apollo pull. It gives you current employees with LinkedIn profile URLs, which then feed into
the profile enrichment pass below.

#### 6b. Profile Enrichment Scraper — deep per-person data

```python
# Apify actor: "curious_coder/linkedin-profile-scraper"
# or: "bebity/linkedin-profile-scraper"
# Given a list of LinkedIn profile URLs, returns full profile data

POST https://api.apify.com/v2/acts/curious_coder~linkedin-profile-scraper/runs
Headers: {"Authorization": f"Bearer {APIFY_TOKEN}"}
Body: {
    "profileUrls": [
        "https://www.linkedin.com/in/neilashton",
        "https://www.linkedin.com/in/sanja-fidler-2846a1a",
        # ... up to 500 per run
    ],
    "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
}
# Returns per profile: {
#     firstName, lastName, headline, summary,
#     experience: [{title, companyName, startDate, endDate, description}],
#     education: [{schoolName, degree, fieldOfStudy, startDate, endDate}],
#     skills: [str],
#     certifications: [{name, issuingOrganization, issueDate}],
#     languages: [str],
#     profilePicUrl: str,
#     connectionsCount: int,
# }
```

This is the **gold standard for employment history and education** — it reads directly from LinkedIn
profiles and returns complete experience and education arrays with month-level date precision.
Use it as the profile enrichment pass instead of PDL for any person where you have a `linkedin_url`.

**When to use Apify profile scraper vs PDL:**
- Apify: when you have the LinkedIn URL and need complete, current data. Higher fidelity, slightly
  higher cost per call.
- PDL: when you don't have a LinkedIn URL and need to search by name + company. PDL's name-based
  search has no equivalent in Apify.
- Run Apify first on everyone with a LinkedIn URL; run PDL only on those Apify missed or didn't
  have a URL for.

#### 6c. Job Postings Scraper — org chart signal extraction

```python
# Apify actor: "curious_coder/linkedin-jobs-scraper"
# Pulls active job postings for a company — rich source of reporting-line signals

POST https://api.apify.com/v2/acts/curious_coder~linkedin-jobs-scraper/runs
Headers: {"Authorization": f"Bearer {APIFY_TOKEN}"}
Body: {
    "companyUrls": ["https://www.linkedin.com/company/intel/"],
    "maxJobs": 200,
    "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
}
# Each job posting is a REPORTING_PATTERN extraction target (CLAUDE.md NLP Extraction Patterns)
# "This role reports to the VP of Hardware Engineering" → explicit org chart signal
# Write matched patterns to signals table as signal_type="job_posting" for the org chart pipeline
```

This feeds the **explicit signal pass** in the org chart hierarchy inference (V3_PT2.md Stage 1.2).
Every job posting with a REPORTING_PATTERN match is a free, authoritative org chart edge.

#### Apify rate limits and cost controls

```python
# Apify pricing (approximate — check your plan):
# Company employee scraper: ~$1.00 per 1,000 profiles
# Profile scraper: ~$0.50 per 100 profiles = $0.005/profile
# Job scraper: ~$0.30 per 100 jobs

# Cost estimate for 500-person coverage at one company:
# Employee discovery: 1,000 pull → ~$1.00
# Profile enrichment: 500 profiles → ~$2.50
# Job postings: 200 jobs → ~$0.60
# Total per company: ~$4.10 — significantly cheaper than equivalent PDL spend

# Rate limit: Apify runs are async. Poll for completion:
run = await apify_client.actor("curious_coder/linkedin-profile-scraper").call(run_input=...)
dataset = await apify_client.dataset(run["defaultDatasetId"]).list_items()
# Use apify-client Python SDK: pip install apify-client

# Concurrency: run at most 3 Apify actors simultaneously to avoid LinkedIn detection
# Stagger company runs by 30–60 seconds between starts
```

**Add to `.env.local`:**
```
APIFY_TOKEN=<your_token>   # already in .env.example; add value here
```

#### LinkedIn (direct Firecrawl fallback — use only if Apify is down)

If the Apify actor fails or returns empty, fall back to Firecrawl on the LinkedIn company people
page. This is lower fidelity (no employment history, no education) but gives you names and titles:

```python
# Fallback only — Apify is preferred
"url": "https://www.linkedin.com/company/nvidia/people/",
"extractorOptions": {"mode": "llm-extraction", "extractionPrompt": "..."}
```

---

## Pipeline Architecture

### `server/credence/enrichment/` — New Package

```
server/credence/enrichment/
├── __init__.py
├── pipeline.py          -- orchestrator: all 15 steps, one company at a time
├── apollo.py            -- Apollo org search + person pull (bulk discovery, verified emails)
├── apify.py             -- Apify: company employees, profile enrichment, job postings,
│                            LinkedIn posts/comments/articles, Twitter
├── pdl.py               -- PDL: name-based enrichment for profiles without LinkedIn URLs
├── firecrawl.py         -- leadership pages, press releases, SEC proxy, blog posts
├── edgar.py             -- SEC EDGAR DEF 14A executive + board member extraction
├── patents.py           -- USPTO ODP + EPO patent inventor + citation graph
├── scholar.py           -- Semantic Scholar + ORCID + arXiv publication enrichment
├── conferences.py       -- program committee scraping + podcast appearance detection
├── recognition.py       -- IEEE/ACM/NAE fellowship + industry award scraping
├── github.py            -- GitHub profile + org membership + repo analysis
├── normalizer.py        -- canonical name resolution, company dedup, seniority + domain assignment
└── writer.py            -- writes to Supabase: all tables, ON CONFLICT upserts, 4KB cap enforced
```

### `server/credence/enrichment/pipeline.py` — Orchestrator

```python
async def enrich_company(
    company_name: str,
    apollo_org_id: Optional[str] = None,
    targets: dict = {
        "min_persons": 80,
        "max_persons": 400,
        "seniority_filter": ["c_suite", "vp", "director", "manager", "senior"],
    }
) -> EnrichmentResult:
    """
    Full enrichment pipeline for one company.
    Returns the number of persons written to Supabase.
    Idempotent: re-running against an already-enriched company upserts, not duplicates.
    Target: < 60 seconds per company for the Apollo pass; < 5 min for full enrichment.
    """

    # Step 1: EDGAR proxy (free, authoritative, zero cost — always first)
    executives = await edgar.extract_executives(company_name)

    # Step 2: Firecrawl leadership page (free, catches public /about/leadership pages)
    leadership = await firecrawl.extract_leadership_page(company_name)

    # Step 3: Apify LinkedIn company employee scraper (primary bulk discovery)
    # Run this BEFORE Apollo — Apify gives us LinkedIn URLs which improve Apollo match quality
    linkedin_company_url = f"https://www.linkedin.com/company/{company_linkedin_slug}/"
    apify_employees = await apify.scrape_company_employees(
        linkedin_company_url, max_results=1000
    )

    # Step 4: Apollo org search (fills gaps Apify missed, adds verified emails)
    if apollo_org_id:
        apollo_persons = await apollo.search_persons(apollo_org_id, targets["seniority_filter"])
    else:
        org = await apollo.find_org_by_name(company_name)
        apollo_persons = await apollo.search_persons(org.id, targets["seniority_filter"])

    # Step 5: Dedup + normalize across all four sources
    merged = normalizer.merge([executives, leadership, apify_employees, apollo_persons])

    # Step 6: Apify LinkedIn profile enrichment (deep pass — employment history + education)
    # Run on everyone with a linkedin_url — this is the highest-fidelity enrichment source
    has_linkedin = [p for p in merged if p.linkedin_url and not p.is_apify_enriched]
    apify_profiles = await apify.scrape_profiles_batch(
        [p.linkedin_url for p in has_linkedin], batch_size=500
    )

    # Step 7: PDL enrichment pass — only for persons Apify couldn't reach (no LinkedIn URL)
    no_linkedin = [p for p in merged if not p.linkedin_url and not p.is_pdl_enriched]
    pdl_results = await pdl.enrich_batch(no_linkedin, batch_size=50)

    # Step 8: Apify job postings scraper — extract org chart signals from active postings
    job_postings = await apify.scrape_job_postings(linkedin_company_url, max_jobs=200)
    org_signals = await firecrawl.extract_reporting_patterns(job_postings)
    # org_signals feed into signals table as signal_type="job_posting" for org chart pipeline

    # Step 9: Write everything to Supabase
    result = await writer.write_persons(merged, apify_profiles, pdl_results, org_signals)

    return result
```

### `server/credence/enrichment/normalizer.py` — The Hardest Part

Entity resolution across three sources (EDGAR, Firecrawl, Apollo) for the same person.
The same person may appear as:
- EDGAR: "Phebe N. Novakovic, Chairman and Chief Executive Officer"
- Firecrawl: "Phebe Novakovic - Chairman & CEO"
- Apollo: `{name: "Phebe Novakovic", title: "CEO", org: "General Dynamics"}`

```python
def merge_person_records(records: list[RawPersonRecord]) -> CanonicalPerson:
    """
    Merge duplicate person records from different sources.
    Resolution priority: EDGAR > Apollo > Firecrawl (EDGAR is ground truth for public companies)
    Name matching: exact → normalized (remove middle initial) → fuzzy (rapidfuzz ≥ 0.88)
    """

    # Seniority assignment: use CLAUDE.md taxonomy
    # "Chairman and Chief Executive Officer" → 100
    # "Executive Vice President" → 82
    # "Vice President, Supply Chain" → 70
    # "Senior Director of Engineering" → 62
    # "Distinguished Engineer" → 55 (IC track)

    # Functional domain assignment: use CLAUDE.md Functional Domain Taxonomy
    # "VP of Hardware Engineering" → hardware_engineering
    # "Director, Supply Chain Operations" → manufacturing_ops
    # "SVP, Sales and Marketing" → sales_marketing
```

**Name normalization gotchas:**
- Middle initials: "James R. Clarke" = "James Clarke"
- Suffixes: "Dr.", "Ph.D.", "Jr." — strip before matching
- International names: TSMC, ASML, Samsung have Romanized names that may differ across sources
- Hyphenated names: "Chang-Gung Lee" vs "Chang Gung Lee" vs "C.G. Lee"
- Company name normalization: "Raytheon Technologies" = "RTX" = "Raytheon" (post-merger)

---

## Deep Signal Enrichment — Beyond Employment History

Employment history and current title are the skeleton. The signals below are the flesh — they feed
directly into the three scoring dimensions (Authenticity 40%, Authority 40%, Warmth 20%) and power
the warm path engine. Run these after the primary person discovery pass is complete.

Each signal type maps to a `signal_type` value in Contract 4's `SignalType` union. New types added
in this section must be appended to that union before the extractors are written.

---

### Signal Group 1: LinkedIn Activity (Apify)

LinkedIn activity is the highest-signal behavioral data available on professionals. It tells you
what a prospect cares about, who they engage with publicly, and how reachable they are — all of
which feed the **Warmth** scoring component.

#### 1a. Posts authored (`signal_type: "linkedin_post"`)

```python
# Apify actor: "curious_coder/linkedin-post-scraper"
# or: "apify/linkedin-posts-scraper"

POST https://api.apify.com/v2/acts/curious_coder~linkedin-post-scraper/runs
Body: {
    "profileUrls": ["https://www.linkedin.com/in/sanja-fidler-2846a1a"],
    "maxPostsPerProfile": 50,    # last 50 posts — enough to characterize topic distribution
    "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
}
# Returns per post: {
#     text: str,                 # full post text
#     publishedAt: datetime,
#     likesCount: int,
#     commentsCount: int,
#     repostsCount: int,
#     url: str,                  # permalink
#     hashtags: [str],
#     mentionedProfiles: [str],  # LinkedIn URLs of mentioned people — warm signal
#     images: [str],
# }
```

What this drives in scoring and warm paths:
- **Warmth**: if a prospect's posts frequently mention topics relevant to your connector's work
  (e.g., both posting about RISC-V architecture), that's a genuine warm signal
- **Warmth**: `mentionedProfiles` — if they publicly mention someone in your network, that's
  a near-certain existing relationship
- **Authenticity**: post count + engagement rate validates that the profile is active and real
- **Suggested opener**: "I've been following your posts on GPU memory bandwidth — your take on
  HBM3E last month was exactly what we've been thinking about."

```python
# Structured value to store (4KB cap — store only extracted metadata, not full text)
{
    "prospect_id": str,
    "post_url": str,
    "published_at": str,          # ISO datetime
    "engagement_score": int,      # likes + comments*3 + reposts*5
    "top_hashtags": [str],        # top 5 hashtags across all posts
    "mentioned_profile_urls": [str],  # deduped across all posts
    "avg_posts_per_month": float,
    "topics": [str],              # LLM-extracted topic labels (max 10) — run Claude Haiku
    "is_thought_leader": bool,    # True if avg engagement > 100 per post
}
```

#### 1b. Comments made (`signal_type: "linkedin_comment"`)

```python
# Apify actor: "apify/linkedin-comments-scraper"
# or scrape comments from each post returned by the post scraper above

# What comments reveal:
# - Who they engage with (other people's posts they comment on)
# - What sub-topics they have opinions on
# - Their communication style and technical depth
# - Potential existing relationships (commenting on the same posts as someone in your network)

# Structured value:
{
    "prospect_id": str,
    "commented_on_post_url": str,
    "post_author_linkedin_url": str,  # who they commented on — KEY warm path signal
    "comment_text_preview": str,      # first 200 chars only (4KB cap)
    "commented_at": str,
    "topic": str,                     # LLM-classified topic
}

# WARM PATH APPLICATION:
# If prospect comments frequently on posts by someone in your connector network,
# write a "linkedin_engagement" edge in person_connections with:
#   base_strength: 0.35  (stronger than conference_co_attendee, weaker than career_overlap)
#   connection_type: "linkedin_engagement"
# Explanation: "Sarah Kim and Wei Chen regularly engage on each other's LinkedIn posts
#               about GPU architecture (12 interactions in 2024)"
```

#### 1c. Reactions / likes (`signal_type: "linkedin_reaction"`)

Weaker signal than comments but useful in aggregate. If a prospect likes posts from someone in your
network consistently, it signals awareness and positive sentiment toward that person.

```python
# Apify: reactions are harder to scrape reliably — treat as best-effort
# Store only aggregate counts, not individual reaction records (too noisy)
{
    "prospect_id": str,
    "total_reactions_given_30d": int,   # volume proxy for LinkedIn activity level
    "topics_reacted_to": [str],         # LLM-classified from liked post text samples
}
```

#### 1d. LinkedIn articles and newsletters (`signal_type: "linkedin_article"`)

Long-form LinkedIn articles signal thought leadership and domain authority. They feed the
**Authenticity** score component.

```python
# Apify actor: "apify/linkedin-articles-scraper"
{
    "prospect_id": str,
    "article_url": str,
    "title": str,
    "published_at": str,
    "views": int,
    "topics": [str],         # LLM-extracted
    "word_count": int,
}
# Authority signal: article_count > 5 with avg_views > 500 → is_thought_leader = True
```

---

### Signal Group 2: Conference Attendance and Participation

Conference signals are currently scoped to co-presenter and co-attendee edges in V3_PT2.md.
This section expands to capture the full range of conference participation — organized by signal
strength.

#### 2a. Conference presentations and keynotes (`signal_type: "conference_presentation"`)

Already covered in V3_PT2.md Plan B. Use Firecrawl + Parallel against the TARGET_CONFERENCES
list. Add to the `event_appearances` table with `role = "presenter"` or `role = "keynote"`.

**Additional signal extraction from presentation content:**
```python
# For each confirmed presentation, extract:
{
    "prospect_id": str,
    "event_id": str,
    "event_name": str,
    "year": int,
    "session_title": str,
    "role": "presenter" | "keynote" | "panelist" | "session_chair",
    "co_presenters": [str],       # LinkedIn URLs of other presenters in same session
    "abstract_topics": [str],     # LLM-extracted from session abstract
    "recording_url": str,         # YouTube/conference archive link if available
    "slides_url": str,            # Speaker Deck, conference proceedings
}
# co_presenters → direct conference_co_presenter edges in person_connections
```

#### 2b. Conference program committee / organizing roles (`signal_type: "conference_committee"`)

Program committee membership is stronger than attendance — it means the person was vetted by the
field as an expert and spent weeks reviewing submissions. Major conferences that publish PC lists:

```python
CONFERENCE_PROGRAM_COMMITTEES = {
    "NeurIPS":    "https://neurips.cc/Conferences/{year}/ProgramCommittee",
    "ICML":       "https://icml.cc/{year}/Reviewers",
    "ICLR":       "https://iclr.cc/Conferences/{year}/Reviewers",
    "CVPR":       "https://cvpr.thecvf.com/{year}/organizers",
    "ISSCC":      "https://www.isscc.org/organizers",
    "Hot Chips":  "https://hotchips.org/organizing-committee/",
    "DAC":        "https://www.dac.com/About/Conference-Committee",
    "IEDM":       "https://www.ieee-iedm.org/committees",
    "DATE":       "https://www.date-conference.com/committees",
}
# Scrape with Firecrawl; entity-resolve names against persons table
# Store in event_appearances with role = "program_committee"
# base_strength for co-PC-member edge: 0.72 (between standards_committee and conference_co_presenter)
```

#### 2c. Industry summits and invite-only events (`signal_type: "summit_attendance"`)

Invite-only events (Davos WEF, Bilderberg, DARPA workshops, Aspen Strategy Group, CEO summits)
are the strongest conference signal because attendance is curated. Sources:

```python
INVITE_ONLY_EVENTS = {
    "DARPA workshops":      "https://www.darpa.mil/work-with-us/opportunities",
    "Semiconductor summit": firecrawl company press releases mentioning "invited to",
    "IEEE medal ceremony":  "https://www.ieee.org/about/awards/medals.html",
    "NAE annual meeting":   "https://www.nae.edu/Events.aspx",
    "NAS symposia":         "https://www.nasonline.org/programs/sackler-colloquia/",
    "YC/a16z summits":      firecrawl press mentions,
}
# Extraction: Firecrawl + Parallel; add to events + event_appearances
# base_strength for co-attendee: 0.55 (higher than public conference, lower than standards committee)
```

#### 2d. Podcast appearances (`signal_type: "podcast_appearance"`)

Podcast guest spots are a proxy for thought leadership and often contain the richest self-reported
career narrative — people say things in podcasts they don't put on LinkedIn.

```python
# Sources: Listen Notes API (free tier), Podchaser, Spotify podcast search
# Or: Firecrawl + Parallel against known industry podcasts

INDUSTRY_PODCASTS = [
    "The Acquired Podcast",      # tech company deep dives
    "Lex Fridman Podcast",       # AI/ML researchers
    "Semiconductor Uncensored",  # semiconductor executives
    "The Chip Letter",           # chip industry
    "Defense & Aerospace Report",# defense executives
    "Software Engineering Daily",# engineering leaders
    "The AI Podcast (NVIDIA)",   # NVIDIA-hosted, guests are industry leaders
    "Eye on AI",                 # AI researchers and executives
]

{
    "prospect_id": str,
    "podcast_name": str,
    "episode_title": str,
    "episode_url": str,
    "published_at": str,
    "topics_discussed": [str],   # LLM-extracted from transcript or description
    "co_guests": [str],          # other guests in same episode — warm signal
}
# Authority signal: podcast_appearance_count feeds into Authenticity score
```

---

### Signal Group 3: Patents — Full Coverage

The current plan covers USPTO via ODP (pending key). Expand to international patents and
forward/backward citation graphs — these are the richest relationship signals in technical fields.

#### 3a. USPTO patents (`signal_type: "patent_co_inventor"`)

Already in CONTRACTS.md Contract 1. Once `USPTO_ODP_API_KEY` is set:

```python
# Per person, pull all patents where they appear as inventor
GET https://api.odp.uspto.gov/api/v1/patent/search
    ?q=inventor_name:"Wei Chen"&fields=patent_number,title,inventors,assignee,filing_date

# Also pull by assignee company to find additional patents:
GET .../patent/search?q=assignee_organization:"Intel Corporation"&fields=...
# Then match inventors against known persons by name

# Store in: patents table + patent_inventors junction + person_connections
```

#### 3b. International patents via EPO (free, no auth) (`signal_type: "patent_international"`)

```python
# EPO Open Patent Services — free, REST API, no key required
# Covers EP, WO (PCT), US, and 90+ countries

GET https://ops.epo.org/3.2/rest-services/published-data/search/biblio
    ?q=inventor%3A"Wei+Chen"&Range=1-50
# Returns: publication_number, title, inventors, applicant (assignee), filing_date, ipc_codes

# Value add over USPTO alone:
# - PCT applications show global IP strategy (signals company priority)
# - EP patents reveal European R&D investment (useful for TSMC, ASML, Infineon targets)
# - IPC codes classify the technology domain — better than title-based classification

{
    "patent_number": str,         # EP2345678, WO2023/123456, etc.
    "title": str,
    "filing_date": str,
    "assignee": str,
    "ipc_codes": [str],           # e.g., ["H01L 27/108", "G06F 17/10"]
    "patent_office": "EPO" | "WIPO" | "USPTO",
    "co_inventors": [str],        # full inventor list for edge derivation
}
```

#### 3c. Patent citation graph (`signal_type: "patent_citation"`)

When person A's patent cites person B's patent, it means A's team studied B's work closely enough
to reference it in a legal document. This is a documented, datable awareness signal.

```python
# USPTO ODP citation endpoint (once key is available):
GET https://api.odp.uspto.gov/api/v1/patent/{patent_number}/citations/forward
GET https://api.odp.uspto.gov/api/v1/patent/{patent_number}/citations/backward

# Derive edges:
# If Wei Chen (Intel) cites a patent by Sanja Fidler (NVIDIA):
#   connection_type: "patent_citation_aware"
#   base_strength: 0.45  (weaker than co-inventor, stronger than conference_co_attendee)
#   explanation: "Wei Chen's 2021 patent US11,234,567 cites Sanja Fidler's 2019 work on
#                 neural rendering at NVIDIA (US10,876,543)"

# This surfaces hidden intellectual awareness between people who may never have met
```

#### 3d. Patent application pending (`signal_type: "patent_pending"`)

Pending applications (published but not granted) reveal current research direction — what the
person and their team are working on right now. Filed 18 months before publication.

```python
# USPTO pre-grant publication search
GET https://api.odp.uspto.gov/api/v1/application/search?q=inventor_name:"Wei Chen"

# Store in signals table as signal_type="patent_pending"
# Feeds into: person_scope_estimates.owns_technologies[] for the org chart pipeline
# Also valuable for sales intelligence: "I see you're working on X based on your recent filings"
```

---

### Signal Group 4: Academic and Research Signals

#### 4a. Publications via Semantic Scholar (`signal_type: "academic_co_author"`)

Already in CONTRACTS.md. Expand to also capture:

```python
# Citation count → Authority score component
# h-index → Authority score (how many papers have been cited ≥ h times)
# Paper venue prestige → Authenticity score (Nature > workshop paper)
# Co-author network → warm paths (already in plan)

# Additional fields to extract:
{
    "semantic_scholar_id": str,
    "paper_count": int,
    "citation_count": int,
    "h_index": int,
    "top_venues": [str],          # Nature, IEEE TPAMI, NeurIPS, ICML, etc.
    "research_topics": [str],     # Semantic Scholar's field-of-study tags
    "influential_citations": int, # papers that are "highly influential" citations
}
```

#### 4b. Google Scholar profile (`signal_type: "scholar_profile"`)

Semantic Scholar misses some researchers. Google Scholar covers more comprehensively but has no API
— use Firecrawl on the public profile page:

```python
# Firecrawl public Google Scholar profile
"url": f"https://scholar.google.com/citations?user={google_scholar_id}",
# Extracting: {name, affiliation, verified_email, h_index, i10_index, citation_count,
#              research_interests, coauthors_shown_on_profile}
# coauthors_shown_on_profile → prioritize those for co-author edge discovery
```

#### 4c. ORCID (`signal_type: "orcid_profile"`)

ORCID is the researcher identifier standard — mandatory for NSF grants, common in IEEE publications.
It provides a clean, self-curated publication list with DOIs.

```python
# ORCID public API — free, no auth for public records
GET https://pub.orcid.org/v3.0/{orcid_id}/works
# Returns: all publications with DOIs, venues, co-authors
# Value: ORCID records are self-maintained — higher accuracy than Semantic Scholar's auto-ingestion

# Find ORCID for a prospect:
GET https://pub.orcid.org/v3.0/search?q=given-names:Wei+AND+family-name:Chen+AND+affiliation-org-name:Intel
```

#### 4d. arXiv preprints (`signal_type: "arxiv_preprint"`)

In AI/ML, arXiv preprints often precede peer-reviewed publication by 1–2 years. The most current
view of what a researcher is working on.

```python
# arXiv API — free, no auth
GET http://export.arxiv.org/api/query?search_query=au:Chen_W+AND+affiliation:Intel&max_results=50
# Returns: title, abstract, authors, categories, submission_date

# Value for warm paths:
# Two people who submitted arXiv papers in the same research area within 6 months of each other
# are almost certainly aware of each other's work even without formal co-authorship
```

---

### Signal Group 5: Professional Recognition and Authority

#### 5a. Professional society fellowships (`signal_type: "professional_fellowship"`)

IEEE Fellow, ACM Fellow, NAE Member, NAS Member — these are the highest-prestige signals of
technical authority and are publicly listed.

```python
FELLOWSHIP_SOURCES = {
    "IEEE Fellow": "https://www.ieee.org/content/dam/ieee-org/ieee/web/org/about/fellows/fellow-directory.html",
    "ACM Fellow":  "https://awards.acm.org/fellows",
    "NAE Member":  "https://www.nae.edu/MembersSection/MemberDirectory.aspx",
    "NAS Member":  "https://www.nasonline.org/membership/members/",
    "AAAS Fellow": "https://www.aaas.org/fellows/search",
}
# All publicly searchable. Firecrawl each directory; entity-resolve names.
# Store in signals table. Feeds Authority score directly.
# base_strength for "shared fellowship society" edge: 0.60
# (two IEEE Fellows know each other exists even without direct contact)

{
    "prospect_id": str,
    "fellowship": "IEEE Fellow" | "ACM Fellow" | "NAE" | "NAS" | "AAAS Fellow",
    "year_elected": int,
    "citation": str,             # the official citation explaining why they were elected
    "section_or_division": str,  # IEEE technical society, NAE section, etc.
}
```

#### 5b. Industry awards (`signal_type: "industry_award"`)

```python
AWARD_SOURCES = {
    "IEEE Medals":          "https://www.ieee.org/about/awards/medals.html",
    "ACM Turing Award":     "https://amturing.acm.org/",
    "ACM SIGDA Awards":     "https://www.sigda.org/sigda-awards/",
    "SEMI Award":           "https://www.semi.org/en/semi-award",
    "EDA Consortium":       "https://www.edac.org/awards/",
    "Forbes lists":         Firecrawl Forbes 50 Over 50, Forbes AI 50, etc.,
    "MIT TR 35":            "https://www.technologyreview.com/lists/innovators-under-35/",
    "Time 100 AI":          "https://time.com/collection/time100-ai/",
}
# Firecrawl + entity resolution
# Authority signal: any major award → authority_score boost of 5–10 points
```

#### 5c. Board seats and advisory roles (`signal_type: "board_seat"`)

Public company proxy statements (DEF 14A, already in EDGAR plan) list board members. For startup
advisory roles, use Crunchbase and AngelList.

```python
# Crunchbase API (requires key — check if available, else Firecrawl public pages)
GET https://api.crunchbase.com/api/v4/entities/people/{person_permalink}
    ?field_ids=advisory_roles,board_members_and_advisors

# AngelList / Wellfound public profiles — Firecrawl
"url": f"https://wellfound.com/u/{handle}"

{
    "prospect_id": str,
    "org_name": str,
    "org_url": str,
    "role": "board_member" | "advisor" | "independent_director",
    "since_year": int,
    "org_stage": "public" | "series_a" | "series_b" | "seed" | etc.,
}
# Two people who are advisors at the same startup → strong warm edge
# base_strength: "co_investor" at 0.78 (same-advisor relationship is peer-equivalent)
```

#### 5d. Government advisory and grant roles (`signal_type: "government_advisory"`)

DARPA program managers and technical advisors, NSF panelists, DOE reviewers — these are publicly
listed and signal both authority and a specific network of government-adjacent peers.

```python
GOVERNMENT_SOURCES = {
    "DARPA program managers":   "https://www.darpa.mil/our-research/offices",
    "NSF grant awardees":       "https://www.nsf.gov/awardsearch/",
    "DOE ARPA-E":               "https://arpa-e.energy.gov/technologies/programs",
    "NIST":                     "https://www.nist.gov/director/staff",
    "NRL":                      "https://www.nrl.navy.mil/",
    "IARPA":                    "https://www.iarpa.gov/research-programs",
}
# NSF grant awardees list: freely searchable by PI name + institution
# Value: two people who both received NSF grants in the same program area are peers
#        and were likely reviewed by the same panel members
```

---

### Signal Group 6: Online Presence and Thought Leadership

#### 6a. GitHub profile (`signal_type: "github_profile"`)

For engineering-heavy targets (semiconductor, AI), GitHub activity is a direct window into
technical work and collaboration networks.

```python
# GitHub API — free, 60 req/hour unauthenticated; add GITHUB_TOKEN for 5000/hour
# (GITHUB_TOKEN already in .env.example)

GET https://api.github.com/users/{username}
GET https://api.github.com/users/{username}/repos
GET https://api.github.com/users/{username}/orgs    # company GitHub org membership

{
    "prospect_id": str,
    "github_username": str,
    "public_repos": int,
    "followers": int,
    "following": int,
    "top_languages": [str],                    # ["Python", "CUDA", "C++"]
    "orgs": [str],                             # GitHub org names (company affiliations)
    "notable_repos": [{                        # top 5 by stars
        "name": str, "stars": int, "description": str, "language": str
    }],
    "is_org_member": {str: bool},              # "nvidia": True, "pytorch": True
}
# GitHub org membership → verifies company affiliation independently of LinkedIn
# Co-contributors on the same repo → contributor_overlap edge
# base_strength for "github_co_contributor": 0.65
```

#### 6b. Twitter/X (`signal_type: "twitter_activity"`)

Senior executives in semiconductor and AI are often active on Twitter. Engagement patterns reveal
relationships and interests.

```python
# Twitter API v2 (requires TWITTER_BEARER_TOKEN — add to .env if available)
# Or: Apify Twitter scraper (no key required via Apify)
# actor: "apify/twitter-scraper"

{
    "prospect_id": str,
    "twitter_handle": str,
    "followers_count": int,
    "following_count": int,
    "tweet_count": int,
    "top_hashtags": [str],
    "frequently_mentioned_accounts": [str],    # Twitter handles they @ frequently
    "topics": [str],                           # LLM-classified
}
# frequently_mentioned_accounts → warm signal if any are in connector network
```

#### 6c. Company blog and technical writing (`signal_type: "technical_publication"`)

Engineering blogs (NVIDIA Technical Blog, Intel Developer Zone, Google AI Blog, etc.) are bylined
and indexed. A person who authors company blog posts is a spokesperson for that team.

```python
COMPANY_TECH_BLOGS = {
    "NVIDIA":    "https://developer.nvidia.com/blog/author/{slug}",
    "Intel":     "https://www.intel.com/content/www/us/en/developer/articles/technical/",
    "Google":    "https://ai.googleblog.com/",
    "Microsoft": "https://www.microsoft.com/en-us/research/blog/",
    "Meta AI":   "https://ai.meta.com/blog/",
    "AMD":       "https://community.amd.com/t5/blogs/bg-p/TechBlog",
    "Qualcomm":  "https://www.qualcomm.com/news/onq",
}
# Firecrawl each blog's author page; entity-resolve to prospect list
# Feeds Authenticity score: authored technical blog post → documented domain expertise
```

---

### How These Signals Map to the Scoring Model

Every signal written to Supabase flows into one of the three scoring dimensions. This table is the
contract between the enrichment pipeline and the scoring engine:

| Signal type | Scoring dimension | Sub-factor | Weight direction |
|---|---|---|---|
| `linkedin_post` (high engagement) | Authenticity | Executive profile depth | ↑ |
| `linkedin_post` (topic relevance to connector) | Warmth | Conference co-appearance | ↑ |
| `linkedin_comment` on connector's network post | Warmth | Career overlap proxy | ↑ |
| `conference_presentation` (keynote) | Authority | Patent/publication count proxy | ↑ |
| `conference_committee` | Authority | Domain influence | ↑ |
| `patent_co_inventor` | Authenticity | Patent/paper evidence | ↑ |
| `patent_citation` | Authority | Domain influence (cited by others) | ↑ |
| `academic_co_author` (high citations) | Authenticity | Patent/paper evidence | ↑ |
| `professional_fellowship` (IEEE Fellow) | Authority | Seniority score boost | ↑ |
| `industry_award` | Authenticity | Award/recognition | ↑ |
| `board_seat` (public company) | Authority | Budget authority level | ↑ |
| `github_profile` (high followers, org member) | Authenticity | Executive profile depth | ↑ |
| `podcast_appearance` | Authenticity | Third-party validation | ↑ |
| `government_advisory` | Authority | Domain influence | ↑ |
| `twitter_activity` (mentions connector network) | Warmth | Conference co-appearance proxy | ↑ |

Falsification notes must be updated for each new signal type. Examples:
- `professional_fellowship`: "IEEE Fellow status confirmed but election year was 2009 — this person
  may have shifted domains substantially since then."
- `patent_citation`: "Citation detected but may be a negative citation (cited to contrast against,
  not endorse). Patent text would need review to confirm sentiment."

---

### New `signal_type` values to add to Contract 4 (`SignalType` union)

```typescript
// Add to SignalType in src/types/index.ts
| "linkedin_post"
| "linkedin_comment"
| "linkedin_reaction"
| "linkedin_article"
| "conference_presentation"
| "conference_committee"
| "summit_attendance"
| "podcast_appearance"
| "patent_international"
| "patent_pending"
| "patent_citation"
| "scholar_profile"
| "orcid_profile"
| "arxiv_preprint"
| "professional_fellowship"
| "industry_award"
| "board_seat"
| "government_advisory"
| "github_profile"
| "twitter_activity"
| "technical_publication"
```

---

### New pipeline steps (add to `pipeline.py` orchestrator after Step 9)

```python
    # Step 10: LinkedIn activity enrichment (posts, comments, articles)
    # Run for VP+ seniority only — IC-level LinkedIn activity is lower signal
    senior_persons = [p for p in merged if p.seniority_score >= 65]
    linkedin_urls = [p.linkedin_url for p in senior_persons if p.linkedin_url]
    await apify.scrape_linkedin_activity(linkedin_urls, max_posts=50)

    # Step 11: Patent enrichment — USPTO ODP (if key set) + EPO (always free)
    await patents.enrich_all_persons(merged)        # writes to patents + patent_inventors tables
    await patents.build_citation_graph(merged)       # writes patent_citation signals

    # Step 12: Academic signals — Semantic Scholar + ORCID + arXiv
    await scholar.enrich_all_persons(merged)
    await orcid.enrich_all_persons(merged)
    await arxiv.enrich_all_persons(merged)

    # Step 13: Professional recognition — fellowships + awards
    await recognition.scrape_fellowships(merged)    # IEEE, ACM, NAE, NAS
    await recognition.scrape_awards(merged)

    # Step 14: GitHub + Twitter (best-effort, non-blocking)
    await asyncio.gather(
        github.enrich_all_persons(merged),
        twitter.enrich_all_persons(merged),
        return_exceptions=True,   # don't fail the pipeline if GitHub/Twitter is down
    )

    # Step 15: Conference program committees + podcast appearances
    await conferences.scrape_program_committees(merged)
    await podcasts.scrape_appearances(merged)

    return await writer.write_final_result(merged)
```

---

## Execution Order

Run companies in this priority order. Each company gets a full pipeline run before moving to the next.
The goal is to have 10 fully-covered companies before the YC demo, not 50 partially-covered ones.

### Phase 1 — Demo-critical (run before anything else)

These 5 companies appear in `DEMO_CASES.md` and their org charts are directly demoed:

1. **NVIDIA** — Cases 1 and 2 (Keith Strier connector); need full engineering + research org
2. **Intel** — Cases 3 and 4 (Martin Ashton connector); need hardware engineering + quantum divisions
3. **AMD** — All 5 cases (AMD is the "your company" side); need full AMD org for connector identification
4. **Qualcomm** — Case 5 (Javed Absar target); need AI compiler + ML engineering org
5. **Graphcore** — Case 5 (Newling ↔ Absar shared employer); smaller company, easier to get full coverage

**Success criterion for Phase 1:** each of these 5 companies has ≥ 500 persons in Supabase with
`functional_domain` and `seniority_score` populated on all `employment_periods` rows.

### Phase 2 — Tier 1 Semiconductor (run after Phase 1)

TSMC, ASML, Broadcom, Marvell, Micron, Applied Materials, Lam Research, KLA, Synopsys, Cadence.
Target: 500 persons per company. Smaller companies (Marvell, KLA, Arm) may not have 500 public
profiles available — pull everything that exists and flag if coverage falls below 300.

### Phase 3 — Defense (run after Phase 2)

Lockheed Martin, Raytheon, Northrop Grumman, General Dynamics, L3Harris, BAE Systems, Leidos.
Target: 500–1,000 persons per company. Defense orgs are large and division-structured — prioritize
coverage across all major divisions (aeronautics, missiles, space, cyber, IT) rather than going deep
in one division.

### Phase 4 — Aerospace + remaining semiconductor

Boeing, Airbus, SpaceX, remaining semiconductor companies from Tier 1 list.
Target: 500 persons per company. SpaceX and Rocket Lab are smaller — pull everything available.

---

## Seniority and Domain Assignment

Every person written to Supabase must have `seniority_score` and `functional_domain` assigned on
their `employment_periods` rows. These drive clustering and hierarchy inference. Without them, the
org chart pipeline produces nothing.

Use these exact keys from CLAUDE.md:

```python
# server/credence/enrichment/normalizer.py

SENIORITY_SCORES = {
    # Map title keywords → seniority score
    # Must match CLAUDE.md taxonomy exactly
    "chief executive officer": 100, "ceo": 100,
    "president": 95,
    "chief operating officer": 90, "coo": 90,
    "chief technology officer": 90, "cto": 90,
    "chief financial officer": 89, "cfo": 89,
    "chief product officer": 88, "cpo": 88,
    "chief revenue officer": 88, "cro": 88,
    "executive vice president": 82, "evp": 82,
    "senior vice president": 80, "svp": 80,
    "vice president": 70, "vp": 70,
    "group vice president": 72,
    "senior director": 62,
    "principal director": 63,
    "director": 60,
    "senior manager": 52,
    "engineering manager": 50,
    "group manager": 52,
    "distinguished engineer": 55,    # IC track
    "principal engineer": 48,        # IC track
    "principal scientist": 48,       # IC track
    "staff engineer": 45,            # IC track
    "fellow": 58,                    # IC track (above Distinguished)
    "senior engineer": 40,
    "engineer": 35,
    "senior researcher": 45,
    "researcher": 38,
    "senior manager": 52,
    "manager": 48,
}

FUNCTIONAL_DOMAINS = {
    # Map title keywords → domain key (from CLAUDE.md Functional Domain Taxonomy)
    "hardware": "hardware_engineering",
    "chip": "hardware_engineering", "rtl": "hardware_engineering",
    "verification": "hardware_engineering", "physical design": "hardware_engineering",
    "analog": "hardware_engineering", "mixed signal": "hardware_engineering",
    "memory design": "hardware_engineering", "soc": "hardware_engineering",
    "architecture": "hardware_engineering",   # careful — could be SW arch too
    "software": "software_engineering", "firmware": "software_engineering",
    "embedded": "software_engineering", "sdk": "software_engineering",
    "driver": "software_engineering", "bsp": "software_engineering",
    "product": "product_management", "program manager": "product_management",
    "tpm": "product_management", "roadmap": "product_management",
    "manufacturing": "manufacturing_ops", "operations": "manufacturing_ops",
    "supply chain": "manufacturing_ops", "yield": "manufacturing_ops",
    "fab": "manufacturing_ops", "foundry": "manufacturing_ops",
    "quality": "manufacturing_ops", "reliability": "manufacturing_ops",
    "sales": "sales_marketing", "marketing": "sales_marketing",
    "business development": "sales_marketing", "gtm": "sales_marketing",
    "account": "sales_marketing", "partnerships": "sales_marketing",
    "research": "research", "advanced development": "research",
    "pathfinding": "research", "exploratory": "research",
    "finance": "finance_legal", "legal": "finance_legal",
    "compliance": "finance_legal", "tax": "finance_legal",
    "hr": "people_ops", "recruiting": "people_ops",
    "people operations": "people_ops",
    "general manager": "general_management", "gm ": "general_management",
    "business unit": "general_management", "p&l": "general_management",
    "quantum": "research",      # quantum computing maps to research domain
    "ai compiler": "software_engineering",
    "machine learning": "research",   # or software_engineering depending on title
    "data science": "research",
}
```

When a title matches multiple keywords, use the most specific one. When no keyword matches, use an
LLM call (via Anthropic API, `ANTHROPIC_API_KEY` in `.env.local`) to classify the title:

```python
async def classify_title_with_llm(title: str) -> tuple[int, str]:
    """Returns (seniority_score, functional_domain) for ambiguous titles."""
    # Only call this when keyword matching returns None
    # Batch ambiguous titles in groups of 20 to minimize API calls
    pass
```

---

## Idempotency and Cost Controls

### Idempotency

Every write to Supabase uses `ON CONFLICT DO UPDATE`. The unique key for `prospects` is
`(linkedin_url)` or `(canonical_name, current_company_id)` if LinkedIn URL is missing. Re-running
the pipeline against a company that was already enriched will update stale data but not create
duplicates.

### Cost control checkpoints

Before running the PDL enrichment pass for any company, check:

```python
# Check PDL credits remaining before bulk enrichment
GET https://api.peopledatalabs.com/v5/system/metrics
# If credits < 500, pause and alert. Each PDL call costs 1 credit.

# Apollo rate limits
# Track calls per minute in a local counter; sleep if approaching 50/min

# Firecrawl: no per-call cost on standard plan but has page limits per month
# Log each Firecrawl call to enrichment_cost_log (Contract 8)
```

For companies with > 10k employees (Intel, Boeing, Lockheed), cap the Apollo pull at 400 persons
per company. You do not need every employee. The selection strategy:

```python
# Apollo pull strategy for large companies (> 10k employees):
# Priority 1: c_suite and vp seniority (always pull all, typically 30–80)
# Priority 2: director seniority — pull up to 150, distributed across functional domains
# Priority 3: manager seniority — pull up to 150, prioritizing technical + product + ops domains
# Priority 4: senior IC (Distinguished, Principal, Staff, Fellow) — pull up to 120
# Priority 5: senior individual contributors (Senior Engineer, Senior Researcher) — pull up to 80
# Total target: 500+ persons per company
# For very large companies (Intel 124k, Boeing 172k): cap at 1,000 to avoid runaway costs
```

### Monitoring

Write all enrichment activity to `enrichment_cost_log` (Contract 8 schema). After each company run,
print a summary:
```
NVIDIA enrichment complete:
  EDGAR executives:              12 persons
  Firecrawl leadership:           8 persons (3 new, 5 already in DB)
  Apify employee discovery:     843 persons pulled from LinkedIn company page
  Apollo pull:                  312 persons (fills gaps, adds verified emails)
  Merged + deduped:             521 unique persons
  Apify profile enrichment:     498/521 enriched (23 had no LinkedIn URL)
  PDL enrichment:                19/23 found by name (4 not in PDL)
  Apify job postings:           187 job postings → 34 reporting-line signals extracted
  Total new persons in DB:      517
  Total Apify spend:            ~$4.20 (discovery $1.00, profiles $2.49, jobs $0.63, buffer $0.08)
  Total Apollo credits used:    312
  Total PDL credits used:        19
  Elapsed: 8m 14s
```

---

## Quality Checks

Run after each company's enrichment pass before moving to the next:

```python
def validate_company_coverage(company_id: str) -> CoverageReport:
    persons = fetch_all_persons_at_company(company_id)

    checks = {
        # Minimum viable org chart
        "total_persons":          len(persons) >= 500,
        "has_c_suite":            any(p.seniority_score >= 85 for p in persons),
        "has_vp_tier":            len([p for p in persons if 65 <= p.seniority_score < 85]) >= 3,
        "has_director_tier":      len([p for p in persons if 55 <= p.seniority_score < 65]) >= 5,
        "has_manager_tier":       len([p for p in persons if 45 <= p.seniority_score < 55]) >= 10,
        "has_ic_track":           any(p.is_ic_track for p in persons),

        # Domain coverage (at least 4 of 9 domains represented)
        "domain_coverage":        len(set(p.functional_domain for p in persons)) >= 4,

        # Data completeness
        "seniority_coverage":     len([p for p in persons if p.seniority_score]) / len(persons) >= 0.90,
        "domain_coverage_pct":    len([p for p in persons if p.functional_domain]) / len(persons) >= 0.85,
        "employment_history":     len([p for p in persons if p.employment_periods]) / len(persons) >= 0.70,

        # Org chart pipeline readiness
        "ready_for_clustering":   passes domain_coverage and seniority_coverage,
    }

    return CoverageReport(company_id=company_id, checks=checks, persons_count=len(persons))
```

If `ready_for_clustering` is False, log which checks failed and run a targeted top-up pass before
calling the company done.

---

## Done Criteria

### Per company (before marking as enriched)

- [ ] ≥ 500 persons in `persons` table with `current_company_id` pointing to this company (or all available if company headcount < 500)
- [ ] All persons have `seniority_score` populated on `employment_periods.is_current = True` row
- [ ] ≥ 85% of persons have `functional_domain` populated
- [ ] ≥ 4 distinct functional domains represented
- [ ] At least 1 person at each of: C-suite tier, VP tier, Director tier, Manager tier
- [ ] At least 1 IC-track person (DE / PE / Staff) represented
- [ ] `enrichment_cost_log` has rows for every Apollo and PDL call made
- [ ] `validate_company_coverage()` returns `ready_for_clustering: True`

### Overall pipeline done

- [ ] All 5 Phase 1 companies (NVIDIA, Intel, AMD, Qualcomm, Graphcore) pass quality checks
- [ ] Total persons in Supabase ≥ 25,000 (20k existing + minimum 500 per company × ~10 Phase 1+2 companies fully enriched)
- [ ] `employment_periods` backfill complete — every existing prospect with `career_history` signal
      has their career_history JSONB materialized into `employment_periods` rows
- [ ] `education_periods` backfill complete — every PDL-enriched person has education records
- [ ] No raw API response stored in Postgres (all raw blobs in S3 per Decision 5)
- [ ] `python -m py_compile server/credence/enrichment/*.py` passes with zero errors

---

## Relationship to Existing Work

This task is a **prerequisite** for:

- **Org chart pipeline** (V3_PT2.md Plan A): clustering and hierarchy inference only produce real
  trees when ≥ 80 enriched persons exist per company. Without this enrichment, the org chart tables
  stay empty.
- **Education extractor** (V3_PT2.md Plan B): `education_periods` rows from PDL enrichment are the
  input. Education extractor finds cohort overlaps across those rows.
- **Career overlap warm paths** (DEMO_CASES.md, Cases 1-5): `employment_periods` rows are the input
  to the career overlap SQL. Richer history = more warm path candidates.

This task does not modify the frontend. It does not modify CONTRACTS.md. It populates data that
existing and planned pipelines consume.

---

## Common Mistakes to Avoid

1. **Do not pull all 124k Intel employees.** Pull strategically by seniority tier and domain.
   1,000 well-chosen people beats 10k random ICs with no functional domain context.

2. **Do not skip the EDGAR pass for public companies.** SEC proxy statements are free,
   authoritative, and give you the CEO/CFO/SVP layer before you spend a single Apollo credit.

3. **Run Apify profile enrichment before PDL.** Apify reads directly from LinkedIn profiles and
   returns complete, current employment and education data. PDL is a database with a cache lag of
   weeks to months. Use PDL only as the fallback for people without a LinkedIn URL. Apify covers
   ~85–90% of senior tech professionals; PDL covers ~60–70%. Don't pay for both on the same person.

4. **Do not assume PDL has everyone.** PDL coverage is ~60–70% for senior US professionals.
   If PDL misses someone, they still need to be in the DB (from Apollo or Apify) —
   just with a lower data completeness score.

5. **Do not create duplicate companies.** Run `normalizer.resolve_company()` before any insert.
   "Raytheon", "Raytheon Technologies", and "RTX" are all the same company. The canonical name
   is whatever is on the company's current SEC filing.

6. **Do not write `functional_domain = null`.** If keyword matching and LLM classification both
   fail, write `functional_domain = 'unknown'`. The clustering pipeline skips `null` rows entirely.
   `unknown` is queryable and can be fixed in a second pass.

7. **Do not run more than 3 Apify actors concurrently.** LinkedIn detection is the binding
   constraint. Stagger company runs by 30–60 seconds. Use residential proxies (`RESIDENTIAL`
   proxy group in Apify) — datacenter proxies get blocked by LinkedIn quickly.

8. **Do not enrich Phase 2 companies before Phase 1 passes quality checks.**
   The YC demo needs NVIDIA, Intel, AMD, Qualcomm, and Graphcore to have full org charts.
   Everything else is secondary.

---

*PROSPECT_ENRICHMENT_TASK.md — Credence v3, authored 2026-04-30*
*Read CLAUDE.md and V3_PT2.md before implementing. This task is a data pipeline task.*
*Output goes to Supabase. No frontend changes.*
