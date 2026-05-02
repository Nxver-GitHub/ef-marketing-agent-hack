-- 2026-04-30: Anon-role SELECT policies — un-break the v2 anon-key read path.
--
-- ## Why this exists
--
-- `20260430_v3_multitenant_rls.sql` enabled RLS on every domain table with
-- policies restricted to the `authenticated` role. The frontend uses the
-- Supabase **anon** key for all reads (`src/lib/supabase.ts` initializes
-- the client with `VITE_SUPABASE_ANON_KEY`). Anon role wasn't covered by
-- any policy, so RLS filtered every query to zero rows — the locally-
-- deployed page showed 0 of 20,075 prospects.
--
-- The original RLS migration explicitly warned this would happen:
--
-- > "Applying it earlier breaks the v2 anon-key reads — anon role gets
-- >  filtered to empty rows because `auth.uid()` is NULL."
--
-- The proper long-term fix is M3 + M5 wiring every frontend read through
-- Supabase Auth (`supabase.auth.getSession()` → JWT → authenticated
-- role). That's partially shipped but not universal yet.
--
-- This migration is the bridge: anon role can SELECT rows belonging to
-- the **default tenant** (`00000000-0000-0000-0000-000000000001`) only.
-- Verified live: all 20,075 v2 prospects are on the default tenant
-- (`SELECT account_id, count(*) FROM prospects GROUP BY 1` returns one
-- row). Tenants other than default are still anon-invisible — preserves
-- multitenant isolation for any future tenant.
--
-- Service-role traffic continues to bypass RLS entirely (its role has
-- `bypassrls = true`).
--
-- Authenticated traffic continues to use the existing `_tenant_isolation`
-- policies — anon-vs-authenticated coexist via separate policies on the
-- same table per Postgres RLS semantics.
--
-- ## Scope
--
-- Adds `FOR SELECT TO anon USING (account_id = DEFAULT)` to every domain
-- table that has an `account_id` column. Tables without `account_id`
-- (signal_weights — intentionally global per Wave 6 design) get an
-- unrestricted anon SELECT.
--
-- INSERT/UPDATE/DELETE are NOT granted to anon. Writes still require
-- authenticated session.

BEGIN;

-- Domain tables with account_id — anon sees default-tenant rows only.
DO $anon$
DECLARE
  tbl TEXT;
  default_acct CONSTANT UUID := '00000000-0000-0000-0000-000000000001';
BEGIN
  FOREACH tbl IN ARRAY ARRAY[
    'prospects',
    'signals',
    'scores',
    'scoring_runs',
    'persons',
    'companies',
    'employment_periods',
    'education_periods',
    'patents',
    'patent_inventors',
    'person_connections',
    'connection_evidence',
    'enrichment_cost_log',
    'score_weights',
    'score_records'
  ]
  LOOP
    -- Skip tables that don't exist or lack account_id
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = 'public' AND table_name = tbl AND column_name = 'account_id'
    ) THEN
      RAISE NOTICE 'Skipping anon SELECT on %: account_id column missing', tbl;
      CONTINUE;
    END IF;

    -- Drop the policy first so re-runs are idempotent
    EXECUTE format(
      'DROP POLICY IF EXISTS %I_anon_default_select ON public.%I',
      tbl, tbl
    );
    EXECUTE format(
      'CREATE POLICY %I_anon_default_select ON public.%I FOR SELECT TO anon '
      'USING (account_id = %L::uuid)',
      tbl, tbl, default_acct
    );
  END LOOP;
END
$anon$;

-- Global tables — anon SELECT unrestricted (no tenant column to scope by).
DROP POLICY IF EXISTS signal_weights_anon_select ON public.signal_weights;
CREATE POLICY signal_weights_anon_select ON public.signal_weights
  FOR SELECT TO anon USING (true);

COMMIT;
