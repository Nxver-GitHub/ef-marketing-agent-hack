-- 2026-05-01: Org-chart schema (v3.1 Plan A prerequisite, SwiftElk).
--
-- ## Status
--
-- DRAFT — needs LP apply. The connection-graph migration
-- (`20260430_v3_connection_graph.sql`) explicitly deferred these tables;
-- V3_PT2.md Plan A assumes they exist. Authoring here so the org-chart
-- pipeline (clustering / hierarchy / scope / corrections / performance)
-- has a target to write to.
--
-- Six tables, in dependency order:
--   1. org_functional_clusters   — (company, functional_domain[, sub_domain]) clusters
--   2. org_cluster_members       — junction person↔cluster + IC-track flag
--   3. org_reporting_edges       — manager → report inferred edges
--   4. person_scope_estimates    — what each person owns (functions, products, regions)
--   5. org_chart_corrections     — user-submitted training signal
--   6. org_signal_performance    — per-inference-method accuracy tracker
--
-- Per CLAUDE.md L182-251 + V3_PT2.md L25-345.
-- Per Wave 6 multitenancy: every table carries account_id NOT NULL FK,
-- + RLS policies via auth.uid() ∈ account_users.
--
-- ## Decisions baked in
--
-- D2 (functional clustering before hierarchy): clusters are first-class.
--   Clusters scope hierarchy assignment — no edge across domain boundaries.
-- D3 (explicit > implicit): inference_method on every edge captures which
--   path produced it; explicit_<signal_type> wins, implicit_scoring fallback.
-- D4 (unknown nodes rendered): clusters can hold stub persons via
--   `enrichment_tier = 0` rows (existing column). Edges to those nodes are
--   marked with `is_unresolved_target=TRUE` so the UI styles them distinctly.
-- D7 (pre-materialized graph): org_reporting_edges is the read surface;
--   the optimizer + validator write to it, the UI reads via indexed lookups.
--
-- ## Not in this migration
--
-- - The actual population code (Plan A1-A8 — `server/credence/orgchart/*.py`)
-- - org_chart_validation_log (V3_PT2.md L316 — defer until first violation
--   surfaces; cheap to add later)
-- - taxonomy seed (functional_domain values) — clustering code emits these
--   at write time; CHECK constraint here just gates the keyspace.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────
-- 1. org_functional_clusters
-- ─────────────────────────────────────────────────────────────────────────
-- One row per (company_id, functional_domain[, sub_domain]) cluster. The
-- partial unique index allows one cluster per company+domain when sub_domain
-- is NULL (the common case) and one cluster per (company, domain, sub_domain)
-- when both are present (sub-cluster on inferred_team).

