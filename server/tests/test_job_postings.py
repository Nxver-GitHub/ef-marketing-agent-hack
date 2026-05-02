"""Tests for `credence.extractors.job_postings` — Phase 2 Task 2-B.

Exercises the `REPORTING_PATTERNS`-based extractor against the 6 reference
sentences from CLAUDE.md REPL Pattern 3 plus several edge cases.

## Source-of-truth resolution (2026-05-01)

CLAUDE.md L313-320 is the authoritative `REPORTING_PATTERNS` definition.
`orgchart_tasks.md` Task 2-B's reference table predicted the 6 sentences'
match behavior, but on 2 sentences the verbatim CLAUDE.md regex behaves
differently from the table. Per user directive: **CLAUDE.md wins.** The
test assertions below are pinned to the regexes' actual behavior. The two
divergent sentences (Sentence 2 + Sentence 3) carry explanatory inline
comments. If the task table is ever to become the ground truth, the
CLAUDE.md regexes must be amended first; the tests then update to match.

Coverage:
1. Sentence 1 (Pattern 0, "reports directly to") → 1 match @ 0.88
2. Sentence 2 (Pattern 2, "work closely under … the SVP") → 0 matches
   because the verbatim regex's `[^,.]+` capture is broken by the period
   in "Dr." — documented divergence from the task table (table expected 1)
3. Sentence 3 (Pattern 3, "dotted line") → 1 match @ 0.70
   (verbatim regex matches under IGNORECASE; documented divergence from
   the task table which expected 0)
4. Sentence 4 (Pattern 1, "Reporting line to") → 1 match @ 0.85
5. Sentence 5 ("3 direct reports") → 0 matches (no Pattern 4 fit —
   missing "to <Name>")
6. Sentence 6 ("oversee a team of 12") → 0 matches (scope, not reports-to)
7. Empty / None input → []
8. Multi-line posting with multiple matches → multiple deduplicated signals
9. Case-insensitive matching (lowercase title still matches)
10. Duplicate match in same posting → deduplicated to one signal
"""
from __future__ import annotations

import pytest

from credence.extractors.job_postings import (
    INFERENCE_METHOD,
    REPORTING_PATTERNS,
    ReportingSignal,
    extract_reporting_from_job_posting,
)

# ── Constants ───────────────────────────────────────────────────────────────

COMPANY_ID = "co-test"
REPORT_TITLE = "Senior Engineer"


# ── Reference-sentence tests (CLAUDE.md REPL Pattern 3) ─────────────────────


@pytest.mark.unit
async def test_pattern_0_reports_directly_to() -> None:
    """Sentence 1: "reports directly to the VP of Process Engineering"."""
    text = "The role reports directly to the VP of Process Engineering"
    signals = await extract_reporting_from_job_posting(text, COMPANY_ID, REPORT_TITLE)

    assert len(signals) == 1
    signal = signals[0]
    assert signal.confidence == 0.88
    assert signal.inference_method == INFERENCE_METHOD
    assert signal.manager_name is None
    assert signal.manager_title is not None
    assert "VP" in signal.manager_title


@pytest.mark.unit
async def test_pattern_2_work_closely_under_period_breaks_capture() -> None:
    """Sentence 2: "work closely under Dr. Wei Chen, the SVP of R&D".

    Documented edge case: Pattern 2's capture group is `[A-Z][^,.]+` —
    the period in "Dr." terminates the negated character class before the
    `,` delimiter, so the verbatim regex does not match this sentence.
    The task table optimistically expected 1 match; verbatim regex says 0.
    """
    text = "You will work closely under Dr. Wei Chen, the SVP of R&D"
    signals = await extract_reporting_from_job_posting(text, COMPANY_ID, REPORT_TITLE)

    assert signals == []


@pytest.mark.unit
async def test_pattern_3_dotted_line_matches_verbatim() -> None:
    """Sentence 3: "dotted line to the Chief Technology Officer".

    Pattern 3 is `dotted\\s+line\\s+to\\s+([A-Z][^,.]+)` — under IGNORECASE
    the verbatim regex matches "the Chief Technology Officer". The task
    table expected 0 matches, but the verbatim CLAUDE.md regex genuinely
    fires here. We honour verbatim regex over the table.
    """
    text = "This position has a dotted line to the Chief Technology Officer"
    signals = await extract_reporting_from_job_posting(text, COMPANY_ID, REPORT_TITLE)

    assert len(signals) == 1
    assert signals[0].confidence == 0.70
    assert "Chief Technology Officer" in (signals[0].manager_title or "")


