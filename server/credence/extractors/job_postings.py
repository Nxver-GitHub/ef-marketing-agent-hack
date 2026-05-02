"""Job-posting reports-to extractor — Phase 2 Task 2-B.

Pure I/O wrapper that scans a job-posting text body for explicit reporting
relationships using the regex patterns defined in CLAUDE.md
(`REPORTING_PATTERNS`). Returns a list of `ReportingSignal` instances; the
caller is responsible for entity resolution and bridging to
`ingest_explicit_edge`.

Module follows the convention of `credence.extractors.apollo` — extractors
remain pure parsers without side effects, so they can be invoked from the
ingestion pipeline, from REPL/scripts, or from tests without DB access.

The patterns are copied verbatim from CLAUDE.md §"NLP Extraction Patterns
— Org Chart Signals". Confidence values per Task 2-B in
`orgchart_tasks.md` (Phase 2 Task 2-B).

Pipeline integration: when an Apify/Firecrawl job-posting crawler ships
and starts writing `signal_type='job_posting'` rows into Supabase, that
producer should call `extract_reporting_from_job_posting()` after the raw
posting is persisted, then run entity resolution on each signal's
`manager_title`, then call `ingest_explicit_edge()` (from
`credence.orgchart.hierarchy`) for each resolved/unresolved match.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── Patterns (copied verbatim from CLAUDE.md L313-320) ──────────────────────
#
# Source of truth: CLAUDE.md §"NLP Extraction Patterns — Org Chart Signals",
# `REPORTING_PATTERNS` list at lines 313-320 (as of 2026-05-01).
#
# Conflict resolution: orgchart_tasks.md Task 2-B's reference table predicts
# behavior for 6 sample sentences; the verbatim CLAUDE.md regexes diverge
# from that table on 2 sentences (Sentence 2 "Dr. Wei Chen" and Sentence 3
# "dotted line"). Per user directive 2026-05-01, **CLAUDE.md is authoritative**
# — patterns stay verbatim, test assertions match the regexes' actual
# behavior, divergences are documented in test comments rather than papered
# over by amending the patterns. If the task table is ever to become the
# ground truth, CLAUDE.md is the file that has to change first.

REPORTING_PATTERNS: tuple[str, ...] = (
    r"reports\s+(?:directly\s+)?to\s+(?:the\s+)?([A-Z][^,.]+(?:Officer|President|Director|VP|Head|Manager|Lead))",
    r"reporting\s+line\s+to\s+([A-Z][^,.]+)",
    r"will\s+work\s+(?:closely\s+)?(?:with|under)\s+([A-Z][^,.]+)\s*,\s*(?:the\s+)?(?:VP|SVP|Director|Head)",
    r"dotted\s+line\s+to\s+([A-Z][^,.]+)",
    r"(?:direct|indirect)\s+report\s+to\s+([A-Z][^,.]+)",
    r"management\s+chain.*?(?:VP|SVP|Director|Head)\s+of\s+([^,.]+)",
)

# Confidence per pattern index — see Task 2-B in orgchart_tasks.md.
# If the pattern list ever grows past 6, every additional pattern defaults to
# the lowest tier (0.65) — flagged in CLAUDE.md as the "weakest signal" floor.
_PATTERN_CONFIDENCES: tuple[float, ...] = (0.88, 0.85, 0.75, 0.70, 0.82, 0.65)
_DEFAULT_CONFIDENCE = 0.65

INFERENCE_METHOD = "job_posting_nlp"


# ── Dataclass ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReportingSignal:
    """Immutable explicit reporting-edge signal extracted from text."""

    report_person_id: str
    manager_name: str | None
    manager_title: str | None
    confidence: float
    inference_method: str


# ── Helpers ─────────────────────────────────────────────────────────────────


def _confidence_for(index: int) -> float:
    if 0 <= index < len(_PATTERN_CONFIDENCES):
        return _PATTERN_CONFIDENCES[index]
    return _DEFAULT_CONFIDENCE


# ── Public API ──────────────────────────────────────────────────────────────


async def extract_reporting_from_job_posting(
    job_posting_text: str,
    company_id: str,
    report_title: str,
) -> list[ReportingSignal]:
    """Extract reporting relationships from a job-posting text body.

    Args:
        job_posting_text: Raw posting text (may be multi-line).
        company_id: Company UUID — accepted for caller convenience and
            future signal-write hooks. Not currently used by the parser.
        report_title: The title of the role described by the posting —
            accepted for caller convenience (mirrors ReportingSignal
            shape used elsewhere). Not currently used by the parser.

    Returns:
        List of `ReportingSignal`, one per *deduplicated* regex match.
        Returns `[]` for empty / None input.
    """
    # Defensive parsing: empty / None input → no signals, no error.
    if not job_posting_text:
        return []

    del company_id, report_title  # Reserved for future use; silence linter.

    seen: set[tuple[str, float]] = set()
    signals: list[ReportingSignal] = []

    for index, pattern in enumerate(REPORTING_PATTERNS):
        try:
            matches = re.finditer(pattern, job_posting_text, flags=re.IGNORECASE)
            confidence = _confidence_for(index)

            for match in matches:
                raw_title = match.group(1)
                if not raw_title:
                    continue
                manager_title = raw_title.strip()
                if not manager_title:
                    continue

                dedupe_key = (manager_title.lower(), confidence)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)

                signals.append(
                    ReportingSignal(
                        report_person_id="",  # Caller fills this in.
                        manager_name=None,
                        manager_title=manager_title,
                        confidence=confidence,
                        inference_method=INFERENCE_METHOD,
                    )
                )
        except re.error as exc:  # pragma: no cover - defensive
            logger.warning(
                "REPORTING_PATTERNS index %d failed to compile/match: %s",
                index,
                exc,
            )
            continue

    return signals
