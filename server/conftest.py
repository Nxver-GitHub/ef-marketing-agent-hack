"""Pytest bootstrap for the credence backend test suite.

Without this file, pytest's collection phase fails with
``ModuleNotFoundError: No module named 'credence'`` because the package lives
flat under ``server/`` (no src/ layout, ``[tool.uv] package = false`` in
pyproject.toml). pytest doesn't add the rootdir to ``sys.path`` automatically
in this layout — adding the directory containing this conftest does the trick
for every test below it (``tests/test_strength.py``, ``tests/test_backfill_v3.py``,
future ``tests/test_signals.py``, etc.).

Equivalent to setting ``pythonpath = ["."]`` in ``[tool.pytest.ini_options]``,
but kept as a conftest so we don't perturb shared backend config.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_SERVER_ROOT = Path(__file__).resolve().parent
if str(_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVER_ROOT))

# Settings has required fields that must be present at import time. Tests
# don't carry a real .env.local, so seed deterministic placeholders here.
# Tests that exercise auth re-patch SUPABASE_JWT_SECRET via a fixture.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("ZAI_API_KEY", "test-zai-key")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-jwt-secret-not-real")
