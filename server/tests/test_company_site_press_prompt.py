"""Smoke tests for the tightened ``_PRESS_PROMPT`` in ``company_site``.

Asserts the new anti-hallucination guidance is present so the prompt cannot
silently regress to a generic shape that re-introduces team-level mentions.
"""
from __future__ import annotations

import pytest

from credence.enrichment import company_site


@pytest.mark.unit
class TestPressPromptHardening:
    def test_press_prompt_constant_exists(self) -> None:
        assert isinstance(company_site._PRESS_PROMPT, str)
        assert len(company_site._PRESS_PROMPT) > 0

    def test_disallows_team_level_references(self) -> None:
        # The hardened prompt must explicitly tell the LLM to skip team-level
        # phrases. We assert on the verbatim guidance string.
        assert "NO team-level references" in company_site._PRESS_PROMPT
        assert "leadership team" in company_site._PRESS_PROMPT

    def test_requires_full_name(self) -> None:
        # Surface phrasing that names the first-AND-last requirement.
        assert "first AND last name" in company_site._PRESS_PROMPT

    def test_anti_hallucination_clause(self) -> None:
        # If unsure, return empty.
        prompt = company_site._PRESS_PROMPT
        assert "OMIT" in prompt
        assert "empty list" in prompt
        # Don't fabricate names that aren't in the article body.
        assert "do NOT fill in a name from outside the article" in prompt

    def test_includes_one_shot_example(self) -> None:
        # The example block must contain a concrete output shape with both
        # mentioned_executives and reporting_phrases keys populated.
        prompt = company_site._PRESS_PROMPT
        assert "Output schema example" in prompt
        assert '"mentioned_executives": ["Jane Doe"]' in prompt
        assert "reporting_phrases" in prompt

    def test_keeps_reporting_phrase_scaffolding(self) -> None:
        # The hardening must not remove the existing reporting_phrases
        # behaviour — the downstream org-chart pipeline depends on it.
        prompt = company_site._PRESS_PROMPT
        assert "Jane Doe will report to John Smith" in prompt
        assert "under the leadership of" in prompt

    def test_pairing_with_press_schema_unchanged(self) -> None:
        # The schema returned alongside the prompt must still be the press
        # schema (caller checks the tuple shape).
        schema, prompt = company_site._schema_and_prompt_for("press")
        assert schema is company_site._PRESS_SCHEMA
        assert prompt is company_site._PRESS_PROMPT
