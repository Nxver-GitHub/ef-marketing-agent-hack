-- 2026-04-30: Wave 6 — auto-create an account + membership on user signup
-- (LavenderPrairie). Companion to multitenant RLS (`20260430_v3_multitenant_rls.sql`).
--
-- Without this trigger, fresh signups land an `auth.users` row but no
-- `account_users` membership → AccountProvider's resolution finds no
-- account → user gets stuck on the "no_account" onboarding gap. With it,
-- signups land:
--   1. A fresh `accounts` row (display_name = email, slug = email-local + first 8 chars of uuid)
--   2. An `account_users` row (role: owner)
--   3. A default `account_settings` row (zero budgets — admin sets later)
--
-- The trigger runs with SECURITY DEFINER so it bypasses RLS on the inserts;
-- it's owned by a privileged role (the project's postgres role here).
--
-- Idempotent: re-running drops + recreates the function and trigger.

BEGIN;

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  new_account_id UUID;
  base_slug      TEXT;
  unique_slug    TEXT;
BEGIN
  -- Slug = email local-part, lowercased + non-alphanumerics → hyphens,
  -- truncated, with the first 8 chars of the user UUID appended for
  -- uniqueness. Falls back to 'user' if the email is malformed.
  base_slug := regexp_replace(lower(split_part(NEW.email, '@', 1)), '[^a-z0-9]+', '-', 'g');
  base_slug := regexp_replace(base_slug, '^-+|-+$', '', 'g');
  base_slug := substring(base_slug FROM 1 FOR 40);
  IF base_slug = '' THEN
    base_slug := 'user';
  END IF;
  unique_slug := base_slug || '-' || substr(NEW.id::text, 1, 8);

  INSERT INTO public.accounts (display_name, slug, plan_tier)
  VALUES (NEW.email, unique_slug, 'free')
  RETURNING id INTO new_account_id;

  INSERT INTO public.account_users (account_id, user_id, role)
  VALUES (new_account_id, NEW.id, 'owner');

  INSERT INTO public.account_settings (
    account_id,
    apollo_monthly_cents,
    pdl_monthly_cents,
    parallel_monthly_cents,
    firecrawl_monthly_cents
  )
  VALUES (new_account_id, 0, 0, 0, 0);

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

COMMIT;
