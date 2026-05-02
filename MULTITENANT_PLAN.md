# Credence — Multi-Tenancy & Onboarding Implementation Plan

> Read CLAUDE.md before touching any file listed here. This document is the
> authoritative implementation roadmap for making Credence a multi-tenant
> production product. Phases must be executed in order. Within each phase,
> tasks marked "parallel" can run as concurrent subagents.

---

## What We're Building

**Onboarding UX (zero friction):**
1. User lands on `/signup`
2. They type their name + work email (e.g. `sarah@nvidia.com`)
3. Backend extracts domain → `nvidia.com` → maps to company
4. Backend checks `company_scrape_status` table
5. If already scraped: user is taken straight to their graph
6. If not scraped: background job fires (Apollo fetch → enrichment pipeline → person_connections build)
7. Frontend shows a "Building your relationship graph…" progress screen with realtime updates via Supabase Realtime
8. When done: user lands on `/discover` with their org's relationship graph pre-loaded

**Multi-tenancy model:**
- Shared Supabase schema with `org_id` on every row
- Row Level Security enforces isolation — users can only read/write rows in their org
- Each paying customer = one `orgs` row tied to their email domain
- All graph data, prospects, signals, scores are scoped per org

---

## Phase 0 — Database Foundation (do this first, everything else depends on it)

### 0.1 — New tables (run as a single Supabase SQL migration)

```sql
-- ─── orgs ─────────────────────────────────────────────────────────────────
CREATE TABLE public.orgs (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name         TEXT NOT NULL,
  domain       TEXT NOT NULL UNIQUE,   -- extracted from signup email (e.g. "nvidia.com")
  plan         TEXT NOT NULL DEFAULT 'free',  -- 'free' | 'starter' | 'pro'
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── org_members ──────────────────────────────────────────────────────────
-- Links Supabase auth.users to orgs. One user, one org (for now).
CREATE TABLE public.org_members (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id     UUID NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
  user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  role       TEXT NOT NULL DEFAULT 'member',   -- 'admin' | 'member'
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id)   -- one org per user
);

-- ─── company_scrape_status ────────────────────────────────────────────────
-- Tracks whether we've already scraped a company domain.
-- Prevents double-scraping if two users from the same company sign up.
CREATE TABLE public.company_scrape_status (
  domain            TEXT PRIMARY KEY,
  org_id            UUID REFERENCES public.orgs(id),
  status            TEXT NOT NULL DEFAULT 'pending',
    -- 'pending' | 'in_progress' | 'complete' | 'failed'
  employee_count    INT,
  error_message     TEXT,
  started_at        TIMESTAMPTZ,
  completed_at      TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 0.2 — Add org_id to existing tables

```sql
-- Every table that holds org-specific data gets org_id.
-- Existing rows (the pre-loaded 20k prospects) get a special
-- 'system' org that is never shown to real customers.

-- First create the system org
INSERT INTO public.orgs (id, name, domain, plan)
VALUES (
  '00000000-0000-0000-0000-000000000099',
  'Credence System',
  'credence.internal',
  'system'
);

-- Add org_id columns
ALTER TABLE public.prospects
  ADD COLUMN org_id UUID REFERENCES public.orgs(id)
    DEFAULT '00000000-0000-0000-0000-000000000099';

ALTER TABLE public.signals
  ADD COLUMN org_id UUID REFERENCES public.orgs(id)
    DEFAULT '00000000-0000-0000-0000-000000000099';

ALTER TABLE public.scores
  ADD COLUMN org_id UUID REFERENCES public.orgs(id)
    DEFAULT '00000000-0000-0000-0000-000000000099';

ALTER TABLE public.scoring_runs
  ADD COLUMN org_id UUID REFERENCES public.orgs(id)
    DEFAULT '00000000-0000-0000-0000-000000000099';

ALTER TABLE public.signal_weights
  ADD COLUMN org_id UUID REFERENCES public.orgs(id)
    DEFAULT '00000000-0000-0000-0000-000000000099';

-- Index for every org_id column (critical for RLS performance)
CREATE INDEX ON public.prospects (org_id);
CREATE INDEX ON public.signals (org_id);
CREATE INDEX ON public.scores (org_id);
CREATE INDEX ON public.scoring_runs (org_id);
CREATE INDEX ON public.signal_weights (org_id);
```

### 0.3 — Row Level Security policies

```sql
-- Enable RLS on all tables
ALTER TABLE public.orgs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.org_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.prospects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scoring_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.signal_weights ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.company_scrape_status ENABLE ROW LEVEL SECURITY;

