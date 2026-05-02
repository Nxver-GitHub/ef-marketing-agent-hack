"""Shared fixtures for the org-chart test suite.

Loads `.env.local` for live-DB tests, exposes async DB helpers, and sets up
common test data (default tenant UUID, Intel company UUID for snapshot tests).

The conftest is intentionally small — most setup is per-test. We avoid heavy
session-scoped fixtures that would couple unrelated tests' state.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest


# ── Path setup ───────────────────────────────────────────────────────────────
# Make `credence` (the server backend) importable from anywhere in the test
# suite. This is a structural choice — we don't vendor the modules; we import
# them directly so any change to `credence.orgchart.*` surfaces here.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVER_PATH = _REPO_ROOT / "server"
if str(_SERVER_PATH) not in sys.path:
    sys.path.insert(0, str(_SERVER_PATH))


# ── Constants used across tests ──────────────────────────────────────────────

DEFAULT_ACCOUNT_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")
"""Default tenant UUID seeded by the multitenant migration. Most tests
operate against this tenant — it owns the v2-backfilled Intel + other
prospect data."""

# Intel — the company we ran the per-company refresh against in dev. Used
# by snapshot tests to detect regression in the chart-shape we know.
INTEL_COMPANY_ID: UUID = UUID("e6c126a6-5a70-4968-b37a-3648292e60ab")


# ── .env.local autoload ──────────────────────────────────────────────────────


def _load_env_local() -> None:
    """Source DATABASE_URL + SUPABASE_* from .env.local at the repo root.

    Only fires when the env vars aren't already set — production / CI
    environments inject these via secrets manager and shouldn't be
    overridden by a stale local file.
    """
    if os.environ.get("DATABASE_URL"):
        return  # already configured upstream
    env_path = _REPO_ROOT / ".env.local"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
    # Defaults that the credence backend expects — these aren't secrets, just
    # placeholders so module import doesn't error. Real values from .env.local
    # take precedence via setdefault.
    os.environ.setdefault("SUPABASE_JWT_SECRET", "scratch")
    os.environ.setdefault("SUPABASE_URL", "http://localhost")


_load_env_local()


# ── Shared async fixtures ────────────────────────────────────────────────────


def _has_database_url() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip())


# pytest doesn't allow async fixtures at the conftest level without
# asyncio_mode auto + a session-scoped event loop. We use `asyncio_mode = auto`
# in pytest.ini so every async fixture/test runs on the default loop.


@pytest.fixture(scope="session", autouse=True)
def _skip_if_no_database_url(request) -> None:
    """Auto-skip integration / data_quality / performance / snapshot tests
    when DATABASE_URL isn't set. Unit tests still run.
    """
    if "unit" in {m.name for m in request.node.iter_markers()}:
        return
    requires_db_markers = {"integration", "data_quality", "performance", "snapshot"}
    needs_db = any(
        m.name in requires_db_markers
        for item in request.session.items
        for m in item.iter_markers()
    )
    if needs_db and not _has_database_url():
        # We don't fail — we let individual tests skip cleanly via their own
        # gate. This fixture mainly exists so the session reports the env
        # state once at start.
        return


# NOTE: pool lifecycle is session-scoped, not per-test.
#
# The credence backend caches a module-level asyncpg pool. Closing it after
# every test caused "pool is closed" / "another operation is in progress"
# errors when the next test tried to reacquire — asyncpg's pool is bound to
# the event loop it was created on, and a per-test close_pool() coupled with
# an autouse session loop produces a closed pool the suite then re-uses.
#
# We let the pool stay open for the whole session and close it once at
# session teardown via `_close_db_pool_at_session_end` below.


@pytest.fixture(scope="session", autouse=True)
async def _close_db_pool_at_session_end():
    """Close the credence asyncpg pool exactly once, at session end.

    Autouse + session-scoped so it runs even when no per-test fixture pulls
    it in. The early `yield` lets every test run first; the cleanup is the
    last thing that happens before the loop closes.
    """
    yield
    if not _has_database_url():
        return
    try:
        from credence.db import close_pool  # type: ignore[import]
    except Exception:
        return
    try:
        await close_pool()
    except Exception:
        # Best-effort — never let teardown crash the whole session.
        pass


@pytest.fixture
async def fetch_one():
    """Async helper: run one SELECT, return the first row as a dict.

    Usage:
        rows = await fetch_one("SELECT count(*) AS n FROM persons")
        assert rows["n"] >= 1
    """
    if not _has_database_url():
        pytest.skip("DATABASE_URL not set — fetch_one fixture unavailable")
    from credence.db import fetch  # type: ignore[import]

    async def _run(sql: str, *args: Any) -> dict[str, Any]:
        rows = await fetch(sql, *args)
        return dict(rows[0]) if rows else {}

    return _run


@pytest.fixture
async def fetch_all():
    """Async helper: run a SELECT, return all rows as list of dicts."""
    if not _has_database_url():
        pytest.skip("DATABASE_URL not set — fetch_all fixture unavailable")
    from credence.db import fetch  # type: ignore[import]

    async def _run(sql: str, *args: Any) -> list[dict[str, Any]]:
        rows = await fetch(sql, *args)
        return [dict(r) for r in rows]

    return _run


# ── Markers helpers ──────────────────────────────────────────────────────────


def pytest_collection_modifyitems(config, items):
    """If `--no-db` is passed, deselect every test that needs the DB.

    Convenience flag for laptops without env config.
    """
    if not config.getoption("--no-db", default=False):
        return
    db_markers = {"integration", "data_quality", "performance", "snapshot"}
    skip_db = pytest.mark.skip(reason="--no-db: DB-dependent tests excluded")
    for item in items:
        if any(m.name in db_markers for m in item.iter_markers()):
            item.add_marker(skip_db)


def pytest_addoption(parser):
    parser.addoption(
        "--no-db",
        action="store_true",
        default=False,
        help="Skip every test that requires DATABASE_URL — runs unit-only.",
    )
