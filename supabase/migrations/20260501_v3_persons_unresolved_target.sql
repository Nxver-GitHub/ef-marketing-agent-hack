-- 2026-05-01: persons.is_unresolved_target — orgchart_tasks.md Task 1-C runtime gate.
--
-- ## Why this migration exists
--
-- Task 1-C in `orgchart_tasks.md` (the Decision-4 honoring stub creation) writes
-- placeholder rows into the `persons` table when `ingest_explicit_edge` receives
-- a `manager_title` that doesn't entity-resolve to a known person. The stub row
-- carries `is_unresolved_target = TRUE` so:
--
--   1. The frontend's StubNode renderer can detect it and apply the dashed-border
--      treatment (per Task 3-C).
--   2. Future entity-resolution passes can target stubs for replacement when a
--      real person is identified for the role.
--   3. Reporting / metrics queries can exclude stubs from "people enriched"
--      counts.
--
-- The flag is also referenced by `_resolve_or_create_stub()` in hierarchy.py
-- (introduced in Wave 3) — without this column, ANY explicit edge ingestion
-- with an unresolved manager_title hard-errors at runtime.
--
-- Companion to:
--   - 20260501_v3_orgchart_unresolved_targets.sql (added `is_unresolved_target`
--     to `org_reporting_edges` + optional FK columns for placeholder targets)
--
-- This migration adds the column to `persons`, completing the stub-creation
-- runtime path.
--
-- ## Apply order
--
-- Independent of the other org-chart migrations (`20260501_v3_orgchart_schema`,
-- `20260501_v3_orgchart_unresolved_targets`, `20260501_v3_orgchart_score_components`).
-- Pure-additive ALTER on the existing `persons` table; safe to apply at any time.
--
-- ## Idempotency
--
-- ADD COLUMN IF NOT EXISTS — re-runs are no-ops.

BEGIN;

ALTER TABLE public.persons
  ADD COLUMN IF NOT EXISTS is_unresolved_target BOOLEAN NOT NULL DEFAULT FALSE;

-- Partial index supporting the stub-lookup query in
-- `hierarchy._resolve_or_create_stub()`:
--   SELECT id FROM persons
--   WHERE current_company_id = $1
--     AND canonical_name = $2
--     AND is_unresolved_target = TRUE
-- The partial predicate keeps the index small — only stub rows are indexed,
-- which is the intended sparse minority in any populated dataset.
CREATE INDEX IF NOT EXISTS idx_persons_unresolved
  ON public.persons (current_company_id)
  WHERE is_unresolved_target = TRUE;

COMMIT;
