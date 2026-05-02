-- 2026-04-30: Track E.1 — UNIQUE indexes on v3 entity match keys.
--
-- Follow-up to 20260430_v3_connection_graph.sql. The original migration
-- created plain B-tree indexes on `companies.canonical_name` and
-- `persons.linkedin_url`; the backfill ETL (server/credence/backfill_v3.py)
-- worked around the lack of uniqueness with SELECT-then-INSERT inside
-- per-prospect transactions plus an in-memory dedupe cache.
--
-- That works at small scale but is brittle:
--   - Concurrent writers (F and J both running) could race and create
--     duplicate companies / persons rows.
--   - Future ON CONFLICT upserts can't use the match keys as conflict
--     targets without unique indexes to back them.
--
-- This migration:
--   1. Drops the redundant non-unique indexes from migration E.
--   2. Adds CREATE UNIQUE INDEX IF NOT EXISTS in their place.
--      `persons.linkedin_url` is partial (WHERE linkedin_url IS NOT NULL)
--      so persons without a known LinkedIn don't all collide on NULL.
--
-- Pre-flight expectation: data must already be deduplicated. Backfill F is
-- match-or-insert so it produces deduped rows; running this migration on a
-- post-F database is safe. If run on a database with duplicates, the
-- CREATE UNIQUE INDEX statement will fail loudly with the offending pair —
-- that's the right failure mode (caller dedupes, then re-applies).
--
-- Idempotent: IF EXISTS / IF NOT EXISTS guards everywhere so re-running is a
-- no-op.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- companies.canonical_name — UNIQUE (every distinct canonical name = one row).
-- ─────────────────────────────────────────────────────────────────────────────

DROP INDEX IF EXISTS public.idx_companies_canonical_name;

CREATE UNIQUE INDEX IF NOT EXISTS companies_canonical_name_key
  ON public.companies (canonical_name);

-- ─────────────────────────────────────────────────────────────────────────────
-- persons.linkedin_url — UNIQUE WHERE NOT NULL (a LinkedIn profile maps to
-- exactly one person; persons without one don't collide on NULL).
-- ─────────────────────────────────────────────────────────────────────────────

DROP INDEX IF EXISTS public.idx_persons_linkedin_url;

CREATE UNIQUE INDEX IF NOT EXISTS persons_linkedin_url_key
  ON public.persons (linkedin_url)
  WHERE linkedin_url IS NOT NULL;

COMMIT;
