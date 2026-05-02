-- 2026-05-01: org_reporting_edges score_components + dominant_signal — Phase 0-A partial.
--
-- ## Why this migration exists
--
-- `orgchart_tasks.md` Task 4-A requires per-component attribution at edge-write time
-- so the optimizer (Task 4-B) can nudge individual scoring components instead of
-- applying a uniform global multiplier. CLAUDE.md Section II issue 4 of plan.md:
-- "we're learning 'implicit_scoring is X% accurate' but not 'the patent-cluster
-- bonus is over-firing.'"
--
-- Two new columns on org_reporting_edges:
--
--   - score_components JSONB
--       Per-component breakdown of the implicit score that produced the edge.
--       Keys (exactly 7 per Task 4-A spec): seniority_gap, domain_match,
--       subdomain_match, manager_title, span_capacity, patent_cluster,
--       geographic_scope. Values are floats in [0, 1]. Sum should ≈ confidence
--       within 0.01 tolerance. NULL for edges written before this migration
--       lands (Task 4-B has a fallback path for NULL components).
--
--   - dominant_signal TEXT
--       max(score_components.values()) component name. Used by the optimizer
--       to attribute corrections to the right scoring lever. Constrained to
--       the 7 keyspace + 'unknown' for explicit edges that bypass implicit
--       scoring entirely.
--
-- ## Apply order
--
-- This migration is pure-additive on top of `20260501_v3_orgchart_schema.sql`.
-- It does NOT depend on `20260501_v3_orgchart_unresolved_targets.sql` and can
-- be applied in either order — both touch org_reporting_edges with disjoint
-- column adds.
--
-- ## Idempotency
--
-- ALTER TABLE … ADD COLUMN IF NOT EXISTS — re-runs are no-ops. Wrapped in
-- BEGIN…COMMIT.

BEGIN;

ALTER TABLE public.org_reporting_edges
  ADD COLUMN IF NOT EXISTS score_components JSONB,
  ADD COLUMN IF NOT EXISTS dominant_signal  TEXT;

-- Validate dominant_signal keyspace when set. NULL is allowed (legacy edges,
-- explicit edges that bypass implicit scoring).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'org_edges_dominant_signal_keyspace'
  ) THEN
    ALTER TABLE public.org_reporting_edges
      ADD CONSTRAINT org_edges_dominant_signal_keyspace
      CHECK (
        dominant_signal IS NULL
        OR dominant_signal IN (
          'seniority_gap',
          'domain_match',
          'subdomain_match',
          'manager_title',
          'span_capacity',
          'patent_cluster',
          'geographic_scope',
          'unknown'
        )
      );
  END IF;
END $$;

-- Index supports A6 optimizer queries: "find all corrections grouped by the
-- dominant component" — filtering org_chart_corrections joined to edges by
-- dominant_signal will hit this index.
CREATE INDEX IF NOT EXISTS idx_org_edges_dominant_signal
  ON public.org_reporting_edges (dominant_signal)
  WHERE dominant_signal IS NOT NULL;

-- GIN index on score_components allows queries like:
--   "all edges where patent_cluster contributed > 0.10"
-- Used by the diagnostic/audit tooling, not by the optimizer hot path.
CREATE INDEX IF NOT EXISTS idx_org_edges_score_components_gin
  ON public.org_reporting_edges USING GIN (score_components)
  WHERE score_components IS NOT NULL;

COMMIT;
