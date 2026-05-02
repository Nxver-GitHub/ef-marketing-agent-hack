"""Unit tests for ``credence.onboarding.rep_resolver``.

All tests use :class:`httpx.MockTransport` — no live network calls. Cover:

1. Happy-path single match
2. Multiple matches → highest-confidence wins
3. Zero matches returns None
4. Actor 5xx returns None (not raises)
5. Actor timeout / network error returns None
6. Confidence below threshold returns None
7. Email domain extraction edge cases
8. Idempotency
9. Caller-provided client is reused
10. Missing API token raises a clear configuration error

Plus a few extra edge cases (free-mail short-circuit, malformed email).
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from credence.onboarding.rep_resolver import (
    MIN_CONFIDENCE,
    RepResolverConfigError,
    ResolvedRep,
    _company_url_from_email,
    _extract_registered_domain,
    resolve_rep_linkedin,
)


_TEST_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture(autouse=True)
def _set_apify_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests assume the env var is set. The one config-error test undoes this."""
    monkeypatch.setenv("APIFY_TOKEN", "test-token-do-not-use")


def _employee_record(**overrides: Any) -> dict[str, Any]:
    """Minimal valid apimaestro employee listing record."""
    base: dict[str, Any] = {
        "company_url": "https://www.linkedin.com/company/nvidia/",
        "profile_url": "https://linkedin.com/in/sarah-kim-nvidia",
        "fullname": "Sarah Kim",
        "first_name": "Sarah",
        "last_name": "Kim",
        "headline": "VP of GPU Architecture, NVIDIA",
        "public_identifier": "sarah-kim-nvidia",
        "location": {
            "country": "United States",
            "city": "Santa Clara, California",
            "full": "Santa Clara, California, United States",
            "country_code": "US",
        },
        "is_premium": True,
        "is_creator": False,
        "is_influencer": False,
        "open_to_work": False,
        "urn": "ACoAABMznFkB_TestSarahKim",
    }
    base.update(overrides)
    return base


