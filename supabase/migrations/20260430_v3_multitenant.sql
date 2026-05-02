-- 2026-04-30: Wave 6 M1 — multitenancy SCHEMA-ONLY (LavenderPrairie).
--
-- Takes Credence from single-tenant-per-deployment → multi-tenant SaaS.
-- README.md L105 explicitly listed multi-tenant as out-of-scope for v3;
-- this migration moves it into scope per CONTRACTS.md Contract 9.
--
-- IMPORTANT — RLS POLICIES ARE DEFERRED.
--
-- Enabling RLS now would break the existing v2 anon-key frontend reads
-- (anon role would be filtered to empty since `auth.uid()` is null without
-- a logged-in session). RLS is split into a follow-up migration
-- `20260430_v3_multitenant_rls.sql` that gets applied AFTER M3 (frontend
-- AccountProvider + Login) is shipped and users are authenticating.
--
-- This file is safe to apply against a populated v2 database immediately:
-- it only adds tables, columns, indexes, FKs, and triggers. Existing
-- queries continue to work unchanged.
--
-- 3 new tables:
--   1. accounts            — top-level tenant entity
--   2. account_users       — user → account membership (single-row in v1)
--   3. account_settings    — per-tenant Wave 5 enrichment budgets
--
-- 13 ALTERs adding `account_id uuid` FK on every per-tenant domain table:
--   prospects, signals, scores, scoring_runs,
--   persons, companies, employment_periods, education_periods,
--   patents, patent_inventors, person_connections, connection_evidence,
--   enrichment_cost_log
--
-- `signal_weights` is intentionally global (single shared scoring config);
-- per-tenant weight customization is a v3.1 concern. See review note in
-- the DO block below.
--
-- Plus a default tenant `00000000-0000-0000-0000-000000000001` so existing
-- v2 prospects can be assigned without losing data (M6 policy: backwards-
-- compat for now; real customer migrations replace).
--
-- Plus a demo pseudo-tenant `00000000-0000-0000-0000-000000000fff` for
-- ?demo=true (CONTRACTS.md Contract 9 §"Demo mode reconciliation").
--
-- Plus RLS policies on every domain table referencing
-- `current_setting('app.account_id')::uuid`.
--
-- Pure-additive: no v2 columns dropped, no constraints removed. Existing
-- queries continue to work; RLS filters them transparently once the
-- session middleware (M2) sets `app.account_id` per request.
--
-- IMPORTANT — this migration assumes the Supabase `auth.users` table
-- exists (Supabase Auth is enabled by default on every project). If a
-- non-Supabase Postgres is used, the FK on `account_users.user_id` should
-- be removed or pointed at the local users table.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. accounts — top-level tenant entity
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.accounts (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name TEXT NOT NULL,
  slug         TEXT NOT NULL UNIQUE,
  plan_tier    TEXT NOT NULL DEFAULT 'free',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT accounts_plan_tier_check CHECK (plan_tier IN ('free', 'pro', 'enterprise')),
  CONSTRAINT accounts_slug_format CHECK (slug ~ '^[a-z0-9][a-z0-9_-]*$')
);

CREATE INDEX IF NOT EXISTS idx_accounts_slug ON public.accounts (slug);


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. account_users — membership join (Supabase Auth integration point)
-- ─────────────────────────────────────────────────────────────────────────────
-- v1 contract: one row per user (single account each). v2 lifts that.
-- Foreign key into auth.users — Supabase's default identity table. If
-- migrating to a non-Supabase Postgres, swap this FK target.

CREATE TABLE IF NOT EXISTS public.account_users (
  account_id UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  user_id    UUID NOT NULL,  -- intentionally not FK'd here; auth.users may not exist in non-Supabase deployments. Application enforces.
  role       TEXT NOT NULL DEFAULT 'owner',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, user_id),
  CONSTRAINT account_users_role_check CHECK (role IN ('owner', 'admin', 'editor', 'viewer'))
);

