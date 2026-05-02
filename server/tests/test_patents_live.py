"""Live-API smoke test for the USPTO PatentsView extractor — J.4.5.

Skipped by default. Run when you have internet access to confirm the canned
PatentsView response shape used in `test_patents.py` still matches reality:

    pytest tests/test_patents_live.py -m integration -v

This is the trade-off LavenderPrairie's J.4 implementation made: doc-driven
implementation + thoroughly mocked unit tests + this single live smoke test
deferred to whoever has internet. Per CLAUDE.md L548 ("Pattern 1: Explore
before implement"), the canonical approach is to REPL-explore first; the
sandbox J.4 was written in had no DNS, so this test is the catch-up.

What it asserts:
- The endpoint returns HTTP 200
- The response body has a `patents` key containing a list
- At least one patent has the documented top-level fields
- A patent's inventors[] entries have the documented `inventor_name_first`
  / `inventor_name_last` keys (or note the schema drift in failure msg)

If this test fails, `_format_patent_record` and `_inventor_matches_person`
in `credence.extractors.patents` need adjustment to match the real shape.
"""
from __future__ import annotations

import json

import httpx
import pytest

from credence.extractors.patents import (
    PATENTSVIEW_BASE_URL,
    PersonRef,
    find_patent_co_inventions,
)


@pytest.mark.integration
async def test_patentsview_endpoint_responds_with_documented_shape() -> None:
    """Single GET to /patent/ verifies the response matches the schema we built against."""
    url = PATENTSVIEW_BASE_URL.rstrip("/") + "/patent/"
    # Use a high-volume inventor name so we always get hits.
    q = {
        "_and": [
            {"_contains": {"inventors.inventor_name_first": "John"}},
            {"_contains": {"inventors.inventor_name_last": "Smith"}},
        ]
    }
    f = [
        "patent_id",
        "patent_title",
        "patent_date",
        "patent_filing_date",
        "inventors.inventor_id",
        "inventors.inventor_name_first",
        "inventors.inventor_name_last",
        "assignees.assignee_organization",
    ]

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            url,
            params={"q": json.dumps(q), "f": json.dumps(f), "o": json.dumps({"size": 3})},
        )

    assert r.status_code == 200, f"PatentsView returned {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert "patents" in body, f"missing `patents` key — schema drift? body keys: {list(body.keys())}"
    patents = body["patents"]
    assert isinstance(patents, list), "`patents` should be a list"
    assert patents, "expected at least one hit for a common inventor name"

    sample = patents[0]
    # At least one of the documented identifier keys
    assert "patent_id" in sample or "patent_number" in sample, (
        f"no identifier key on patent record — schema drift? keys: {list(sample.keys())}"
    )
    # Inventors array with the documented field names
    inventors = sample.get("inventors")
    assert isinstance(inventors, list) and inventors, "patent should have inventors[]"
    inv = inventors[0]
    has_first = "inventor_name_first" in inv
    has_last = "inventor_name_last" in inv
    assert has_first and has_last, (
        f"inventor entry missing first/last name — schema drift? keys: {list(inv.keys())}"
    )


@pytest.mark.integration
async def test_find_patent_co_inventions_against_live_api() -> None:
    """Run the real extractor against PatentsView for a known co-invention pair.

    NOTE — replace `PERSON_A` and `PERSON_B` with a known co-inventor pair
    if you want to assert non-empty results. As written, this just asserts
    the function returns a list (possibly empty) without raising.
    """
    # Use generic names; result may be empty but the function must not raise.
    person_a = PersonRef(person_id="p:a", canonical_name="Wei Chen")
    person_b = PersonRef(person_id="p:b", canonical_name="John Smith")

    result = await find_patent_co_inventions(person_a, person_b, max_results=3)

    # Allow empty (these particular names may not co-invent), but the
    # structure must be correct when populated.
    assert isinstance(result, list)
    for record in result:
        assert "patent_number" in record
        assert "patent_title" in record
        assert "filing_date" in record
        assert "assignee" in record
