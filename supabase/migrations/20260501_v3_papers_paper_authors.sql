-- 2026-05-01: papers + paper_authors schema for v3 academic_co_author edges.
--
-- ## Why this exists
--
-- v3 person_connections supports two academic_co_author edge kinds:
--   - academic_co_author_single (base 0.85)
--   - academic_co_author_multi  (base 0.90 — 3+ shared papers)
--
-- ``credence/jobs/bulk_scholar_ingest.py`` already pulls Semantic Scholar
-- and emits v2 ``signals(signal_type='academic_co_author')`` rows. To
-- materialize v3 person_connections we need a normalized ``papers``
-- + ``paper_authors`` graph that ``paper_clustering.py`` (also already
-- shipped) can pivot into ordered (person_a, person_b) pairs.
--
-- Mirrors the patents / patent_inventors schema shape exactly so the
-- clustering layer treats both citation graphs the same way.

BEGIN;

-- ─── 1. papers ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.papers (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  semantic_scholar_id   TEXT NOT NULL,
  title                 TEXT NOT NULL,
  venue                 TEXT,
  year                  INT,
  citation_count        INT NOT NULL DEFAULT 0,
  doi                   TEXT,
  url                   TEXT,
  account_id            UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT papers_year_range
    CHECK (year IS NULL OR (year >= 1900 AND year <= EXTRACT(YEAR FROM now())::int + 1))
);

-- One row per (account_id, semantic_scholar_id) — multi-tenant scoped.
CREATE UNIQUE INDEX IF NOT EXISTS papers_account_ssid_uniq
  ON public.papers (account_id, semantic_scholar_id);

CREATE INDEX IF NOT EXISTS idx_papers_account
  ON public.papers (account_id);
CREATE INDEX IF NOT EXISTS idx_papers_year
  ON public.papers (year) WHERE year IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_papers_doi
  ON public.papers (doi) WHERE doi IS NOT NULL;

-- ─── 2. paper_authors ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.paper_authors (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  paper_id          UUID NOT NULL REFERENCES public.papers(id) ON DELETE CASCADE,
  person_id         UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  author_order      INT,
  is_corresponding  BOOLEAN NOT NULL DEFAULT FALSE,
  affiliation       TEXT,
  account_id        UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotency: one row per (paper, person) pair.
CREATE UNIQUE INDEX IF NOT EXISTS paper_authors_paper_person_uniq
  ON public.paper_authors (paper_id, person_id);

-- Hot lookup paths used by paper_clustering.py:
--   1. "for this person, list all their papers"  → person_id
--   2. "for this paper, list all its authors"    → paper_id (uniq covers)
CREATE INDEX IF NOT EXISTS idx_paper_authors_person
  ON public.paper_authors (person_id);
CREATE INDEX IF NOT EXISTS idx_paper_authors_account
  ON public.paper_authors (account_id);

-- ─── 3. RLS ─────────────────────────────────────────────────────────────

ALTER TABLE public.papers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.paper_authors ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS papers_tenant_isolation ON public.papers;
CREATE POLICY papers_tenant_isolation ON public.papers
  FOR ALL TO authenticated
  USING (account_id IN (SELECT au.account_id FROM public.account_users au WHERE au.user_id = auth.uid()))
  WITH CHECK (account_id IN (SELECT au.account_id FROM public.account_users au WHERE au.user_id = auth.uid()));

DROP POLICY IF EXISTS papers_anon_default_select ON public.papers;
CREATE POLICY papers_anon_default_select ON public.papers
  FOR SELECT TO anon
  USING (account_id = '00000000-0000-0000-0000-000000000001'::uuid);

DROP POLICY IF EXISTS paper_authors_tenant_isolation ON public.paper_authors;
CREATE POLICY paper_authors_tenant_isolation ON public.paper_authors
  FOR ALL TO authenticated
  USING (account_id IN (SELECT au.account_id FROM public.account_users au WHERE au.user_id = auth.uid()))
  WITH CHECK (account_id IN (SELECT au.account_id FROM public.account_users au WHERE au.user_id = auth.uid()));

DROP POLICY IF EXISTS paper_authors_anon_default_select ON public.paper_authors;
CREATE POLICY paper_authors_anon_default_select ON public.paper_authors
  FOR SELECT TO anon
  USING (account_id = '00000000-0000-0000-0000-000000000001'::uuid);

COMMIT;
