"""Unit tests for credence.extractors.press_releases.

The Z.AI client is fully mocked via ``AsyncMock`` — these tests never
hit the network. Coverage focuses on (a) the cost guard, (b) the happy
path, (c) error/parse failure modes, and (d) entry validation +
confidence clamping.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from credence.extractors.press_releases import (
    LEADERSHIP_VERBS,
    PressReleaseReportingSignal,
    extract_reporting_from_press_release,
)


def _mock_client_returning(content: str) -> AsyncMock:
    """Build an AsyncMock client whose chat completion returns ``content``."""
    fake_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    client = AsyncMock()
    client.chat = AsyncMock()
    client.chat.completions = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=fake_response)
    return client


def _mock_client_raising(exc: Exception) -> AsyncMock:
    client = AsyncMock()
    client.chat = AsyncMock()
    client.chat.completions = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=exc)
    return client


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cost_guard_skips_llm_when_no_leadership_verb() -> None:
    """Text without any LEADERSHIP_VERBS must short-circuit before any LLM call."""
    client = _mock_client_returning("[]")
    text = "Acme reports Q4 revenue of $1.2B, up 14% year over year."

    result = await extract_reporting_from_press_release(text, client=client, model="m")

    assert result == []
    client.chat.completions.create.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_extracts_signal() -> None:
    text = (
        "Acme appoints Alice Kim as VP Engineering, reporting to CTO Bob Chen. "
        "She leads the platform team."
    )
    llm_content = (
        '[{"person_name": "Alice Kim", "person_title": "VP Engineering", '
        '"reports_to_name": "Bob Chen", "reports_to_title": "CTO", '
        '"confidence": 0.95}]'
    )
    client = _mock_client_returning(llm_content)

    result = await extract_reporting_from_press_release(text, client=client, model="m")

    assert len(result) == 1
    sig = result[0]
    assert isinstance(sig, PressReleaseReportingSignal)
    assert sig.person_name == "Alice Kim"
    assert sig.person_title == "VP Engineering"
    assert sig.reports_to_name == "Bob Chen"
    assert sig.reports_to_title == "CTO"
    assert sig.confidence >= 0.90
    assert sig.inference_method == "press_release_llm"
    client.chat.completions.create.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_llm_failure_returns_empty_list() -> None:
    text = "Bob Chen leads engineering and oversees product."
    client = _mock_client_raising(RuntimeError("boom"))

    result = await extract_reporting_from_press_release(text, client=client, model="m")

    assert result == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_json_response_returns_empty_list() -> None:
    text = "Bob Chen leads engineering and oversees product."
    client = _mock_client_returning("sorry I can't help with that")

    result = await extract_reporting_from_press_release(text, client=client, model="m")

    assert result == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_array_response_returns_empty_list() -> None:
    text = "Bob Chen leads engineering and oversees product."
    client = _mock_client_returning("[]")

    result = await extract_reporting_from_press_release(text, client=client, model="m")

    assert result == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_entries_filtered_silently() -> None:
    text = "Alice Kim leads platform engineering at Acme."
    llm_content = (
        '[{"person_name": "Alice"}, '
        '{"foo": "bar"}, '
        '{"person_name": ""}]'
    )
    client = _mock_client_returning(llm_content)

    result = await extract_reporting_from_press_release(text, client=client, model="m")

    assert len(result) == 1
    assert result[0].person_name == "Alice"
    assert result[0].inference_method == "press_release_llm"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confidence_clamping() -> None:
    text = "Alice Kim leads platform engineering."
    llm_content = (
        '[{"person_name": "High", "confidence": 1.5}, '
        '{"person_name": "Low", "confidence": -0.2}]'
    )
    client = _mock_client_returning(llm_content)

    result = await extract_reporting_from_press_release(text, client=client, model="m")

    assert len(result) == 2
    by_name = {s.person_name: s for s in result}
    assert by_name["High"].confidence == 1.0
    assert by_name["Low"].confidence == 0.0


@pytest.mark.unit
def test_leadership_verbs_is_frozenset() -> None:
    """Sanity check the exported constant matches the task spec."""
    assert isinstance(LEADERSHIP_VERBS, frozenset)
    expected = {
        "leads",
        "heads",
        "manages",
        "oversees",
        "directs",
        "runs",
        "is responsible for",
        "spearheads",
        "drives",
    }
    assert LEADERSHIP_VERBS == expected
