-- 2026-05-01: drop redundant person_connections._type_valid CHECK.
--
-- ## Why
--
-- `person_connections.connection_type` had TWO CHECK constraints:
--   1. `person_connections_type_keyspace` — wide (includes cohort kinds:
--      same_mba_cohort, same_phd_program, executive_education,
--      same_undergrad_cohort, plus all canonical kinds)
--   2. `person_connections_type_valid` — narrow (the original; predates the
--      cohort-kinds expansion)
--
-- Postgres ANDs every CHECK constraint, so the narrow constraint silently
-- rejected cohort-kind inserts even though the wide constraint allows them.
-- This blocked `education_cohort_clustering --write-v3` (and any other
-- writer that emits cohort kinds).
--
-- The wide constraint is the canonical one going forward (it's a
-- superset). Drop the narrow one.
--
-- ## Safety
--
-- Strictly relaxes the constraint surface — every value the narrow CHECK
-- previously accepted is also accepted by the wide CHECK. No existing rows
-- can become invalid; no writers that previously succeeded will start
-- failing.

BEGIN;

ALTER TABLE public.person_connections
  DROP CONSTRAINT IF EXISTS person_connections_type_valid;

COMMIT;
