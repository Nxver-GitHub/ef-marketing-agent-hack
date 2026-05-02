"""Press release named-officer reporting extractor (Z.AI LLM).

Extracts named reporting relationships from press release text using the
same Z.AI client pattern as ``credence.chat``. The extractor is purely an
I/O wrapper — it does not call ``ingest_explicit_edge`` itself. Producers
hooking this into the pipeline (e.g., a future Apify/LLM press release
crawler) are responsible for entity resolution and edge ingestion.

Cost guard: skips the LLM call entirely unless the text contains at least
one ``LEADERSHIP_VERBS`` token. This avoids burning tokens on Q4 earnings
boilerplate, product announcements, and similar non-org-chart releases.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from credence.config import get_settings

log = logging.getLogger(__name__)


LEADERSHIP_VERBS: frozenset[str] = frozenset(
    {
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
)


@dataclass(frozen=True, slots=True)
class PressReleaseReportingSignal:
    """A single named reporting relationship pulled from press-release text.

    Distinct from ``ReportingSignal`` in ``job_postings.py`` — different
    fields (``person_name``/``reports_to_name`` vs ``report_person_id``)
    and a different ``inference_method``.
    """

    person_name: str
    person_title: str | None
    reports_to_name: str | None
    reports_to_title: str | None
    confidence: float
    inference_method: str  # always 'press_release_llm'


_SYSTEM_PROMPT = "You are an org chart signal extractor."

_USER_PROMPT_TEMPLATE = (
    "Extract all named reporting relationships from the following press "
    "release excerpt.\n"
    "Return JSON array only.\n"
    "Format: [{{\"person_name\": str, \"person_title\": str, "
    "\"reports_to_name\": str|null, \"reports_to_title\": str|null, "
    "\"confidence\": float}}]\n"
    "If none found, return [].\n"
    "Confidence: 0.95 if explicit (\"reporting to X\"), 0.80 if implied "
    "(\"joining under X\"), 0.70 if inferred from context.\n"
    "Text: {text}"
)


def _has_leadership_verb(text: str) -> bool:
    lowered = text.lower()
    return any(verb in lowered for verb in LEADERSHIP_VERBS)


def _build_default_client() -> tuple[AsyncOpenAI, str]:
    s = get_settings()
    client = AsyncOpenAI(api_key=s.zai_api_key, base_url=s.zai_base_url)
    return client, s.zai_model


def _clamp_confidence(value: Any) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.0
    if c < 0.0:
        return 0.0
    if c > 1.0:
        return 1.0
    return c


def _coerce_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def _validate_entry(entry: Any) -> PressReleaseReportingSignal | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("person_name")
    if not isinstance(name, str) or not name.strip():
        return None
    return PressReleaseReportingSignal(
        person_name=name.strip(),
        person_title=_coerce_str_or_none(entry.get("person_title")),
        reports_to_name=_coerce_str_or_none(entry.get("reports_to_name")),
        reports_to_title=_coerce_str_or_none(entry.get("reports_to_title")),
        confidence=_clamp_confidence(entry.get("confidence", 0.0)),
        inference_method="press_release_llm",
    )


def _parse_llm_content(content: str) -> list[PressReleaseReportingSignal]:
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        log.warning("press_release_llm: non-JSON response, dropping")
        return []
    if not isinstance(parsed, list):
        log.warning("press_release_llm: expected list, got %s", type(parsed).__name__)
        return []
    signals: list[PressReleaseReportingSignal] = []
    for entry in parsed:
        sig = _validate_entry(entry)
        if sig is not None:
            signals.append(sig)
    return signals


async def extract_reporting_from_press_release(
    text: str,
    *,
    client: AsyncOpenAI | None = None,
    model: str | None = None,
) -> list[PressReleaseReportingSignal]:
    """Extract named reporting relationships from a press release excerpt.

    Returns ``[]`` on:
      * absence of any LEADERSHIP_VERBS in ``text`` (cost guard)
      * any LLM error (network, auth, schema)
      * non-JSON LLM response
      * empty / malformed JSON arrays

    The function never raises. Callers can safely treat ``[]`` as
    "no extractable signals" without distinguishing failure modes.
    """
    if not text or not _has_leadership_verb(text):
        return []

    if client is None:
        client, default_model = _build_default_client()
        if model is None:
            model = default_model
    elif model is None:
        # Caller injected a client but not a model — fall back to settings.
        try:
            model = get_settings().zai_model
        except Exception:  # pragma: no cover — only hit in misconfigured tests
            model = "glm-4.6"

    user_prompt = _USER_PROMPT_TEMPLATE.format(text=text)

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 — extractor must never raise
        log.warning("press_release_llm: LLM call failed: %s", exc)
        return []

    try:
        content = resp.choices[0].message.content or ""
    except (AttributeError, IndexError) as exc:
        log.warning("press_release_llm: malformed response shape: %s", exc)
        return []

    return _parse_llm_content(content)
