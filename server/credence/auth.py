"""Wave 6 M2 — backend session middleware.

Resolves an incoming Supabase Auth JWT into `(user_id, account_id)`. Demo
mode and service-role calls short-circuit JWT verification via dedicated
headers. Per CONTRACTS.md Contract 9 §"Session-context invariant".

## What M2 added (over LavenderPrairie's skeleton)

1. **Real JWT signature verification** via `python-jose` (HS256), reading
   `SUPABASE_JWT_SECRET` from settings. `_decode_supabase_jwt` raises 401
   on invalid signature, expired token, wrong audience, or malformed body.
2. **`SessionMiddleware`** — a Starlette `BaseHTTPMiddleware` that resolves
   the session before route handlers run, attaches it to `request.state`,
   and exempts a small allow-list (health, docs, OpenAPI). Wired into
   `api.py` via `app.add_middleware(SessionMiddleware)`.
3. **`session_dependency`** — a thin FastAPI dependency that reads
   `request.state.session`, used by enrichment / signals routes that need
   the resolved `account_id` for cost-log writes.

## Demo mode short-circuit

Per Contract 9 §"Demo mode reconciliation", `?demo=true` is detected by the
frontend (graphStore + `getCredenceHeaders()` → `X-Credence-Demo: true`).
This middleware honors that header and binds the request to the demo
pseudo-tenant without JWT verification. SwiftElk's M5 thin slice (msg 82)
ships the frontend half of this contract.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from .config import get_settings

logger = logging.getLogger(__name__)

# Stable UUIDs from the multitenant migration. The demo pseudo-tenant
# `…000fff` is public-readable; the default tenant `…000001` carries v2
# backwards-compat data per M6 policy.
DEMO_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000fff")
DEFAULT_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Session:
    """Resolved session state attached to every authenticated request.

    Exposed via `request.state.session`. Routes that need the account_id
    (e.g., enrichment_cost_log writes) read it from there.
    """

    user_id: UUID | None  # None for demo / service-role sessions
    account_id: UUID
    is_demo: bool = False
    is_service: bool = False  # True when service-role JWT (extractors, cron)


# ─── JWT decoding (skeleton) ─────────────────────────────────────────────────


def _decode_supabase_jwt(token: str) -> dict[str, Any]:
    """Decode and verify a Supabase Auth JWT.

    Verifies signature (HS256), expiry, and audience against the project's
    JWT secret. Raises HTTPException(401) on any failure with a specific
    error code that the frontend can branch on (`invalid_token`, `token_expired`,
    `wrong_audience`, `malformed_token`).
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=[settings.supabase_jwt_algorithm],
            audience=settings.supabase_jwt_audience,
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": "token_expired"},
        ) from exc
    except jwt.JWTClaimsError as exc:
        # Wrong audience, wrong issuer, etc.
        raise HTTPException(
            status_code=401,
            detail={"error": "wrong_audience", "message": str(exc)},
        ) from exc
    except JWTError as exc:
        # Invalid signature, malformed token, unsupported algorithm, etc.
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token"},
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=401, detail={"error": "malformed_token"})
    return payload


# ─── Account resolution ──────────────────────────────────────────────────────


async def _account_for_user(conn: asyncpg.Connection, user_id: UUID) -> UUID | None:
    """Look up the account_id the user belongs to.

    v1 contract: one user belongs to exactly one account. Returns None if
    the user has no account (e.g., signup just completed but they haven't
    been assigned to an account yet).
    """
    row = await conn.fetchrow(
        "SELECT account_id FROM account_users WHERE user_id = $1 LIMIT 1",
        user_id,
    )
    if row is None:
        return None
    return UUID(str(row["account_id"]))


# ─── Demo / service short-circuits ───────────────────────────────────────────


