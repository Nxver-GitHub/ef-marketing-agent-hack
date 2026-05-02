"""Supabase Auth webhook signature verification.

Validates the inbound INSERT-on-auth.users webhook from Supabase, which is
configured per CUSTOMER_ONBOARDING_PLAN.md §"Supabase Auth Webhook Setup".

Design notes:

- Verification is done with ``hmac.compare_digest`` so the time taken to
  reject a bad secret does not leak information about how many bytes
  matched. A naive ``==`` would let an attacker discover the secret one
  byte at a time over many requests.
- Header lookup is case-insensitive because HTTP headers are. The wire
  could carry ``X-Webhook-Secret``, ``x-webhook-secret``, or any mixed
  casing — all must verify identically.
- ``verify_supabase_webhook`` returns ``None`` on every failure mode
  (missing header, bad secret, unparseable body, missing field, malformed
  UUID) and never raises. Raising would let an attacker who controls the
  request body trigger backend errors / 5xxs by sending crafted payloads.
- Pure function, no top-level state — safe to call concurrently from any
  request handler.
"""
from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError


class WebhookPayload(BaseModel):
    """Parsed Supabase auth.users INSERT webhook payload.

    Only carries the three fields the onboarding pipeline needs. Additional
    fields in the wire payload are silently ignored.
    """

    user_id: UUID
    email: str
    full_name: str | None = None


def _lookup_header_case_insensitive(
    headers: Mapping[str, str],
    target: str,
) -> str | None:
    """Return the first header value matching ``target`` ignoring case.

    Works with plain ``dict[str, str]`` and Starlette ``Headers`` (which
    is itself case-insensitive but exposes a ``Mapping`` interface). We
    lowercase both sides and walk the mapping rather than relying on a
    specific implementation, so any ``Mapping`` works.
    """
    target_lower = target.lower()
    # Starlette's Headers object exposes case-insensitive __getitem__, so
    # try the direct lookup first as a fast path. Fall back to the linear
    # scan when that raises (plain dict with mismatched casing).
    try:
        value = headers[target]
    except (KeyError, TypeError):
        value = None
    if value is not None:
        return value

    for key in headers:
        if isinstance(key, str) and key.lower() == target_lower:
            return headers[key]
    return None


def _parse_body(body_bytes: bytes) -> dict[str, Any] | None:
    """Decode + JSON-parse the request body, returning None on any error."""
    try:
        text = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _extract_payload_fields(body: dict[str, Any]) -> dict[str, Any] | None:
    """Pull (user_id, email, full_name) out of the Supabase payload shape.

    Supabase webhook bodies look like::

        {"type": "INSERT", "table": "users", "schema": "auth",
         "record": {"id": "...", "email": "...",
                    "raw_user_meta_data": {"full_name": "..."}}}

    Returns ``None`` if ``record``, ``record.id``, or ``record.email`` is
    missing. ``full_name`` is optional — absence yields ``None``, never a
    failure.
    """
    record = body.get("record")
    if not isinstance(record, dict):
        return None

    user_id = record.get("id")
    email = record.get("email")
    if user_id is None or email is None:
        return None

    raw_meta = record.get("raw_user_meta_data")
    full_name: str | None = None
    if isinstance(raw_meta, dict):
        meta_full_name = raw_meta.get("full_name")
        if isinstance(meta_full_name, str):
            full_name = meta_full_name

    return {
        "user_id": user_id,
        "email": email,
        "full_name": full_name,
    }


def verify_supabase_webhook(
    body_bytes: bytes,
    headers: Mapping[str, str],
    expected_secret: str,
) -> WebhookPayload | None:
    """Verify and parse a Supabase Auth webhook.

    Returns the parsed :class:`WebhookPayload` if the request is authentic
    and well-formed, otherwise returns ``None``. Never raises.

    Validates, in order:

    1. ``X-Webhook-Secret`` header is present and matches
       ``expected_secret`` under :func:`hmac.compare_digest` (constant time).
    2. ``body_bytes`` decodes as UTF-8 JSON object.
    3. ``record.id`` and ``record.email`` are present.
    4. ``record.id`` parses as a UUID.

    Failures of any kind collapse to ``None`` — callers must not be able
    to distinguish between "wrong secret" and "missing header" via
    response timing or shape, which would create an oracle.
    """
    # --- Step 1: header presence + constant-time secret check.
    received_secret = _lookup_header_case_insensitive(headers, "X-Webhook-Secret")
    if received_secret is None:
        return None

    received_bytes = received_secret.encode("utf-8")
    expected_bytes = expected_secret.encode("utf-8")
    if not hmac.compare_digest(received_bytes, expected_bytes):
        return None

    # --- Step 2: JSON parse.
    body = _parse_body(body_bytes)
    if body is None:
        return None

    # --- Step 3: extract required fields.
    fields = _extract_payload_fields(body)
    if fields is None:
        return None

    # --- Step 4: pydantic validation (UUID parse + type checks).
    try:
        return WebhookPayload(**fields)
    except (ValidationError, ValueError, TypeError):
        return None
