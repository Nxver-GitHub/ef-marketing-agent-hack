-- 2026-05-01: Education + conference + standards schema (v3.1 Plan B, SwiftElk).
--
-- ## Status
--
-- DRAFT — needs LP apply. Adds the three tables V3_PT2.md L437-479 specs:
--
--   1. institutions          — canonical school name registry + aliases
--   2. education_overlaps    — pre-computed cohort overlaps (mirrors person_connections)
--   3. conference_attendances — non-presenting conference appearances
--
-- Hidden-connections strength edges flow through person_connections; these
-- tables are the substrate the cohort-strength job + conference extractor
-- write to before deriving person_connections rows.
--
-- ## Decisions baked in
--
--   - institutions.canonical_name is UNIQUE + lowercase-comparison-safe;
--     aliases[] is TEXT[] keyed for lookup. Per V3_PT2.md L520-563 the
--     extractor normalizes via this table, never against ad-hoc string
--     literals
--   - education_overlaps preserves D1 from connection_graph: person_a_id <
--     person_b_id (CHECK + UNIQUE on the symmetric tuple). Mirrors
--     person_connections invariants exactly so the cohort_strength_job can
--     stream from this table into person_connections without re-checking
--     ordering.
--   - conference_attendances UNIQUE(person_id, event_id) so a person can't
--     be double-counted at the same conference; multi-year same-conference
--     visits use distinct event_id rows (one event per year)
--   - Wave 6 multi-tenancy: every table carries account_id NOT NULL FK +
--     RLS auth.uid() pattern + anon-default-tenant bridge SELECT. Same
--     style as orgchart schema migration to keep maintenance one-place
--
-- ## Not in this migration
--
-- - The actual extractors (Plan B3-B5 in `server/credence/extractors/`)
-- - person_connections schema changes — none needed; the four new
--   connection_type strings (`same_mba_cohort`, `same_phd_program`,
--   `executive_education`, `same_undergrad_cohort`) are added to the CHECK
--   constraint via ALTER below since the existing CHECK is value-list
-- - Seed data for institutions — the extractor writes them lazily on
--   first encounter, normalizing aliases as they appear

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────
-- 1. institutions — canonical school name registry
-- ─────────────────────────────────────────────────────────────────────────
-- Stored once per institution, not per overlap. Aliases captures the long
-- tail of how PDL/Apollo/manual entries spell the same school. The
-- extractor pre-checks aliases[] before fuzzy-matching as fallback.

CREATE TABLE IF NOT EXISTS public.institutions (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_name        TEXT NOT NULL UNIQUE,
  short_name            TEXT,
  aliases               TEXT[] NOT NULL DEFAULT '{}',
  institution_type      TEXT NOT NULL,
  prestige_tier         INTEGER NOT NULL DEFAULT 3,
  typical_cohort_size   INTEGER,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT institutions_type_keyspace
    CHECK (institution_type IN ('mba', 'phd', 'undergrad', 'exec_ed', 'other')),
  CONSTRAINT institutions_prestige_range
    CHECK (prestige_tier >= 1 AND prestige_tier <= 5)
);

CREATE INDEX IF NOT EXISTS idx_institutions_aliases
  ON public.institutions USING GIN (aliases);
CREATE INDEX IF NOT EXISTS idx_institutions_type
  ON public.institutions (institution_type);

-- ─────────────────────────────────────────────────────────────────────────
-- 2. education_overlaps — pre-computed cohort overlaps
-- ─────────────────────────────────────────────────────────────────────────
-- One row per (person_a_id < person_b_id, institution_id, degree_type)
-- triple. Same-year detection lives in graduation_year_a/b — caller decides
-- the year-gap penalty during cohort_strength_job. Source records who
-- found this overlap so a future audit can attribute back.

CREATE TABLE IF NOT EXISTS public.education_overlaps (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id          UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  person_a_id         UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  person_b_id         UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  institution_id      UUID NOT NULL REFERENCES public.institutions(id) ON DELETE CASCADE,
  degree_type         TEXT NOT NULL,
  graduation_year_a   INTEGER,
  graduation_year_b   INTEGER,
  same_program        BOOLEAN NOT NULL DEFAULT FALSE,
  source              TEXT NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT education_a_lt_b
    CHECK (person_a_id < person_b_id),
  CONSTRAINT education_degree_keyspace
    CHECK (degree_type IN ('mba', 'phd', 'emba', 'bs', 'ms', 'exec_ed')),
  CONSTRAINT education_source_keyspace
    CHECK (source IN ('pdl', 'apollo', 'linkedin_scrape', 'manual')),
  CONSTRAINT education_year_range
    CHECK (
      (graduation_year_a IS NULL OR (graduation_year_a >= 1900 AND graduation_year_a <= 2100))
      AND (graduation_year_b IS NULL OR (graduation_year_b >= 1900 AND graduation_year_b <= 2100))
    ),
  UNIQUE (person_a_id, person_b_id, institution_id, degree_type)
);

CREATE INDEX IF NOT EXISTS idx_education_overlaps_account_id
  ON public.education_overlaps (account_id);
CREATE INDEX IF NOT EXISTS idx_education_overlaps_a
  ON public.education_overlaps (person_a_id);
CREATE INDEX IF NOT EXISTS idx_education_overlaps_b
  ON public.education_overlaps (person_b_id);
CREATE INDEX IF NOT EXISTS idx_education_overlaps_institution
  ON public.education_overlaps (institution_id);

-- ─────────────────────────────────────────────────────────────────────────
-- 3. conference_attendances — non-presenting conference appearances
-- ─────────────────────────────────────────────────────────────────────────
-- Co-presenters land in `event_appearances` (existing table, role=presenter
-- + co-author shape). This table captures attendance/panelist roles surfaced
-- by Firecrawl crawls of public speaker programs. Year is denormalized for
-- query convenience even though the foreign event row also has it.