def _demo_session(request: Request) -> Session | None:
    """Detect demo mode via header convention.

    Frontend sets `X-Credence-Demo: true` when `?demo=true` is on the URL
    (M5 wires this in graphStore + the fetch layer). When detected, return
    a Session bound to the demo pseudo-tenant with no user_id.

    Returns None when the request isn't demo-flagged.
    """
    demo_header = request.headers.get("x-credence-demo")
    if demo_header and demo_header.lower() in ("1", "true", "yes"):
        return Session(user_id=None, account_id=DEMO_ACCOUNT_ID, is_demo=True)
    return None


def _service_session(request: Request) -> Session | None:
    """Detect service-role calls (extractors, cron jobs).

    Service role uses a shared internal token via `X-Credence-Service: <token>`.
    The token is a deployment-wide secret stored in `SERVICE_ROLE_TOKEN`.
    Service sessions skip RLS entirely (the connection role has bypassrls).

    Returns None when the request isn't service-flagged.
    """
    import os

    expected = os.environ.get("SERVICE_ROLE_TOKEN")
    presented = request.headers.get("x-credence-service")
    if expected and presented and presented == expected:
        # Service sessions don't need an account_id at the policy level
        # (RLS is bypassed) but we still set DEFAULT_ACCOUNT_ID so any
        # accidental code path that reads request.state.session.account_id
        # has a deterministic value rather than a None deref.
        return Session(
            user_id=None,
            account_id=DEFAULT_ACCOUNT_ID,
            is_demo=False,
            is_service=True,
        )
    return None


# ─── Public middleware entry points ──────────────────────────────────────────


