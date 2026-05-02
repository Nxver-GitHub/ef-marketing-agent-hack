"""Unit tests for ``credence.onboarding.entity_resolver``.

The resolver is asyncpg-native — it expects a real ``conn.fetchrow`` /
``conn.execute`` shape plus a ``conn.transaction()`` context manager.
We don't have (and don't want) a live Postgres for unit tests, so we
mirror the FakeDB pattern from ``test_search_org_context.py`` and add
a substring-router that records every SQL statement seen.

Each test queues canned responses for the substrings it cares about
(e.g. ``"WHERE LOWER(linkedin_url)"`` for the Tier-1 lookup) and then
inspects the recorded calls + the returned ``ResolvedTeamMember``.

Coverage:
  1. Tier-1 hit (linkedin_url exact)
  2. Tier-1 hit triggers UPDATE on missing fields
  3. Tier-2 hit (canonical_name + company_id)
  4. Tier-2 hit triggers UPDATE on missing linkedin_url
  5. Tier-3 INSERT path
  6. Tier-3 derives seniority_score via taxonomy
  7. Tier-3 derives functional_domain via taxonomy
  8. account_team_members upserted on every path
  9. account_team_members ON CONFLICT updates scrape_status='done'
 10. Idempotency — second call returns same person_id, was_new=False
 11. canonical_name normalization (whitespace, case)
 12. company URL slug normalization (`_normalize_company_url`)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from credence.onboarding import entity_resolver
from credence.onboarding.entity_resolver import (
    ResolvedTeamMember,
    _normalize_company_url,
    _normalize_linkedin_url,
    resolve_or_insert_team_member,
)

ACCOUNT_ID = UUID("00000000-0000-0000-0000-aaaa00000001")
COMPANY_ID = UUID("00000000-0000-0000-0000-cccc00000001")


# ─── ScrapedEmployee stub (matches the Wave-A3 shape) ──────────────────────


@dataclass(frozen=True)
class ScrapedEmployeeStub:
    """Minimal in-test mirror of the Wave-A3 ``ScrapedEmployee`` shape."""

    name: str
    title: str | None = None
    linkedin_url: str | None = None
    headline: str | None = None
    company_url: str | None = None
    company_name: str | None = None


# ─── FakeConn — substring SQL router with call recording ───────────────────


class FakeConn:
    """Stand-in for ``asyncpg.Connection`` used by the resolver.

    Tests register canned responses keyed off SQL substrings — the
    resolver issues only a handful of queries and each one has a
    distinctive substring (``WHERE LOWER(linkedin_url)``,
    ``canonical_name = $1`` + company match, ``INSERT INTO public.persons``,
    ``INSERT INTO public.account_team_members``). First substring match
    wins per call so test setup stays declarative.

    Every call is recorded on ``self.calls`` so tests can assert on
    SQL + bound args (e.g. confirming the UPDATE actually fired).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._fetchrow: list[tuple[str, dict[str, Any] | None]] = []
        self._execute: list[tuple[str, str]] = []
        self.transaction_count: int = 0

    # ── Test setup helpers ─────────────────────────────────────────────

    def on_fetchrow(self, substring: str, response: dict[str, Any] | None) -> None:
        self._fetchrow.append((substring, response))

    def on_execute(self, substring: str, response: str = "UPDATE 1") -> None:
        self._execute.append((substring, response))

    # ── asyncpg.Connection surface ─────────────────────────────────────

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((sql, args))
        for substr, resp in self._fetchrow:
            if substr in sql:
                return resp
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        for substr, resp in self._execute:
            if substr in sql:
                return resp
        return "UPDATE 0"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return []

    def transaction(self) -> Any:
        outer = self

        @asynccontextmanager
        async def _txn() -> Any:
            outer.transaction_count += 1
            yield

        return _txn()

    # ── Convenience accessors ──────────────────────────────────────────

    def sqls(self) -> list[str]:
        return [c[0] for c in self.calls]

    def find_call(self, substring: str) -> tuple[str, tuple[Any, ...]] | None:
        for sql, args in self.calls:
            if substring in sql:
                return sql, args
        return None


# ─── Helpers ───────────────────────────────────────────────────────────────


