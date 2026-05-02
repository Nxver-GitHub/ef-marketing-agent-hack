-- 2026-05-01: Seed `institutions` with top-30 MBA / PhD / exec-ed schools.
--
-- ## Why this exists
--
-- `20260501_v3_education_conference.sql` (B1) created the `institutions`
-- table but left it empty. Per V3_PT2.md "Done Criteria for Education +
-- Conference Hidden Connections" — first item: "institutions table
-- populated with canonical names + aliases for top 30 MBA/PhD/exec-ed
-- programs." Without the seed:
--
-- - B3 education extractor's `normalize_school()` falls through to
--   fuzzy-match (rapidfuzz, threshold 0.88) for every PDL education row,
--   which is slower and less precise than alias hits.
-- - `education_overlaps.institution_id` FK has no targets, so the
--   extractor would have to insert institutions inline as a side effect.
-- - school normalization is non-deterministic across batches.
--
-- This migration is the canonical reference data — alias dictionary +
-- prestige_tier + cohort_size for the 30 institutions most likely to
-- appear in the prospect base (semiconductor / AI / enterprise GTM).
-- All entries are sourced from V3_PT2.md L523-550 plus a few
-- top-tier PhD programs that produce semiconductor talent.
--
-- Idempotent: `ON CONFLICT (canonical_name) DO UPDATE` so re-runs
-- refresh aliases / cohort_size if the canonical row already exists.
-- Institutions are global reference data (no account_id) — RLS on the
-- table is `FOR SELECT USING (true)` per B1.
--
-- ## Source notes for cohort_size
-- - HBS: 930/year, ~94/section — using 94 (the bond-relevant unit)
-- - Wharton: 870/year, ~70/cohort — using 870 (no formal sections)
-- - Stanford GSB: 420/year, no sections — using 420
-- - Top PhD programs: 30-50/year per dept — using 40
-- - Exec Ed AMP: ~170/cohort — using 170
-- - INSEAD/LBS: ~520-460/year — using mid

BEGIN;

INSERT INTO public.institutions
  (canonical_name, short_name, aliases, institution_type, prestige_tier, typical_cohort_size)
