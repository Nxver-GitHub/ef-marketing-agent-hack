-- 2026-05-01: standards_memberships schema for v3 standards_committee_peer edges.
--
-- ## Why this exists
--
-- v3 person_connections supports a ``standards_committee_peer`` edge
-- kind (base 0.82). Standards committee co-membership (JEDEC, IEEE-SA,
-- SEMI, Wi-Fi Alliance, 3GPP, etc.) is a strong professional-trust
-- signal — committee work involves repeated multi-year collaboration
-- with the same engineers from rival companies.
--
-- ``credence/extractors/standards.py`` exists; the bulk runner +
-- ``standards_clustering.py`` need this table to land. Without it the
-- runner has nowhere to UPSERT.
--
-- Schema mirrors ``patent_inventors`` / ``paper_authors`` shape so the
-- clustering layer treats all three citation-style graphs uniformly.

BEGIN;

-- ─── 1. standards_memberships ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.standards_memberships (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id       UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  organization    TEXT NOT NULL,            -- e.g. 'JEDEC', 'IEEE-SA', 'SEMI'
  committee       TEXT NOT NULL,            -- e.g. 'JC-42 (Memory)', 'IEEE 802.11'
  role            TEXT,                     -- e.g. 'chair', 'voting member', 'observer'
  start_year      INT,
  end_year        INT,                      -- NULL = currently active
  source_url      TEXT,                     -- where we extracted this from
  account_id      UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT standards_memberships_year_range
    CHECK (
      (start_year IS NULL OR (start_year >= 1950 AND start_year <= 2100))
      AND (end_year IS NULL OR (end_year >= 1950 AND end_year <= 2100))
      AND (start_year IS NULL OR end_year IS NULL OR end_year >= start_year)
    )
);

-- Idempotency: one row per (person, organization, committee, start_year).
-- Two stints in the same committee separated by years still produce two
-- rows because start_year differs.
CREATE UNIQUE INDEX IF NOT EXISTS standards_memberships_uniq
  ON public.standards_memberships (
    person_id, organization, committee, COALESCE(start_year, 0)
  );

-- Hot lookup paths used by standards_clustering.py:
--   1. "find all members of committee X"             → (organization, committee)
--   2. "find all committees this person sits on"     → person_id
CREATE INDEX IF NOT EXISTS idx_standards_memberships_committee
  ON public.standards_memberships (organization, committee);
CREATE INDEX IF NOT EXISTS idx_standards_memberships_person
  ON public.standards_memberships (person_id);
CREATE INDEX IF NOT EXISTS idx_standards_memberships_account
  ON public.standards_memberships (account_id);

-- ─── 2. RLS ────────────────────────────────────────────────────────────

ALTER TABLE public.standards_memberships ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS standards_memberships_tenant_isolation ON public.standards_memberships;
CREATE POLICY standards_memberships_tenant_isolation ON public.standards_memberships
  FOR ALL TO authenticated
  USING (account_id IN (SELECT au.account_id FROM public.account_users au WHERE au.user_id = auth.uid()))
  WITH CHECK (account_id IN (SELECT au.account_id FROM public.account_users au WHERE au.user_id = auth.uid()));

DROP POLICY IF EXISTS standards_memberships_anon_default_select ON public.standards_memberships;
CREATE POLICY standards_memberships_anon_default_select ON public.standards_memberships
  FOR SELECT TO anon
  USING (account_id = '00000000-0000-0000-0000-000000000001'::uuid);

COMMIT;