def _person_row(person_id: UUID, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": person_id,
        "current_title": None,
        "headline": None,
        "current_company_id": None,
        "current_seniority_score": None,
        "current_functional_domain": None,
        "linkedin_url": None,
    }
    base.update(overrides)
    return base


# ─── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_normalize_company_url_extracts_slug() -> None:
    assert _normalize_company_url("https://www.linkedin.com/company/foo/") == "foo"
    assert _normalize_company_url("https://linkedin.com/company/Foo-Bar") == "foo-bar"
    assert _normalize_company_url("/company/baz/about/") == "baz"
    assert _normalize_company_url(None) is None
    assert _normalize_company_url("   ") is None


@pytest.mark.unit
async def test_normalize_linkedin_url_strips_and_lowercases() -> None:
    assert (
        _normalize_linkedin_url("https://www.linkedin.com/in/Alice/")
        == "https://www.linkedin.com/in/alice"
    )
    assert _normalize_linkedin_url("  ") is None
    assert _normalize_linkedin_url(None) is None


@pytest.mark.unit
async def test_tier1_linkedin_match_returns_existing_person() -> None:
    conn = FakeConn()
    existing_pid = uuid4()
    atm_id = uuid4()

    conn.on_fetchrow(
        "WHERE LOWER(linkedin_url)",
        _person_row(
            existing_pid,
            current_title="Director of Sales",
            headline="Director of Sales at Foo",
            current_company_id=COMPANY_ID,
            current_seniority_score=60,
            current_functional_domain="sales_marketing",
        ),
    )
    conn.on_fetchrow("INSERT INTO public.account_team_members", {"id": atm_id})

    employee = ScrapedEmployeeStub(
        name="Alice Smith",
        title="Director of Sales",
        linkedin_url="https://linkedin.com/in/alice/",
    )

    result = await resolve_or_insert_team_member(
        employee, ACCOUNT_ID, COMPANY_ID, conn  # type: ignore[arg-type]
    )

    assert isinstance(result, ResolvedTeamMember)
    assert result.person_id == existing_pid
    assert result.was_new is False
    assert result.account_team_member_id == atm_id
    assert conn.transaction_count == 1
    # Tier-1 query fired with the lowercased URL
    tier1 = conn.find_call("WHERE LOWER(linkedin_url)")
    assert tier1 is not None
    assert tier1[1][0] == "https://linkedin.com/in/alice"


@pytest.mark.unit
async def test_tier1_hit_updates_missing_fields() -> None:
    conn = FakeConn()
    existing_pid = uuid4()
    # Existing row: company set, but title and headline missing
    conn.on_fetchrow(
        "WHERE LOWER(linkedin_url)",
        _person_row(existing_pid, current_company_id=COMPANY_ID),
    )
    conn.on_fetchrow("INSERT INTO public.account_team_members", {"id": uuid4()})
    conn.on_execute("UPDATE public.persons", "UPDATE 1")

    employee = ScrapedEmployeeStub(
        name="Bob Jones",
        title="VP of Engineering",
        headline="VP Eng at Bar",
        linkedin_url="https://linkedin.com/in/bob",
    )

    await resolve_or_insert_team_member(
        employee, ACCOUNT_ID, COMPANY_ID, conn  # type: ignore[arg-type]
    )

    update_call = conn.find_call("UPDATE public.persons")
    assert update_call is not None
    sql, args = update_call
    # args = (person_id, title, headline, linkedin_url, company_id, seniority, domain)
    assert args[0] == existing_pid
    assert args[1] == "VP of Engineering"
    assert args[2] == "VP Eng at Bar"
    assert args[3] == "https://linkedin.com/in/bob"


@pytest.mark.unit
async def test_tier2_match_on_name_plus_company() -> None:
    conn = FakeConn()
    existing_pid = uuid4()
    atm_id = uuid4()

    # Tier 1 misses (no linkedin_url on input → query never even runs)
    conn.on_fetchrow(
        "canonical_name = $1",
        _person_row(existing_pid, current_company_id=COMPANY_ID),
    )
    conn.on_fetchrow("INSERT INTO public.account_team_members", {"id": atm_id})
    conn.on_execute("UPDATE public.persons", "UPDATE 1")

    employee = ScrapedEmployeeStub(name="Carol Lee", title="Engineer")

    result = await resolve_or_insert_team_member(
        employee, ACCOUNT_ID, COMPANY_ID, conn  # type: ignore[arg-type]
    )
    assert result.person_id == existing_pid
    assert result.was_new is False
    assert result.account_team_member_id == atm_id

    tier2 = conn.find_call("canonical_name = $1")
    assert tier2 is not None
    assert tier2[1] == ("Carol Lee", COMPANY_ID)