-- Helper function: resolve caller's org_id from auth.uid()
-- Called inside every RLS policy — Postgres inlines it, so one lookup per query.
CREATE OR REPLACE FUNCTION public.my_org_id()
RETURNS UUID
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
  SELECT org_id FROM public.org_members WHERE user_id = auth.uid() LIMIT 1;
$$;

-- orgs: members can read their own org
CREATE POLICY "read own org"
  ON public.orgs FOR SELECT
  USING (id = public.my_org_id());

-- org_members: members can read their org's member list
CREATE POLICY "read org members"
  ON public.org_members FOR SELECT
  USING (org_id = public.my_org_id());

-- prospects: read/write own org only
CREATE POLICY "read own prospects"
  ON public.prospects FOR SELECT
  USING (org_id = public.my_org_id());

CREATE POLICY "insert own prospects"
  ON public.prospects FOR INSERT
  WITH CHECK (org_id = public.my_org_id());

-- signals
CREATE POLICY "read own signals"
  ON public.signals FOR SELECT
  USING (org_id = public.my_org_id());

CREATE POLICY "insert own signals"
  ON public.signals FOR INSERT
  WITH CHECK (org_id = public.my_org_id());

-- scores
CREATE POLICY "read own scores"
  ON public.scores FOR SELECT
  USING (org_id = public.my_org_id());

CREATE POLICY "insert own scores"
  ON public.scores FOR INSERT
  WITH CHECK (org_id = public.my_org_id());

-- scoring_runs
CREATE POLICY "read own scoring_runs"
  ON public.scoring_runs FOR SELECT
  USING (org_id = public.my_org_id());

-- signal_weights: read own org (fallback to system org weights if none)
CREATE POLICY "read own signal_weights"
  ON public.signal_weights FOR SELECT
  USING (
    org_id = public.my_org_id()
    OR org_id = '00000000-0000-0000-0000-000000000099'
  );

-- company_scrape_status: any authenticated user can read (not org-scoped —
-- if NVIDIA is already scraped, a second NVIDIA user shouldn't re-trigger it)
CREATE POLICY "read scrape status"
  ON public.company_scrape_status FOR SELECT
  TO authenticated
  USING (true);

-- Backend service role bypasses RLS entirely — used only server-side.
-- Never expose the service role key to the frontend.
```

**Done when:** Migration runs without errors in Supabase SQL editor. All existing queries from `db.ts` still return data (they see the system org's prospects via the anon key — temporarily disable RLS on prospects for anon reads if needed during transition).

---

## Phase 1 — Auth Layer (depends on Phase 0)

### 1.1 — Enable Supabase Auth providers

In the Supabase dashboard (Settings → Auth):
- Enable Email/Password provider
- Enable Google OAuth (for later — not required for launch)
- Set "Confirm email" to OFF for faster onboarding (optional during early launch)
- Set redirect URL to your Vercel domain

### 1.2 — Update `src/lib/supabase.ts`

```typescript
// Add auth session export so all components can subscribe to auth state
export const supabase = HAS_REAL_SUPABASE
  ? createClient<Database>(supabaseUrl, supabaseAnonKey, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
      }
    })
  : null;
```

### 1.3 — Create `src/lib/auth.ts` (new file)

```typescript
import { supabase } from './supabase'
import type { User, Session } from '@supabase/supabase-js'

export interface OrgMember {
  org_id: string
  role: 'admin' | 'member'
}

export async function signUp(name: string, email: string, password: string) {
  const { data, error } = await supabase!.auth.signUp({
    email,
    password,
    options: { data: { full_name: name } }
  })
  if (error) throw error
  return data
}

export async function signIn(email: string, password: string) {
  const { data, error } = await supabase!.auth.signInWithPassword({ email, password })
  if (error) throw error
  return data
}

export async function signOut() {
  await supabase!.auth.signOut()
}

export async function getSession(): Promise<Session | null> {
  const { data } = await supabase!.auth.getSession()
  return data.session
}

