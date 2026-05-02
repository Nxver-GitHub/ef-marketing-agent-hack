"""Unit tests for credence.onboarding.webhook.verify_supabase_webhook.

Covers the full failure surface — missing header, bad secret, invalid JSON,
missing fields, malformed UUID — plus case-insensitive header lookup and
the optional ``full_name`` field. Every failure mode must collapse to
``None`` (never raise) so the webhook handler cannot be turned into a
timing or error oracle by an attacker who controls the body.
"""
from __future__ import annotations

import json
from uuid import UUID

import pytest

from credence.onboarding.webhook import WebhookPayload, verify_supabase_webhook

SECRET = "shared-secret-not-real"
USER_ID = "8a39c1d5-2c1e-4a31-9c8d-9a2c0e7c6d3e"
EMAIL = "rep@example.com"
FULL_NAME = "Wei Chen"


def _build_body(
    *,
    include_id: bool = True,
    include_email: bool = True,
    include_full_name: bool = True,
    user_id: str = USER_ID,
) -> bytes:
    """Construct a Supabase auth.users INSERT webhook body."""
    record: dict[str, object] = {}
    if include_id:
        record["id"] = user_id
    if include_email:
        record["email"] = EMAIL
    if include_full_name:
        record["raw_user_meta_data"] = {"full_name": FULL_NAME}
    body = {"type": "INSERT", "table": "users", "schema": "auth", "record": record}
    return json.dumps(body).encode("utf-8")


@pytest.mark.unit
def test_valid_signature_and_body_returns_parsed_payload() -> None:
    body = _build_body()
    headers = {"X-Webhook-Secret": SECRET, "Content-Type": "application/json"}

    result = verify_supabase_webhook(body, headers, SECRET)

    assert result is not None
    assert isinstance(result, WebhookPayload)
    assert result.user_id == UUID(USER_ID)
    assert result.email == EMAIL
    assert result.full_name == FULL_NAME


@pytest.mark.unit
def test_wrong_signature_returns_none() -> None:
    body = _build_body()
    headers = {"X-Webhook-Secret": "wrong-secret", "Content-Type": "application/json"}

    result = verify_supabase_webhook(body, headers, SECRET)

    assert result is None


@pytest.mark.unit
def test_missing_secret_header_returns_none() -> None:
    body = _build_body()
    headers = {"Content-Type": "application/json"}

    result = verify_supabase_webhook(body, headers, SECRET)

    assert result is None


@pytest.mark.unit
def test_invalid_json_body_returns_none() -> None:
    body = b"this is not { json"
    headers = {"X-Webhook-Secret": SECRET}

    result = verify_supabase_webhook(body, headers, SECRET)

    assert result is None


@pytest.mark.unit
def test_missing_record_id_returns_none() -> None:
    body = _build_body(include_id=False)
    headers = {"X-Webhook-Secret": SECRET}

    result = verify_supabase_webhook(body, headers, SECRET)

    assert result is None


@pytest.mark.unit
def test_missing_record_email_returns_none() -> None:
    body = _build_body(include_email=False)
    headers = {"X-Webhook-Secret": SECRET}

    result = verify_supabase_webhook(body, headers, SECRET)

    assert result is None


@pytest.mark.unit
def test_full_name_is_optional() -> None:
    body = _build_body(include_full_name=False)
    headers = {"X-Webhook-Secret": SECRET}

    result = verify_supabase_webhook(body, headers, SECRET)

    assert result is not None
    assert result.user_id == UUID(USER_ID)
    assert result.email == EMAIL
    assert result.full_name is None


@pytest.mark.unit
def test_case_insensitive_header_lookup() -> None:
    body = _build_body()
    # Plain dict with all-lowercase header — should still verify since
    # HTTP headers are case-insensitive on the wire.
    headers = {"x-webhook-secret": SECRET}

    result = verify_supabase_webhook(body, headers, SECRET)

    assert result is not None
    assert result.user_id == UUID(USER_ID)


@pytest.mark.unit
def test_malformed_uuid_returns_none() -> None:
    body = _build_body(user_id="not-a-uuid")
    headers = {"X-Webhook-Secret": SECRET}

    result = verify_supabase_webhook(body, headers, SECRET)

    assert result is None


@pytest.mark.unit
def test_record_not_a_dict_returns_none() -> None:
    body = json.dumps({"type": "INSERT", "record": "not a dict"}).encode("utf-8")
    headers = {"X-Webhook-Secret": SECRET}

    result = verify_supabase_webhook(body, headers, SECRET)

    assert result is None


@pytest.mark.unit
def test_top_level_not_a_dict_returns_none() -> None:
    body = json.dumps([1, 2, 3]).encode("utf-8")
    headers = {"X-Webhook-Secret": SECRET}

    result = verify_supabase_webhook(body, headers, SECRET)

    assert result is None


@pytest.mark.unit
def test_mixed_case_header_lookup() -> None:
    body = _build_body()
    headers = {"X-WeBhOoK-SeCrEt": SECRET}

    result = verify_supabase_webhook(body, headers, SECRET)

    assert result is not None
