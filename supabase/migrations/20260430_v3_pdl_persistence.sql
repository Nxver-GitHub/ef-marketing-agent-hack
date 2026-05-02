-- 2026-04-30: Wave 5 P2 follow-up — PDL persistence target on prospects (LavenderPrairie).
--
-- ## Status
--
-- Closes the gap I flagged in msg 95: PDL's structured `employment_periods[]` /
-- `skills[]` come back in `EnrichResponse.records[]` but never land on the
-- prospect row, so re-fetches don't see them and the UI can't render them
-- without re-calling /enrich.
--
-- Deferred the normalized `employment_periods` table approach (would require
-- prospect→persons + company-name→companies entity resolution before any PDL
-- write could succeed). Going JSONB-on-prospects for v1 — mirrors how Apollo's
-- `current_title` lands today. Normalize in a v3.1 sweep when persons/companies
-- coverage > 80%.
--
-- ## What this adds
--
-- - `prospects.employment_periods JSONB NOT NULL DEFAULT '[]'::jsonb` — array
--   of {company_name, title, functional_domain, start_date, end_date,
--   is_current} matching the `PDLEmploymentPeriod` TypedDict in
--   `server/credence/enrichment/pdl.py`. Replaces freeform v2 `past_companies`
--   for fully-enriched prospects (v2 column preserved for legacy reads).
-- - `prospects.skills TEXT[] NOT NULL DEFAULT '{}'::text[]` — domain
--   expertise hints from PDL.
-- - `prospects.pdl_person_id TEXT` — back-reference for future PDL re-fetch /
--   reconciliation. Indexed for lookup.
--
-- No backfill — existing rows get the empty defaults. RLS already covers
-- `prospects` per `20260430_v3_multitenant_rls.sql`; new columns inherit it.

BEGIN;

ALTER TABLE public.prospects
  ADD COLUMN IF NOT EXISTS employment_periods JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS skills             TEXT[] NOT NULL DEFAULT '{}'::text[],
  ADD COLUMN IF NOT EXISTS pdl_person_id      TEXT;

CREATE INDEX IF NOT EXISTS idx_prospects_employment_periods_gin
  ON public.prospects USING GIN (employment_periods);

CREATE INDEX IF NOT EXISTS idx_prospects_skills_gin
  ON public.prospects USING GIN (skills);

CREATE INDEX IF NOT EXISTS idx_prospects_pdl_person_id
  ON public.prospects (pdl_person_id) WHERE pdl_person_id IS NOT NULL;

COMMIT;