export async function getMyOrg(): Promise<OrgMember | null> {
  const { data } = await supabase!
    .from('org_members')
    .select('org_id, role')
    .single()
  return data ?? null
}

// Reactive hook for auth state changes
export function onAuthStateChange(callback: (user: User | null) => void) {
  return supabase!.auth.onAuthStateChange((_event, session) => {
    callback(session?.user ?? null)
  })
}
```

### 1.4 — Create `src/store/authStore.ts` (new file)

```typescript
// Zustand-compatible auth store (same pattern as graphStore.ts)
// Holds current user + org. All components import from here.
// Shape:
//   user: User | null
//   orgId: string | null
//   orgRole: 'admin' | 'member' | null
//   status: 'loading' | 'authenticated' | 'unauthenticated' | 'onboarding'
//   setUser(user, orgMember) — called after sign-in
//   clear() — called after sign-out
```

### 1.5 — Create `src/components/AuthGuard.tsx` (new file)

```typescript
// Wraps routes that require auth. Redirects to /signup if unauthenticated.
// Redirects to /onboarding if authenticated but org not yet provisioned.
// Shows spinner while auth status is 'loading'.
```

### 1.6 — Update `src/App.tsx`

```typescript
// Add routes:
//   /signup       → SignupPage (public)
//   /signin       → SigninPage (public)
//   /onboarding   → OnboardingPage (auth required, no org required)
//   /             → AuthGuard → existing routes
//   /discover     → AuthGuard → Discover
//   /validate     → AuthGuard → Validate
//   /settings     → AuthGuard → Settings
```

### 1.7 — Backend: validate JWT on every request

```python
# server/credence/auth.py (new file)
# FastAPI dependency that extracts org_id from the Supabase JWT.
# All protected endpoints call: org_id = Depends(require_org)

from fastapi import Header, HTTPException, Depends
import jwt
import httpx

SUPABASE_JWT_SECRET = os.environ["SUPABASE_JWT_SECRET"]  # from Supabase dashboard

async def require_org(authorization: str = Header(...)) -> str:
    token = authorization.removeprefix("Bearer ")
    try:
        payload = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"])
        user_id = payload["sub"]
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    # Look up org from org_members using service-role client
    result = service_supabase.table("org_members") \
        .select("org_id") \
        .eq("user_id", user_id) \
        .single() \
        .execute()
    
    if not result.data:
        raise HTTPException(status_code=403, detail="No org found for user")
    
    return result.data["org_id"]
```

**Done when:** A test user can sign up, sign in, and sign out. Protected routes redirect to /signup when unauthenticated. The JWT dependency resolves org_id correctly.

---

## Phase 2 — Onboarding Flow (parallel: 2A frontend, 2B backend)

### Phase 2A — Frontend (parallel with 2B)

#### 2A.1 — Create `src/pages/SignupPage.tsx`

Single form: **Full name** + **Work email** + **Password**.

On submit:
1. Call `signUp(name, email, password)` from `auth.ts`
2. On success → call `POST /onboard` with `{ name, email }` (the backend provisions the org)
3. Redirect to `/onboarding` with the domain being scraped

Design: clean centered card, no navigation bar, Credence logo top-left. Minimal — this is not the product, it's the door.

#### 2A.2 — Create `src/pages/OnboardingPage.tsx`

The "building your graph" loading screen. Shows while the company scrape pipeline runs.

```
┌─────────────────────────────────────┐
│  🔍 Building your relationship graph │
│                                     │
│  We're mapping all the hidden        │
│  connections at [Company Name]...   │
│                                     │
│  ████████████░░░░░░  62%            │
│                                     │
│  ✓ Found 847 people at nvidia.com   │
│  ✓ Checking USPTO patents...        │
│  ◦ Building connection graph...     │
│                                     │
│  Usually takes 2-3 minutes.         │
└─────────────────────────────────────┘
```

Uses **Supabase Realtime** to subscribe to `company_scrape_status` table changes:
```typescript
supabase
  .channel('scrape-progress')
  .on('postgres_changes', {
    event: 'UPDATE',
    schema: 'public',
    table: 'company_scrape_status',
    filter: `domain=eq.${domain}`
  }, (payload) => {
    updateProgress(payload.new)
  })
  .subscribe()
