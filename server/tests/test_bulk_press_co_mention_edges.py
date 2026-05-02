"""Pure-function + light-mock tests for bulk_press_co_mention_edges.

DB-touching code paths (the orchestrator + the upsert SQL) are exercised
via ``unittest.mock.AsyncMock`` so the suite stays fast and deterministic.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from credence.jobs import bulk_press_co_mention_edges as job


# ── _pair_executives ────────────────────────────────────────────────────────


@pytest.mark.unit
class TestPairExecutives:
    def test_two_names_yields_one_pair(self) -> None:
        pairs = job._pair_executives(["Jane Smith", "John Doe"])
        assert pairs == [("Jane Smith", "John Doe")]

    def test_three_names_yields_three_pairs(self) -> None:
        pairs = job._pair_executives(["Alice Adams", "Bob Brown", "Carol Chen"])
        assert len(pairs) == 3
        assert ("Alice Adams", "Bob Brown") in pairs
        assert ("Alice Adams", "Carol Chen") in pairs
        assert ("Bob Brown", "Carol Chen") in pairs

    def test_five_names_yields_ten_pairs(self) -> None:
        names = [f"Person{i} Last{i}" for i in range(5)]
        pairs = job._pair_executives(names)
        assert len(pairs) == 10  # 5 choose 2

    def test_dedupes_case_insensitive(self) -> None:
        pairs = job._pair_executives(["Jane Smith", "JANE SMITH", "John Doe"])
        # "JANE SMITH" duplicate collapses; only one pair remains.
        assert len(pairs) == 1
        assert pairs[0][1] == "John Doe"

    def test_drops_empty_and_non_strings(self) -> None:
        pairs = job._pair_executives(["Jane Smith", "", "  ", None])  # type: ignore[list-item]
        assert pairs == []

    def test_collapses_whitespace(self) -> None:
        pairs = job._pair_executives(["  Jane   Smith ", "John Doe"])
        assert pairs == [("Jane Smith", "John Doe")]


# ── _recency_factor ────────────────────────────────────────────────────────


@pytest.mark.unit
class TestRecencyFactor:
    def test_zero_years_is_one(self) -> None:
        assert job._recency_factor(0) == pytest.approx(1.0)

    def test_one_year_decay(self) -> None:
        assert job._recency_factor(1) == pytest.approx(math.exp(-0.10 * 1))

    def test_five_year_decay(self) -> None:
        assert job._recency_factor(5) == pytest.approx(math.exp(-0.10 * 5))

    def test_ten_year_decay(self) -> None:
        assert job._recency_factor(10) == pytest.approx(math.exp(-0.10 * 10))

    def test_negative_years_clamps_to_zero(self) -> None:
        # A press release dated in the future shouldn't boost the strength.
        assert job._recency_factor(-3) == pytest.approx(1.0)

    def test_decays_faster_than_career_overlap(self) -> None:
        # career_overlap_general DECAY_RATE is 0.06; press is 0.10 → smaller
        # factor (faster decay) at the same horizon.
        years = 5
        career = math.exp(-0.06 * years)
        press = job._recency_factor(years)
        assert press < career


# ── _frequency_factor ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestFrequencyFactor:
    def test_one_corroboration_is_one(self) -> None:
        assert job._frequency_factor(1) == pytest.approx(1.0)

    def test_two_corroborations_uses_log(self) -> None:
        assert job._frequency_factor(2) == pytest.approx(1.0 + math.log(2) * 0.10)

    def test_zero_floors_to_one(self) -> None:
        assert job._frequency_factor(0) == pytest.approx(1.0)

    def test_coefficient_is_lower_than_standard(self) -> None:
        # Standard career_overlap uses 0.15. Here it's 0.10 — so for 2
        # corroborations the boost is smaller.
        ours = job._frequency_factor(2)
        standard = 1.0 + math.log(2) * 0.15
        assert ours < standard


# ── _compute_strength ──────────────────────────────────────────────────────


@pytest.mark.unit
class TestComputeStrength:
    def test_simple_product(self) -> None:
        assert job._compute_strength(0.55, 1.0, 1.0, 1.1) == pytest.approx(0.55 * 1.1)

    def test_capped_at_99(self) -> None:
        # Force a large product; cap kicks in.
        assert job._compute_strength(0.9, 2.0, 2.0, 2.0) == pytest.approx(0.99)

    def test_zero_recency_zeroes_result(self) -> None:
        assert job._compute_strength(0.55, 0.0, 1.0, 1.1) == 0.0

    def test_press_baseline_is_below_career_overlap_general(self) -> None:
        # base 0.55 with no boosts must be < career_overlap_general's 0.60 base.
        active = job._compute_strength(job.BASE_STRENGTH, 1.0, 1.0, 1.0)
        assert active < 0.60


# ── _split_first_last ──────────────────────────────────────────────────────


@pytest.mark.unit
class TestSplitFirstLast:
    def test_two_token_name(self) -> None:
        assert job._split_first_last("Jane Smith") == ("Jane", "Smith")

    def test_multi_token_uses_first_and_last(self) -> None:
        assert job._split_first_last("Jane Q. Public Smith") == ("Jane", "Smith")

    def test_single_token_returns_none(self) -> None:
        assert job._split_first_last("Jane") is None

    def test_empty_returns_none(self) -> None:
        assert job._split_first_last("") is None
        assert job._split_first_last("   ") is None


# ── _years_since ───────────────────────────────────────────────────────────


@pytest.mark.unit
class TestYearsSince:
    def test_now_is_close_to_zero(self) -> None:
        # Run at jan 1st of CURRENT_YEAR so we know exactly what to expect.
        when = datetime(job.CURRENT_YEAR, 1, 1, tzinfo=timezone.utc)
        assert job._years_since(when, now_year=job.CURRENT_YEAR) == pytest.approx(0.0)

    def test_one_year_ago(self) -> None:
        when = datetime(job.CURRENT_YEAR - 1, 1, 1, tzinfo=timezone.utc)
        assert job._years_since(when, now_year=job.CURRENT_YEAR) == pytest.approx(1.0, rel=0.01)

    def test_naive_datetime_treated_as_utc(self) -> None:
        when = datetime(job.CURRENT_YEAR - 2, 1, 1)  # naive
        result = job._years_since(when, now_year=job.CURRENT_YEAR)
        assert result == pytest.approx(2.0, rel=0.01)


# ── _build_releases_query ──────────────────────────────────────────────────


@pytest.mark.unit
class TestBuildReleasesQuery:
    def test_unscoped_query_has_no_account_predicate(self) -> None:
        sql, args = job._build_releases_query(account_id=None, limit=None)
        assert args == []
        # No WHERE-clause predicate against account_id (the SELECT projects it
        # for downstream use, which is fine).
        assert "cs.account_id = $" not in sql
        assert "LIMIT" not in sql
        assert "press_release" in sql
        assert "mentioned_executives" in sql

    def test_account_scope_appends_predicate(self) -> None:
        aid = UUID("11111111-1111-1111-1111-111111111111")
        sql, args = job._build_releases_query(account_id=aid, limit=None)
        assert args == [aid]
        assert "cs.account_id = $1" in sql

    def test_limit_appears_when_set(self) -> None:
        sql, _ = job._build_releases_query(account_id=None, limit=42)
        assert "LIMIT 42" in sql


# ── _resolve_person (mocked DB) ────────────────────────────────────────────


@pytest.mark.unit
class TestResolvePerson:
    @pytest.mark.asyncio
    async def test_no_candidates_returns_none(self) -> None:
        conn = AsyncMock()
        conn.fetch.return_value = []
        result = await job._resolve_person(
            conn, "Jane Smith",
            UUID("00000000-0000-0000-0000-000000000001"),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_one_candidate_returns_id(self) -> None:
        person_id = UUID("22222222-2222-2222-2222-222222222222")
        conn = AsyncMock()
        conn.fetch.return_value = [{"id": person_id}]
        result = await job._resolve_person(
            conn, "Jane Smith",
            UUID("00000000-0000-0000-0000-000000000001"),
        )
        assert result == person_id

    @pytest.mark.asyncio
    async def test_two_candidates_returns_none_ambiguous(self) -> None:
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"id": UUID("22222222-2222-2222-2222-222222222222")},
            {"id": UUID("33333333-3333-3333-3333-333333333333")},
        ]
        result = await job._resolve_person(
            conn, "Jane Smith",
            UUID("00000000-0000-0000-0000-000000000001"),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_single_token_name_returns_none(self) -> None:
        conn = AsyncMock()
        result = await job._resolve_person(
            conn, "Madonna",
            UUID("00000000-0000-0000-0000-000000000001"),
        )
        assert result is None
        conn.fetch.assert_not_called()


# ── _stronger_edge_exists (mocked DB) ──────────────────────────────────────


@pytest.mark.unit
class TestStrongerEdgeExists:
    @pytest.mark.asyncio
    async def test_returns_true_when_match(self) -> None:
        conn = AsyncMock()
        conn.fetchrow.return_value = {"?column?": 1}
        a = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        assert await job._stronger_edge_exists(conn, a, b) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_match(self) -> None:
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        a = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        assert await job._stronger_edge_exists(conn, a, b) is False

    @pytest.mark.asyncio
    async def test_query_excludes_self_connection_type(self) -> None:
        """The gate must not count an existing co-mention edge as 'stronger' —
        re-emits of the same type need to flow through the upsert path."""
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        a = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        await job._stronger_edge_exists(conn, a, b)
        called_sql = conn.fetchrow.await_args.args[0]
        assert "connection_type <> $3" in called_sql
        # Third positional arg is the connection type literal, fourth is the
        # base_strength threshold.
        assert conn.fetchrow.await_args.args[3] == job.CONNECTION_TYPE
        assert conn.fetchrow.await_args.args[4] == job.BASE_STRENGTH


# ── Regression: weak baseline ─────────────────────────────────────────────


@pytest.mark.unit
class TestWeakBaseline:
    def test_base_strength_is_055(self) -> None:
        assert job.BASE_STRENGTH == 0.55

    def test_decay_rate_is_010(self) -> None:
        assert job.DECAY_RATE == 0.10

    def test_frequency_coefficient_is_010(self) -> None:
        assert job.FREQUENCY_COEFFICIENT == 0.10

    def test_connection_type_string(self) -> None:
        assert job.CONNECTION_TYPE == "mentioned_in_same_release"