def _validate_bearer_token(request: Request) -> UUID:
    """Extract + verify the Bearer JWT, return the user_id (sub claim).

    Raises HTTPException(401) on missing/malformed/invalid/expired tokens.
    Does NOT touch the database — splitting verification from account
    lookup means the middleware can fail closed on bad auth without
    burning a pool connection.
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": "auth_required", "hint": "Bearer <supabase_jwt>"},
        )
    token = auth[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail={"error": "auth_required"})

    payload = _decode_supabase_jwt(token)
    user_id_raw = payload.get("sub")
    if not isinstance(user_id_raw, str):
        raise HTTPException(status_code=401, detail={"error": "malformed_token"})
    try:
        return UUID(user_id_raw)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail={"error": "malformed_token"}) from exc


async def resolve_session(
    request: Request,
    conn: asyncpg.Connection,
) -> Session:
    """Resolve a session from an incoming Request.

    Order of resolution:
      1. Demo header → demo pseudo-tenant
      2. Service-role header → service session (bypasses RLS)
      3. `Authorization: Bearer <jwt>` → Supabase Auth user → account lookup
      4. Otherwise → 401
    """
    demo = _demo_session(request)
    if demo is not None:
        return demo

    svc = _service_session(request)
    if svc is not None:
        return svc

    user_id = _validate_bearer_token(request)
    account_id = await _account_for_user(conn, user_id)
    if account_id is None:
        # User signed up but isn't a member of any account — onboarding
        # incomplete. Return 403 so the frontend can route to onboarding.
        raise HTTPException(
            status_code=403,
            detail={"error": "no_account", "user_id": str(user_id)},
        )

    return Session(user_id=user_id, account_id=account_id, is_demo=False)


async def apply_session_to_connection(
    conn: asyncpg.Connection,
    session: Session,
) -> None:
    """No-op for the auth.uid()-based RLS pattern.

    The original Contract 9 design used `set_config('app.account_id', …)`
    on each request connection. We switched to Supabase's native
    `auth.uid()` pattern (RLS policies subquery against `account_users`
    using the JWT's sub claim), so the backend connection doesn't need
    to set anything — RLS is enforced by PostgREST when the frontend
    queries directly, and bypassed when the backend uses the postgres
    role on `DATABASE_URL`.

    Kept as a stable interface so middleware code in api.py doesn't
    need to change if we ever revert the pattern.
    """
    return None


# ─── FastAPI dependency ──────────────────────────────────────────────────────
# Routes inject `session: Session = Depends(get_session)` to read the
# resolved session. The `request.state.session` is populated by the global
# middleware (added by SunnyRidge in M2) before any route handler runs.


def get_session(request: Request) -> Session:
    """FastAPI dependency. Read the session middleware put on request.state."""
    session = getattr(request.state, "session", None)
    if not isinstance(session, Session):
        # Middleware wasn't installed correctly — fail loud rather than
        # silently leak data via missing tenant filter.
        raise HTTPException(
            status_code=500,
            detail={"error": "session_middleware_missing", "hint": "wire credence.auth into api.py"},
        )
    return session


# ─── ASGI middleware ─────────────────────────────────────────────────────────


# Routes that don't require a session. Health checks need to be reachable
# before auth is configured; OpenAPI / docs are public per FastAPI convention.
EXEMPT_PATH_PREFIXES: tuple[str, ...] = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
)


class SessionMiddleware(BaseHTTPMiddleware):
    """Resolve the session for every non-exempt request and attach it to state.

    Order:
      1. If the request path starts with an exempt prefix, pass through.
      2. Otherwise resolve a Session via `resolve_session` (demo header,
         service header, or Bearer JWT). On 401 / 403, return a JSON error
         response without invoking the route handler.
      3. Attach the Session to `request.state.session`. Routes that depend
         on it pull it via `Depends(get_session)`.

    The middleware does NOT acquire a database connection per request — that
    would double the pool pressure for read-only routes. JWT validation is
    purely token-based; the only DB hit is the `account_users` lookup inside
    `resolve_session`, which acquires its own short-lived connection.
    """

    def __init__(self, app: ASGIApp, *, exempt_prefixes: tuple[str, ...] | None = None) -> None:
        super().__init__(app)
        self._exempt = exempt_prefixes if exempt_prefixes is not None else EXEMPT_PATH_PREFIXES

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        if any(path == p or path.startswith(p) for p in self._exempt):
            return await call_next(request)

        try:
            session = await self._resolve(request)
        except HTTPException as exc:
            return _json_error(exc)

        request.state.session = session
        return await call_next(request)

    async def _resolve(self, request: Request) -> Session:
        """Resolve a session, acquiring a DB connection only when needed.

        Demo and service short-circuits skip the pool entirely. The
        Bearer-JWT path validates the token shape (no DB) before acquiring
        a connection for the `account_users` lookup — so missing/invalid
        auth never burns a pool slot.
        """
        demo = _demo_session(request)
        if demo is not None:
            return demo

        svc = _service_session(request)
        if svc is not None:
            return svc

        # Validate the JWT before touching the pool. Raises 401 on any
        # missing/expired/invalid token — middleware catches and returns
        # JSON error without invoking the route or the DB.
        user_id = _validate_bearer_token(request)

        # Bearer-JWT path needs DB access for the account lookup. Import
        # here to avoid a top-level circular import (db imports config;
        # this module is imported by api.py during create_app).
        from .db import acquire

        async with acquire() as conn:
            account_id = await _account_for_user(conn, user_id)

        if account_id is None:
            raise HTTPException(
                status_code=403,
                detail={"error": "no_account", "user_id": str(user_id)},
            )
        return Session(user_id=user_id, account_id=account_id, is_demo=False)


def _json_error(exc: HTTPException) -> Response:
    """Render an HTTPException as a JSONResponse for middleware-time errors."""
    detail = exc.detail
    if not isinstance(detail, dict):
        detail = {"error": str(detail) if detail else "auth_error"}
    return JSONResponse(status_code=exc.status_code, content=detail)


def install_session_middleware(app: FastAPI) -> None:
    """Wire SessionMiddleware into a FastAPI app.

    Used by `api.py` so the wiring stays in one place — if we ever need to
    swap the middleware for a different mechanism, the call site is one
    line and the test fixtures don't change shape.
    """
    app.add_middleware(SessionMiddleware)