```

When `status === 'complete'`: redirect to `/discover`.
When `status === 'failed'`: show error with retry button.

#### 2A.3 — Update `src/lib/db.ts`

All Supabase queries need to scope to org_id. Since RLS handles this server-side automatically (using the JWT → `my_org_id()` function), no query changes are needed. The anon key + user JWT combo is enough — Supabase will enforce RLS transparently.

**One change required:** update the `supabase` client init to always pass the auth session. This is handled by `persistSession: true` in Phase 1.2.

### Phase 2B — Backend (parallel with 2A)

#### 2B.1 — Create `server/credence/routes/onboard.py`

```python
@router.post("/onboard")
async def onboard(request: OnboardRequest):
    """
    Called immediately after Supabase auth.signUp() succeeds.
    
    1. Extract domain from email
    2. Create or retrieve org for that domain
    3. Link user to org via org_members
    4. Check company_scrape_status
    5. If not scraped: insert pending row + fire background task
    6. Return { org_id, domain, scrape_status }
    """
    domain = request.email.split("@")[1].lower()
    
    # Step 1: create or get org
    org = upsert_org(domain, derive_company_name(domain))
    
    # Step 2: link user to org
    link_user_to_org(request.user_id, org.id)
    
    # Step 3: check scrape status
    existing = get_scrape_status(domain)
    if existing and existing.status in ("in_progress", "complete"):
        return { "org_id": org.id, "domain": domain, "scrape_status": existing.status }
    
    # Step 4: insert pending + fire background task
    insert_scrape_status(domain, org.id, "in_progress")
    background_tasks.add_task(scrape_company, domain, org.id)
    
    return { "org_id": org.id, "domain": domain, "scrape_status": "in_progress" }


def derive_company_name(domain: str) -> str:
    """
    nvidia.com → Nvidia
    deepmind.google.com → DeepMind (strip common TLDs, capitalize)
    """
    base = domain.split(".")[0]
    return base.capitalize()
```

#### 2B.2 — BLOCKLISTED domains

Not every work email domain is a real company. Block personal email providers:

```python
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "protonmail.com", "aol.com", "live.com",
    "msn.com", "me.com", "mac.com"
}

def validate_work_email(email: str) -> str:
    domain = email.split("@")[1].lower()
    if domain in PERSONAL_EMAIL_DOMAINS:
        raise ValueError("Please use your work email address.")
    return domain
```

**Done when:** Hitting `POST /onboard` with `sarah@nvidia.com` creates an `orgs` row for nvidia.com, an `org_members` row linking the user, a `company_scrape_status` row with `status=in_progress`, and returns without error.

---

## Phase 3 — Company Scrape Pipeline (depends on Phase 2B)

This is the most important pipeline. It's what turns a work email into a populated relationship graph.

### Architecture

```
POST /onboard
  └─ background_task: scrape_company(domain, org_id)
       ├─ Step 1: Apollo → get all employees at domain
       ├─ Step 2: For each employee → upsert into persons table
       ├─ Step 3: Batch run PatentsView extractor (free)
       ├─ Step 4: Batch run Semantic Scholar extractor (free)  
       ├─ Step 5: Compute career overlaps via SQL
       ├─ Step 6: Write person_connections rows
       └─ Step 7: Update company_scrape_status → 'complete'
```

Each step updates `company_scrape_status.status` with a progress message. The frontend Realtime subscription picks this up and updates the progress bar.

### 3.1 — Create `server/credence/pipeline/scrape_company.py`

```python
async def scrape_company(domain: str, org_id: str) -> None:
    try:
        await update_scrape_status(domain, "in_progress", "Fetching employees from Apollo...")
        
        # Step 1: Get all people at this domain from Apollo
        employees = await apollo_get_company_employees(domain)
        await update_scrape_status(domain, "in_progress", 
            f"Found {len(employees)} people. Enriching profiles...")
        
        # Step 2: Upsert into persons (with org_id)
        person_ids = []
        for emp in employees:
            pid = await upsert_person(emp, org_id)
            person_ids.append(pid)
        
        await update_scrape_status(domain, "in_progress",
            f"Checking USPTO patents for {len(person_ids)} people...")
        
        # Step 3: Patent co-inventions (PatentsView — free, no rate limit pressure)
        patent_tasks = [find_patent_connections(pid, org_id) for pid in person_ids]
        await asyncio.gather(*patent_tasks, return_exceptions=True)
        
        await update_scrape_status(domain, "in_progress",
            "Checking Semantic Scholar for academic co-authorships...")
        
        # Step 4: Academic co-authorships (Semantic Scholar — free, 1 req/sec)
        # Use semaphore to respect rate limit
        sem = asyncio.Semaphore(1)
        scholar_tasks = [find_scholar_connections(pid, org_id, sem) for pid in person_ids]
        await asyncio.gather(*scholar_tasks, return_exceptions=True)
        
        await update_scrape_status(domain, "in_progress",
            "Computing career overlaps...")
        
        # Step 5: Career overlaps — pure SQL, no external API
        await compute_career_overlaps(org_id)
        
        await update_scrape_status(domain, "complete",
            f"Done. Found connections across {len(person_ids)} people.",
            employee_count=len(employees))
    
    except Exception as e:
        await update_scrape_status(domain, "failed", str(e))
        raise
