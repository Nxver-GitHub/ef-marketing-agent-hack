-- Credence schema for Supabase / PostgreSQL
-- Run via: supabase db push  or paste into the Supabase SQL editor

-- prospects
CREATE TABLE IF NOT EXISTS prospects (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  company     TEXT NOT NULL,
  role        TEXT NOT NULL,
  industry    TEXT NOT NULL,
  linkedin_url TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_prospects_industry ON prospects(industry);
CREATE INDEX IF NOT EXISTS idx_prospects_company  ON prospects(company);

-- signals — one row per source per signal type per prospect
CREATE TABLE IF NOT EXISTS signals (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  prospect_id  UUID NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
  source       TEXT NOT NULL,   -- e.g. "linkedin_profile", "uspto", "github"
  signal_type  TEXT NOT NULL,   -- e.g. "tenure_years", "patent_count"
  value        JSONB NOT NULL,
  raw_data     JSONB,
  weight       NUMERIC NOT NULL DEFAULT 1.0,
  confidence   NUMERIC NOT NULL,          -- 0..1
  collected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signals_prospect_id     ON signals(prospect_id);
CREATE INDEX IF NOT EXISTS idx_signals_prospect_source ON signals(prospect_id, source);

-- scores — one row per scoring run
CREATE TABLE IF NOT EXISTS scores (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  prospect_id         UUID NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
  authenticity_score  NUMERIC NOT NULL,
  authority_score     NUMERIC NOT NULL,
  warmth_score        NUMERIC NOT NULL,
  overall_score       NUMERIC NOT NULL,
  falsification_notes TEXT[] NOT NULL DEFAULT '{}',
  computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scores_prospect_id ON scores(prospect_id);

-- signal_weights — runtime-tunable from /settings
CREATE TABLE IF NOT EXISTS signal_weights (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_type         TEXT NOT NULL UNIQUE,
  authenticity_weight NUMERIC NOT NULL,
  authority_weight    NUMERIC NOT NULL,
  warmth_weight       NUMERIC NOT NULL
);

-- scoring_runs — live progress for /validate UX
CREATE TABLE IF NOT EXISTS scoring_runs (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  prospect_id        UUID NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
  status             TEXT NOT NULL CHECK (status IN ('pending','running','complete','error')),
  sources_attempted  TEXT[] NOT NULL DEFAULT '{}',
  sources_succeeded  TEXT[] NOT NULL DEFAULT '{}',
  current_source     TEXT,
  error_log          TEXT,
  started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_scoring_runs_prospect_id ON scoring_runs(prospect_id);

-- Default signal weights (mirrors DEFAULT_SIGNAL_WEIGHTS in the former convex/constants.ts)
INSERT INTO signal_weights (signal_type, authenticity_weight, authority_weight, warmth_weight)
VALUES
  ('tenure_years',       0.8, 0.6, 0.0),
  ('post_activity',      0.5, 0.2, 0.3),
  ('recommendations',    0.7, 0.3, 0.2),
  ('patent_count',       0.6, 0.9, 0.0),
  ('patent_citations',   0.4, 0.8, 0.0),
  ('github_commits',     0.5, 0.6, 0.1),
  ('conference_talks',   0.6, 0.8, 0.2),
  ('hiring_signal',      0.2, 0.7, 0.1),
  ('mutual_connections', 0.1, 0.1, 0.9),
  ('crunchbase_role',    0.6, 0.7, 0.0)
ON CONFLICT (signal_type) DO NOTHING;
