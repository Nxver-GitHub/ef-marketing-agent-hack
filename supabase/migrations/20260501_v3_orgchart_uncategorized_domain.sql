-- 2026-05-01: extend functional_domain keyspace with `uncategorized` so
-- every prospect lands in a cluster row, even when their title can't be
-- bucketed by the NLP taxonomy.
--
-- ## Why
--
-- Prior design (Decision 4 in CLAUDE.md): drop persons whose title doesn't
-- classify into one of the 9 functional domains. The reasoning was that an
-- "uncategorized" cluster would be useless to hierarchy inference.
--
-- That stance held when the dataset was 1k enriched prospects with tidy
-- titles. At 37k persons across 584 companies, dropping unclassifiable
-- titles leaves ~22k prospects with NO cluster row — meaning the
-- /prospect/:id chart UI has nothing to render for those people. Per
-- the operator's directive: "every prospect needs to be in a chart, even
-- when it's just this person + unscraped peer placeholders."
--
-- ## What changes
--
-- Add `'uncategorized'` to the CHECK keyspace on
-- `org_functional_clusters.functional_domain`. Application code routes
-- every unclassifiable title into this bucket per company. Hierarchy
-- inference SHORT-CIRCUITS on uncategorized clusters (no edges produced)
-- so we don't fabricate fake reporting lines from people whose role we
-- don't even know.
--
-- ## Apply order
--
-- Pure-additive constraint widening. Safe to run at any time. The matching
-- application-side change in `taxonomy.FUNCTIONAL_DOMAINS` and
-- `clustering._build_cluster_plan` ships in the same commit; running this
-- migration without the code change just leaves the constraint slightly
-- looser than the planner uses, which is harmless.
--
-- ## Idempotency
--
-- Drops the old constraint by name and recreates it; both steps are guarded
-- so re-running is a no-op once the wider keyspace is in place.

BEGIN;

DO $$
BEGIN
  -- Drop the existing constraint if it still has the old (9-element) keyspace.
  -- We can't introspect the CHECK expression cleanly without parsing pg_get_constraintdef,
  -- so we drop unconditionally and recreate with the wider keyspace.
  IF EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'org_clusters_domain_keyspace'
  ) THEN
    ALTER TABLE public.org_functional_clusters
      DROP CONSTRAINT org_clusters_domain_keyspace;
  END IF;

  ALTER TABLE public.org_functional_clusters
    ADD CONSTRAINT org_clusters_domain_keyspace
    CHECK (functional_domain IN (
      'hardware_engineering', 'software_engineering', 'product_management',
      'manufacturing_ops',    'sales_marketing',      'research',
      'finance_legal',        'people_ops',           'general_management',
      'uncategorized'
    ));
END $$;

COMMIT;
