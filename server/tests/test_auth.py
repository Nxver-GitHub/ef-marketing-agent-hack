"""Wave 6 M2 — tests for credence.auth (JWT verify + middleware short-circuits).

Tests are split into:
- `_decode_supabase_jwt` (signature/expiry/audience validation)
- `_demo_session` and `_service_session` (header short-circuits)
- `SessionMiddleware` end-to-end via FastAPI TestClient (exempt paths,
  401 on missing auth, 200 on demo header)

Live `account_users` DB lookup (`resolve_session` happy path) is left to an
integration test once an end-to-end Supabase fixture exists.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from jose import jwt

from credence.auth import (
    DEFAULT_ACCOUNT_ID,
    DEMO_ACCOUNT_ID,
    EXEMPT_PATH_PREFIXES,
    _decode_supabase_jwt,
    _demo_session,
    _service_session,
    install_session_middleware,
)

# Test-fixed secrets. These must match the Settings values; the fixture below
# patches the env to install them.
_TEST_SECRET = "unit-test-secret-not-real"
_TEST_AUD = "authenticated"
_TEST_ALG = "HS256"


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    """Reset Settings cache and install test JWT env vars."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _TEST_SECRET)
    monkeypatch.setenv("SUPABASE_JWT_AUDIENCE", _TEST_AUD)
    monkeypatch.setenv("SUPABASE_JWT_ALGORITHM", _TEST_ALG)
    # Other required settings — already populated by conftest, but be defensive.
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@z/db")
    monkeypatch.setenv("ZAI_API_KEY", "test-zai")
    # Drop cached Settings so the new env values are picked up.
    from credence.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _sign(payload: dict, secret: str = _TEST_SECRET, alg: str = _TEST_ALG) -> str:
    return jwt.encode(payload, secret, algorithm=alg)


# ── _decode_supabase_jwt ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_decode_jwt_happy_path():
    payload = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "aud": _TEST_AUD,
        "exp": int(time.time()) + 3600,
    }
    token = _sign(payload)
    decoded = _decode_supabase_jwt(token)
    assert decoded["sub"] == payload["sub"]
    assert decoded["aud"] == _TEST_AUD


@pytest.mark.unit
def test_decode_jwt_rejects_bad_signature():
    payload = {"sub": "u", "aud": _TEST_AUD, "exp": int(time.time()) + 3600}
    bad_token = _sign(payload, secret="different-secret")
    with pytest.raises(Exception) as exc_info:
        _decode_supabase_jwt(bad_token)
    # FastAPI HTTPException, status_code=401, detail.error == "invalid_token"
    assert getattr(exc_info.value, "status_code", None) == 401
    assert exc_info.value.detail["error"] == "invalid_token"


@pytest.mark.unit
def test_decode_jwt_rejects_expired_token():
    payload = {
        "sub": "u",
        "aud": _TEST_AUD,
        "exp": int(time.time()) - 60,  # expired 1 minute ago
    }
    token = _sign(payload)
    with pytest.raises(Exception) as exc_info:
        _decode_supabase_jwt(token)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"] == "token_expired"


@pytest.mark.unit
def test_decode_jwt_rejects_wrong_audience():
    payload = {
        "sub": "u",
        "aud": "wrong-audience",
        "exp": int(time.time()) + 3600,
    }
    token = _sign(payload)
    with pytest.raises(Exception) as exc_info:
        _decode_supabase_jwt(token)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"] == "wrong_audience"


@pytest.mark.unit
def test_decode_jwt_rejects_malformed_token():
    with pytest.raises(Exception) as exc_info:
        _decode_supabase_jwt("not.a.valid.jwt.token")
    assert exc_info.value.status_code == 401


# ── Header short-circuits (no DB) ────────────────────────────────────────────


@pytest.mark.unit
def test_demo_session_recognizes_true_header():
    """The demo short-circuit accepts 'true', '1', 'yes' (case-insensitive)."""
    from starlette.requests import Request

    for value in ("true", "TRUE", "True", "1", "yes", "YES"):
        scope = {
            "type": "http",
            "headers": [(b"x-credence-demo", value.encode())],
            "method": "GET",
            "path": "/",
        }
        req = Request(scope)
        sess = _demo_session(req)
        assert sess is not None
        assert sess.account_id == DEMO_ACCOUNT_ID
        assert sess.is_demo is True
        assert sess.user_id is None


@pytest.mark.unit
def test_demo_session_returns_none_without_header():
    from starlette.requests import Request

    req = Request({"type": "http", "headers": [], "method": "GET", "path": "/"})
    assert _demo_session(req) is None


@pytest.mark.unit
def test_service_session_validates_token(monkeypatch):
    monkeypatch.setenv("SERVICE_ROLE_TOKEN", "shared-secret")
    from starlette.requests import Request

    good = Request(
        {
            "type": "http",
            "headers": [(b"x-credence-service", b"shared-secret")],
            "method": "GET",
            "path": "/",
        }
    )
    sess = _service_session(good)
    assert sess is not None
    assert sess.is_service is True
    assert sess.account_id == DEFAULT_ACCOUNT_ID

    bad = Request(
        {
            "type": "http",
            "headers": [(b"x-credence-service", b"wrong")],
            "method": "GET",
            "path": "/",
        }
    )
    assert _service_session(bad) is None


# ── SessionMiddleware end-to-end ─────────────────────────────────────────────


def _make_test_app() -> FastAPI:
    """Build a tiny FastAPI app with the middleware + a couple of routes."""
    app = FastAPI()
    install_session_middleware(app)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/protected")
    async def protected(request: Request):
        from credence.auth import get_session

        s = get_session(request)
        return {"account_id": str(s.account_id), "is_demo": s.is_demo}

    return app


@pytest.mark.unit
def test_middleware_passes_through_health():
    app = _make_test_app()
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.unit
def test_middleware_returns_401_on_missing_auth():
    app = _make_test_app()
    with TestClient(app) as client:
        r = client.get("/protected")
    assert r.status_code == 401
    assert r.json()["error"] == "auth_required"


@pytest.mark.unit
def test_middleware_accepts_demo_header():
    app = _make_test_app()
    with TestClient(app) as client:
        r = client.get("/protected", headers={"X-Credence-Demo": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == str(DEMO_ACCOUNT_ID)
    assert body["is_demo"] is True


@pytest.mark.unit
def test_middleware_returns_401_on_bad_jwt():
    app = _make_test_app()
    with TestClient(app) as client:
        r = client.get(
            "/protected",
            headers={"Authorization": "Bearer not.a.valid.jwt"},
        )
    assert r.status_code == 401


@pytest.mark.unit
def test_exempt_path_prefixes_includes_health_and_docs():
    """Sanity-check that we don't accidentally remove health-check exemption."""
    assert "/health" in EXEMPT_PATH_PREFIXES
    assert "/docs" in EXEMPT_PATH_PREFIXES
    assert "/openapi.json" in EXEMPT_PATH_PREFIXES
