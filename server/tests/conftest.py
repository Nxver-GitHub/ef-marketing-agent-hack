"""Test-suite-local environment defaults.

`credence.api` instantiates `Settings()` (pydantic-settings) at import time.
Without DATABASE_URL and ZAI_API_KEY, that raises ValidationError before any
test runs. Setting harmless defaults here lets tests that import
`credence.api` (e.g. `test_signals.py` via `httpx.AsyncClient(transport=ASGITransport(app))`)
collect without a real `.env` file.

These values are NOT used at runtime in the tests — the route under test
monkeypatches `_load_person_ref`, `_persist_signal`, and `_EXTRACTORS`, so
neither Postgres nor Z.AI is actually contacted. The lifespan that opens the
DB pool is also bypassed when using ASGITransport.

`os.environ.setdefault` is intentional: a developer running tests against a
real `.env` (e.g. integration tests against a sandbox DB) keeps their values.
We only fill the gap when nothing is set.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("ZAI_API_KEY", "test-zai-key")
