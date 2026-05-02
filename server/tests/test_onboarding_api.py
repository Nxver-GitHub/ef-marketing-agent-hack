"""Onboarding HTTP route — start + status.

Mocks the asyncpg pool + run_onboarding_pipeline so tests stay
deterministic. No live DB. No live external APIs.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_pool():
    """Mock the asyncpg pool so route handlers can acquire a fake connection."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetchrow = AsyncMock(return_value=None)

    pool_ctx = AsyncMock()
    pool_ctx.__aenter__ = AsyncMock(return_value=conn)
    pool_ctx.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=pool_ctx)
    return pool, conn


@pytest.fixture
def app_with_mocked_pool(mock_pool):
    pool, _conn = mock_pool
    with patch("credence.onboarding.api.db.get_pool", AsyncMock(return_value=pool)):
        with patch("credence.onboarding.api.run_onboarding_pipeline", AsyncMock()):
            from credence.api import create_app
            yield create_app()


@pytest.fixture
def client(app_with_mocked_pool):
    return TestClient(app_with_mocked_pool)


# ── POST /onboarding/start (direct-call path) ─────────────────────────────


def test_start_direct_call_returns_job_id(client, mock_pool):
    _pool, conn = mock_pool
    account_id = uuid4()
    user_id = uuid4()

    resp = client.post(
        "/onboarding/start",
        json={
            "user_id": str(user_id),
            "email": "rep@nvidia.com",
            "full_name": "Sarah Kim",
            "account_id": str(account_id),
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    UUID(data["job_id"])  # must parse
    assert data["status"] == "running"
    assert data["stage"] == "identity"

    # Verify INSERT was called with the account_id
    conn.execute.assert_awaited()
    call_args = conn.execute.await_args
    assert "INSERT INTO public.onboarding_jobs" in call_args.args[0]
    assert call_args.args[2] == str(account_id)


def test_start_direct_call_400_on_missing_field(client):
    resp = client.post(
        "/onboarding/start",
        json={"email": "rep@nvidia.com"},  # missing user_id, account_id
    )
    assert resp.status_code == 400


def test_start_direct_call_400_on_invalid_json(client):
    resp = client.post(
        "/onboarding/start",
        data="not json{",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_start_full_name_optional(client, mock_pool):
    _pool, _conn = mock_pool
    resp = client.post(
        "/onboarding/start",
        json={
            "user_id": str(uuid4()),
            "email": "rep@acme.com",
            "account_id": str(uuid4()),
            # full_name omitted
        },
    )
    assert resp.status_code == 200


# ── POST /onboarding/start (webhook path) ─────────────────────────────────


def test_start_webhook_path_invalid_secret_returns_401(client):
    """When X-Webhook-Secret is present but doesn't match, return 401."""
    with patch(
        "credence.onboarding.api.get_settings",
        return_value=MagicMock(supabase_webhook_secret="real-secret"),
    ):
        resp = client.post(
            "/onboarding/start",
            json={"record": {"id": str(uuid4()), "email": "x@y.com"}},
            headers={"X-Webhook-Secret": "wrong-secret"},
        )
    assert resp.status_code == 401


def test_start_webhook_path_no_secret_configured_returns_500(client):
    """When the server hasn't configured supabase_webhook_secret, fail loudly."""
    with patch(
        "credence.onboarding.api.get_settings",
        return_value=MagicMock(supabase_webhook_secret=None),
    ):
        resp = client.post(
            "/onboarding/start",
            json={"record": {"id": str(uuid4()), "email": "x@y.com"}},
            headers={"X-Webhook-Secret": "anything"},
        )
    assert resp.status_code == 500


# ── GET /onboarding/status/:account_id ────────────────────────────────────


def test_status_no_job_returns_pending(client, mock_pool):
    """No onboarding_jobs row → status='pending', no job_id."""
    _pool, conn = mock_pool
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.get(f"/onboarding/status/{uuid4()}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["job_id"] is None
    assert data["progress"] == {}


def test_status_running_job_returned(client, mock_pool):
    _pool, conn = mock_pool
    job_id = uuid4()
    from datetime import datetime, timezone
    started = datetime(2026, 5, 2, 19, 35, tzinfo=timezone.utc)
    conn.fetchrow = AsyncMock(
        return_value={
            "id": job_id,
            "status": "running",
            "stage": "team",
            "strategy": "gtm_only",
            "progress": {"total": 150, "scraped": 67, "matched": 30, "new_persons": 37},
            "error_message": None,
            "started_at": started,
            "completed_at": None,
        }
    )

    resp = client.get(f"/onboarding/status/{uuid4()}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"
    assert data["stage"] == "team"
    assert data["strategy"] == "gtm_only"
    assert data["progress"]["scraped"] == 67
    assert data["completed_at"] is None
    assert data["started_at"] is not None
    assert UUID(data["job_id"]) == job_id


def test_status_complete_job_returned(client, mock_pool):
    _pool, conn = mock_pool
    from datetime import datetime, timezone
    completed = datetime(2026, 5, 2, 20, 35, tzinfo=timezone.utc)
    conn.fetchrow = AsyncMock(
        return_value={
            "id": uuid4(),
            "status": "done",
            "stage": "complete",
            "strategy": "all_employees",
            "progress": {"total": 300, "scraped": 300},
            "error_message": None,
            "started_at": completed,
            "completed_at": completed,
        }
    )

    resp = client.get(f"/onboarding/status/{uuid4()}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["stage"] == "complete"
    assert data["completed_at"] is not None


def test_status_invalid_account_id_returns_422(client):
    resp = client.get("/onboarding/status/not-a-uuid")
    assert resp.status_code == 422