def _make_handler(payload: list[dict[str, Any]] | None = None,
                  *,
                  status: int = 201,
                  raise_exc: Exception | None = None,
                  capture: dict[str, Any] | None = None) -> Any:
    """Build a :class:`httpx.MockTransport` request handler."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["url"] = str(request.url)
            capture["body"] = json.loads(request.content) if request.content else {}
            capture["call_count"] = capture.get("call_count", 0) + 1
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(status, json=payload if payload is not None else [])

    return handler


def _make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── 1. Happy path ──────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_single_match_returns_resolved_rep():
    handler = _make_handler([_employee_record()])
    async with _make_client(handler) as client:
        result = await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )

    assert result is not None
    assert isinstance(result, ResolvedRep)
    assert result.linkedin_url == "https://linkedin.com/in/sarah-kim-nvidia"
    assert result.headline == "VP of GPU Architecture, NVIDIA"
    assert result.current_title == "VP of GPU Architecture, NVIDIA"
    assert result.profile_photo_url is None  # Stage A doesn't expose photo
    assert result.confidence >= MIN_CONFIDENCE
    assert result.confidence == 1.0  # exact normalized match


# ── 2. Multiple matches → highest confidence wins ──────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multiple_matches_highest_confidence_wins():
    handler = _make_handler([
        _employee_record(  # weak fuzzy match
            profile_url="https://linkedin.com/in/sara-kimura",
            public_identifier="sara-kimura",
            fullname="Sara Kimura",
            first_name="Sara",
            last_name="Kimura",
        ),
        _employee_record(),  # exact match
        _employee_record(
            profile_url="https://linkedin.com/in/john-doe",
            public_identifier="john-doe",
            fullname="John Doe",
            first_name="John",
            last_name="Doe",
        ),
    ])

    async with _make_client(handler) as client:
        result = await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )

    assert result is not None
    assert result.linkedin_url == "https://linkedin.com/in/sarah-kim-nvidia"
    assert result.confidence == 1.0


# ── 3. Zero matches returns None ───────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zero_employees_returns_none():
    handler = _make_handler([])  # empty employee list
    async with _make_client(handler) as client:
        result = await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )
    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_matching_employee_returns_none():
    handler = _make_handler([
        _employee_record(
            profile_url="https://linkedin.com/in/john-doe",
            public_identifier="john-doe",
            fullname="John Doe",
            first_name="John",
            last_name="Doe",
        ),
        _employee_record(
            profile_url="https://linkedin.com/in/jane-roe",
            public_identifier="jane-roe",
            fullname="Jane Roe",
            first_name="Jane",
            last_name="Roe",
        ),
    ])
    async with _make_client(handler) as client:
        result = await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )
    assert result is None


# ── 4. Actor 5xx returns None ──────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_actor_5xx_returns_none_not_raises():
    handler = _make_handler(status=500, payload=[])
    async with _make_client(handler) as client:
        result = await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )
    # list_company_employees swallows non-201 and returns ([], 0); we get None.
    assert result is None


# ── 5. Network/timeout errors return None ──────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_network_error_returns_none():
    handler = _make_handler(raise_exc=httpx.ConnectError("connection refused"))
    async with _make_client(handler) as client:
        result = await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )
    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_timeout_returns_none():
    handler = _make_handler(raise_exc=httpx.ReadTimeout("read timed out"))
    async with _make_client(handler) as client:
        result = await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )
    assert result is None


# ── 6. Confidence below threshold returns None ─────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confidence_below_threshold_returns_none():
    # "Sarah Kim" vs "Bob Anderson" — sequence ratio ~0.15, well under 0.60.
    handler = _make_handler([
        _employee_record(
            profile_url="https://linkedin.com/in/bob-anderson",
            public_identifier="bob-anderson",
            fullname="Bob Anderson",
            first_name="Bob",
            last_name="Anderson",
        ),
    ])
    async with _make_client(handler) as client:
        result = await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )
    assert result is None


# ── 7. Email-domain extraction edge cases ──────────────────────────────


@pytest.mark.unit
def test_email_domain_extraction_edge_cases():
    # Vanilla
    assert _extract_registered_domain("sarah@nvidia.com") == "nvidia.com"
    # Plus-addressing
    assert _extract_registered_domain("sarah+filters@nvidia.com") == "nvidia.com"
    # Mixed case
    assert _extract_registered_domain("SARAH@NVIDIA.COM") == "nvidia.com"
    # Subdomain → registered (eTLD+1)
    assert _extract_registered_domain("sarah@mail.eng.nvidia.com") == "nvidia.com"
    # 2-part TLD
    assert _extract_registered_domain("alex@deepmind.co.uk") == "deepmind.co.uk"
    assert _extract_registered_domain("alex@mail.deepmind.co.uk") == "deepmind.co.uk"
    # Free-mail providers → None (can't infer company)
    assert _extract_registered_domain("sarah@gmail.com") is None
    assert _extract_registered_domain("sarah@googlemail.com") is None
    assert _extract_registered_domain("sarah@outlook.com") is None
    assert _extract_registered_domain("sarah@proton.me") is None
    # Malformed
    assert _extract_registered_domain("not-an-email") is None
    assert _extract_registered_domain("sarah@") is None
    assert _extract_registered_domain("@nvidia.com") is None
    assert _extract_registered_domain("") is None
    # Single-label domain (no TLD) is malformed.
    assert _extract_registered_domain("sarah@localhost") is None


@pytest.mark.unit
def test_company_url_from_email():
    assert _company_url_from_email("sarah@nvidia.com") == \
        "https://linkedin.com/company/nvidia/"
    # Subdomain reduces to root before slug
    assert _company_url_from_email("alex@mail.eng.nvidia.com") == \
        "https://linkedin.com/company/nvidia/"
    # Free mail returns None — caller will short-circuit to no-match.
    assert _company_url_from_email("sarah@gmail.com") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_freemail_skips_actor_call_and_returns_none():
    """Free-mail addresses must NEVER trigger an actor call (cost-saving)."""
    capture: dict[str, Any] = {}
    handler = _make_handler([_employee_record()], capture=capture)
    async with _make_client(handler) as client:
        result = await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@gmail.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )
    assert result is None
    assert capture.get("call_count", 0) == 0  # no HTTP call


# ── 8. Idempotency ─────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_idempotent_same_input_returns_equal_output():
    handler = _make_handler([
        _employee_record(),
        _employee_record(
            profile_url="https://linkedin.com/in/john-doe",
            public_identifier="john-doe",
            fullname="John Doe",
            first_name="John",
            last_name="Doe",
        ),
    ])

    async with _make_client(handler) as client:
        first = await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )
        second = await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )

    assert first is not None
    assert second is not None
    # Frozen dataclass equality is by-value, so this validates immutability
    # AND determinism in one assertion.
    assert first == second


# ── 9. Caller-provided client is reused ────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_caller_provided_client_is_reused():
    capture: dict[str, Any] = {}
    handler = _make_handler([_employee_record()], capture=capture)

    async with _make_client(handler) as client:
        # Two back-to-back calls should both go through the SAME mock
        # transport — captured.call_count proves we didn't create a new
        # client (which would have a different transport and bypass the mock).
        await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )
        # Confirm the client is still open BEFORE the context-manager exits —
        # this is what proves the resolver didn't close our client out from
        # under us. (After the `async with` block exits, of course it's closed.)
        assert not client.is_closed
        await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
            client=client,
        )

    assert capture.get("call_count") == 2


# ── 10. Missing API token raises a clear configuration error ───────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_api_token_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APIFY_TOKEN", raising=False)

    with pytest.raises(RepResolverConfigError) as exc_info:
        await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
        )

    assert "APIFY_TOKEN" in str(exc_info.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blank_api_token_also_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace-only token is treated as missing — defensive against bad .env files."""
    monkeypatch.setenv("APIFY_TOKEN", "   ")

    with pytest.raises(RepResolverConfigError):
        await resolve_rep_linkedin(
            "Sarah Kim",
            "sarah@nvidia.com",
            account_id=_TEST_ACCOUNT_ID,
        )


# ── Bonus: when no client is provided, the resolver creates its own ────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_client_provided_creates_internal_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``client=None``, ``list_company_employees`` opens a one-shot client.

    We patch that downstream function so we don't need a real network.
    """
    captured: dict[str, Any] = {}

    async def fake_list(
        company_url: str,
        *,
        max_items: int,
        api_token: str,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[list, int]:
        captured["company_url"] = company_url
        captured["client_was_none"] = client is None
        return [], 0

    monkeypatch.setattr(
        "credence.onboarding.rep_resolver.list_company_employees",
        fake_list,
    )

    result = await resolve_rep_linkedin(
        "Sarah Kim",
        "sarah@nvidia.com",
        account_id=_TEST_ACCOUNT_ID,
        client=None,
    )

    assert result is None  # empty employees → None
    assert captured["company_url"] == "https://linkedin.com/company/nvidia/"
    # Resolver passes through the None-client unchanged so the downstream
    # helper opens its own short-lived session.
    assert captured["client_was_none"] is True
