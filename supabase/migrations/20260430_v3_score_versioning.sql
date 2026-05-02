-- 2026-04-30: Contract 6 — score versioning (LavenderPrairie, DRAFT).
--
-- ## Status
--
-- DRAFT — NOT APPLIED. Authored to close the gap SunnyRidge flagged in
-- the v3-checklist-final audit (msg 76): "Contract 6 DDL specified but
-- the score_weights / score_records migration was not authored." This
-- file is the missing migration; it is parked here for review before
-- being applied to the live Supabase project.
--
-- Decision points still open (asking via orchestration thread before apply):
--   1. FK target: `prospects(id)` (v2 working-set, what scoring touches today)
--      vs `persons(id)` (v3 connection-graph canonical). CONTRACTS.md says
--      `persons(id)`, but score_runner.py currently writes to `scores` with
--      a prospect_id. Picking `prospects(id)` here for parity with the
--      live v2 path; flag if the canonical-id rewrite happens before this
--      migration.
--   2. Migration of existing v2 `scores` rows: do we backfill into
--      `score_records` with a synthesized "v2-baseline" weight_version_id,
--      or leave them in the legacy `scores` table for historical audit?
--      Drafting WITHOUT backfill — `scores` table is preserved untouched.
--   3. /settings UI is currently single-version (live edit mutates
--      signal_weights). Switching it to insert-new-row + flip-active is
--      a frontend change tracked in a separate PR.
--
-- ## What this migration does
--
-- Adds two tables per CONTRACTS.md Contract 6:
--   - score_weights — versioned snapshots of (auth_w, authority_w, warmth_w)
--     plus per-component sub_weights JSONB. Exactly one row per tenant has
--     is_active = true (enforced by partial unique index).
--   - score_records — materialized per-prospect scores keyed by
--     (prospect_id, weight_version_id). Append-only.
--
-- Both carry account_id FK + RLS per Wave 6 multitenancy. The check
-- constraint `sum_to_one` enforces that the three top-level weights sum
-- to 1.0 within float tolerance.
--
-- Seeds one active baseline row per existing tenant using the canonical
-- 0.40/0.40/0.20 weights from CLAUDE.md "Scoring Model" so /score reads
-- have a default version_id to write against from day one.
--
-- ## Apply order
--
-- Safe to apply at any time — these are net-new tables and do not modify
-- the v2 `scores` table or v3 `signal_weights` table. RLS is enabled
-- but does not affect existing flows (no other code reads/writes these
-- tables yet). Activation of score_records writes is gated by a
-- follow-up score_runner.py change.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. score_weights — versioned weight configurations.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.score_weights (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id      UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  authenticity_w  NUMERIC NOT NULL,
  authority_w     NUMERIC NOT NULL,
  warmth_w        NUMERIC NOT NULL,
  sub_weights     JSONB   NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by      TEXT    NOT NULL DEFAULT 'system',
  is_active       BOOLEAN NOT NULL DEFAULT FALSE,

  CONSTRAINT score_weights_authenticity_range
    CHECK (authenticity_w >= 0 AND authenticity_w <= 1),
  CONSTRAINT score_weights_authority_range
    CHECK (authority_w    >= 0 AND authority_w    <= 1),
  CONSTRAINT score_weights_warmth_range
    CHECK (warmth_w       >= 0 AND warmth_w       <= 1),
  CONSTRAINT score_weights_sum_to_one
    CHECK (abs((authenticity_w + authority_w + warmth_w) - 1.0) < 0.001)
);

