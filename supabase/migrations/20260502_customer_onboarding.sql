-- 2026-05-02: CUSTOMER_ONBOARDING_PLAN.md schema additions (LavenderPrairie
-- delegated this to SwiftElk in msg 274 batch A1).
--
-- Two new tables:
--   * account_team_members — the rep's company team. These rows are the
--     "connector" / source-node set for find_warm_paths BFS (Wave A5
--     adds source_person_ids to that function).
--   * onboarding_jobs — tracks the multi-stage Apify scrape pipeline
--     (identity → company → team → connections → complete). The
--     onboarding route POSTs a row in `pending`, the worker advances
--     `stage` and `progress`, the frontend polls `GET /onboarding/status`.
--
-- RLS pattern mirrors 20260501_v3_orgchart_schema.sql — full
-- SELECT/INSERT/UPDATE/DELETE policies via account_users membership +
-- service-role bypass. Plan spec only showed SELECT; I'm extending to
-- the full keyspace because every other tenant-scoped table in this
-- repo gets the full set, and onboarding writes need to come from the
-- frontend in the future (e.g. user removing a teammate).

CREATE TABLE IF NOT EXISTS public.account_team_members (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id     UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  person_id      UUID NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
  linkedin_url   TEXT,
  role           TEXT NOT NULL DEFAULT 'member'
                  CHECK (role IN ('owner', 'admin', 'member')),
  scrape_status  TEXT NOT NULL DEFAULT 'pending'
                  CHECK (scrape_status IN ('pending', 'scraping', 'done', 'error')),
  scraped_at     TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (account_id, person_id)
);

CREATE INDEX IF NOT EXISTS account_team_members_account_idx
  ON public.account_team_members (account_id);
CREATE INDEX IF NOT EXISTS account_team_members_person_idx
  ON public.account_team_members (person_id);

CREATE TABLE IF NOT EXISTS public.onboarding_jobs (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id     UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  status         TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'running', 'done', 'error')),
  stage          TEXT
                  CHECK (stage IS NULL OR stage IN (
                    'identity', 'company', 'team', 'connections', 'complete'
                  )),
  strategy       TEXT
                  CHECK (strategy IS NULL OR strategy IN (
                    'all_employees', 'gtm_only'
                  )),
  progress       JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_message  TEXT,
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS onboarding_jobs_account_idx
  ON public.onboarding_jobs (account_id);
CREATE INDEX IF NOT EXISTS onboarding_jobs_status_idx
  ON public.onboarding_jobs (status) WHERE status IN ('pending', 'running');

-- ── RLS ─────────────────────────────────────────────────────────────────────
--
-- Mirrors 20260501_v3_orgchart_schema.sql: full CRUD policies via
-- account_users membership. Service-role bypasses RLS automatically
-- (bypassrls=true on the postgres service role), so backend writers
-- (worker, webhook handler, etc.) don't need an extra policy.

ALTER TABLE public.account_team_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.onboarding_jobs      ENABLE ROW LEVEL SECURITY;

DO $rls$
DECLARE
  tbl TEXT;
BEGIN
  FOREACH tbl IN ARRAY ARRAY['account_team_members', 'onboarding_jobs'] LOOP
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
  END LOOP;
END
$rls$;

-- ── updated_at trigger for account_team_members ────────────────────────────

CREATE OR REPLACE FUNCTION public.account_team_members_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS account_team_members_updated_at_trigger
  ON public.account_team_members;
CREATE TRIGGER account_team_members_updated_at_trigger
  BEFORE UPDATE ON public.account_team_members
  FOR EACH ROW
  EXECUTE FUNCTION public.account_team_members_set_updated_at();