@pytest.mark.unit
async def test_tier2_hit_updates_missing_linkedin_url() -> None:
    conn = FakeConn()
    existing_pid = uuid4()
    conn.on_fetchrow(
        "WHERE LOWER(linkedin_url)",
        None,  # Tier 1 miss
    )
    conn.on_fetchrow(
        "canonical_name = $1",
        _person_row(existing_pid, current_company_id=COMPANY_ID),
    )
    conn.on_fetchrow("INSERT INTO public.account_team_members", {"id": uuid4()})
    conn.on_execute("UPDATE public.persons", "UPDATE 1")

    employee = ScrapedEmployeeStub(
        name="Dana Yu",
        title="Sales Manager",
        linkedin_url="https://linkedin.com/in/danayu/",
    )

    await resolve_or_insert_team_member(
        employee, ACCOUNT_ID, COMPANY_ID, conn  # type: ignore[arg-type]
    )

    update_call = conn.find_call("UPDATE public.persons")
    assert update_call is not None
    _, args = update_call
    # linkedin_url arg (index 3) should be the normalized scrape value so
    # the COALESCE in the SQL fills the existing NULL.
    assert args[3] == "https://linkedin.com/in/danayu"


@pytest.mark.unit
async def test_tier3_insert_returns_new_person_id() -> None:
    conn = FakeConn()
    new_pid = uuid4()
    atm_id = uuid4()

    # Both lookups miss
    conn.on_fetchrow("WHERE LOWER(linkedin_url)", None)
    conn.on_fetchrow("canonical_name = $1", None)
    conn.on_fetchrow("INSERT INTO public.persons", {"id": new_pid})
    conn.on_fetchrow("INSERT INTO public.account_team_members", {"id": atm_id})

    employee = ScrapedEmployeeStub(
        name="Eve Park",
        title="Account Executive",
        linkedin_url="https://linkedin.com/in/evepark",
    )

    result = await resolve_or_insert_team_member(
        employee, ACCOUNT_ID, COMPANY_ID, conn  # type: ignore[arg-type]
    )

    assert result.person_id == new_pid
    assert result.was_new is True
    assert result.account_team_member_id == atm_id

    insert_call = conn.find_call("INSERT INTO public.persons")
    assert insert_call is not None
    _, args = insert_call
    # args = (canonical_name, name_variants, linkedin_url, headline, title,
    #         company_id, seniority, domain, account_id)
    assert args[0] == "Eve Park"
    assert args[1] == ["Eve Park"]
    assert args[2] == "https://linkedin.com/in/evepark"
    assert args[5] == COMPANY_ID
    assert args[8] == ACCOUNT_ID


@pytest.mark.unit
async def test_tier3_insert_extracts_seniority_score_from_title() -> None:
    """Title 'VP of Sales' → taxonomy.seniority_from_title returns 70."""
    conn = FakeConn()
    new_pid = uuid4()
    conn.on_fetchrow("WHERE LOWER(linkedin_url)", None)
    conn.on_fetchrow("canonical_name = $1", None)
    conn.on_fetchrow("INSERT INTO public.persons", {"id": new_pid})
    conn.on_fetchrow("INSERT INTO public.account_team_members", {"id": uuid4()})

    employee = ScrapedEmployeeStub(name="Frank Lin", title="VP of Sales")
    await resolve_or_insert_team_member(
        employee, ACCOUNT_ID, COMPANY_ID, conn  # type: ignore[arg-type]
    )

    insert_call = conn.find_call("INSERT INTO public.persons")
    assert insert_call is not None
    _, args = insert_call
    # args[6] = seniority_score (VP → 70 per taxonomy)
    assert args[6] == 70


