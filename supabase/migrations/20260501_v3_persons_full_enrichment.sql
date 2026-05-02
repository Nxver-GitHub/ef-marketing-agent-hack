-- 2026-05-01: full-enrichment columns on persons + person_signals table.
--
-- ## Why this exists
--
-- Bug found 2026-05-01: ``credence.enrichment.writer.write_canonical_persons``
-- was throwing away ~13 fields per profile that Apify Full+email returns —
-- including verified work emails (the most expensive ones, $0.012 each).
-- The 2,253 profiles recovered from the abandoned bulk runs landed in
-- Supabase with only employment_periods + education_periods populated;
-- email/skills/headline/etc. were silently dropped because the persons
-- table had no columns for them.
--
-- This migration:
-- 1. Adds high-query-frequency columns to ``persons`` (email, headline,
--    location, country, plus 4 small attribute fields)
-- 2. Creates ``person_signals`` for low-cardinality structured signal
--    payloads (skills, certifications, publications, patents,
--    honors_and_awards, organizations, languages)
--
-- After apply: re-running the writer against existing Apify datasets
-- (which persist on their side at zero re-fetch cost) backfills all
-- the previously-discarded fields.
--
-- ## Why both
--
-- - ``persons`` columns: stuff we query/filter on — email_status (where
--   Apollo's verified flag matters), location/country (geo filters),
--   headline (display), connections_count (Authority signal weighting)
-- - ``person_signals`` rows: bag-of-signals data that's append-only and
--   per-prospect optional. Skills as a TEXT[] column would be fine but
--   keeping it shaped as a signal row aligns with the existing v2
--   ``signals`` table semantics + lets us add new signal types in v3.1
--   without ALTER TABLE every time.

BEGIN;

-- ─── 1. Persons — add high-query-frequency enrichment columns ──────────

ALTER TABLE public.persons
  ADD COLUMN IF NOT EXISTS email TEXT,
  ADD COLUMN IF NOT EXISTS email_status TEXT,
  ADD COLUMN IF NOT EXISTS headline TEXT,
  ADD COLUMN IF NOT EXISTS location_text TEXT,
  ADD COLUMN IF NOT EXISTS country_code TEXT,
  ADD COLUMN IF NOT EXISTS connections_count INT,
  ADD COLUMN IF NOT EXISTS followers_count INT,
  ADD COLUMN IF NOT EXISTS premium BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS open_to_work BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS hiring BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS registered_at TIMESTAMPTZ;

-- Optional indexes on filterable fields. Keeping them tight — partial
-- where the column is sparse to keep index size sane.
CREATE INDEX IF NOT EXISTS idx_persons_email
  ON public.persons (email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_persons_country_code
  ON public.persons (country_code) WHERE country_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_persons_email_status
  ON public.persons (email_status) WHERE email_status IS NOT NULL;

DO $constraint_check$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'persons_email_status_keyspace'
  ) THEN
    ALTER TABLE public.persons
      ADD CONSTRAINT persons_email_status_keyspace
      CHECK (email_status IS NULL OR email_status IN (
        'verified', 'guessed', 'unverified', 'unavailable'
      ));
  END IF;
END $constraint_check$;

-- ─── 2. person_signals — append-only structured signals ─────────────────

CREATE TABLE IF NOT EXISTS public.person_signals (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id       UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  account_id      UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  signal_type     TEXT NOT NULL,
  structured_value JSONB NOT NULL,
  source          TEXT NOT NULL,
  cost_cents      INTEGER NOT NULL DEFAULT 0,
  confidence      NUMERIC NOT NULL DEFAULT 1.0,
  collected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT person_signals_signal_type_keyspace
    CHECK (signal_type IN (
      -- LinkedIn-derived (Apify Full+email)
      'linkedin_skill_set',
      'linkedin_certifications',
      'linkedin_languages',
      'linkedin_publications',
      'linkedin_patents',
      'linkedin_honors_and_awards',
      'linkedin_organizations',
      -- Tier-2 enrichment (apify_posts, news, recognition)
      'linkedin_post',
      'news_mention',
      'github_profile',
      'formal_recognition',
      -- Per-company site scrapes (company_site.py)
      'company_leadership_listing',
      'press_mention'
    )),
  CONSTRAINT person_signals_confidence_range
    CHECK (confidence >= 0 AND confidence <= 1)
);

-- Idempotency: re-running the writer for the same (person, signal_type)
-- shouldn't pile up duplicate rows. Use a partial unique index keyed on
-- (person_id, signal_type) for the LinkedIn-derived rollup signals where
-- there's exactly one row per person — and let posts/news/etc. multi-row.
CREATE UNIQUE INDEX IF NOT EXISTS person_signals_one_per_rollup_signal
  ON public.person_signals (person_id, signal_type)
  WHERE signal_type IN (
    'linkedin_skill_set', 'linkedin_certifications', 'linkedin_languages',
    'linkedin_publications', 'linkedin_patents',
    'linkedin_honors_and_awards', 'linkedin_organizations',
    'github_profile'
  );

CREATE INDEX IF NOT EXISTS idx_person_signals_person
  ON public.person_signals (person_id);
CREATE INDEX IF NOT EXISTS idx_person_signals_account
  ON public.person_signals (account_id);
CREATE INDEX IF NOT EXISTS idx_person_signals_signal_type
  ON public.person_signals (signal_type);

-- ─── 3. RLS policies — match the rest of the v3 multitenant stack ──────

ALTER TABLE public.person_signals ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS person_signals_tenant_isolation ON public.person_signals;
CREATE POLICY person_signals_tenant_isolation ON public.person_signals
  FOR ALL TO authenticated
  USING (account_id IN (SELECT au.account_id FROM public.account_users au WHERE au.user_id = auth.uid()))
  WITH CHECK (account_id IN (SELECT au.account_id FROM public.account_users au WHERE au.user_id = auth.uid()));

DROP POLICY IF EXISTS person_signals_anon_default_select ON public.person_signals;
CREATE POLICY person_signals_anon_default_select ON public.person_signals
  FOR SELECT TO anon
  USING (account_id = '00000000-0000-0000-0000-000000000001'::uuid);

COMMIT;