@pytest.mark.unit
async def test_pattern_1_reporting_line_to() -> None:
    """Sentence 4: "Reporting line to the Head of Memory Architecture"."""
    text = "Reporting line to the Head of Memory Architecture"
    signals = await extract_reporting_from_job_posting(text, COMPANY_ID, REPORT_TITLE)

    assert len(signals) == 1
    assert signals[0].confidence == 0.85
    assert "Head of Memory Architecture" in (signals[0].manager_title or "")


@pytest.mark.unit
async def test_scope_sentence_yields_no_signal() -> None:
    """Sentence 5: "will have 3 direct reports" — scope, not reports-to."""
    text = "The successful candidate will have 3 direct reports"
    signals = await extract_reporting_from_job_posting(text, COMPANY_ID, REPORT_TITLE)

    assert signals == []


@pytest.mark.unit
async def test_oversee_team_yields_no_signal() -> None:
    """Sentence 6: "oversee a team of 12 engineers" — scope, not reports-to."""
    text = "Oversee a team of 12 engineers across 3 locations"
    signals = await extract_reporting_from_job_posting(text, COMPANY_ID, REPORT_TITLE)

    assert signals == []


# ── Edge-case tests ─────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_empty_input_returns_empty_list() -> None:
    """None / empty string must not raise — returns []."""
    assert await extract_reporting_from_job_posting("", COMPANY_ID, REPORT_TITLE) == []
    # `None` is technically out-of-contract but the parser must be defensive.
    assert (
        await extract_reporting_from_job_posting(None, COMPANY_ID, REPORT_TITLE)  # type: ignore[arg-type]
        == []
    )


@pytest.mark.unit
async def test_multi_line_posting_multiple_matches() -> None:
    """Multi-line posting with two distinct matches → two signals."""
    text = (
        "About the role:\n"
        "- The role reports directly to the VP of Process Engineering.\n"
        "- Reporting line to the Head of Memory Architecture.\n"
        "- Collaborate with peer ICs across the org.\n"
    )
    signals = await extract_reporting_from_job_posting(text, COMPANY_ID, REPORT_TITLE)

    assert len(signals) == 2
    confidences = sorted(s.confidence for s in signals)
    assert confidences == [0.85, 0.88]
    assert all(s.inference_method == INFERENCE_METHOD for s in signals)
    assert all(s.manager_name is None for s in signals)


@pytest.mark.unit
async def test_case_insensitive_matching() -> None:
    """Lowercase verb start (mid-sentence) still matches under IGNORECASE."""
    text = "The candidate REPORTS DIRECTLY TO the VP of Engineering"
    signals = await extract_reporting_from_job_posting(text, COMPANY_ID, REPORT_TITLE)

    assert len(signals) == 1
    assert signals[0].confidence == 0.88


@pytest.mark.unit
async def test_duplicate_mention_deduplicated() -> None:
    """Same role mentioned twice → single signal (dedupe by title+conf)."""
    text = (
        "The role reports directly to the VP of Engineering. "
        "Then the candidate reports directly to the VP of Engineering, repeated."
    )
    signals = await extract_reporting_from_job_posting(text, COMPANY_ID, REPORT_TITLE)

    # Both mentions produce the same captured title "the VP" → dedupe to 1.
    assert len(signals) == 1


@pytest.mark.unit
async def test_signal_is_immutable_dataclass() -> None:
    """ReportingSignal is a frozen dataclass — mutation raises."""
    text = "Reporting line to the Head of Memory Architecture"
    signals = await extract_reporting_from_job_posting(text, COMPANY_ID, REPORT_TITLE)

    assert isinstance(signals[0], ReportingSignal)
    with pytest.raises(Exception):  # FrozenInstanceError
        signals[0].confidence = 0.99  # type: ignore[misc]


@pytest.mark.unit
def test_pattern_count_and_confidence_alignment() -> None:
    """Sanity: REPORTING_PATTERNS has exactly 6 entries (CLAUDE.md spec)."""
    assert len(REPORTING_PATTERNS) == 6
