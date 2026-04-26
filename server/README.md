# Credence backend (`server/`)

The layer between Surya's scrapers and Abhinav's frontend. Owns enrichment,
scoring, search, and the chat proxy. Reads/writes the same Supabase database
the frontend hits via `supabase-js`.

## Stack

- **FastAPI** + **asyncpg** + **Pydantic v2** (Python 3.12, managed by `uv`)
- **OpenAI Python SDK** pointed at Z.AI (`https://api.z.ai/api/paas/v4`, model `glm-4.6`)

## Setup

```bash
# One-time (Mac / Linux)
curl -LsSf https://astral.sh/uv/install.sh | sh

cd server/
uv sync                          # creates .venv/ and installs deps
uv run python scripts/apply_migrations.py   # creates prospects_enriched view
```

`.env.local` at the repo root supplies `DATABASE_URL`, `ZAI_API_KEY`,
`ZAI_BASE_URL`. The server reads from there; no separate env file needed.

## Run

```bash
uv run uvicorn credence.api:app --reload --port 8000
```

Visit `http://localhost:8000/docs` for the OpenAPI / Swagger UI.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/health`                  | Basic liveness |
| GET  | `/health/db`               | DB connectivity check |
| GET  | `/focus?q=…`               | Fuzzy match across people / companies / industries |
| GET  | `/search?company=…&min_score=…&limit=…` | Filter prospects, sorted by score |
| GET  | `/prospect/{id}`           | Rich bundle: identity + scores + top signals |
| GET  | `/neighborhood/{id}?hops=1`| 1-hop neighbors (colleague + past_employer + education) |
| POST | `/score/{id}`              | Recompute one prospect's score |
| POST | `/chat`                    | Z.AI proxy with full tool loop |

## Workers / one-shots

```bash
# Recompute scores for prospects whose newest signal is newer than their
# newest score (incremental — fast).
uv run python scripts/score_all.py

# Force-rescore everyone (after a signal_weights change).
uv run python scripts/score_all.py --all

# Limit for sanity-checks before a full pass.
uv run python scripts/score_all.py --all --limit 100
```

Throughput is ~14 prospects/s with concurrency 16; full pass on 10k prospects
takes about 13 minutes.

## File map

```
server/
├── pyproject.toml              # deps + ruff + pytest config
├── credence/
│   ├── api.py                  # FastAPI app + route handlers
│   ├── chat.py                 # Z.AI client + tool dispatcher + system prompt
│   ├── search.py               # focus_node / filter / explain / neighborhood SQL
│   ├── score.py                # pure scoring math (port of mockStore.computeScore)
│   ├── score_runner.py         # DB-backed wrapper around score.py
│   ├── models.py               # Pydantic shapes for chat + graph + domain
│   ├── db.py                   # asyncpg pool + JSONB codec
│   └── config.py               # env loader (Pydantic Settings)
├── migrations/
│   └── 001_prospects_enriched.sql   # rolls signals -> per-prospect arrays
├── scripts/
│   ├── apply_migrations.py
│   └── score_all.py
└── tests/                      # (TODO)
```

## Frontend integration

`src/lib/agent.ts` is now a thin client over `POST /chat`. Set `VITE_API_URL`
in `.env.local` to point at the backend (defaults to `http://localhost:8000`).
The Z.AI key no longer ships to the browser.

## Adding a new tool

1. Implement the SQL/compute in `credence/search.py` (or wherever it belongs).
2. Add the JSON-schema entry to `TOOL_SCHEMAS` in `credence/chat.py`.
3. Add the dispatch branch in `_dispatch()` in the same file.
4. (Frontend) If the tool result should mutate the canvas, extend
   `applyToolResult()` in `src/lib/agent.ts`.

## Adding a migration

```bash
# Drop a new file under migrations/ with a higher numeric prefix:
echo "ALTER VIEW prospects_enriched ..." > server/migrations/002_whatever.sql

uv run python scripts/apply_migrations.py
```

Idempotent — re-running is a no-op (use `CREATE OR REPLACE`, `IF NOT EXISTS`).

## Roadmap

- Real Apify wiring → already in progress (Surya).
- pgvector embeddings on `bio` / `news_mention` for semantic search.
- SSE streaming for `/chat` so the frontend renders tokens as they arrive.
- Materialize `prospects_enriched` once row-counts justify it (currently
  ~80ms scan; promote when > 300ms).
- Auth (currently a single anon role; everyone reads everything).