```

### 3.2 — Create `server/credence/pipeline/apollo_employees.py`

Uses the existing Apollo enrichment infrastructure in `server/credence/enrichment/apollo.py`:

```python
async def apollo_get_company_employees(domain: str) -> list[PersonRecord]:
    """
    Apollo's people/search endpoint: filter by organization_domains.
    Returns up to 10k people (paginated, 100/page, ~100 Apollo credits).
    Fields: name, title, linkedin_url, email (if available).
    """
    url = "https://api.apollo.io/v1/mixed_people/search"
    all_people = []
    page = 1
    
    while True:
        resp = await httpx.AsyncClient().post(url, json={
            "api_key": APOLLO_API_KEY,
            "organization_domains": [domain],
            "per_page": 100,
            "page": page,
            "person_seniorities": ["director", "vp", "c_suite", "partner", "manager", "senior"],
        })
        data = resp.json()
        people = data.get("people", [])
        if not people:
            break
        all_people.extend(people)
        if len(all_people) >= data.get("pagination", {}).get("total_entries", 0):
            break
        page += 1
    
    return [parse_apollo_person(p) for p in all_people]
```

**Apollo credit cost note:** 1 credit per person returned. A 500-person company costs ~500 credits. Free tier has 50 credits/month. For launch, budget ~$50/mo on Starter ($49/mo, 1200 credits = ~12 companies/month).

### 3.3 — Update `company_scrape_status` for Realtime

Add a `progress_message` column:

```sql
ALTER TABLE public.company_scrape_status
  ADD COLUMN progress_message TEXT,
  ADD COLUMN progress_pct INT DEFAULT 0;
```

Update the pipeline to write `progress_pct` at each step (0 → 20 → 40 → 60 → 80 → 100).

**Done when:** Signing up with a real work email triggers the pipeline, the OnboardingPage shows progress updating in realtime, and after ~2 minutes the user lands on `/discover` with a populated graph.

---

## Phase 4 — Graph Store Multi-Tenancy (depends on Phase 1)

The graph store and all data fetches need to pass the auth session. Since RLS handles org isolation at the database level, the main changes are:

### 4.1 — Update `src/lib/db.ts`

The Supabase client already includes the auth session (from `persistSession: true`). RLS policies automatically scope results to `my_org_id()`. No query-level `org_id` filters needed — Supabase handles it.

**One addition:** the `useProspects` hook currently fetches all prospects. With RLS active, it will automatically return only the authenticated user's org's prospects. Verify this in testing.

### 4.2 — Update `src/lib/supabase.ts`

Export a helper for components that need to check if the user is authenticated before rendering:

```typescript
export async function requireAuth() {
  const { data: { session } } = await supabase!.auth.getSession()
  if (!session) throw new Error('Not authenticated')
  return session
}
```

### 4.3 — Update `src/pages/Discover.tsx`

Add `AuthGuard` wrapper. The graph will now load only the authenticated org's prospects automatically (RLS handles it).

---

## Phase 5 — Target Accounts (post-launch, but plan it now)

One thing the onboarding plan above doesn't cover: **the other side of the graph**.

When a customer signs up with `sarah@acme.com`, we scrape Acme's team. But Credence's value is showing connections *between* Acme's team and their *target accounts* (NVIDIA, Google, whoever they're trying to sell to).

Post-launch addition to the onboarding flow:

After the graph loads, show a step: **"Who are you trying to sell to?"**
- Text field: "Enter domains of target accounts (one per line)"
- e.g. `nvidia.com`, `google.com`, `amd.com`

For each target domain: run the same scrape pipeline but mark persons as `prospect_type = 'target'` vs `'team'`. The warm path BFS then finds connections between `'team'` nodes and `'target'` nodes.

This is the actual product. Ship Phase 0-4 first, then add this in the first week post-launch.

---

## Subagent Task Decomposition

Run these in parallel once dependencies are met:

```
Phase 0 (sequential — 1 subagent):
  SA-1: Run Supabase migrations (tables + RLS + indexes)
        Files: SQL run in Supabase dashboard; no code files touched

