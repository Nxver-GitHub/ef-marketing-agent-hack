-- 2026-05-01: Org-chart unresolved-target nodes (v3.1 Phase A.5, schema half).
--
-- ## Status
--
-- DRAFT — needs LP apply. Pure-additive follow-up to
-- `20260501_v3_orgchart_schema.sql`. The companion writer-support PR
-- (extending `server/credence/orgchart/hierarchy.py`) is reserved by
-- another agent (SwiftElk) and lands separately. This migration only
-- adds the storage surface; nothing here changes existing edge writers.
--
-- ## Why this migration exists
--
-- The original org-chart schema header (lines 29-31 of
-- `20260501_v3_orgchart_schema.sql`) claimed Decision 4 — "unknown
-- nodes are rendered, not omitted" — was honored via an
-- `is_unresolved_target=TRUE` flag on `org_reporting_edges`. That
-- column was never actually added. As a result, any edge that points
-- to a job-posting-derived role (e.g., "VP of Manufacturing" referenced
-- in a TSMC posting but not yet mapped to a known person) has nowhere
-- to land — `manager_id`/`report_id` are NOT NULL FKs to `persons(id)`.
--
-- This migration:
--   1. Adds the missing `is_unresolved_target` column to `org_reporting_edges`.
--   2. Introduces `org_unresolved_targets` — a first-class table for
--      placeholder roles inferred from postings, press releases, etc.
--   3. Relaxes `org_reporting_edges` so each side of the edge can point
--      EITHER to a known person OR to an unresolved target — exactly one,
--      enforced by an XOR CHECK on each side.
--
-- ## XOR CHECK rationale
--
-- An edge always has a manager and a report. In the new world, each side
-- can resolve to one of two FK targets:
--   - `manager_id`            → public.persons(id)        (known human)
--   - `manager_unresolved_id` → public.org_unresolved_targets(id)  (placeholder)
--
-- Exactly one must be set per side. Encoding this as an XOR CHECK keeps
-- the constraint inside the DB (so any writer — Python, SQL, Supabase
-- client — gets the same guarantee) without needing a discriminator
-- column. We sum boolean casts and assert == 1 rather than using
-- `(a IS NULL) <> (b IS NULL)` because the int form generalizes if we
-- ever add a third target kind.
--
-- ## Apply order dependency
--
-- This migration MUST run AFTER `20260501_v3_orgchart_schema.sql`. It
-- references `org_reporting_edges`, `org_functional_clusters`, and the
-- `touch_updated_at()` trigger function defined there. The filename
-- shares the date but sorts after by name, which matches the supabase
-- CLI ordering rule.
--
-- ## Pure-additive guarantee
--
-- - Existing rows in `org_reporting_edges` keep `manager_id`/`report_id`
--   set; the new `*_unresolved_id` columns default to NULL. The XOR
--   CHECKs evaluate to (1 + 0) = 1 → pass.
-- - We DROP NOT NULL on `manager_id`/`report_id` so the NEW row pattern
--   (placeholder side) is legal. Existing writers in `hierarchy.py` set
--   both columns to NOT NULL person UUIDs and continue to pass.
-- - `is_unresolved_target` defaults to FALSE so existing reads/filters
--   that don't know about it see no behavioral change.
-- - No data is mutated. No constraints existing rows pass today are
--   tightened.
--
-- ## Not in this migration
--
-- - The Python writer changes in `hierarchy.py` (SwiftElk's PR).
-- - A unique index covering `(manager_unresolved_id, report_id)` etc. —
--   we'll add those once the writer ships and we know the actual
--   uniqueness invariants for placeholder edges.
-- - Backfill of placeholder rows from existing postings — that's a
--   separate batch job that runs after the writer lands.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────
-- 1. org_reporting_edges: add is_unresolved_target flag
-- ─────────────────────────────────────────────────────────────────────────
-- Honors the schema header docstring claim. Defaults FALSE so existing
-- rows are unchanged and existing readers (UI, validators, optimizer)
-- continue to see only resolved edges unless they opt in.

ALTER TABLE public.org_reporting_edges
  ADD COLUMN IF NOT EXISTS is_unresolved_target BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_org_edges_is_unresolved_target
  ON public.org_reporting_edges (is_unresolved_target)
  WHERE is_unresolved_target = TRUE;

-- ─────────────────────────────────────────────────────────────────────────
-- 2. org_unresolved_targets
-- ─────────────────────────────────────────────────────────────────────────
-- One row per inferred placeholder role at a cluster. Example: a TSMC
-- Hardware Engineering cluster has 11 known ICs and 2 known managers,
-- but a recent posting references an unnamed "VP of Manufacturing" who
-- the postings clearly imply exists. We materialize that as a row here,
-- with `inferred_seniority_score=70`, `inferred_functional_domain=
-- 'hardware_engineering'`, and a confidence reflecting how many
-- corroborating postings/press references we found.

CREATE TABLE IF NOT EXISTS public.org_unresolved_targets (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id                  UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  cluster_id                  UUID NOT NULL REFERENCES public.org_functional_clusters(id) ON DELETE CASCADE,
  placeholder_label           TEXT NOT NULL,
  inferred_seniority_score    SMALLINT,
  inferred_functional_domain  TEXT,
  confidence                  NUMERIC NOT NULL,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT org_unresolved_confidence_range
    CHECK (confidence >= 0 AND confidence <= 1),
  CONSTRAINT org_unresolved_seniority_range
    CHECK (
      inferred_seniority_score IS NULL
      OR (inferred_seniority_score >= 0 AND inferred_seniority_score <= 100)
    ),
  CONSTRAINT org_unresolved_label_nonempty
    CHECK (length(btrim(placeholder_label)) > 0),
  -- Mirror the keyspace from org_functional_clusters.functional_domain so
  -- placeholders can never name a domain the cluster table doesn't know.
  CONSTRAINT org_unresolved_domain_keyspace
    CHECK (
      inferred_functional_domain IS NULL
      OR inferred_functional_domain IN (
        'hardware_engineering', 'software_engineering', 'product_management',
        'manufacturing_ops',    'sales_marketing',      'research',
        'finance_legal',        'people_ops',           'general_management'
      )
    )
);

-- Re-running clustering on the same posting set must not produce
-- duplicate placeholder rows. Dedup key is (cluster, label) — a cluster
-- can have at most one "VP of Manufacturing" placeholder.
CREATE UNIQUE INDEX IF NOT EXISTS org_unresolved_cluster_label_unique
  ON public.org_unresolved_targets (cluster_id, placeholder_label);

CREATE INDEX IF NOT EXISTS idx_org_unresolved_account_id
  ON public.org_unresolved_targets (account_id);
CREATE INDEX IF NOT EXISTS idx_org_unresolved_cluster_id
  ON public.org_unresolved_targets (cluster_id);

-- updated_at trigger — reuses touch_updated_at() defined in the
-- prerequisite migration `20260501_v3_orgchart_schema.sql`.
DROP TRIGGER IF EXISTS trg_org_unresolved_updated_at
  ON public.org_unresolved_targets;
CREATE TRIGGER trg_org_unresolved_updated_at
  BEFORE UPDATE ON public.org_unresolved_targets
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

-- ─────────────────────────────────────────────────────────────────────────
-- 3. org_reporting_edges: allow placeholder targets on either side
-- ─────────────────────────────────────────────────────────────────────────
-- Strategy: relax existing NOT NULLs, add optional FKs to placeholders,
-- enforce XOR per side. Existing rows pass because they have person FKs
-- set and placeholder FKs NULL. New writers can produce edges where the
-- manager is a placeholder ("VP of Mfg") and the report is a known person,
-- or vice versa, or both placeholders (rare but legal).

ALTER TABLE public.org_reporting_edges
  ALTER COLUMN manager_id DROP NOT NULL;

ALTER TABLE public.org_reporting_edges
  ALTER COLUMN report_id DROP NOT NULL;

ALTER TABLE public.org_reporting_edges
  ADD COLUMN IF NOT EXISTS manager_unresolved_id UUID
    REFERENCES public.org_unresolved_targets(id) ON DELETE CASCADE;

ALTER TABLE public.org_reporting_edges
  ADD COLUMN IF NOT EXISTS report_unresolved_id UUID
    REFERENCES public.org_unresolved_targets(id) ON DELETE CASCADE;

-- XOR: exactly one of (manager_id, manager_unresolved_id) is set.
-- We use NOT VALID + VALIDATE pattern only when there's risk of existing
-- violations; here we expect none (existing rows have manager_id NOT NULL
-- and the new column NULL → sum = 1). Use the strict form.
DO $xor_constraints$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'org_edges_manager_xor'
      AND conrelid = 'public.org_reporting_edges'::regclass
  ) THEN
    ALTER TABLE public.org_reporting_edges
      ADD CONSTRAINT org_edges_manager_xor CHECK (
        (manager_id IS NOT NULL)::int
        + (manager_unresolved_id IS NOT NULL)::int
        = 1
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'org_edges_report_xor'
      AND conrelid = 'public.org_reporting_edges'::regclass
  ) THEN
    ALTER TABLE public.org_reporting_edges
      ADD CONSTRAINT org_edges_report_xor CHECK (
        (report_id IS NOT NULL)::int
        + (report_unresolved_id IS NOT NULL)::int
        = 1
      );
  END IF;
