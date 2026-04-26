-- prospects_enriched
-- Rolls up career_history / education / conference_talk signals into per-prospect
-- arrays in the shape the frontend graph builder (src/lib/graph.ts) expects.
--
-- Field renames vs. Surya's raw JSONB:
--   career_history.roles[].company  -> past_companies[] (distinct, excluding current employer)
--   education.degrees[]             -> education[]      (school, degree, field, year)
--   conference_talk { event, title } -> talks[]         (venue, topic, year, url)
--
-- Year parsing: strip non-digit suffixes (e.g. "1993–present" -> 1993; "annual" -> NULL).
--
-- This is a regular VIEW for v0. Promote to MATERIALIZED VIEW + REFRESH cron once
-- the prospect count justifies it (current 10k prospects executes in ~80ms).

CREATE OR REPLACE VIEW prospects_enriched AS
SELECT
  p.id,
  p.name,
  p.company,
  p.role,
  p.industry,
  p.linkedin_url,
  p.created_at,
  p.updated_at,

  -- past_companies: unique non-current employers
  COALESCE((
    SELECT array_agg(DISTINCT trim(role_obj->>'company') ORDER BY trim(role_obj->>'company'))
    FROM signals s
    CROSS JOIN LATERAL jsonb_array_elements(s.value->'roles') AS role_obj
    WHERE s.prospect_id = p.id
      AND s.signal_type = 'career_history'
      AND role_obj->>'company' IS NOT NULL
      AND trim(role_obj->>'company') <> ''
      AND lower(trim(role_obj->>'company')) <> lower(p.company)
  ), ARRAY[]::text[]) AS past_companies,

  -- education: structured array
  COALESCE((
    SELECT jsonb_agg(
      jsonb_strip_nulls(jsonb_build_object(
        'school', trim(deg->>'school'),
        'degree', NULLIF(trim(deg->>'degree'), ''),
        'field',  NULLIF(trim(deg->>'field'),  ''),
        'year',   NULL
      ))
    )
    FROM signals s
    CROSS JOIN LATERAL jsonb_array_elements(s.value->'degrees') AS deg
    WHERE s.prospect_id = p.id
      AND s.signal_type = 'education'
      AND deg->>'school' IS NOT NULL
      AND trim(deg->>'school') <> ''
  ), '[]'::jsonb) AS education,

  -- talks: rename event->venue, title->topic; coerce year to int when possible
  COALESCE((
    SELECT jsonb_agg(
      jsonb_strip_nulls(jsonb_build_object(
        'venue', trim(s.value->>'event'),
        'topic', NULLIF(trim(s.value->>'title'), ''),
        'year',  CASE
                   WHEN s.value->>'year' ~ '^[0-9]{4}'
                   THEN substring(s.value->>'year' from '^[0-9]{4}')::int
                   ELSE NULL
                 END,
        'url',   NULLIF(trim(s.value->>'url'), '')
      ))
    )
    FROM signals s
    WHERE s.prospect_id = p.id
      AND s.signal_type = 'conference_talk'
      AND s.value->>'event' IS NOT NULL
      AND trim(s.value->>'event') <> ''
  ), '[]'::jsonb) AS talks,

  -- career_history: full structured stints (unlike past_companies, includes role + years)
  COALESCE((
    SELECT jsonb_agg(
      jsonb_strip_nulls(jsonb_build_object(
        'company',    trim(role_obj->>'company'),
        'title',      NULLIF(trim(role_obj->>'role'), ''),
        'years',      NULLIF(trim(role_obj->>'years'), ''),
        'start_year', CASE
                       WHEN role_obj->>'years' ~ '^[0-9]{4}'
                       THEN substring(role_obj->>'years' from '^[0-9]{4}')::int
                       ELSE NULL
                     END
      ))
    )
    FROM signals s
    CROSS JOIN LATERAL jsonb_array_elements(s.value->'roles') AS role_obj
    WHERE s.prospect_id = p.id
      AND s.signal_type = 'career_history'
      AND role_obj->>'company' IS NOT NULL
      AND trim(role_obj->>'company') <> ''
  ), '[]'::jsonb) AS career_history

FROM prospects p;

COMMENT ON VIEW prospects_enriched IS 'Per-prospect roll-up of career_history / education / conference_talk signals. Read-only; see server/migrations/001_prospects_enriched.sql for field semantics.';
