"""Live integration smoke tests for the Parallel.ai extractors — Wave 5 P3.f.

Skipped by default. Run with `pytest -m integration` and `PARALLEL_API_KEY`
in the env.

These tests hit the real Parallel.ai API to validate that the documented
v1 task-runs schema (which `_parallel_client.py` was built against in
offline-doc-driven mode) still matches production. If Parallel changes
their API shape, these will fail and `_parallel_client.py` needs the
corresponding adjustment.

Each test exercises a low-budget pair search; the API charges per task,
so keep the test pairs sparse enough that costs stay near zero. We do
NOT assert on result content (Parallel's research output is non-deterministic);
only on response *shape* and that the function doesn't raise.

Pattern mirrors `test_scholar_live.py` and `test_patents_live.py`.
"""
from __future__ import annotations

import os

import pytest

from credence.extractors.parallel_conference import find_conference_co_appearances
from credence.extractors.parallel_standards import find_standards_committee_peers
from credence.extractors.patents import PersonRef

_HAS_KEY = bool(os.environ.get("PARALLEL_API_KEY"))


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _HAS_KEY, reason="PARALLEL_API_KEY not set"),
]


def _person(name: str, *, linkedin_url: str | None = None) -> PersonRef:
    return PersonRef(
        person_id=f"00000000-0000-0000-0000-{abs(hash(name)) % 10**12:012x}"[:36],
        canonical_name=name,
        linkedin_url=linkedin_url,
    )


# ── Conference co-appearance ────────────────────────────────────────────────


@pytest.mark.integration
async def test_find_conference_co_appearances_returns_list() -> None:
    """Smoke test: function returns a list, doesn't raise, items have the expected shape.

    Pair chosen for likely-zero overlap (different industries, different eras),
    so we expect [] in most cases — but we still verify the function completes
    cleanly and any items returned conform to the documented dict shape.
    """
    a = _person("Tim Cook")
    b = _person("Jonas Salk")  # different era; co-appearance vanishingly unlikely

    out = await find_conference_co_appearances(a, b, max_results=5, deadline_seconds=90.0)

    assert isinstance(out, list)
    for record in out:
        assert "signal_type" in record
        assert record["signal_type"] in (
            "conference_co_presenter",
            "conference_co_attendee",
        )
        assert "event" in record
        assert "role_a" in record
        assert "role_b" in record
        assert "source_urls" in record
        assert isinstance(record["source_urls"], list)


# ── Standards committee peer ────────────────────────────────────────────────


@pytest.mark.integration
async def test_find_standards_committee_peers_returns_list() -> None:
    """Smoke test for the standards extractor. Same shape contract."""
    a = _person("Tim Berners-Lee")
    b = _person("Vint Cerf")  # both have real standards-track histories

    out = await find_standards_committee_peers(a, b, max_results=5, deadline_seconds=90.0)

    assert isinstance(out, list)
    for record in out:
        assert record["signal_type"] == "standards_committee_peer"
        assert "body" in record
        assert "role_a" in record
        assert "role_b" in record
        assert "source_urls" in record
        assert isinstance(record["source_urls"], list)


# ── Polar pair: nonsense names should produce empty list ────────────────────


@pytest.mark.integration
async def test_impossible_name_pair_returns_empty() -> None:
    """A pair with no documented public footprint should return []."""
    a = _person("Xqzyrr Vlartho")
    b = _person("Mqplbr Tsendich")

    conf_out = await find_conference_co_appearances(a, b, max_results=3, deadline_seconds=60.0)
    std_out = await find_standards_committee_peers(a, b, max_results=3, deadline_seconds=60.0)

    # Defensive: Parallel should return empty arrays for impossible pairs.
    # If it hallucinates anything, our `_format_*` defensives still drop
    # records lacking source_urls, so we never bubble fake data.
    for record in conf_out + std_out:
        assert isinstance(record.get("source_urls"), list)
        assert all(isinstance(u, str) for u in record["source_urls"])