@pytest.mark.unit
async def test_tier3_insert_extracts_functional_domain_from_title() -> None:
    """Title 'Director of Marketing' → 'sales_marketing' per taxonomy."""
    conn = FakeConn()
    new_pid = uuid4()
    conn.on_fetchrow("WHERE LOWER(linkedin_url)", None)
    conn.on_fetchrow("canonical_name = $1", None)
    conn.on_fetchrow("INSERT INTO public.persons", {"id": new_pid})
    conn.on_fetchrow("INSERT INTO public.account_team_members", {"id": uuid4()})

    employee = ScrapedEmployeeStub(name="Gina Hu", title="Director of Marketing")
    await resolve_or_insert_team_member(
        employee, ACCOUNT_ID, COMPANY_ID, conn  # type: ignore[arg-type]
    )

    insert_call = conn.find_call("INSERT INTO public.persons")
    assert insert_call is not None
    _, args = insert_call
    # args[7] = functional_domain
    assert args[7] == "sales_marketing"


@pytest.mark.unit
async def test_team_member_upserted_on_every_path() -> None:
    """Tier 1, Tier 2, and Tier 3 all hit the team_member upsert."""
    paths_tried = 0
    for tier_setup in (
        # Tier-1 hit
        lambda c, pid: (
            c.on_fetchrow("WHERE LOWER(linkedin_url)", _person_row(pid)),
        ),
        # Tier-2 hit
        lambda c, pid: (
            c.on_fetchrow("WHERE LOWER(linkedin_url)", None),
            c.on_fetchrow("canonical_name = $1", _person_row(pid)),
        ),
        # Tier-3 insert
        lambda c, pid: (
            c.on_fetchrow("WHERE LOWER(linkedin_url)", None),
            c.on_fetchrow("canonical_name = $1", None),
            c.on_fetchrow("INSERT INTO public.persons", {"id": pid}),
        ),
    ):
        conn = FakeConn()
        pid = uuid4()
        atm_id = uuid4()
        tier_setup(conn, pid)
        conn.on_fetchrow("INSERT INTO public.account_team_members", {"id": atm_id})
        conn.on_execute("UPDATE public.persons", "UPDATE 1")

        employee = ScrapedEmployeeStub(
            name="Hank Mu",
            title="Engineer",
            linkedin_url="https://linkedin.com/in/hank",
        )
        result = await resolve_or_insert_team_member(
            employee, ACCOUNT_ID, COMPANY_ID, conn  # type: ignore[arg-type]
        )
        assert result.account_team_member_id == atm_id
        assert (
            conn.find_call("INSERT INTO public.account_team_members") is not None
        )
        paths_tried += 1

    assert paths_tried == 3


@pytest.mark.unit
async def test_team_member_on_conflict_sets_scrape_status_done() -> None:
    """The team_member upsert SQL must include ON CONFLICT DO UPDATE
    that overwrites scrape_status to 'done'."""
    conn = FakeConn()
    conn.on_fetchrow("WHERE LOWER(linkedin_url)", _person_row(uuid4()))
    conn.on_fetchrow("INSERT INTO public.account_team_members", {"id": uuid4()})

    employee = ScrapedEmployeeStub(
        name="Ivy Park", title="Engineer", linkedin_url="https://linkedin.com/in/ivy"
    )
    await resolve_or_insert_team_member(
        employee, ACCOUNT_ID, COMPANY_ID, conn  # type: ignore[arg-type]
    )

    atm_call = conn.find_call("INSERT INTO public.account_team_members")
    assert atm_call is not None
    sql = atm_call[0]
    assert "ON CONFLICT (account_id, person_id)" in sql
    assert "scrape_status = 'done'" in sql
    assert "scraped_at = NOW()" in sql
    # COALESCE on linkedin_url so existing values aren't nulled out
    assert "COALESCE(EXCLUDED.linkedin_url" in sql


