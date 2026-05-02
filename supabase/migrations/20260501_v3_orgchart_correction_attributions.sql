-- 2026-05-01: org_chart_corrections.component_attributions — plan.md D.1.
--
-- ## Why this migration exists
--
-- plan.md Phase D.1 requires per-component attribution on each user-submitted
-- correction so the A6 optimizer (Phase D.2) can nudge individual scoring
-- components instead of applying a uniform global multiplier when a
-- correction lands. The companion column `org_reporting_edges.score_components`
-- (added by `20260501_v3_orgchart_score_components.sql`) records what *built*
-- the edge; this column records what *fixed* it.
--
-- One new column on org_chart_corrections:
--
--   - component_attributions JSONB
--       Optional mapping ``{component_name: blame_share}`` summing to ~1.0
--       across keys. Keys belong to the same 7-component keyspace as
--       `score_components` (seniority_gap, domain_match, subdomain_match,
--       manager_title, span_capacity, patent_cluster, geographic_scope).
--       NULL when the operator submitted the correction without selecting
--       a blame component (binary "this edge is wrong" without nuance).
--
-- ## Why JSONB instead of a separate table
--
-- A correction either has zero attribution (NULL) or one-to-many small
-- floats. A JSONB column stays in the hot row, indexes via GIN if we need
-- range queries, and avoids a join in the optimizer hot path. We never
-- update it after insert (corrections are immutable training signals), so
-- the JSONB-vs-relational tradeoffs around UPDATE cost don't apply.
--
-- ## Apply order
--
-- Pure-additive ALTER on `org_chart_corrections`. Depends on:
--   * `20260501_v3_orgchart_schema.sql` (creates the table)
--   * `20260501_v3_orgchart_score_components.sql` (defines the keyspace —
--     same 7 names; we don't FK them, but the CHECK below mirrors that
--     migration's keyspace so a typo in one place fails at INSERT).
--
-- ## Idempotency
--
-- ALTER TABLE … ADD COLUMN IF NOT EXISTS — re-runs are no-ops. Wrapped in
-- BEGIN…COMMIT so the column add and the constraint+index land atomically.

BEGIN;

ALTER TABLE public.org_chart_corrections
  ADD COLUMN IF NOT EXISTS component_attributions JSONB;

-- Postgres doesn't allow subqueries directly inside a CHECK constraint
-- expression. We wrap the keyspace test in an IMMUTABLE helper function
-- and call that from the constraint — same logical guard, same per-row
-- enforcement at INSERT time. The function is `OR REPLACE`-able so a
-- future taxonomy widening only needs the function update, not a
-- column rebuild.
CREATE OR REPLACE FUNCTION public.corrections_attribution_keys_ok(c jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT c IS NULL
      OR (
        jsonb_typeof(c) = 'object'
        AND NOT EXISTS (
          SELECT 1
          FROM jsonb_object_keys(c) AS k
          WHERE k NOT IN (
            'seniority_gap',
            'domain_match',
            'subdomain_match',
            'manager_title',
            'span_capacity',
            'patent_cluster',
            'geographic_scope'
          )
        )
      )
$$;

-- We don't enforce "values sum to 1.0" via a constraint because float-
-- summed JSONB blame shares are inherently fuzzy (operator could
-- intentionally submit 0.5 + 0.3 = 0.8 to express "the rest is
-- unattributed"). The optimizer code normalizes when it reads.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'corrections_component_attributions_keyspace'
  ) THEN
    ALTER TABLE public.org_chart_corrections
      ADD CONSTRAINT corrections_component_attributions_keyspace
      CHECK (public.corrections_attribution_keys_ok(component_attributions));
  END IF;
END $$;

-- GIN index supports the A6 optimizer query: "show me corrections where
-- the blame on component X is > Y". Partial index on non-NULL keeps the
-- index slim because most corrections will land without attributions
-- (binary thumbs-down) until the UI exposes the per-component picker.
CREATE INDEX IF NOT EXISTS idx_corrections_component_attributions_gin
  ON public.org_chart_corrections USING GIN (component_attributions)
  WHERE component_attributions IS NOT NULL;

COMMIT;
