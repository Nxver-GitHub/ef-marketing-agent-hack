-- 2026-04-30: Wave 5 enrichment scaffolding (LavenderPrairie).
--
-- Adds the columns + table needed to support Contract 8 (per-prospect
-- enrichment via Apollo / PDL / Parallel / Firecrawl). Pure-additive; no
-- ALTERs touch v2 columns or v3 tables shipped in earlier migrations.
--
-- 1. prospects gains lightweight enrichment columns: email, email_status,
--    current_title (v2 had role only), last_enriched_at. These are direct
--    from Apollo's /enrich path — adding them on `prospects` rather than a
--    separate `contact_info` table keeps the read path cheap (single SELECT
--    for the prospect detail page; no JOIN).
--    Phone numbers are intentionally NOT enriched per user direction —
--    warm-intro flow ends in an email send. Re-add a `phone` column here
--    if a future workflow needs them.
-- 2. enrichment_cost_log: per-vendor invocation audit trail. Every Apollo /
--    PDL / Parallel call lands a row here with cost_cents + success +
--    cache_hit so $$ monitoring is a single SELECT. Cascades on prospect
--    deletion so erasure requests don't leave orphan audit rows.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. prospects — enrichment columns
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE public.prospects
  ADD COLUMN IF NOT EXISTS email                TEXT,
  ADD COLUMN IF NOT EXISTS email_status         TEXT,
  ADD COLUMN IF NOT EXISTS current_title        TEXT,
  ADD COLUMN IF NOT EXISTS last_enriched_at     TIMESTAMPTZ;

-- email_status enum is enforced at write time by the apollo extractor; using
-- TEXT here so future vendors that report different categorical values
-- (e.g., findymail's "verified" / "risky" / "invalid") can land without
-- another ALTER. A CHECK constraint can be added later if we settle on
-- a fixed enum across vendors.

-- Partial index: only ~30-60% of prospects will have an email after Wave 5.
-- A partial B-tree on `email WHERE email IS NOT NULL` keeps the index small
-- and supports the common "find prospect by email" lookup.
CREATE INDEX IF NOT EXISTS idx_prospects_email
  ON public.prospects (email)
  WHERE email IS NOT NULL;

-- Drives the cache-freshness check in Contract 8 (skip vendor call if the
-- last enrichment is < 24h old). Partial since rows that have never been
-- enriched don't need to be in this index.
CREATE INDEX IF NOT EXISTS idx_prospects_last_enriched_at
  ON public.prospects (last_enriched_at)
  WHERE last_enriched_at IS NOT NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. enrichment_cost_log — every vendor invocation, for cost monitoring
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.enrichment_cost_log (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  prospect_id   UUID NOT NULL REFERENCES public.prospects(id) ON DELETE CASCADE,
  vendor        TEXT NOT NULL,
  -- The endpoint or task type within the vendor (e.g., "people/match" for
  -- Apollo, "task" for Parallel, "scrape" for Firecrawl). Optional; some
  -- vendors expose a single endpoint.
  endpoint      TEXT,
  cost_cents    INTEGER NOT NULL DEFAULT 0,
  -- True when the call was served from cache and no vendor request was
  -- issued. Cache hits write a row anyway so the audit trail is complete
  -- and "saved spend via cache" reports are trivial to query.
  cache_hit     BOOLEAN NOT NULL DEFAULT FALSE,
  -- True when the vendor returned usable data; false on 5xx, timeout,
  -- empty response, or post-processing rejection.
  success       BOOLEAN NOT NULL DEFAULT TRUE,
  -- Vendor-side error message when success=FALSE; capped at 1KB at write time.
  error_message TEXT,
  called_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- vendor names enforced at write time by the application; not constrained
  -- here so adding new vendors doesn't require a migration.

  CONSTRAINT enrichment_cost_log_cost_nonneg CHECK (cost_cents >= 0),
  CONSTRAINT enrichment_cost_log_error_size  CHECK (
    error_message IS NULL OR length(error_message) <= 1024
  )
);

-- Sum-by-vendor-by-day reports, the bread-and-butter cost dashboard query.
CREATE INDEX IF NOT EXISTS idx_enrichment_cost_log_vendor_called_at
  ON public.enrichment_cost_log (vendor, called_at DESC);

-- Per-prospect cost trail (used by /enrich endpoint when checking cumulative
-- spend per prospect against `max_cost_cents` cap).
CREATE INDEX IF NOT EXISTS idx_enrichment_cost_log_prospect_called_at
  ON public.enrichment_cost_log (prospect_id, called_at DESC);

-- Failed-call rate per vendor — alert dashboards rank vendors by this.
CREATE INDEX IF NOT EXISTS idx_enrichment_cost_log_failures
  ON public.enrichment_cost_log (vendor, called_at DESC)
  WHERE success = FALSE;

COMMIT;
