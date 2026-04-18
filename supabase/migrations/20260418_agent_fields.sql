-- Add multi-role and keyword arrays to prospects
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS roles TEXT[] DEFAULT '{}';
ALTER TABLE prospects ADD COLUMN IF NOT EXISTS keywords TEXT[] DEFAULT '{}';

-- Add agent_steps JSONB to scoring_runs for live Claude reasoning log
ALTER TABLE scoring_runs ADD COLUMN IF NOT EXISTS agent_steps JSONB DEFAULT '[]';
