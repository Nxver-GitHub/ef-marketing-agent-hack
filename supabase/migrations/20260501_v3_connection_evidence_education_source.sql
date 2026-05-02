-- 20260501_v3_connection_evidence_education_source.sql
--
-- Extends the connection_evidence_source_type_valid CHECK constraint to
-- include 'education_overlap', which is the source_type written by
-- education_cohort_clustering.py.
--
-- The original constraint (20260430_v3_connection_graph.sql) was defined
-- before the education clustering job and omitted this value. Postgres
-- requires dropping and re-adding CHECK constraints; no data migration
-- needed because connection_evidence has 0 rows at the time of this
-- migration (education_cohort_clustering.py hasn't run yet).

BEGIN;

ALTER TABLE public.connection_evidence
    DROP CONSTRAINT IF EXISTS connection_evidence_source_type_valid;

ALTER TABLE public.connection_evidence
    ADD CONSTRAINT connection_evidence_source_type_valid
    CHECK (source_type IN (
        'uspto',
        'semantic_scholar',
        'standards_committee',
        'conference_program',
        'employment_overlap',
        'education_overlap',
        'phd_advisor_record',
        'sec_filing',
        'press_release',
        'github_org',
        'crunchbase',
        'manual'
    ));

COMMIT;
