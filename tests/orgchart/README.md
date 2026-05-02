# Org Chart Test Suite

Long-lasting tests covering the org-chart inference pipeline end-to-end. Lives at the repo root (not under `server/tests/`) because it's cross-cutting — covers backend modules, live-DB invariants, and pipeline regressions.

## Why this suite exists

`server/tests/test_orgchart_*.py` covers per-module unit logic with monkeypatched DB. That catches *algorithmic* bugs (e.g., union-find cycle check, span-cap math). It does **not** catch:

- Drift in the live `org_reporting_edges` table (e.g., a migration accidentally drops a constraint).
- Pipeline-stage interaction bugs (e.g., A2 writes edges that A8 propagation can't traverse).
- Coverage regressions (e.g., today's pipeline produces 7,094 edges; tomorrow's silently produces 1,000).
- Performance regressions (e.g., a clustering query goes from 2s/company to 30s/company).
- Data-quality drift (e.g., a small fraction of edges acquire a non-keyspace `dominant_signal`).

This suite is the safety net for those. Designed to be re-run on every PR + nightly + before any promotion to a customer-facing environment.

## Layout

```
tests/orgchart/
├── unit/                  Pure-logic invariants (no DB; runnable on any laptop).
├── integration/           Live-DB pipeline tests (requires DATABASE_URL + RW access).
├── data_quality/          Live-DB assertions on the current materialized graph.
├── performance/           Timing budgets for the pipeline stages.
├── snapshot/              "Frozen" snapshots of known-good company charts.
├── conftest.py            Shared fixtures: env loading, db pool, cleanup.
└── pytest.ini             Markers + paths. Default to unit-only.
```

## Test categories (markers)

Every test carries one of these `pytest.mark` tags so CI can run subsets:

| Marker | What it does | DB required | Runtime |
|---|---|---|---|
| `unit` | Pure-logic invariants. No DB, no network. | No | <1s |
| `integration` | Pipeline e2e against live or staging DB. RW required. | Yes | seconds-minutes |
| `data_quality` | Read-only assertions on the current `org_reporting_edges` etc. | Yes (R) | seconds |
| `performance` | Timing budgets. Fails if a stage takes longer than spec. | Yes | minutes |
| `snapshot` | Diffs a known-good chart against current state. | Yes (R) | seconds |

## Running

### Default — unit only (fast, safe everywhere)

```bash
cd tests/orgchart
pytest -m unit
```

### Full suite (requires live DB env vars)

```bash
# Set DATABASE_URL pointing at a staging Supabase instance, NOT prod.
export DATABASE_URL=postgresql://...
export SUPABASE_JWT_SECRET=...

cd tests/orgchart
pytest -m "unit or integration or data_quality or performance or snapshot"
```

### Single category

```bash
pytest -m integration
pytest -m data_quality
pytest -m performance
```

### Convenience shell script

```bash
./run.sh unit          # unit only
./run.sh dq            # data_quality only — read-only, safe against prod
./run.sh full          # everything
./run.sh ci            # what CI runs (unit + dq, no destructive integration)
```

## Adding new tests

When the pipeline acquires a new invariant or bug class, add a test here. Pattern:

1. Pick the right folder (unit/integration/data_quality/performance).
2. Tag with the matching marker.
3. Document in the test's docstring **why** it exists — what regression it protects against.
4. If asserting against live data, pin tolerances liberally — false positives kill test trust faster than false negatives.

## What the suite does NOT cover

- Frontend rendering (covered by `e2e/` Playwright suite).
- Vendor extractor unit tests (covered by `server/tests/test_apollo.py`, etc.).
- The Wave 6 multitenancy / RLS layer (covered by `server/tests/test_auth.py`).
- The cost-log / budget plumbing (covered by `server/tests/test_budget.py`).

This suite focuses specifically on **the materialized org graph and its inference pipeline**.

## Stability policy

These tests are intended to be long-lasting. Two corollaries:

- **Tests should be skeptical of their own assertions before touching code.** A failing data-quality test usually means a real data drift, not a flaky test. Investigate the data first.
- **Tolerances and baselines are tunable but documented.** Every magic number (e.g., "≥ 30% of enriched persons should have a manager assigned") carries an inline comment explaining the rationale and a date stamp. Update the date when you change the number.

— DarkBeaver, 2026-05-01
