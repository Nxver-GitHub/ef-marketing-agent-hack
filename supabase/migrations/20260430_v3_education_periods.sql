-- 2026-04-30: v3 education_periods — additive follow-up to E (LavenderPrairie).
--
-- CLAUDE.md L110 specifies an `education_periods` table; Track E (DarkBeaver,
-- 20260430_v3_connection_graph.sql) deferred it as not on the warm-path-only
-- critical cut. Adding it now unblocks two connection types:
--   - same_phd_advisor (STRENGTH_TABLE = 0.92, second-highest after patent)
--   - alumni_network (0.25, included for completeness)
-- and gives the future scholar.py extractor (Track J.5) somewhere structured
-- to write PhD advisor relationships discovered via Semantic Scholar / ORCID.
--
-- Pure-additive: no ALTER on E or E.1 tables. `touch_updated_at()` function
-- already exists from E (line 345 of 20260430_v3_connection_graph.sql) — we
-- just attach a new trigger to this table.
--
-- Migration order (lexical date suffix):
--   1. 20260430_v3_connection_graph.sql      (E — creates persons)
--   2. 20260430_v3_education_periods.sql     (this — depends on persons)
--   3. 20260430_v3_unique_constraints.sql    (E.1 — independent)
-- The FK on persons(id) is satisfied because connection_graph runs first.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- education_periods — every academic institution attended.
--
-- `advisor_person_id` links to another person record when the advisor is
-- itself in the database — the source of `same_phd_advisor` warm-path edges
-- (CLAUDE.md STRENGTH_TABLE: 0.92 base, 0.01 decay per year).
--
-- `school_canonical_name` is denormalized rather than FK'd to a `schools`
-- table — there's no schools table yet, and the alumni-network connection
-- only requires that two persons attended the same school string. A future
-- migration can introduce a canonical schools table and back-fill.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.education_periods (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id             UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  school_canonical_name TEXT NOT NULL,
  school_name_variants  TEXT[] NOT NULL DEFAULT '{}',
  degree                TEXT,
  field_of_study        TEXT,
  start_year            SMALLINT,
  end_year              SMALLINT,
  -- PhD advisor link — points at another person if known. NULL is the common
  -- case (most advisors won't be in the persons table). ON DELETE SET NULL so
  -- removing an advisor's person row preserves the education record.
  advisor_person_id     UUID REFERENCES public.persons(id) ON DELETE SET NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- Year sanity: end_year >= start_year when both are present.
  CONSTRAINT education_periods_year_range_check
    CHECK (end_year IS NULL OR start_year IS NULL OR end_year >= start_year),
  -- Self-advise is meaningless and would corrupt phd_advisor edge BFS.
  CONSTRAINT education_periods_no_self_advisor
    CHECK (advisor_person_id IS NULL OR advisor_person_id <> person_id)
);

-- Lookup by person — drives the warm-path BFS expansion when an advisor edge
-- is known to exist.
CREATE INDEX IF NOT EXISTS idx_education_periods_person
  ON public.education_periods (person_id);

-- Lookup by school — drives the alumni-network connection write path
-- (find pairs of persons sharing a school_canonical_name).
CREATE INDEX IF NOT EXISTS idx_education_periods_school
  ON public.education_periods (school_canonical_name);

-- Partial index on advisor_person_id — only ~1% of education rows are
-- expected to have a non-null advisor (PhDs only); a partial index avoids
-- bloating the index for the NULL majority. Drives the same_phd_advisor
-- connection write path (find pairs of persons sharing an advisor_person_id).
CREATE INDEX IF NOT EXISTS idx_education_periods_advisor
  ON public.education_periods (advisor_person_id)
  WHERE advisor_person_id IS NOT NULL;

-- GIN on school_name_variants[] — supports lookup-by-variant when an extractor
-- hits a school name that doesn't match canonical (e.g., "MIT" vs
-- "Massachusetts Institute of Technology").
CREATE INDEX IF NOT EXISTS idx_education_periods_school_variants_gin
  ON public.education_periods USING GIN (school_name_variants);

-- updated_at trigger — reuses touch_updated_at() function defined in E.
DROP TRIGGER IF EXISTS trg_education_periods_touch_updated_at ON public.education_periods;
CREATE TRIGGER trg_education_periods_touch_updated_at
  BEFORE UPDATE ON public.education_periods
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

COMMIT;
