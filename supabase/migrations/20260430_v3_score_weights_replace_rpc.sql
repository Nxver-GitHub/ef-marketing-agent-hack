-- 2026-04-30: Contract 6 — atomic flip-then-insert on score_weights (SwiftElk).
--
-- ## Why a function and not two client-side writes
--
-- `score_weights` has a partial unique index `(account_id) WHERE is_active = TRUE`,
-- enforcing exactly-one-active-row per tenant. Saving a new sub-score mix from
-- the Settings UI must:
--   1. flip the previously active row to is_active = false
--   2. insert the new row with is_active = true
-- These two writes have to be atomic — between (1) and (2) the unique index
-- is satisfied (zero active rows is fine; the partial index only forbids
-- two), but if (1) commits without (2), the tenant has *no* active version
-- and `score_runner._resolve_active_weight_version` raises ScoreSetupError
-- on every score request until the next save.
--
-- A plpgsql function runs both writes in one implicit transaction, so a
-- failure mid-way rolls everything back.
--
-- ## Why SECURITY INVOKER (not DEFINER)
--
-- The Wave 6 RLS policies on `score_weights` enforce
-- `account_id IN (SELECT au.account_id FROM account_users au WHERE au.user_id = auth.uid())`
-- for both UPDATE and INSERT. SECURITY INVOKER preserves those checks —
-- the function still runs as the calling user, so RLS rejects cross-tenant
-- writes without any extra logic in this function. SECURITY DEFINER would
-- bypass RLS, requiring a re-implementation of the membership check inside
-- the function body and creating a second source of truth for tenancy.
--
-- ## Inputs / outputs
--
-- Returns the new row's UUID so the caller (Settings.tsx) can immediately
-- update its local state to the newly-active version_id without a re-query.

BEGIN;

CREATE OR REPLACE FUNCTION public.replace_active_score_weights(
  p_account_id      UUID,
  p_authenticity_w  NUMERIC,
  p_authority_w     NUMERIC,
  p_warmth_w        NUMERIC,
  p_created_by      TEXT DEFAULT 'user'
) RETURNS UUID
LANGUAGE plpgsql
SECURITY INVOKER
AS $func$
DECLARE
  new_id UUID;
BEGIN
  UPDATE public.score_weights
  SET is_active = FALSE
  WHERE account_id = p_account_id
    AND is_active = TRUE;

  INSERT INTO public.score_weights
    (account_id, authenticity_w, authority_w, warmth_w, sub_weights, created_by, is_active)
  VALUES
    (p_account_id, p_authenticity_w, p_authority_w, p_warmth_w, '{}'::jsonb, p_created_by, TRUE)
  RETURNING id INTO new_id;

  RETURN new_id;
END;
$func$;

GRANT EXECUTE ON FUNCTION public.replace_active_score_weights(
  UUID, NUMERIC, NUMERIC, NUMERIC, TEXT
) TO authenticated;

COMMIT;
