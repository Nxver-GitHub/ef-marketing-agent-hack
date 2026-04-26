# Hand-off: data contracts for Surya

Hi Surya — quick reference for the shapes the backend (`server/`) reads. Your
current Apify writes already line up with most of this; mark anything you want
to change.

## Where things live

- **Database**: Supabase Postgres (project `xqqjpqicukuqqgnwblib`)
- **You write to**: `prospects` (insert), `signals` (insert)
- **You don't write to**: `prospects_enriched` (view, derived from your signals),
  `scores` (computed by `server/credence/score.py`), `signal_weights`,
  `scoring_runs`. Backend owns those.

## Canonical shapes

The backend rolls these three signal types into per-prospect arrays in
[`server/migrations/001_prospects_enriched.sql`](server/migrations/001_prospects_enriched.sql).
Keep emitting them in the shapes below and the graph stays dense.

### `signal_type = 'career_history'`

```jsonc
{
  "value": {
    "roles": [
      { "company": "AMD",       "role": "Chair & CEO",         "years": "2014–present" },
      { "company": "IBM",       "role": "VP, Semiconductor R&D", "years": "1995–2007" }
    ]
  },
  "source": "web_enrichment",   // or "apify_linkedin_*", etc.
  "confidence": 0.95            // 0..1
}
```

- `roles[]` order: most recent first.
- `years` is a free-form string. The backend extracts `start_year` by regex
  (`^[0-9]{4}`); patterns like `1995–2007` or `2014–present` work as-is.
  No need to parse on your end.
- The backend de-duplicates `roles[].company` against the prospect's current
  `company` field when computing `past_companies` — so it's fine to include
  the current job in this list.

### `signal_type = 'education'`

```jsonc
{
  "value": {
    "degrees": [
      { "school": "Stanford University", "degree": "M.S.", "field": "Electrical Engineering" },
      { "school": "MIT",                 "degree": "Ph.D.", "field": "Computer Science" }
    ]
  },
  "source": "web_enrichment",
  "confidence": 0.95
}
```

- `school` is required; `degree`, `field` are optional but encouraged.
- Don't worry about graduation `year` — most data sources don't give it,
  and the frontend treats it as optional.

### `signal_type = 'conference_talk'`

```jsonc
{
  "value": {
    "event": "NVIDIA GTC",                                  // -> graph "venue"
    "year":  "2024",                                        // string OK; "annual" -> null
    "title": "Keynote on accelerated computing",            // -> graph "topic"
    "url":   "https://www.nvidia.com/gtc/keynote/"          // optional
  },
  "source": "web_enrichment",
  "confidence": 0.95
}
```

- `year` can be `"2024"`, `"2023"`, `"annual"`, etc. The backend coerces a
  leading 4-digit year and falls back to NULL otherwise.

### Numeric signal types (drive scoring)

These are the existing `signal_weights` rows and contribute to the
Authenticity / Authority / Warmth sub-scores. Scalar values, not objects:

```jsonc
{ "value": 12.5,  "signal_type": "tenure_years",     "confidence": 0.9 }
{ "value": 47,    "signal_type": "patent_count",     "confidence": 0.95 }
{ "value": 1820,  "signal_type": "patent_citations", "confidence": 0.9 }
{ "value": 312,   "signal_type": "github_commits",   "confidence": 0.85 }
{ "value": 8,     "signal_type": "conference_talks", "confidence": 0.9 }
{ "value": 6,     "signal_type": "post_activity",    "confidence": 0.7 }
{ "value": 3,     "signal_type": "recommendations",  "confidence": 0.8 }
{ "value": 1,     "signal_type": "hiring_signal",    "confidence": 0.9 }
{ "value": 47,    "signal_type": "mutual_connections","confidence": 0.7 }
{ "value": 1,     "signal_type": "crunchbase_role",  "confidence": 0.95 }
```

If you want a new numeric signal_type to count toward scoring, add a default
row to `signal_weights` (or just tell me the type and I'll add it):

```sql
INSERT INTO signal_weights (signal_type, authenticity_weight, authority_weight, warmth_weight)
VALUES ('your_new_signal', 0.5, 0.5, 0.0);
```

### Free-form descriptive signals (not scored)

`bio`, `linkedin_profile`, `social_link`, `news_mention`, `publication`,
`tech_stack`, `company_firmographic`, `exec_profile`, `ats_hiring_summary`.
Keep emitting whatever JSONB makes sense — these are surfaced by the chat
agent's `explain()` tool but don't contribute to numeric scores.

## Gotchas

- **Don't update `prospects.company` / `prospects.role`** after the canonical
  values are set unless you have higher-confidence data. The graph builder
  uses these for the primary `works_at` edge.
- **Confidence is honored**. Anything with `confidence < 0.4` gets de-emphasized
  in scoring. Don't pad confidence — better to omit a signal than ship a
  fake-confident one.
- **Idempotency**: dedupe before insert if you can. The graph treats every
  `signals` row as evidence; duplicates inflate the apparent evidence count.
  When in doubt, `(prospect_id, source, signal_type, value)` is a reasonable
  uniqueness key for the structured types.

## How to verify

```bash
# Coverage of the three graph signal types
psql "$DATABASE_URL" -c "
  SELECT signal_type, count(*) AS rows, count(DISTINCT prospect_id) AS prospects
  FROM signals
  WHERE signal_type IN ('career_history', 'education', 'conference_talk')
  GROUP BY signal_type
  ORDER BY 1;
"

# After your batch lands, ping me — I'll re-run `score_all.py --all` to
# refresh the scores table.
```

Ping me on anything ambiguous. Thanks!