Phase 1 (after Phase 0 — 2 parallel subagents):
  SA-2A: Frontend auth (supabase.ts, auth.ts, authStore.ts, AuthGuard.tsx, App.tsx)
  SA-2B: Backend auth (server/credence/auth.py, update api.py to use require_org)

Phase 2 (after Phase 1 — 2 parallel subagents):
  SA-3A: SignupPage.tsx + OnboardingPage.tsx (Realtime subscription)
  SA-3B: server/credence/routes/onboard.py (POST /onboard endpoint)

Phase 3 (after Phase 2B — 3 parallel subagents):
  SA-4A: pipeline/scrape_company.py (orchestrator)
  SA-4B: pipeline/apollo_employees.py (Apollo fetch)
  SA-4C: SQL career overlap computation (pure SQL, no external API)

Phase 4 (after Phase 1 — 1 subagent):
  SA-5: db.ts RLS verification + AuthGuard wrapping existing pages
```

---

## Done Criteria (full checklist)

- [ ] `orgs`, `org_members`, `company_scrape_status` tables exist in Supabase
- [ ] All existing tables have `org_id` column with index
- [ ] RLS enabled on all tables; `my_org_id()` function exists
- [ ] User can sign up with work email → org provisioned automatically
- [ ] Personal email domains (gmail, yahoo, etc.) are blocked with clear error
- [ ] POST /onboard creates org + links user + fires background scrape
- [ ] OnboardingPage shows realtime progress (Supabase Realtime subscription)
- [ ] After scrape completes: user lands on /discover with populated graph
- [ ] Graph only shows the authenticated org's data (RLS verified with two separate test orgs)
- [ ] Sign out clears session; protected routes redirect to /signup
- [ ] Existing demo mode (?demo=true) still works without auth (bypass AuthGuard for demo)
- [ ] TypeScript: `tsc --noEmit` passes
- [ ] Python: `python -m py_compile server/credence/routes/onboard.py` passes

---

## Files Created / Modified

### New files:
- `src/lib/auth.ts`
- `src/store/authStore.ts`
- `src/components/AuthGuard.tsx`
- `src/pages/SignupPage.tsx`
- `src/pages/SigninPage.tsx`
- `src/pages/OnboardingPage.tsx`
- `server/credence/auth.py`
- `server/credence/routes/onboard.py`
- `server/credence/pipeline/__init__.py`
- `server/credence/pipeline/scrape_company.py`
- `server/credence/pipeline/apollo_employees.py`
- `supabase/migrations/001_multitenant.sql`

### Modified files:
- `src/lib/supabase.ts` — add auth session persistence
- `src/lib/db.ts` — verify RLS compatibility (minimal changes)
- `src/App.tsx` — add auth routes + AuthGuard
- `server/credence/api.py` — register onboard router
- `server/credence/config.py` — add SUPABASE_JWT_SECRET, APOLLO_API_KEY

---

## Environment Variables to Add

```
# Backend — add to .env.local
SUPABASE_JWT_SECRET=<from Supabase dashboard Settings → API → JWT Secret>
SUPABASE_SERVICE_ROLE_KEY=<from Supabase dashboard Settings → API>
```

The service role key bypasses RLS and is used only server-side for the scrape pipeline to write across orgs. Never expose it to the frontend. The frontend only ever uses the anon key + user JWT.

---

*MULTITENANT_PLAN.md — Credence*
*This plan is the source of truth for multi-tenancy implementation.*
*Update CONTRACTS.md when new interfaces are defined during implementation.*