@pytest.mark.unit
async def test_idempotency_second_call_returns_same_person_id() -> None:
    """Calling twice with the same input → same person_id, was_new=False."""
    existing_pid = uuid4()
    employee = ScrapedEmployeeStub(
        name="Jack Wei",
        title="Engineer",
        linkedin_url="https://linkedin.com/in/jackwei",
    )

    # First call — Tier 3 insert
    conn1 = FakeConn()
    conn1.on_fetchrow("WHERE LOWER(linkedin_url)", None)
    conn1.on_fetchrow("canonical_name = $1", None)
    conn1.on_fetchrow("INSERT INTO public.persons", {"id": existing_pid})
    conn1.on_fetchrow("INSERT INTO public.account_team_members", {"id": uuid4()})

    first = await resolve_or_insert_team_member(
        employee, ACCOUNT_ID, COMPANY_ID, conn1  # type: ignore[arg-type]
    )
    assert first.was_new is True
    assert first.person_id == existing_pid

    # Second call — Tier 1 hit (same linkedin_url)
    conn2 = FakeConn()
    conn2.on_fetchrow(
        "WHERE LOWER(linkedin_url)",
        _person_row(
            existing_pid,
            current_title="Engineer",
            current_company_id=COMPANY_ID,
        ),
    )
    conn2.on_fetchrow("INSERT INTO public.account_team_members", {"id": uuid4()})
    conn2.on_execute("UPDATE public.persons", "UPDATE 1")

    second = await resolve_or_insert_team_member(
        employee, ACCOUNT_ID, COMPANY_ID, conn2  # type: ignore[arg-type]
    )
    assert second.was_new is False
    assert second.person_id == existing_pid


@pytest.mark.unit
async def test_canonical_name_normalization_whitespace_and_prefix() -> None:
    """`Dr. James R. Clarke, Jr.   ` → canonical 'James Clarke'."""
    conn = FakeConn()
    new_pid = uuid4()
    conn.on_fetchrow("WHERE LOWER(linkedin_url)", None)
    conn.on_fetchrow("canonical_name = $1", None)
    conn.on_fetchrow("INSERT INTO public.persons", {"id": new_pid})
    conn.on_fetchrow("INSERT INTO public.account_team_members", {"id": uuid4()})

    employee = ScrapedEmployeeStub(
        name="  Dr. James R. Clarke, Jr.  ",
        title="Engineer",
    )
    await resolve_or_insert_team_member(
        employee, ACCOUNT_ID, COMPANY_ID, conn  # type: ignore[arg-type]
    )

    insert_call = conn.find_call("INSERT INTO public.persons")
    assert insert_call is not None
    _, args = insert_call
    assert args[0] == "James Clarke"
    assert args[1] == ["James Clarke"]


@pytest.mark.unit
async def test_company_url_slug_normalization_logged_does_not_break_resolve() -> None:
    """Verifies that a scraped company_url field doesn't trip the path —
    it's used only for logging/dedupe, not entity matching."""
    conn = FakeConn()
    new_pid = uuid4()
    conn.on_fetchrow("WHERE LOWER(linkedin_url)", None)
    conn.on_fetchrow("canonical_name = $1", None)
    conn.on_fetchrow("INSERT INTO public.persons", {"id": new_pid})
    conn.on_fetchrow("INSERT INTO public.account_team_members", {"id": uuid4()})

    employee = ScrapedEmployeeStub(
        name="Karen Singh",
        title="Engineer",
        company_url="https://www.linkedin.com/company/foo/about/",
        company_name="Foo Inc",
    )

    result = await resolve_or_insert_team_member(
        employee, ACCOUNT_ID, COMPANY_ID, conn  # type: ignore[arg-type]
    )
    assert result.was_new is True
    # Direct test of the helper too — proves the slug parser handles
    # the trailing /about/ path that LinkedIn often appends.
    assert _normalize_company_url(employee.company_url) == "foo"


@pytest.mark.unit
async def test_missing_name_raises_value_error() -> None:
    """Defensive: a scrape with no usable name surfaces as ValueError so
    the orchestrator can record + skip it without crashing the batch."""
    conn = FakeConn()
    employee = ScrapedEmployeeStub(name="", title="Engineer")
    with pytest.raises(ValueError):
        await resolve_or_insert_team_member(
            employee, ACCOUNT_ID, COMPANY_ID, conn  # type: ignore[arg-type]
        )
    # No persistence side effects
    assert conn.transaction_count == 0


@pytest.mark.unit
async def test_module_exports_are_stable() -> None:
    """Public API contract — keep the export surface tight."""
    assert "ResolvedTeamMember" in entity_resolver.__all__
    assert "resolve_or_insert_team_member" in entity_resolver.__all__
