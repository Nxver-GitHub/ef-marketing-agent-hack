-- 2026-04-30: v3 connection-graph schema — Track E (DarkBeaver).
--
-- Adds the warm-path-only subset of the v3 entity model. Org-chart layer
-- (org_reporting_edges, org_functional_clusters, org_cluster_members,
-- person_scope_estimates, org_chart_corrections, org_signal_performance) is
-- intentionally deferred — not on the YC-demo critical path.
--
-- Pure-additive: no ALTER on v2 tables (prospects, signals, scores,
-- signal_weights, scoring_runs). v2 paths keep working untouched. ETL from
-- prospects → persons/companies/employment_periods is a separate migration
-- (Track F, 20260430_v3_backfill.sql).
--
-- 7 tables added, in dependency order:
--   1. companies              — canonical company record
--   2. persons                — canonical person record (FK → companies)
--   3. employment_periods     — job history (FK → persons, companies)
--   4. patents                — USPTO patent records (FK → companies)
--   5. patent_inventors       — junction (FK → patents, persons)
--   6. person_connections     — pre-materialized warm-path graph (FK → persons)
--   7. connection_evidence    — what supports each connection (loose FKs)
--
-- Enforces CLAUDE.md architectural decisions and CONTRACTS.md Contract 7
-- (person_connections invariants + warm-path BFS query contract):
--   D1: person_connections has CHECK (person_a_id < person_b_id).
--   D5: connection_evidence.raw_uri stores S3 URI, NOT raw blobs;
--       structured_value is JSONB and is application-capped at 4KB.
--   D7: person_connections.computed_strength is the indexed strength column;
--       BFS reads it directly, never computes on the fly.
-- See: CONTRACTS.md → Contract 7 for the bidirectional read pattern
--      (WHERE person_a_id = $id OR person_b_id = $id) and the no-on-the-fly
--      strength rule downstream BFS code must follow.
--
-- Functional-domain taxonomy (9 keys + 'unknown' fallback) per CLAUDE.md
-- "Functional Domain Taxonomy" section is enforced via CHECK constraint on
-- both persons.current_functional_domain and employment_periods.functional_domain.
--
-- Connection-type taxonomy matches CLAUDE.md STRENGTH_TABLE keys exactly.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. companies — canonical company record.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.companies (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_name              TEXT NOT NULL,
  name_variants               TEXT[] NOT NULL DEFAULT '{}',
  domains                     TEXT[] NOT NULL DEFAULT '{}',
  industry                    TEXT,
  hq_country                  TEXT,
  employee_count_estimate     INTEGER,
  -- Org-chart build state (org-chart layer reads these even though the
  -- org_chart tables themselves arrive in a later migration):
  org_chart_confidence        NUMERIC,
  org_chart_last_built        TIMESTAMPTZ,
  org_chart_signal_count      INTEGER NOT NULL DEFAULT 0,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_companies_canonical_name
  ON public.companies (canonical_name);
CREATE INDEX IF NOT EXISTS idx_companies_domains_gin
  ON public.companies USING GIN (domains);
CREATE INDEX IF NOT EXISTS idx_companies_name_variants_gin
  ON public.companies USING GIN (name_variants);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. persons — canonical person record. One row per real human.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.persons (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_name              TEXT NOT NULL,
  name_variants               TEXT[] NOT NULL DEFAULT '{}',
  linkedin_url                TEXT,
  orcid                       TEXT,
  uspto_inventor_id           TEXT,
  current_company_id          UUID REFERENCES public.companies(id) ON DELETE SET NULL,
  current_title               TEXT,
  -- Seniority taxonomy (CLAUDE.md "Seniority Taxonomy"): numeric 0-100.
  current_seniority_score     SMALLINT,
  -- Functional-domain taxonomy (CLAUDE.md "Functional Domain Taxonomy").
  current_functional_domain   TEXT,
  -- Enrichment tier 0-3 (CLAUDE.md "Core Entities").
  enrichment_tier             SMALLINT NOT NULL DEFAULT 0,
  -- Blocking keys for entity-resolution pipelines.
  blocking_keys               TEXT[] NOT NULL DEFAULT '{}',
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT persons_seniority_score_range
    CHECK (current_seniority_score IS NULL OR (current_seniority_score >= 0 AND current_seniority_score <= 100)),
  CONSTRAINT persons_enrichment_tier_range
    CHECK (enrichment_tier >= 0 AND enrichment_tier <= 3),
  CONSTRAINT persons_functional_domain_valid
    CHECK (current_functional_domain IS NULL OR current_functional_domain IN (
      'hardware_engineering',
      'software_engineering',
      'product_management',
      'manufacturing_ops',
      'sales_marketing',
      'research',
      'finance_legal',
      'people_ops',
      'general_management',
      'unknown'
    ))
);

CREATE INDEX IF NOT EXISTS idx_persons_canonical_name
  ON public.persons (canonical_name);
CREATE INDEX IF NOT EXISTS idx_persons_current_company
  ON public.persons (current_company_id);
CREATE INDEX IF NOT EXISTS idx_persons_linkedin_url
  ON public.persons (linkedin_url) WHERE linkedin_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_persons_uspto_inventor_id
  ON public.persons (uspto_inventor_id) WHERE uspto_inventor_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_persons_orcid
  ON public.persons (orcid) WHERE orcid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_persons_blocking_keys_gin
  ON public.persons USING GIN (blocking_keys);
CREATE INDEX IF NOT EXISTS idx_persons_name_variants_gin
  ON public.persons USING GIN (name_variants);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. employment_periods — every job a person has held.
--    Backbone of warm-path (career_overlap_*) and org-chart features.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.employment_periods (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id                   UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  company_id                  UUID NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  title                       TEXT,
  functional_domain           TEXT,
  seniority_score             SMALLINT,
  start_year                  SMALLINT,
  end_year                    SMALLINT,
  is_current                  BOOLEAN NOT NULL DEFAULT FALSE,
  inferred_team               TEXT,
  inferred_team_confidence    NUMERIC,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT employment_periods_seniority_score_range
    CHECK (seniority_score IS NULL OR (seniority_score >= 0 AND seniority_score <= 100)),
  CONSTRAINT employment_periods_year_order
    CHECK (start_year IS NULL OR end_year IS NULL OR end_year >= start_year),
  CONSTRAINT employment_periods_team_confidence_range
    CHECK (inferred_team_confidence IS NULL OR (inferred_team_confidence >= 0 AND inferred_team_confidence <= 1)),
  CONSTRAINT employment_periods_functional_domain_valid
    CHECK (functional_domain IS NULL OR functional_domain IN (
      'hardware_engineering',
      'software_engineering',
      'product_management',
      'manufacturing_ops',
      'sales_marketing',
      'research',
      'finance_legal',
      'people_ops',
      'general_management',
      'unknown'
    ))
);

CREATE INDEX IF NOT EXISTS idx_employment_periods_person
  ON public.employment_periods (person_id);
CREATE INDEX IF NOT EXISTS idx_employment_periods_company
  ON public.employment_periods (company_id);
-- Composite index for the career-overlap SQL (CLAUDE.md "Connection Priority
-- for YC Demo"): joins on company_id and ranges over start_year/end_year.
CREATE INDEX IF NOT EXISTS idx_employment_periods_company_years
  ON public.employment_periods (company_id, start_year, end_year);
CREATE INDEX IF NOT EXISTS idx_employment_periods_current
  ON public.employment_periods (person_id) WHERE is_current = TRUE;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. patents — USPTO patent records.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.patents (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  patent_number               TEXT NOT NULL UNIQUE,
  title                       TEXT,
  abstract                    TEXT,
  filing_date                 DATE,
  grant_date                  DATE,
  assignee_company_id         UUID REFERENCES public.companies(id) ON DELETE SET NULL,
  assignee_organization_raw   TEXT,
  cpc_codes                   TEXT[] NOT NULL DEFAULT '{}',
  citation_count              INTEGER NOT NULL DEFAULT 0,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_patents_assignee_company
  ON public.patents (assignee_company_id);
CREATE INDEX IF NOT EXISTS idx_patents_grant_date
  ON public.patents (grant_date DESC);
CREATE INDEX IF NOT EXISTS idx_patents_cpc_codes_gin
  ON public.patents USING GIN (cpc_codes);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. patent_inventors — junction: patent_id + person_id.
--    Source of patent_co_inventor connections.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.patent_inventors (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  patent_id                   UUID NOT NULL REFERENCES public.patents(id) ON DELETE CASCADE,
  person_id                   UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  -- Position in the inventor list (1 = first inventor). Useful for relevance
  -- weighting later; first-inventor co-invention is stronger signal.
  inventor_sequence           SMALLINT,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (patent_id, person_id)
);

CREATE INDEX IF NOT EXISTS idx_patent_inventors_person
  ON public.patent_inventors (person_id);
CREATE INDEX IF NOT EXISTS idx_patent_inventors_patent
  ON public.patent_inventors (patent_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. person_connections — pre-materialized warm-path graph.
--    Decision 1: person_a_id < person_b_id always (no directional edges).
--    Decision 7: warm-path BFS reads computed_strength directly; never on-the-fly.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.person_connections (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  person_a_id                 UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  person_b_id                 UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  -- connection_type values match CLAUDE.md STRENGTH_TABLE keys exactly.
  connection_type             TEXT NOT NULL,
  base_strength               NUMERIC NOT NULL,
  recency_factor              NUMERIC NOT NULL DEFAULT 1.0,
  frequency_factor            NUMERIC NOT NULL DEFAULT 1.0,
  corroboration_factor        NUMERIC NOT NULL DEFAULT 1.0,
  -- The indexed strength column. BFS reads this; do not compute on the fly.
  computed_strength           NUMERIC NOT NULL,
  last_active_year            SMALLINT,
  corroboration_count         INTEGER NOT NULL DEFAULT 1,
  source_type_count           INTEGER NOT NULL DEFAULT 1,
  evidence_ids                UUID[] NOT NULL DEFAULT '{}',
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Decision 1 — enforced.
  CONSTRAINT person_connections_pair_order
    CHECK (person_a_id < person_b_id),
  -- Strength values are bounded [0, 0.99] per CLAUDE.md formula.
  CONSTRAINT person_connections_computed_strength_range
    CHECK (computed_strength >= 0 AND computed_strength <= 0.99),
  CONSTRAINT person_connections_base_strength_range
    CHECK (base_strength >= 0 AND base_strength <= 1.0),
  CONSTRAINT person_connections_factor_nonneg
    CHECK (recency_factor >= 0 AND frequency_factor >= 0 AND corroboration_factor >= 0),
  -- Connection-type taxonomy must match STRENGTH_TABLE keys.
  CONSTRAINT person_connections_type_valid
    CHECK (connection_type IN (
      'patent_co_inventor',
      'same_phd_advisor',
      'co_board_member',
      'academic_co_author_multi',
      'academic_co_author_single',
      'career_overlap_same_team',
      'standards_committee_peer',
      'conference_co_presenter',
      'co_investor',
      'career_overlap_same_domain',
      'career_overlap_general',
      'alumni_network',
      'conference_co_attendee'
    )),
  -- One row per (a, b, type) pair — corroboration accumulates on the same row.
  UNIQUE (person_a_id, person_b_id, connection_type)
);

-- BFS read pattern: WHERE person_a_id = $id OR person_b_id = $id ORDER BY
-- computed_strength DESC. Index both directions + the strength sort.
CREATE INDEX IF NOT EXISTS idx_person_connections_a_strength
  ON public.person_connections (person_a_id, computed_strength DESC);
CREATE INDEX IF NOT EXISTS idx_person_connections_b_strength
  ON public.person_connections (person_b_id, computed_strength DESC);
CREATE INDEX IF NOT EXISTS idx_person_connections_strength
  ON public.person_connections (computed_strength DESC);
CREATE INDEX IF NOT EXISTS idx_person_connections_type
  ON public.person_connections (connection_type);
CREATE INDEX IF NOT EXISTS idx_person_connections_evidence_gin
  ON public.person_connections USING GIN (evidence_ids);

-- ─────────────────────────────────────────────────────────────────────────────
-- 7. connection_evidence — supporting documentary evidence for each connection.
--    Decision 5: raw_uri points at S3; structured_value JSONB ≤ 4KB.
--    The 4KB cap is application-enforced (Postgres has no native column-byte
--    cap on JSONB), but we add a CHECK on length(structured_value::text) as a
--    defensive guard.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.connection_evidence (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Source-type taxonomy: 'uspto', 'semantic_scholar', 'standards_committee',
  -- 'conference_program', 'employment_overlap', 'phd_advisor_record'.
  source_type                 TEXT NOT NULL,
  source_url                  TEXT,
  source_id                   TEXT,
  extracted_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- S3 URI for raw API blob (Decision 5). Raw responses are 50-500KB and
  -- belong outside Postgres.
  raw_uri                     TEXT,
  -- Structured subset (4KB cap enforced via CHECK below).
  structured_value            JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- Loose foreign keys to the entities the evidence backs. All nullable —
  -- a single row may reference any subset of these.
  patent_id                   UUID REFERENCES public.patents(id) ON DELETE SET NULL,
  -- paper_id / event_id intentionally omitted; tables ship in a later migration.
  CONSTRAINT connection_evidence_structured_value_size
    CHECK (length(structured_value::text) <= 4096),
  CONSTRAINT connection_evidence_source_type_valid
    CHECK (source_type IN (
      'uspto',
      'semantic_scholar',
      'standards_committee',
      'conference_program',
      'employment_overlap',
      'phd_advisor_record',
      'sec_filing',
      'press_release',
      'github_org',
      'crunchbase',
      'manual'
    ))
);

CREATE INDEX IF NOT EXISTS idx_connection_evidence_source
  ON public.connection_evidence (source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_connection_evidence_patent
  ON public.connection_evidence (patent_id) WHERE patent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_connection_evidence_structured_gin
  ON public.connection_evidence USING GIN (structured_value);

-- ─────────────────────────────────────────────────────────────────────────────
-- updated_at triggers — keep persons.updated_at, companies.updated_at,
-- person_connections.updated_at fresh on UPDATE.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_persons_touch_updated_at ON public.persons;
CREATE TRIGGER trg_persons_touch_updated_at
  BEFORE UPDATE ON public.persons
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

DROP TRIGGER IF EXISTS trg_companies_touch_updated_at ON public.companies;
CREATE TRIGGER trg_companies_touch_updated_at
  BEFORE UPDATE ON public.companies
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

DROP TRIGGER IF EXISTS trg_person_connections_touch_updated_at ON public.person_connections;
CREATE TRIGGER trg_person_connections_touch_updated_at
  BEFORE UPDATE ON public.person_connections
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

COMMIT;
