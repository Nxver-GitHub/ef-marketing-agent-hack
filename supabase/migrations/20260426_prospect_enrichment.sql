-- 2026-04-26: Denormalized enrichment columns on public.prospects.
--
-- Why denormalize? The v2 graph view (Discover) reads prospects with their
-- past employers / education / talks already shaped as small JSON arrays.
-- The source-of-truth lives in lead_scoring.evidence (one row per
-- linkedin_experience or linkedin_education item), but joining 67k+
-- evidence rows from the browser would balloon the Discover page payload.
-- scripts/etl_to_public.py aggregates the evidence per person and writes
-- these columns; the frontend reads them flat.
--
-- Shape:
--   past_companies : jsonb array of distinct company-name strings (excludes current company)
--   education      : jsonb array of {school, degree, year}
--   talks          : jsonb array of {venue, year, topic?} — empty for now;
--                    no upstream conference-talks evidence kind exists yet.

ALTER TABLE public.prospects
  ADD COLUMN IF NOT EXISTS past_companies JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS education     JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS talks         JSONB NOT NULL DEFAULT '[]'::jsonb;

-- GIN indexes — small payloads but useful for "all alumni of MIT" / "everyone
-- who worked at Stripe" filters that the graph chat copilot may want later.
CREATE INDEX IF NOT EXISTS idx_prospects_past_companies_gin
  ON public.prospects USING GIN (past_companies);
CREATE INDEX IF NOT EXISTS idx_prospects_education_gin
  ON public.prospects USING GIN (education);