END
$xor_constraints$;

CREATE INDEX IF NOT EXISTS idx_org_edges_manager_unresolved_id
  ON public.org_reporting_edges (manager_unresolved_id)
  WHERE manager_unresolved_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_org_edges_report_unresolved_id
  ON public.org_reporting_edges (report_unresolved_id)
  WHERE report_unresolved_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────
-- 4. RLS — same auth.uid() pattern as the prerequisite schema migration.
-- ─────────────────────────────────────────────────────────────────────────
-- Plus the anon-default-tenant SELECT bridge so the public marketing
-- frontend (anon key) sees default tenant placeholder rows, matching
-- 20260430_v3_anon_default_tenant_read.sql and the policies installed
-- by 20260501_v3_orgchart_schema.sql.

ALTER TABLE public.org_unresolved_targets ENABLE ROW LEVEL SECURITY;

DO $rls$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'org_unresolved_targets'
      AND policyname = 'org_unresolved_targets_tenant_select'
  ) THEN
    CREATE POLICY org_unresolved_targets_tenant_select
      ON public.org_unresolved_targets
      FOR SELECT TO authenticated
      USING (account_id IN (
        SELECT au.account_id FROM public.account_users au
        WHERE au.user_id = auth.uid()
      ));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'org_unresolved_targets'
      AND policyname = 'org_unresolved_targets_tenant_insert'
  ) THEN
    CREATE POLICY org_unresolved_targets_tenant_insert
      ON public.org_unresolved_targets
      FOR INSERT TO authenticated
      WITH CHECK (account_id IN (
        SELECT au.account_id FROM public.account_users au
        WHERE au.user_id = auth.uid()
      ));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'org_unresolved_targets'
      AND policyname = 'org_unresolved_targets_tenant_update'
  ) THEN
    CREATE POLICY org_unresolved_targets_tenant_update
      ON public.org_unresolved_targets
      FOR UPDATE TO authenticated
      USING (account_id IN (
        SELECT au.account_id FROM public.account_users au
        WHERE au.user_id = auth.uid()
      ))
      WITH CHECK (account_id IN (
        SELECT au.account_id FROM public.account_users au
        WHERE au.user_id = auth.uid()
      ));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'org_unresolved_targets'
      AND policyname = 'org_unresolved_targets_tenant_delete'
  ) THEN
    CREATE POLICY org_unresolved_targets_tenant_delete
      ON public.org_unresolved_targets
      FOR DELETE TO authenticated
      USING (account_id IN (
        SELECT au.account_id FROM public.account_users au
        WHERE au.user_id = auth.uid()
      ));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'org_unresolved_targets'
      AND policyname = 'org_unresolved_targets_anon_default_tenant'
  ) THEN
    CREATE POLICY org_unresolved_targets_anon_default_tenant
      ON public.org_unresolved_targets
      FOR SELECT TO anon
      USING (account_id = '00000000-0000-0000-0000-000000000001'::uuid);
  END IF;
END
$rls$;

COMMIT;