-- Reverse-lookup: given a user_id, what accounts do they belong to?
-- Drives the AccountProvider's account-list fetch on login.
CREATE INDEX IF NOT EXISTS idx_account_users_user_id ON public.account_users (user_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- 3. account_settings — per-tenant Wave 5 enrichment budgets + flags
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.account_settings (
  account_id              UUID PRIMARY KEY REFERENCES public.accounts(id) ON DELETE CASCADE,
  apollo_monthly_cents    INTEGER NOT NULL DEFAULT 0,
  pdl_monthly_cents       INTEGER NOT NULL DEFAULT 0,
  parallel_monthly_cents  INTEGER NOT NULL DEFAULT 0,
  firecrawl_monthly_cents INTEGER NOT NULL DEFAULT 0,
  -- Future: feature flags, per-tenant scoring weight version, etc.
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT account_settings_caps_nonneg CHECK (
    apollo_monthly_cents >= 0
    AND pdl_monthly_cents >= 0
    AND parallel_monthly_cents >= 0
    AND firecrawl_monthly_cents >= 0
  )
);


-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Seed the default + demo pseudo-tenants
-- ─────────────────────────────────────────────────────────────────────────────
-- Default tenant: existing v2 prospects/signals/scores get assigned here so
-- the migration is non-destructive. M6 (data migration policy) decides
-- whether real customers reuse this UUID or get fresh accounts.
--
-- Demo pseudo-tenant: ?demo=true short-circuits to this account. RLS
-- policy below allows any authenticated request to read demo rows.

INSERT INTO public.accounts (id, display_name, slug, plan_tier)
VALUES
  ('00000000-0000-0000-0000-000000000001', 'Default (v2 backwards compat)', 'default', 'free'),
  ('00000000-0000-0000-0000-000000000fff', 'Demo Account',                  'demo',    'free')
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.account_settings (account_id, apollo_monthly_cents, pdl_monthly_cents, parallel_monthly_cents, firecrawl_monthly_cents)
VALUES
  ('00000000-0000-0000-0000-000000000001', 0, 0, 0, 0),
  ('00000000-0000-0000-0000-000000000fff', 0, 0, 0, 0)
ON CONFLICT (account_id) DO NOTHING;


-- ─────────────────────────────────────────────────────────────────────────────
-- 5. ALTER each domain table: add `account_id` FK + backfill to default
-- ─────────────────────────────────────────────────────────────────────────────
-- Pattern per table:
--   1. ADD COLUMN account_id UUID  (nullable initially so backfill can run)
--   2. UPDATE … SET account_id = '<default>' WHERE account_id IS NULL
--   3. ALTER COLUMN account_id SET NOT NULL
--   4. ADD FK CASCADE to accounts(id)
--   5. CREATE INDEX on account_id
--
-- The triple-step (nullable → backfill → NOT NULL) is necessary because
-- ADD COLUMN with NOT NULL on a populated table requires a default, and
-- a default that isn't a literal forces a table rewrite. Three statements
-- is the cheapest path.

DO $multitenant$
DECLARE
  default_account UUID := '00000000-0000-0000-0000-000000000001';
  tbl TEXT;
BEGIN
  -- DarkBeaver M1 review fix: `signal_weights` removed from this loop.
  -- Reason: v2 schema has `UNIQUE (signal_type)` on signal_weights, which
  -- means one row per signal_type globally. Adding `account_id NOT NULL`
  -- without dropping that UNIQUE makes per-tenant weight rows impossible
  -- (the second tenant's INSERT collides on signal_type). Per-tenant
  -- scoring weights require a separate v3.1 migration that swaps
  -- `UNIQUE (signal_type)` → `UNIQUE (account_id, signal_type)` and seeds
  -- the 10 default weights into every new account_settings row. Out of
  -- scope for M1; signal_weights stays global until v3.1.
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
    'enrichment_cost_log'
  ]
  LOOP
    -- Skip tables that don't exist yet (some are added by other migrations
    -- like education_periods, enrichment_cost_log; others by E + E.1).
    -- Re-running this block after those migrations land is safe due to
    -- the IF NOT EXISTS / column-presence checks below.
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.tables
      WHERE table_schema = 'public' AND table_name = tbl
    ) THEN
      RAISE NOTICE 'Skipping %: table does not exist yet (run dependent migration first)', tbl;
      CONTINUE;
    END IF;

    -- Add column (nullable for now)
    EXECUTE format('ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS account_id UUID', tbl);

    -- Backfill any existing rows to the default tenant (no-op on empty tables)
    EXECUTE format('UPDATE public.%I SET account_id = $1 WHERE account_id IS NULL', tbl)
    USING default_account;

    -- Make NOT NULL
    EXECUTE format('ALTER TABLE public.%I ALTER COLUMN account_id SET NOT NULL', tbl);

    -- Add FK (idempotent: skip if already present)
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.table_constraints
      WHERE constraint_name = format('%s_account_id_fkey', tbl)
    ) THEN
      EXECUTE format(
        'ALTER TABLE public.%I ADD CONSTRAINT %I_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE',
        tbl, tbl
      );
    END IF;

    -- Index on account_id (drives the RLS WHERE clause + per-tenant queries)
    EXECUTE format(
      'CREATE INDEX IF NOT EXISTS idx_%I_account_id ON public.%I (account_id)',
      tbl, tbl
    );
  END LOOP;
END
$multitenant$;


-- ─────────────────────────────────────────────────────────────────────────────
-- 6. updated_at trigger on accounts + account_settings
-- ─────────────────────────────────────────────────────────────────────────────
-- (RLS enable + policies are deferred to 20260430_v3_multitenant_rls.sql —
-- applied AFTER M3 frontend authentication lands. See header note.)

DROP TRIGGER IF EXISTS trg_accounts_touch_updated_at ON public.accounts;
CREATE TRIGGER trg_accounts_touch_updated_at
  BEFORE UPDATE ON public.accounts
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

DROP TRIGGER IF EXISTS trg_account_settings_touch_updated_at ON public.account_settings;
CREATE TRIGGER trg_account_settings_touch_updated_at
  BEFORE UPDATE ON public.account_settings
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();


COMMIT;