CREATE TABLE IF NOT EXISTS public.conference_attendances (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id      UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  person_id       UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  event_id        UUID NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
  role            TEXT NOT NULL DEFAULT 'attendee',
  year            INTEGER NOT NULL,
  source          TEXT NOT NULL,
  confidence      NUMERIC NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT conf_attendances_role_keyspace
    CHECK (role IN ('attendee', 'panelist', 'speaker', 'keynote', 'session_chair')),
  CONSTRAINT conf_attendances_source_keyspace
    CHECK (source IN ('firecrawl', 'parallel', 'scholar', 'manual')),
  CONSTRAINT conf_attendances_year_range
    CHECK (year >= 1900 AND year <= 2100),
  CONSTRAINT conf_attendances_confidence_range
    CHECK (confidence >= 0 AND confidence <= 1),
  UNIQUE (person_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_conf_attendances_account_id
  ON public.conference_attendances (account_id);
CREATE INDEX IF NOT EXISTS idx_conf_attendances_person_id
  ON public.conference_attendances (person_id);
CREATE INDEX IF NOT EXISTS idx_conf_attendances_event_id
  ON public.conference_attendances (event_id);
CREATE INDEX IF NOT EXISTS idx_conf_attendances_year
  ON public.conference_attendances (year);

-- ─────────────────────────────────────────────────────────────────────────
-- 4. Extend person_connections.connection_type CHECK with the 4 new kinds
-- ─────────────────────────────────────────────────────────────────────────
-- The existing CHECK constraint on person_connections has a fixed value
-- list — drop and recreate with the four new connection_type strings the
-- cohort_strength_job will write. Idempotent on re-run because we drop the
-- old constraint by name first.

DO $extend_check$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'person_connections_type_keyspace'
      AND conrelid = 'public.person_connections'::regclass
  ) THEN
    ALTER TABLE public.person_connections
      DROP CONSTRAINT person_connections_type_keyspace;
  END IF;

  ALTER TABLE public.person_connections
    ADD CONSTRAINT person_connections_type_keyspace
    CHECK (connection_type IN (
      -- existing v3 connection types
      'patent_co_inventor', 'same_phd_advisor', 'co_board_member',
      'academic_co_author_multi', 'academic_co_author_single',
      'career_overlap_same_team', 'standards_committee_peer',
      'conference_co_presenter', 'co_investor',
      'career_overlap_same_domain', 'career_overlap_general',
      'alumni_network', 'conference_co_attendee',
      -- v3.1 expansions (V3_PT2.md L376-380)
      'same_mba_cohort', 'same_phd_program',
      'executive_education', 'same_undergrad_cohort'
    ));
EXCEPTION WHEN undefined_table THEN
  -- person_connections doesn't exist yet (running on a fresh DB before
  -- connection_graph migration). Skip — when that migration runs, it will
  -- ship its own CHECK and a future migration can re-extend.
  RAISE NOTICE 'person_connections not present; skipping CHECK extension';
END
$extend_check$;

-- ─────────────────────────────────────────────────────────────────────────
-- 5. RLS — same auth.uid() pattern + anon-default-tenant bridge
-- ─────────────────────────────────────────────────────────────────────────

ALTER TABLE public.institutions             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.education_overlaps       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conference_attendances   ENABLE ROW LEVEL SECURITY;

-- institutions is intentionally tenant-agnostic (canonical name registry,
-- shared across accounts). authenticated role gets unrestricted read; anon
-- gets unrestricted read; only service-role writes.
DO $i_rls$
BEGIN
  EXECUTE 'CREATE POLICY institutions_anyone_select ON public.institutions FOR SELECT USING (true)';
  -- No insert/update/delete policies → only service role can write.
END
$i_rls$;

DO $rls$
DECLARE
  tbl TEXT;
BEGIN
  FOREACH tbl IN ARRAY ARRAY['education_overlaps', 'conference_attendances']
  LOOP
    EXECUTE format(
      'CREATE POLICY %I_tenant_select ON public.%I FOR SELECT TO authenticated '
      'USING (account_id IN (SELECT au.account_id FROM public.account_users au '
      'WHERE au.user_id = auth.uid()))',
      tbl, tbl
    );
    EXECUTE format(
      'CREATE POLICY %I_tenant_insert ON public.%I FOR INSERT TO authenticated '
      'WITH CHECK (account_id IN (SELECT au.account_id FROM public.account_users au '
      'WHERE au.user_id = auth.uid()))',
      tbl, tbl
    );
    EXECUTE format(
      'CREATE POLICY %I_tenant_update ON public.%I FOR UPDATE TO authenticated '
      'USING (account_id IN (SELECT au.account_id FROM public.account_users au '
      'WHERE au.user_id = auth.uid())) '
      'WITH CHECK (account_id IN (SELECT au.account_id FROM public.account_users au '
      'WHERE au.user_id = auth.uid()))',
      tbl, tbl
    );
    EXECUTE format(
      'CREATE POLICY %I_tenant_delete ON public.%I FOR DELETE TO authenticated '
      'USING (account_id IN (SELECT au.account_id FROM public.account_users au '
      'WHERE au.user_id = auth.uid()))',
      tbl, tbl
    );
    -- Bridge: anon-key SELECT of default tenant rows, mirroring
    -- 20260430_v3_anon_default_tenant_read.sql so demo paths read these.
    EXECUTE format(
      'CREATE POLICY %I_anon_default_tenant ON public.%I FOR SELECT TO anon '
      'USING (account_id = ''00000000-0000-0000-0000-000000000001''::uuid)',
      tbl, tbl
    );
  END LOOP;
END
$rls$;

COMMIT;
