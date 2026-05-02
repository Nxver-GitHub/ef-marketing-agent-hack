-- 2026-05-02: Company enrichment surface — COMPANY_ENRICHMENT_PLAN.md Step 1.
--
-- ## Why
--
-- The chat agent currently fails on company nodes because `companies` carries
-- only canonical_name + a few firmographic columns and `explain_prospect`
-- doesn't handle non-person UUIDs. This migration lays the data surface for:
--
--   1. Static metadata (description, HQ, employee count, partnerships) seeded
--      from the frontend `company-meta.generated.ts` build artifact.
--   2. Bulk-enriched signals (executive_profile, press_release) written by
--      `bulk_company_enrichment.py` via Firecrawl.
--   3. Refresh tracking so a cron job knows what's stale.
--
-- ## What changes
--
-- 1. `companies` gets enrichment metadata columns + a status enum tracked via
--    a CHECK constraint (`pending|running|done|error`).
-- 2. New `company_signals` table mirrors the existing `signals(prospect_id)`
--    pattern but keys to companies. Cap structured_value at 4KB at the
--    application layer (raw blobs go to S3 per Decision 5 in CLAUDE.md).
-- 3. RLS policy: any caller with an `account_companies` membership can read.
--    Same model as the prospect-side policies — the writes are service-role
--    only, the reads are tenant-scoped.
--
-- ## Idempotency
--
-- Every ALTER and CREATE uses `IF NOT EXISTS`. The CHECK constraint is
-- guarded by a `pg_constraint` lookup. The RLS policy is guarded by a
-- `pg_policies` lookup. Re-runs are no-ops once the surface is in place.

BEGIN;

-- ── 1. Extend companies table ───────────────────────────────────────────────
ALTER TABLE public.companies
  ADD COLUMN IF NOT EXISTS enrichment_status      TEXT DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS enrichment_last_run    TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS description            TEXT,
  ADD COLUMN IF NOT EXISTS hq_city                TEXT,
  ADD COLUMN IF NOT EXISTS hq_state               TEXT,
  ADD COLUMN IF NOT EXISTS founded_year           INT,
  ADD COLUMN IF NOT EXISTS industry_tags          TEXT[],
  ADD COLUMN IF NOT EXISTS partnerships           TEXT[];

-- The CHECK constraint enforces the enrichment_status keyspace at the DB
-- layer so a buggy writer can't land an invalid status. Guarded so re-runs
-- of the migration don't trip pg_constraint duplication.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'companies_enrichment_status_keyspace'
  ) THEN
    ALTER TABLE public.companies
      ADD CONSTRAINT companies_enrichment_status_keyspace
      CHECK (
        enrichment_status IS NULL
        OR enrichment_status IN ('pending', 'running', 'done', 'error')
      );
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_companies_enrichment_status
  ON public.companies (enrichment_status)
  WHERE enrichment_status IS NOT NULL;

-- Index on enrichment_last_run lets the refresh job efficiently find stale
-- companies (`WHERE enrichment_status='done' AND enrichment_last_run < cutoff`).
CREATE INDEX IF NOT EXISTS idx_companies_enrichment_last_run
  ON public.companies (enrichment_last_run)
  WHERE enrichment_last_run IS NOT NULL;

-- ── 2. company_signals table ────────────────────────────────────────────────
-- Per-signal-type rows attached to a company, mirroring the
-- `signals(prospect_id, signal_type, structured_value, ...)` shape we use
-- for prospect signals. structured_value caps at 4KB per Decision 5
-- (raw API responses go to S3 at raw_data_uri).
--
-- account_id is denormalized from the parent company so RLS policies can
-- gate on it directly without a JOIN — same pattern as `signals` and
-- `org_chart_corrections`. Writers must populate it from the company row;
-- the FK to companies guarantees referential integrity, the explicit column
-- keeps the read-side policy a single index hit.
CREATE TABLE IF NOT EXISTS public.company_signals (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id       UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  company_id       UUID NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  signal_type      TEXT NOT NULL,
  source           TEXT NOT NULL,
  structured_value JSONB NOT NULL,
  confidence       NUMERIC(4,3) NOT NULL,
  raw_data_uri     TEXT,
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_until      TIMESTAMPTZ,

  CONSTRAINT company_signals_confidence_range CHECK (confidence BETWEEN 0 AND 1),
  CONSTRAINT company_signals_signal_type_keyspace
    CHECK (signal_type IN (
      'executive_profile', 'press_release', 'firmographic', 'product_line',
      'partnership', 'funding_round', 'product_launch'
    )),
  CONSTRAINT company_signals_source_keyspace
    CHECK (source IN (
      'firecrawl_leadership', 'firecrawl_press', 'firecrawl_about',
      'clearbit', 'wikipedia', 'crunchbase', 'sec_edgar', 'manual'
    ))
);

CREATE INDEX IF NOT EXISTS idx_company_signals_company_id
  ON public.company_signals (company_id);
CREATE INDEX IF NOT EXISTS idx_company_signals_account_id
  ON public.company_signals (account_id);
CREATE INDEX IF NOT EXISTS idx_company_signals_signal_type
  ON public.company_signals (signal_type);
CREATE INDEX IF NOT EXISTS idx_company_signals_fetched_at
  ON public.company_signals (fetched_at DESC);

-- ── 3. RLS — tenant-isolated reads, service-role writes ─────────────────────
ALTER TABLE public.company_signals ENABLE ROW LEVEL SECURITY;

-- Two policies, mirroring the established pattern on `signals` and
-- `org_chart_corrections`:
--
--   1. `company_signals_anon_default_tenant` — allow the anon/demo role to
--      read the demo tenant's signals so the public-demo `/discover` view
--      keeps working without auth.
--   2. `company_signals_tenant_read` — authenticated users can read any
--      account_id their JWT maps to via `account_users`.
--
-- Writes are not policied here → service-role only by default.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'company_signals'
      AND policyname = 'company_signals_anon_default_tenant'
  ) THEN
    CREATE POLICY company_signals_anon_default_tenant ON public.company_signals
      FOR SELECT
      USING (account_id = '00000000-0000-0000-0000-000000000001'::uuid);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'company_signals'
      AND policyname = 'company_signals_tenant_read'
  ) THEN
    CREATE POLICY company_signals_tenant_read ON public.company_signals
      FOR SELECT
      USING (
        account_id IN (
          SELECT au.account_id FROM public.account_users au
          WHERE au.user_id = auth.uid()
        )
      );
  END IF;
END $$;

COMMIT;
