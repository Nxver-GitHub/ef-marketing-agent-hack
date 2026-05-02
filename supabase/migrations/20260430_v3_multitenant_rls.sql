-- 2026-04-30: Wave 6 M1.5 — multitenancy RLS policies (LavenderPrairie).
--
-- DEFER APPLICATION — apply this migration AFTER:
--   1. `20260430_v3_multitenant.sql` (schema-only) is applied
--   2. M2 (server/credence/auth.py) is wired into api.py middleware
--   3. M3 (src/contexts/AccountContext.tsx + Login.tsx) is shipped and
--      every frontend Supabase read is authenticated (no anon-key bypass)
--
-- Applying it earlier breaks the v2 anon-key reads — anon role gets
-- filtered to empty rows because `auth.uid()` is NULL.
--
-- ## Policy pattern
--
-- Uses Supabase-native `auth.uid()` (returns the authenticated user's UUID
-- from the JWT) rather than the original Contract 9 draft of
-- `current_setting('app.account_id')`. The auth.uid() pattern is preferred
-- because it works directly with PostgREST's standard JWT-role assumption,
-- so the frontend's authenticated Supabase queries enforce RLS without any
-- backend session-context middleware setting GUCs.
--
-- The backend FastAPI service connects via DATABASE_URL with the postgres
-- role, which bypasses RLS by default. So backend writes (extractors,
-- cost-tracking) need application-level account_id filtering — RLS is the
-- safety net for user-driven traffic, not the only filter.
--
-- ## Service-role bypass
--
-- The Supabase `service_role` key resolves to a role with `bypassrls = true`.
-- Any backend code path that uses the service-role JWT (e.g., system-level
-- enrichment, admin tools) automatically skips RLS without further config.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- Domain tables — tenant isolation via account_users membership lookup
-- ─────────────────────────────────────────────────────────────────────────────
-- A user sees rows where account_id IN (their account memberships).
-- Insert / update / delete also gated on the same membership.
--
-- The subquery `(SELECT au.account_id FROM account_users au WHERE
-- au.user_id = auth.uid())` is uncorrelated and Postgres caches it per-
-- query, so the per-row cost is essentially the bitmap-index probe of
-- `idx_<tbl>_account_id`. At 1M rows / account, EXPLAIN should show this
-- as `Index Only Scan` followed by `Hash Anti Join` — well under 50ms.

DO $rls$
DECLARE
  tbl TEXT;
BEGIN
  FOREACH tbl IN ARRAY ARRAY[
    'prospects',
    'signals',
    'scores',
    'signal_weights',
    'scoring_runs',
    'persons',
    'companies',
    'employment_periods',
    'education_periods',
    'patents',
    'patent_inventors',
    'person_connections',
    'connection_evidence',
    'enrichment_cost_log'
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.tables
      WHERE table_schema = 'public' AND table_name = tbl
    ) THEN
      RAISE NOTICE 'Skipping RLS on %: table does not exist (apply schema migration first)', tbl;
      CONTINUE;
    END IF;

    -- Skip if `account_id` column doesn't exist (schema migration didn't run)
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = 'public' AND table_name = tbl AND column_name = 'account_id'
    ) THEN
      RAISE NOTICE 'Skipping RLS on %: account_id column missing (apply schema migration first)', tbl;
      CONTINUE;
    END IF;

    -- Enable RLS
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);

    -- Drop existing policy (idempotent on re-run)
    EXECUTE format('DROP POLICY IF EXISTS %I_tenant_isolation ON public.%I', tbl, tbl);

    -- Tenant isolation: account_id ∈ (current user's account memberships).
    -- Demo account `…000fff` is intentionally membership-gated too — demo
    -- users get a row in account_users via the M3 demo onboarding flow,
    -- so this single policy covers both real users and demo users.
    EXECUTE format(
      'CREATE POLICY %I_tenant_isolation ON public.%I '
      'FOR ALL TO authenticated '
      'USING (account_id IN ( '
      '  SELECT au.account_id FROM public.account_users au '
      '  WHERE au.user_id = auth.uid() '
      ')) '
      'WITH CHECK (account_id IN ( '
      '  SELECT au.account_id FROM public.account_users au '
      '  WHERE au.user_id = auth.uid() '
      '))',
      tbl, tbl
    );
  END LOOP;
END
$rls$;


-- ─────────────────────────────────────────────────────────────────────────────
-- accounts + account_users + account_settings — RLS
-- ─────────────────────────────────────────────────────────────────────────────
-- A user can read accounts they belong to, see their own membership rows,
-- and read/write the settings of their own account(s).

ALTER TABLE public.accounts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS accounts_self_read ON public.accounts;
CREATE POLICY accounts_self_read ON public.accounts
  FOR SELECT TO authenticated
  USING (id IN (
    SELECT au.account_id FROM public.account_users au
    WHERE au.user_id = auth.uid()
  ));

ALTER TABLE public.account_users ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS account_users_self_read ON public.account_users;
CREATE POLICY account_users_self_read ON public.account_users
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

ALTER TABLE public.account_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS account_settings_self_rw ON public.account_settings;
CREATE POLICY account_settings_self_rw ON public.account_settings
  FOR ALL TO authenticated
  USING (account_id IN (
    SELECT au.account_id FROM public.account_users au
    WHERE au.user_id = auth.uid()
  ))
  WITH CHECK (account_id IN (
    SELECT au.account_id FROM public.account_users au
    WHERE au.user_id = auth.uid()
  ));

COMMIT;
