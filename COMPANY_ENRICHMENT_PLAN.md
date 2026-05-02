# Company Enrichment Plan

> **Author:** LavenderPrairie  
> **Date:** 2026-05-01  
> **Status:** APPROVED — ready to implement  
> **Estimated effort:** ~18 hr total  
> **Goal:** Give the chat agent real company context so it can answer questions like "Who leads TSMC's memory division?" or "What did NVIDIA announce recently?" instead of returning a bare headcount number.

---

## Problem Statement

The GraphChat agent currently fails silently when asked about companies. The `explain` tool dispatches to `explain_prospect(UUID(args["id"]))`, which only handles person UUIDs. When it receives a company handle like `co:nvidia`, it returns nothing useful. The underlying cause is structural: the `companies` table is nearly empty (canonical name, domains, industry, rough headcount — that's it), and the backend has zero access to the rich company descriptions that live in `src/lib/company-meta.generated.ts` (a frontend-only build artifact).

The fix is a company signals table + a chain of enrichment jobs that populate it + an `explain_company()` function in `search.py` that bundles the enriched data + a dispatch patch in `chat.py` that routes company node clicks to that function.

---

## Parallel.ai vs Direct Scraping — Decision

### What Parallel.ai is built for

Parallel is a task-based async web research API (submit → poll, 10–60 seconds per task). The existing usage in `parallel_conference.py` and `parallel_standards.py` illustrates the pattern exactly: "given these two people, search the open web and tell me if they appeared at the same conference or served on the same standards committee together." That's an open-ended pairwise lookup where the query is natural language, the relevant URLs are unknown in advance, and the answer may require visiting 5–15 pages to assemble.

### Why Parallel is the wrong tool for company enrichment

Company enrichment is not open-ended — the URLs are known in advance:

- `/about`, `/leadership`, `/press` on the company's own domain
- Crunchbase, LinkedIn company page, SEC EDGAR, Wikipedia for firmographics

Parallel would cost 1 task-run per company per enrichment field, at 10–60 seconds latency, billed per task. For 170 companies × 4 enrichment targets = 680 task runs at ~$0.05–0.15 each → $34–$102 per full backfill, with no structured extraction guarantee.

**The existing `company_site.py` already solves this.** It uses Firecrawl's structured LLM extraction (cost: ~$0.03/page) against known URLs and returns typed Python dataclasses (`CompanyExecutive`, `PressRelease`, etc.). This is precisely the right tool: known URLs, structured output, cheap per-page cost, synchronous-friendly.

### When Parallel IS appropriate for company enrichment

One use case: "find recent executive hires or departures at [company]" where the answer lives across press releases, LinkedIn announcements, and news articles on URLs we don't know. In that case, Parallel's open-ended research model wins. That's a future "executive change detection" feature, not part of this plan.

### Decision

| Task | Tool | Reason |
|---|---|---|
| /leadership scrape | Firecrawl (`company_site.py`) | Known URL, structured extraction |
| /press scrape | Firecrawl (`company_site.py`) | Known URL, structured extraction |
| Firmographic data | httpx + Clearbit/Wikipedia | Structured API or HTML, cheap |
| Open-ended exec change detection | Parallel.ai | Future feature, not this plan |

---

## Implementation Plan

### Step 1 — Schema: `company_signals` table + `companies` additions

**File:** `supabase/migrations/20260501_company_enrichment.sql`

```sql
-- Extend companies table with enrichment metadata
ALTER TABLE companies
  ADD COLUMN IF NOT EXISTS enrichment_status  TEXT    DEFAULT 'pending'
                                              CHECK (enrichment_status IN ('pending','running','done','error')),
  ADD COLUMN IF NOT EXISTS enrichment_last_run TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS description        TEXT,
  ADD COLUMN IF NOT EXISTS hq_city            TEXT,
  ADD COLUMN IF NOT EXISTS hq_state           TEXT,
  ADD COLUMN IF NOT EXISTS hq_country         TEXT,
  ADD COLUMN IF NOT EXISTS employee_count_estimate INT,
  ADD COLUMN IF NOT EXISTS founded_year       INT,
  ADD COLUMN IF NOT EXISTS industry_tags      TEXT[],
  ADD COLUMN IF NOT EXISTS partnerships       TEXT[];

-- Company signals table (mirrors signals table pattern, but keyed to companies)
CREATE TABLE IF NOT EXISTS company_signals (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id       UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  signal_type      TEXT NOT NULL,         -- 'executive_profile', 'press_release', 'firmographic', 'product_line'
  source           TEXT NOT NULL,         -- 'firecrawl_leadership', 'firecrawl_press', 'clearbit', 'wikipedia'
  structured_value JSONB NOT NULL,        -- cap at 4KB; raw responses go to S3
  confidence       NUMERIC(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  raw_data_uri     TEXT,                  -- S3 URI for full response blob
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_until      TIMESTAMPTZ           -- null = no expiry
);

CREATE INDEX company_signals_company_id_idx ON company_signals (company_id);
CREATE INDEX company_signals_signal_type_idx ON company_signals (signal_type);

-- RLS: same tenant isolation as signals table
ALTER TABLE company_signals ENABLE ROW LEVEL SECURITY;

CREATE POLICY company_signals_tenant_read ON company_signals
  FOR SELECT USING (
    EXISTS (
      SELECT 1 FROM companies c
      JOIN account_companies ac ON ac.company_id = c.id
      JOIN account_users au ON au.account_id = ac.account_id
      WHERE c.id = company_signals.company_id
        AND au.user_id = auth.uid()
    )
  );
```

**Effort:** 1 hr

---

### Step 2 — Backfill seed job: `scripts/seed_company_meta.py`

This job one-time-migrates the data already in `company-meta.generated.ts` into the `companies` table rows so the backend immediately has the static descriptions, HQ city, industry, and partnerships for ~170 companies.

```python
#!/usr/bin/env python3
"""
Seed company enrichment columns from the static TypeScript build artifact.

Usage:
    python scripts/seed_company_meta.py --dry-run
    python scripts/seed_company_meta.py
"""

import json
import re
import os
import sys
import asyncio
from supabase import create_client

# Parse the TS file with a regex — brittle but sufficient for a one-time seed
TS_PATH = "src/lib/company-meta.generated.ts"
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

def parse_company_meta(ts_path: str) -> list[dict]:
    """Extract the JS object literal from the TS file."""
    text = open(ts_path).read()
    # Strip TS type annotations, then eval as JSON (crude but correct for this shape)
    # The file is machine-generated and has a known safe structure
    match = re.search(r"export const COMPANY_META[^=]+=\s*(\{[\s\S]+?\}) as const", text)
    if not match:
        raise ValueError("Could not find COMPANY_META export in TS file")
    blob = match.group(1)
    # Convert TS object to valid JSON (keys are already quoted in this generated file)
    blob = re.sub(r",\s*\}", "}", blob)   # trailing commas
    blob = re.sub(r",\s*\]", "]", blob)
    return json.loads(blob)

async def seed(dry_run: bool = False):
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    meta = parse_company_meta(TS_PATH)
    updated = 0
    for canonical_name, data in meta.items():
        row = {
            "description":             data.get("description"),
            "hq_city":                 data.get("hq_city"),
            "hq_country":              data.get("country"),
            "hq_state":                data.get("state"),
            "industry_tags":           [data["industry"]] if data.get("industry") else [],
            "employee_count_estimate": data.get("employee_count_estimate"),
            "partnerships":            data.get("partnerships", []),
            "enrichment_status":       "done",   # static data counts as done
        }
        if not dry_run:
            supabase.table("companies").update(row)\
                .eq("canonical_name", canonical_name)\
                .execute()
        else:
            print(f"[DRY RUN] Would update: {canonical_name}")
            print(json.dumps(row, indent=2)[:300])
        updated += 1
    print(f"{'[DRY RUN] ' if dry_run else ''}Seeded {updated} companies.")

if __name__ == "__main__":
    asyncio.run(seed(dry_run="--dry-run" in sys.argv))
```

**Effort:** 1.5 hr

---

### Step 3 — Bulk enrichment job: `server/credence/enrichment/bulk_company_enrichment.py`

This job calls the existing `company_site.py` Firecrawl scraper for each company and writes results to `company_signals`. It respects `enrichment_status` so it is safe to re-run.

```python
"""
Bulk company enrichment job. Runs on-demand or via cron.

For each company with enrichment_status != 'done':
  1. Scrape /leadership and /press via company_site.py (Firecrawl)
  2. Write CompanyExecutive rows as signal_type='executive_profile'
  3. Write PressRelease rows as signal_type='press_release'
  4. Mark company enrichment_status='done'

Cost: ~$0.03/page × 2 pages × 170 companies = ~$10.20 for full backfill.
Rate: 10 concurrent companies (Firecrawl default concurrency limit).
"""

import asyncio
import os
from datetime import datetime, UTC
from typing import Any
from supabase import create_client
from credence.enrichment.company_site import scrape_company_site

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
CONCURRENCY = 10

async def enrich_one(supabase, company: dict) -> None:
    company_id = company["id"]
    domain = (company.get("domains") or [None])[0]
    if not domain:
        await _mark(supabase, company_id, "error")
        return

    await _mark(supabase, company_id, "running")
    try:
        result = await scrape_company_site(domain)   # returns CompanySiteData dataclass
    except Exception as exc:
        print(f"[ERROR] {domain}: {exc}")
        await _mark(supabase, company_id, "error")
        return

    signals: list[dict[str, Any]] = []

    for exec_ in (result.executives or []):
        signals.append({
            "company_id":       company_id,
            "signal_type":      "executive_profile",
            "source":           "firecrawl_leadership",
            "structured_value": {
                "name":         exec_.name,
                "title":        exec_.title,
                "bio_snippet":  exec_.bio_snippet,
                "linkedin_url": exec_.linkedin_url,
            },
            "confidence":       0.85,
        })

    for pr in (result.press_releases or []):
        signals.append({
            "company_id":       company_id,
            "signal_type":      "press_release",
            "source":           "firecrawl_press",
            "structured_value": {
                "headline":          pr.headline,
                "date":              pr.date,
                "url":               pr.url,
                "summary":           pr.summary,
                "reporting_phrases": pr.reporting_phrases,
            },
            "confidence":       0.90,
        })

    if signals:
        supabase.table("company_signals").insert(signals).execute()

    await _mark(supabase, company_id, "done")
    print(f"[OK] {domain}: {len(signals)} signals written")

async def _mark(supabase, company_id: str, status: str) -> None:
    supabase.table("companies").update({
        "enrichment_status":   status,
        "enrichment_last_run": datetime.now(UTC).isoformat(),
    }).eq("id", company_id).execute()

async def run_bulk(limit: int = 500) -> None:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    result = supabase.table("companies")\
        .select("id, canonical_name, domains")\
        .in_("enrichment_status", ["pending", "error"])\
        .limit(limit)\
        .execute()
    companies = result.data
    print(f"Enriching {len(companies)} companies (concurrency={CONCURRENCY})")

    sem = asyncio.Semaphore(CONCURRENCY)
    async def guarded(c):
        async with sem:
            await enrich_one(supabase, c)

    await asyncio.gather(*[guarded(c) for c in companies])

if __name__ == "__main__":
    asyncio.run(run_bulk())
```

**Effort:** 3 hr

---

### Step 4 — `explain_company()` in `server/credence/search.py`

This function builds the same rich bundle that `explain_prospect()` builds for people, but for companies. The chat agent calls this when the selected node is a company.

```python
async def explain_company(company_id_or_handle: str) -> dict:
    """
    Return a rich context bundle for a company node.
    Handles both UUID strings and 'co:nvidia' handles.
    """
    supabase = get_supabase()

    # Resolve handle → UUID if needed
    if company_id_or_handle.startswith("co:"):
        slug = company_id_or_handle[3:]
        row = supabase.table("companies")\
            .select("*")\
            .ilike("canonical_name", slug.replace("-", " "))\
            .single()\
            .execute()
    else:
        row = supabase.table("companies")\
            .select("*")\
            .eq("id", company_id_or_handle)\
            .single()\
            .execute()

    if not row.data:
        return {"error": f"Company not found: {company_id_or_handle}"}

    company = row.data
    company_id = company["id"]

    # Load enriched signals
    signals_result = supabase.table("company_signals")\
        .select("signal_type, structured_value, confidence, fetched_at")\
        .eq("company_id", company_id)\
        .order("fetched_at", desc=True)\
        .limit(50)\
        .execute()

    executives = [
        s["structured_value"]
        for s in signals_result.data
        if s["signal_type"] == "executive_profile"
    ]
    press = [
        s["structured_value"]
        for s in signals_result.data
        if s["signal_type"] == "press_release"
    ][:10]  # cap at 10 most recent

    # Load org chart summary
    org_result = supabase.table("org_reporting_edges")\
        .select("id")\
        .eq("company_id", company_id)\
        .eq("is_current", True)\
        .execute()
    org_edge_count = len(org_result.data)

    # Load prospect coverage
    prospect_result = supabase.table("persons")\
        .select("id")\
        .eq("current_company_id", company_id)\
        .execute()
    prospect_count = len(prospect_result.data)

    return {
        "company": {
            "id":                      company_id,
            "canonical_name":          company.get("canonical_name"),
            "description":             company.get("description"),
            "industry":                company.get("industry"),
            "industry_tags":           company.get("industry_tags", []),
            "hq_city":                 company.get("hq_city"),
            "hq_country":              company.get("hq_country"),
            "employee_count_estimate": company.get("employee_count_estimate"),
            "partnerships":            company.get("partnerships", []),
            "founded_year":            company.get("founded_year"),
        },
        "executives":      executives,
        "recent_press":    press,
        "org_chart": {
            "edge_count":  org_edge_count,
            "confidence":  company.get("org_chart_confidence"),
            "last_built":  company.get("org_chart_last_built"),
        },
        "prospect_count": prospect_count,
        "enrichment_status": company.get("enrichment_status"),
    }
```

**Effort:** 2 hr

---

### Step 5 — Chat dispatch patch: `server/credence/chat.py`

The `explain` tool currently calls `explain_prospect(UUID(args["id"]))` unconditionally. This crashes on company handles. The fix is a one-line routing check:

```python
# In chat.py — inside the explain tool handler block

node_id = args["id"]

if node_id.startswith("co:") or _is_company_uuid(node_id):
    result = await explain_company(node_id)
else:
    result = await explain_prospect(UUID(node_id))

tool_results.append({
    "type": "tool_result",
    "tool_use_id": tool_use.id,
    "content": json.dumps(result),
})
```

```python
def _is_company_uuid(node_id: str) -> bool:
    """Return True if this UUID maps to a company rather than a person."""
    # Simple heuristic: check the companies table. Cache in a module-level set
    # populated lazily on first call.
    return node_id in _company_uuid_cache()
```

The cache population can be a simple `SELECT id FROM companies` executed once at startup and stored in a module-level `frozenset`. This avoids a per-request DB round-trip while staying correct.

**Effort:** 1.5 hr

---

### Step 6 — Scheduled refresh

Companies change. Press releases go stale after 30 days; executive pages after 90 days. The refresh job re-enriches any company whose `enrichment_last_run` is older than the threshold.

**File:** `server/credence/enrichment/refresh_company_enrichment.py`

```python
"""
Refresh company enrichment on a schedule.
Re-enriches companies whose signals are stale.

Cron suggestion: daily at 03:00 UTC
  POST /admin/refresh-company-enrichment (authenticated)
  or: python -m credence.enrichment.refresh_company_enrichment
"""

import asyncio
from datetime import datetime, timedelta, UTC
from supabase import create_client
import os
from credence.enrichment.bulk_company_enrichment import enrich_one

PRESS_STALENESS_DAYS    = 30
EXEC_STALENESS_DAYS     = 90

async def run_refresh():
    supabase = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )
    cutoff = (datetime.now(UTC) - timedelta(days=PRESS_STALENESS_DAYS)).isoformat()
    result = supabase.table("companies")\
        .select("id, canonical_name, domains")\
        .eq("enrichment_status", "done")\
        .lt("enrichment_last_run", cutoff)\
        .execute()

    print(f"Refreshing {len(result.data)} stale companies")
    # Reset to pending so bulk job picks them up
    ids = [c["id"] for c in result.data]
    if ids:
        supabase.table("companies")\
            .update({"enrichment_status": "pending"})\
            .in_("id", ids)\
            .execute()
        # Then run bulk immediately
        from credence.enrichment.bulk_company_enrichment import run_bulk
        await run_bulk(limit=len(ids) + 10)

if __name__ == "__main__":
    asyncio.run(run_refresh())
```

Wire into the FastAPI app as a background task endpoint:

```python
# In server/routes/admin.py
@router.post("/admin/refresh-company-enrichment")
async def trigger_company_refresh(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_refresh)
    return {"status": "queued"}
```

**Effort:** 1.5 hr

---

## Cost Model

| Item | Unit cost | Volume | Total |
|---|---|---|---|
| Firecrawl /leadership scrape | $0.03/page | 170 companies | $5.10 |
| Firecrawl /press scrape | $0.03/page | 170 companies | $5.10 |
| Monthly refresh (30-day cadence) | $0.03/page × 2 | 170 companies | $10.20/mo |
| Parallel.ai (not used) | — | — | $0.00 |
| **Backfill total** | | | **$10.20** |
| **Monthly ongoing** | | | **$10.20/mo** |

This is well within demo budget. At 1,000 companies the monthly cost would be ~$60/mo — still negligible.

---

## File Map

```
server/
└── credence/
    └── enrichment/
        ├── company_site.py              (EXISTING — no changes needed)
        ├── bulk_company_enrichment.py   (NEW — Step 3)
        └── refresh_company_enrichment.py (NEW — Step 6)

server/credence/
├── search.py                           (MODIFY — add explain_company(), Step 4)
└── chat.py                             (MODIFY — routing patch, Step 5)

server/routes/
└── admin.py                            (MODIFY or CREATE — refresh endpoint, Step 6)

supabase/migrations/
└── 20260501_company_enrichment.sql     (NEW — Step 1)

scripts/
└── seed_company_meta.py                (NEW — Step 2)
```

---

## Implementation Order

Steps 1 and 2 are independent and can be done in parallel. Steps 3–5 depend on Step 1 (schema must exist). Step 6 depends on Step 3.

```
[Step 1: schema migration]  ──┐
[Step 2: seed backfill]       │
                              ▼
                        [Step 3: bulk enrichment job]
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
             [Step 4: explain_company]  [Step 5: chat dispatch]
                    │
                    ▼
             [Step 6: scheduled refresh]
```

---

## Definition of Done

- [ ] `company_signals` table exists and has RLS policies applied
- [ ] `companies` table has `description`, `hq_city`, `enrichment_status` columns
- [ ] `seed_company_meta.py` dry-run shows 170 companies with valid descriptions
- [ ] `bulk_company_enrichment.py` writes at least 1 `executive_profile` and 1 `press_release` signal for 5 test companies
- [ ] `explain_company("co:nvidia")` returns a bundle with company metadata, executives array, and recent_press array
- [ ] `chat.py` test: clicking a company node in the graph triggers `explain_company`, not a UUID-parse crash
- [ ] `refresh_company_enrichment.py` runs without error and resets stale rows to `pending`
- [ ] `POST /admin/refresh-company-enrichment` returns `{"status": "queued"}` and runs the refresh in background

---

*COMPANY_ENRICHMENT_PLAN.md — Credence v3*  
*Owner: LavenderPrairie*
