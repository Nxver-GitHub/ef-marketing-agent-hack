-- 20260501_v3_persons_prospect_link.sql
--
-- Adds the persons.source_prospect_id link column + idempotent backfill.
--
-- Why: backfill_v3.upsert_person creates one persons row per enriched
-- prospect, but never recorded the originating prospect.id on the persons
-- record. Without this column, every read path that needs to translate
-- persons.id (v3 UUID space) → prospects.id (v2 UUID space) — eg. the
-- bulk_career_overlap_signals job, the Phase 3 useSupaPersonConnections
-- hook, every future v3 person_connections → frontend bridge — has no
-- mapping to traverse.
--
-- The bulk_career_overlap_signals.py runner (575 LOC, 30 unit tests, ready
-- to fire) verified the gap empirically with a safety guard: 191 persons
-- in the live employment_periods scope, 0 mappable to prospects via id-eq
-- assumption. linkedin_url JOIN workaround tested independently — only
-- 11/201 year-filled persons resolve that way (linkedin_url isn't
-- populated on past-employer-derived person rows). Ergo: this migration.
--
-- Drafted in orchestration thread msg 187. Owner: SunnyRidge (per msg 187
-- offer to ship if LavenderPrairie hadn't in 30 min — they hadn't).

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Forward link column.
-- ─────────────────────────────────────────────────────────────────────────────
--
-- ON DELETE CASCADE: when a prospect is deleted (rare), the derived person
-- row is also pruned. This matches v2's existing CASCADE pattern for
-- prospect-derived rows (see signals, scores, employment_periods, etc.).

ALTER TABLE public.persons
  ADD COLUMN IF NOT EXISTS source_prospect_id UUID
    REFERENCES public.prospects(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_persons_source_prospect_id
  ON public.persons (source_prospect_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Backfill — two-pass idempotent UPDATE.
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Both passes guard with `WHERE source_prospect_id IS NULL` so the migration
-- is idempotent: re-running picks up only newly-created persons rows that
-- haven't been resolved yet. Future backfill_v3 runs should write
-- source_prospect_id directly on insert (out of scope for this migration —
-- handled in a follow-up commit to backfill_v3.upsert_person).

-- Pass 1 — linkedin_url exact match.
-- Live probe at migration draft time: covers ~1,879 of 2,192 persons rows
-- (86%). High-precision — both sides record the same canonical URL.

UPDATE public.persons p
SET source_prospect_id = pr.id
FROM public.prospects pr
WHERE p.source_prospect_id IS NULL
  AND p.linkedin_url IS NOT NULL
  AND p.linkedin_url = pr.linkedin_url;

-- Pass 2 — name + (optional) company match.
-- Catches rows where backfill never copied linkedin_url. Common case: past-
-- employer-derived placeholder persons created from career_history role
-- entries that have only name + company. Tiebreak on company match to avoid
-- collapsing same-name persons across tenants/orgs (e.g. two different
-- "John Smith"s at different companies — only the one whose current_company
-- matches the prospect's company should resolve).
--
-- Empty-name guard prevents the rare empty-name persons row (created from a
-- malformed signal) from absorbing every empty-name prospect.

UPDATE public.persons p
SET source_prospect_id = pr.id
FROM public.prospects pr
WHERE p.source_prospect_id IS NULL
  AND lower(trim(coalesce(p.canonical_name, ''))) <> ''
  AND lower(trim(p.canonical_name)) = lower(trim(pr.name))
  AND (
    p.current_company_id IS NULL
    OR EXISTS (
      SELECT 1 FROM public.companies c
      WHERE c.id = p.current_company_id
        AND lower(trim(c.canonical_name)) = lower(trim(coalesce(pr.company, '')))
    )
  );

-- Expected post-migration state (per live probe):
--   ~1,879 persons resolved via Pass 1 (linkedin_url)
--   ~  120 additional via Pass 2 (name+company)
--   ~  150-200 unresolved (likely past-employer-derived placeholders that
--                          have only a company name + an inferred title)
--
-- The unresolved tail is acceptable for v3.1 — those persons don't have
-- enough identity to participate in person↔prospect edges anyway. A v3.2
-- backfill pass can run a fuzzy-match cleanup if the tail grows.