VALUES
  -- ── MBA — top 10 US (prestige_tier 1) ───────────────────────────────
  ('Harvard Business School', 'HBS',
   ARRAY['HBS','Harvard Business','Harvard MBA','Harvard Univ Business School','Harvard University Business School'],
   'mba', 1, 94),
  ('Wharton School', 'Wharton',
   ARRAY['Wharton','UPenn Wharton','Penn Wharton','University of Pennsylvania Wharton','The Wharton School','Wharton MBA'],
   'mba', 1, 870),
  ('Stanford Graduate School of Business', 'Stanford GSB',
   ARRAY['Stanford GSB','Stanford Business','Stanford University GSB','Stanford MBA','GSB'],
   'mba', 1, 420),
  ('MIT Sloan School of Management', 'MIT Sloan',
   ARRAY['MIT Sloan','Sloan MIT','Sloan School','MIT Sloan MBA','Massachusetts Institute of Technology Sloan'],
   'mba', 1, 410),
  ('Kellogg School of Management', 'Kellogg',
   ARRAY['Kellogg','Northwestern Kellogg','Northwestern University Kellogg','Kellogg MBA'],
   'mba', 1, 480),
  ('Booth School of Business', 'Booth',
   ARRAY['Booth','Chicago Booth','University of Chicago Booth','Booth MBA','UChicago Booth'],
   'mba', 1, 620),
  ('Columbia Business School', 'CBS',
   ARRAY['Columbia Business','CBS','Columbia MBA','Columbia University Business School'],
   'mba', 1, 750),
  ('Haas School of Business', 'Haas',
   ARRAY['Haas','UC Berkeley Haas','Berkeley Haas','Haas MBA','UC Berkeley Business'],
   'mba', 1, 280),
  ('Tuck School of Business', 'Tuck',
   ARRAY['Tuck','Dartmouth Tuck','Dartmouth MBA','Tuck MBA'],
   'mba', 1, 290),
  ('Fuqua School of Business', 'Fuqua',
   ARRAY['Fuqua','Duke Fuqua','Duke MBA','Fuqua MBA'],
   'mba', 1, 440),

  -- ── MBA — top international + US ext (prestige_tier 1-2) ──────────
  ('Yale School of Management', 'Yale SOM',
   ARRAY['Yale SOM','Yale MBA','Yale Management','Yale University School of Management'],
   'mba', 2, 350),
  ('NYU Stern School of Business', 'NYU Stern',
   ARRAY['NYU Stern','Stern','NYU MBA','New York University Stern'],
   'mba', 2, 340),
  ('INSEAD', 'INSEAD',
   ARRAY['INSEAD','INSEAD MBA','INSEAD Singapore','INSEAD Fontainebleau'],
   'mba', 1, 520),
  ('London Business School', 'LBS',
   ARRAY['LBS','London Business','London MBA','London Business School MBA'],
   'mba', 1, 480),
  ('IESE Business School', 'IESE',
   ARRAY['IESE','IESE MBA','IESE Barcelona','IESE Business'],
   'mba', 2, 350),

  -- ── PhD — Top CS / EE / EECS programs (prestige_tier 1) ────────────
  ('MIT EECS', 'MIT EECS',
   ARRAY['MIT Electrical Engineering','MIT Computer Science','MIT EECS PhD',
         'Massachusetts Institute of Technology EECS','MIT EE','MIT CS','MIT CSAIL'],
   'phd', 1, 40),
  ('Stanford EE', 'Stanford EE',
   ARRAY['Stanford Electrical Engineering','Stanford EE PhD','Stanford CS',
         'Stanford Computer Science','Stanford CSD','Stanford Engineering'],
   'phd', 1, 40),
  ('Carnegie Mellon SCS', 'CMU CS',
   ARRAY['CMU CS','CMU SCS','Carnegie Mellon Computer Science','Carnegie Mellon CS',
         'Carnegie Mellon SCS','CMU MLD','Carnegie Mellon Machine Learning'],
   'phd', 1, 50),
  ('UC Berkeley EECS', 'Berkeley EECS',
   ARRAY['Berkeley EECS','UC Berkeley Computer Science','Berkeley CS','UCB EECS',
         'University of California Berkeley EECS','Berkeley AI Research','BAIR'],
   'phd', 1, 45),
  ('Caltech', 'Caltech',
   ARRAY['California Institute of Technology','Caltech CS','Caltech EE','CIT'],
   'phd', 1, 30),
  ('Princeton CS', 'Princeton CS',
   ARRAY['Princeton Computer Science','Princeton CS PhD','Princeton EE',
         'Princeton University CS'],
   'phd', 1, 30),
  ('Cornell CS', 'Cornell CS',
   ARRAY['Cornell Computer Science','Cornell CS PhD','Cornell Tech','Cornell ECE'],
   'phd', 1, 35),
  ('UIUC CS', 'UIUC CS',
   ARRAY['UIUC Computer Science','UIUC CS PhD','University of Illinois CS',
         'University of Illinois Urbana-Champaign CS','Illinois CS'],
   'phd', 2, 50),
  ('Georgia Tech CS', 'GaTech CS',
   ARRAY['Georgia Tech Computer Science','GaTech CS','Georgia Institute of Technology CS',
         'Georgia Tech College of Computing'],
   'phd', 2, 50),
  ('University of Washington CSE', 'UW CSE',
   ARRAY['UW CSE','University of Washington CS','UW Allen School','Allen School',
         'Paul G. Allen School','UW Computer Science'],
   'phd', 1, 45),
  ('University of Toronto CS', 'UofT CS',
   ARRAY['UofT CS','University of Toronto Computer Science','U of T CS',
         'Toronto CS','UToronto Computer Science','Vector Institute Toronto'],
   'phd', 1, 40),
  ('ETH Zurich', 'ETH',
   ARRAY['ETH Zurich','ETH','ETHZ','Eidgenössische Technische Hochschule Zürich',
         'Swiss Federal Institute of Technology Zurich'],
   'phd', 1, 50),

  -- ── Executive Education (prestige_tier 1) ──────────────────────────
  ('Harvard Business School Advanced Management Program', 'HBS AMP',
   ARRAY['HBS AMP','HBS Executive Education','Harvard Advanced Management Program',
         'AMP Harvard','Harvard AMP'],
   'exec_ed', 1, 170),
  ('Stanford Executive Program', 'Stanford SEP',
   ARRAY['Stanford SEP','Stanford Executive Education','SEP Stanford',
         'Stanford GSB Executive Program'],
   'exec_ed', 1, 130),
  ('Wharton Advanced Management Program', 'Wharton AMP',
   ARRAY['Wharton AMP','Wharton Executive Education','UPenn Wharton AMP',
         'Wharton Advanced Management'],
   'exec_ed', 1, 130)

ON CONFLICT (canonical_name) DO UPDATE
  SET short_name          = EXCLUDED.short_name,
      aliases             = EXCLUDED.aliases,
      institution_type    = EXCLUDED.institution_type,
      prestige_tier       = EXCLUDED.prestige_tier,
      typical_cohort_size = EXCLUDED.typical_cohort_size;

COMMIT;