-- Exactly one active row per account at any time.
CREATE UNIQUE INDEX IF NOT EXISTS score_weights_one_active_per_account
  ON public.score_weights (account_id) WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_score_weights_account_id
  ON public.score_weights (account_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. score_records — materialized per-prospect scores at a given weight version.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.score_records (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id          UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  prospect_id         UUID NOT NULL REFERENCES public.prospects(id) ON DELETE CASCADE,
  weight_version_id   UUID NOT NULL REFERENCES public.score_weights(id) ON DELETE RESTRICT,
  authenticity_score  NUMERIC NOT NULL,
  authority_score     NUMERIC NOT NULL,
  warmth_score        NUMERIC NOT NULL,
  overall_score       NUMERIC NOT NULL,
  falsification_note  TEXT    NOT NULL,
  computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT score_records_authenticity_range
    CHECK (authenticity_score >= 0 AND authenticity_score <= 100),
  CONSTRAINT score_records_authority_range
    CHECK (authority_score    >= 0 AND authority_score    <= 100),
  CONSTRAINT score_records_warmth_range
    CHECK (warmth_score       >= 0 AND warmth_score       <= 100),
  CONSTRAINT score_records_overall_range
    CHECK (overall_score      >= 0 AND overall_score      <= 100),
  CONSTRAINT score_records_falsification_nonempty
    CHECK (length(trim(falsification_note)) > 0),

  UNIQUE (prospect_id, weight_version_id)
);

CREATE INDEX IF NOT EXISTS idx_score_records_prospect_recent
  ON public.score_records (prospect_id, computed_at DESC);

CREATE INDEX IF NOT EXISTS idx_score_records_account_id
  ON public.score_records (account_id);

CREATE INDEX IF NOT EXISTS idx_score_records_weight_version
  ON public.score_records (weight_version_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Seed an initial active weight row per existing account.
-- ─────────────────────────────────────────────────────────────────────────────
-- CLAUDE.md "Scoring Model": OVERALL = Authenticity*0.40 + Authority*0.40 + Warmth*0.20.
-- Inserts only when the tenant doesn't already have an active row (idempotent re-run).

INSERT INTO public.score_weights
  (account_id, authenticity_w, authority_w, warmth_w, sub_weights, created_by, is_active)
SELECT
  a.id, 0.40, 0.40, 0.20, '{}'::jsonb, 'system:contract6_seed', TRUE
FROM public.accounts a
WHERE NOT EXISTS (
  SELECT 1 FROM public.score_weights sw
  WHERE sw.account_id = a.id AND sw.is_active = TRUE
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. RLS — same auth.uid()-based pattern as 20260430_v3_multitenant_rls.sql.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE public.score_weights ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.score_records ENABLE ROW LEVEL SECURITY;

DO $rls$
DECLARE
  tbl TEXT;
BEGIN
  FOREACH tbl IN ARRAY ARRAY['score_weights', 'score_records']
  LOOP
    -- SELECT
    EXECUTE format(
      'CREATE POLICY %I_tenant_select ON public.%I FOR SELECT TO authenticated '
      'USING (account_id IN (SELECT au.account_id FROM public.account_users au '
      'WHERE au.user_id = auth.uid()))',
      tbl, tbl
    );
    -- INSERT
    EXECUTE format(
      'CREATE POLICY %I_tenant_insert ON public.%I FOR INSERT TO authenticated '
      'WITH CHECK (account_id IN (SELECT au.account_id FROM public.account_users au '
      'WHERE au.user_id = auth.uid()))',
      tbl, tbl
    );
    -- UPDATE
    EXECUTE format(
      'CREATE POLICY %I_tenant_update ON public.%I FOR UPDATE TO authenticated '
      'USING (account_id IN (SELECT au.account_id FROM public.account_users au '
      'WHERE au.user_id = auth.uid())) '
      'WITH CHECK (account_id IN (SELECT au.account_id FROM public.account_users au '
      'WHERE au.user_id = auth.uid()))',
      tbl, tbl
    );
    -- DELETE
    EXECUTE format(
      'CREATE POLICY %I_tenant_delete ON public.%I FOR DELETE TO authenticated '
      'USING (account_id IN (SELECT au.account_id FROM public.account_users au '
      'WHERE au.user_id = auth.uid()))',
      tbl, tbl
    );
  END LOOP;
END
$rls$;

COMMIT;