CREATE TABLE IF NOT EXISTS public.org_functional_clusters (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id        UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  company_id        UUID NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  functional_domain TEXT NOT NULL,
  sub_domain        TEXT,
  member_count      INTEGER NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT org_clusters_domain_keyspace
    CHECK (functional_domain IN (
      'hardware_engineering', 'software_engineering', 'product_management',
      'manufacturing_ops',    'sales_marketing',      'research',
      'finance_legal',        'people_ops',           'general_management'
    )),
  CONSTRAINT org_clusters_member_count_nonneg
    CHECK (member_count >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS org_clusters_company_domain_main
  ON public.org_functional_clusters (company_id, functional_domain)
  WHERE sub_domain IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS org_clusters_company_domain_sub
  ON public.org_functional_clusters (company_id, functional_domain, sub_domain)
  WHERE sub_domain IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_org_clusters_account_id
  ON public.org_functional_clusters (account_id);
CREATE INDEX IF NOT EXISTS idx_org_clusters_company_id
  ON public.org_functional_clusters (company_id);

-- ─────────────────────────────────────────────────────────────────────────
-- 2. org_cluster_members
-- ─────────────────────────────────────────────────────────────────────────
-- Junction person↔cluster. One row per membership; a person can belong to
-- multiple clusters across companies (career history) but only one cluster
-- per (person, company, current-or-not). is_ic_track flags the IC parallel
-- track (Distinguished Engineer / Principal Engineer / Staff Engineer / etc.)
-- so hierarchy.py never assigns them as managers of non-IC personnel.

CREATE TABLE IF NOT EXISTS public.org_cluster_members (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id               UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  cluster_id               UUID NOT NULL REFERENCES public.org_functional_clusters(id) ON DELETE CASCADE,
  person_id                UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  membership_confidence    NUMERIC NOT NULL,
  is_ic_track              BOOLEAN NOT NULL DEFAULT FALSE,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT org_cluster_members_confidence_range
    CHECK (membership_confidence > 0 AND membership_confidence <= 1),
  UNIQUE (cluster_id, person_id)
);

CREATE INDEX IF NOT EXISTS idx_cluster_members_person_id
  ON public.org_cluster_members (person_id);
CREATE INDEX IF NOT EXISTS idx_cluster_members_cluster_id
  ON public.org_cluster_members (cluster_id);
CREATE INDEX IF NOT EXISTS idx_cluster_members_account_id
  ON public.org_cluster_members (account_id);
CREATE INDEX IF NOT EXISTS idx_cluster_members_ic_track
  ON public.org_cluster_members (is_ic_track) WHERE is_ic_track = TRUE;

-- ─────────────────────────────────────────────────────────────────────────
-- 3. org_reporting_edges
-- ─────────────────────────────────────────────────────────────────────────
-- The read surface for the org chart. confidence is the local edge score;
-- path_confidence is the product-of-confidences from root (computed by
-- A8 propagation pass). inference_method records which scoring path
-- produced the edge so corrections can attribute back to it.

CREATE TABLE IF NOT EXISTS public.org_reporting_edges (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id         UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  manager_id         UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  report_id          UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  confidence         NUMERIC NOT NULL,
  path_confidence    NUMERIC,
  inference_method   TEXT NOT NULL,
  is_current         BOOLEAN NOT NULL DEFAULT TRUE,
  valid_from         DATE,
  valid_to           DATE,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT org_edges_confidence_range
    CHECK (confidence >= 0 AND confidence <= 1),
  CONSTRAINT org_edges_path_confidence_range
    CHECK (path_confidence IS NULL OR (path_confidence >= 0 AND path_confidence <= 1)),
  CONSTRAINT org_edges_no_self_report
    CHECK (manager_id <> report_id)
);

-- One current edge per report (people only have one current manager).
-- Historical (is_current=FALSE) rows are unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS org_edges_one_current_manager_per_report
  ON public.org_reporting_edges (report_id) WHERE is_current = TRUE;
CREATE INDEX IF NOT EXISTS idx_org_edges_manager_id
  ON public.org_reporting_edges (manager_id);
CREATE INDEX IF NOT EXISTS idx_org_edges_report_id
  ON public.org_reporting_edges (report_id);
CREATE INDEX IF NOT EXISTS idx_org_edges_account_id
  ON public.org_reporting_edges (account_id);
CREATE INDEX IF NOT EXISTS idx_org_edges_inference_method
  ON public.org_reporting_edges (inference_method);

-- ─────────────────────────────────────────────────────────────────────────
-- 4. person_scope_estimates
-- ─────────────────────────────────────────────────────────────────────────
-- One row per person summarizing what they own. Feeds the Authority
-- sub-score of the scoring model. team_size_min/max derive from the
-- reporting tree; the others come from clustering + patent inventor data.

CREATE TABLE IF NOT EXISTS public.person_scope_estimates (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id               UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  person_id                UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  owns_products            TEXT[] NOT NULL DEFAULT '{}',
  owns_technologies        TEXT[] NOT NULL DEFAULT '{}',
  owns_functions           TEXT[] NOT NULL DEFAULT '{}',
  owns_regions             TEXT[] NOT NULL DEFAULT '{}',
  team_size_min            INTEGER,
  team_size_max            INTEGER,
  budget_authority_level   TEXT,
  computed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT scope_team_size_consistent
    CHECK (
      team_size_min IS NULL
      OR team_size_max IS NULL
      OR team_size_min <= team_size_max
    ),
  CONSTRAINT scope_budget_level_keyspace
    CHECK (
      budget_authority_level IS NULL
      OR budget_authority_level IN ('individual', 'team', 'department', 'division', 'company')
    ),
  UNIQUE (person_id)
);

CREATE INDEX IF NOT EXISTS idx_scope_account_id
  ON public.person_scope_estimates (account_id);

-- ─────────────────────────────────────────────────────────────────────────
-- 5. org_chart_corrections
-- ─────────────────────────────────────────────────────────────────────────
-- Every row is a labeled training example. correction_type is one of the
-- four UI options (V3_PT2.md L189-192). correct_value carries either a free
-- text or a JSON-encoded structured override, depending on the type.

CREATE TABLE IF NOT EXISTS public.org_chart_corrections (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id         UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  person_a_id        UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  person_b_id        UUID REFERENCES public.persons(id) ON DELETE SET NULL,
  edge_id            UUID REFERENCES public.org_reporting_edges(id) ON DELETE SET NULL,
  correction_type    TEXT NOT NULL,
  correct_value      TEXT,
  submitted_by       TEXT NOT NULL,
  inference_method   TEXT,
  submitted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT corrections_type_keyspace
    CHECK (correction_type IN (
      'not_reports_to', 'reports_to_other', 'are_peers', 'team_wrong'
    ))
);

CREATE INDEX IF NOT EXISTS idx_corrections_account_id
  ON public.org_chart_corrections (account_id);
CREATE INDEX IF NOT EXISTS idx_corrections_edge_id
  ON public.org_chart_corrections (edge_id);
CREATE INDEX IF NOT EXISTS idx_corrections_inference_method
  ON public.org_chart_corrections (inference_method);
CREATE INDEX IF NOT EXISTS idx_corrections_submitted_at
  ON public.org_chart_corrections (submitted_at DESC);

-- ─────────────────────────────────────────────────────────────────────────
-- 6. org_signal_performance
-- ─────────────────────────────────────────────────────────────────────────
-- Per-inference-method tally. The performance.py nightly job upserts this
-- table; the optimizer.py reads it to tune component weights.

CREATE TABLE IF NOT EXISTS public.org_signal_performance (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id         UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  inference_method   TEXT NOT NULL,
  success_count      INTEGER NOT NULL DEFAULT 0,
  error_count        INTEGER NOT NULL DEFAULT 0,
  accuracy           NUMERIC,
  last_computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT perf_counts_nonneg
    CHECK (success_count >= 0 AND error_count >= 0),
  CONSTRAINT perf_accuracy_range
    CHECK (accuracy IS NULL OR (accuracy >= 0 AND accuracy <= 1)),
  UNIQUE (account_id, inference_method)
);

CREATE INDEX IF NOT EXISTS idx_perf_account_id
  ON public.org_signal_performance (account_id);

-- ─────────────────────────────────────────────────────────────────────────
-- 7. RLS — same auth.uid()-based pattern as the rest of v3
-- ─────────────────────────────────────────────────────────────────────────

ALTER TABLE public.org_functional_clusters ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.org_cluster_members      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.org_reporting_edges      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.person_scope_estimates   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.org_chart_corrections    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.org_signal_performance   ENABLE ROW LEVEL SECURITY;

DO $rls$
DECLARE
  tbl TEXT;
BEGIN
  FOREACH tbl IN ARRAY ARRAY[
    'org_functional_clusters',
    'org_cluster_members',
    'org_reporting_edges',
    'person_scope_estimates',
    'org_chart_corrections',
    'org_signal_performance'
  ]
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
    -- Bridge: anon-key read of default tenant rows, mirroring the pattern
    -- LP shipped in 20260430_v3_anon_default_tenant_read.sql so the public
    -- frontend doesn't filter to zero rows for these new tables either.
    EXECUTE format(
      'CREATE POLICY %I_anon_default_tenant ON public.%I FOR SELECT TO anon '
      'USING (account_id = ''00000000-0000-0000-0000-000000000001''::uuid)',
      tbl, tbl
    );
  END LOOP;
END
$rls$;

COMMIT;
