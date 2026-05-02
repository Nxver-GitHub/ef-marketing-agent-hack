"""Pure-function tests for career_overlap_clustering.

Exercises the strength-factor math + the SQL builder. DB-touching code is
covered separately by the live smoke (no integration test here so the unit
suite stays fast and deterministic).
"""
from __future__ import annotations

import math
from uuid import UUID

import pytest

from credence.jobs import career_overlap_clustering as job
from credence.strength import DECAY_RATES, STRENGTH_TABLE, compute_strength_for_type


@pytest.mark.unit
class TestComputeFactors:
    """The factor breakdown stored on person_connections must equal the
    canonical strength formula in credence.strength."""

    @pytest.mark.parametrize(
        "connection_type",
        [job.SAME_TEAM, job.SAME_DOMAIN, job.GENERAL],
    )
    def test_match_canonical_compute_strength(self, connection_type: str) -> None:
        last_active_year = 2020
        years_since = job.CURRENT_YEAR - last_active_year
        canonical = compute_strength_for_type(
            connection_type,
            years_since_active=years_since,
            corroboration_count=1,
            source_type_count=1,
        )
        _b, _r, _f, _c, computed = job._compute_factors(
            connection_type, last_active_year=last_active_year, corroboration_count=1
        )
        assert computed == pytest.approx(canonical, rel=1e-9)

    def test_factor_breakdown_multiplies_to_computed(self) -> None:
        b, r, f, c, computed = job._compute_factors(
            job.SAME_TEAM, last_active_year=2018, corroboration_count=2
        )
        assert b == STRENGTH_TABLE[job.SAME_TEAM]
        assert r == pytest.approx(math.exp(-DECAY_RATES[job.SAME_TEAM] * (job.CURRENT_YEAR - 2018)))
        assert f == pytest.approx(1.0 + math.log(2) * 0.15)
        assert c == pytest.approx(1.0 + 1 * 0.10)
        assert computed == pytest.approx(min(0.99, b * r * f * c))

    def test_strength_capped_at_99(self) -> None:
        # Active this year, many corroborations, max source types — should cap
        _, _, _, _, computed = job._compute_factors(
            job.SAME_TEAM, last_active_year=job.CURRENT_YEAR, corroboration_count=1000
        )
        assert computed <= 0.99

    def test_corroboration_count_floors_at_one(self) -> None:
        # log(0) is undefined; the formula guards via max(1, ...). Passing 0
        # must not crash and must produce log(1)=0 → frequency=1.0.
        _, _, freq, _, _ = job._compute_factors(
            job.SAME_TEAM, last_active_year=job.CURRENT_YEAR, corroboration_count=0
        )
        assert freq == pytest.approx(1.0)


@pytest.mark.unit
class TestBuildQuery:
    """The planner SQL string + args must reflect the company filter and limit."""

    def test_unscoped_query_has_one_arg(self) -> None:
        sql, args = job._build_query(company_id=None, limit=None)
        assert args == [job.CURRENT_YEAR]
        # No company-id PREDICATE (the JOIN's `a.company_id = b.company_id`
        # is required and unrelated).
        assert "AND a.company_id = $" not in sql
        assert "LIMIT" not in sql

    def test_company_scope_appends_predicate_and_arg(self) -> None:
        cid = UUID("11111111-1111-1111-1111-111111111111")
        sql, args = job._build_query(company_id=cid, limit=None)
        assert args == [job.CURRENT_YEAR, cid]
        assert "AND a.company_id = $2" in sql

    def test_limit_appears_in_sql_only(self) -> None:
        sql, args = job._build_query(company_id=None, limit=10)
        assert args == [job.CURRENT_YEAR]
        assert "LIMIT 10" in sql

    def test_case_emits_canonical_connection_types(self) -> None:
        sql, _ = job._build_query(company_id=None, limit=None)
        for ct in (job.SAME_TEAM, job.SAME_DOMAIN, job.GENERAL):
            assert ct in sql, f"missing {ct} branch in CASE expression"

    def test_account_id_constraint_present(self) -> None:
        """Cross-tenant pairs must never form."""
        sql, _ = job._build_query(company_id=None, limit=None)
        assert "a.account_id = b.account_id" in sql

    def test_pair_order_invariant_in_sql(self) -> None:
        """Decision 1: person_a_id < person_b_id always."""
        sql, _ = job._build_query(company_id=None, limit=None)
        assert "LEAST(a.person_id, b.person_id)" in sql
        assert "GREATEST(a.person_id, b.person_id)" in sql
        assert "a.person_id < b.person_id" in sql


@pytest.mark.unit
class TestEvidence:
    """Helper functions for the connection_evidence row."""

    def _make_pair(self) -> job.OverlapPair:
        return job.OverlapPair(
            person_a_id=UUID("00000000-0000-0000-0000-00000000000a"),
            person_b_id=UUID("00000000-0000-0000-0000-00000000000b"),
            company_id=UUID("00000000-0000-0000-0000-0000000000c0"),
            company_name="Acme",
            connection_type=job.SAME_TEAM,
            overlap_start=2018,
            overlap_end=2022,
            overlap_years=4,
            seniority_gap=5,
            team_a="Search",
            team_b="Search",
            domain_a="software_engineering",
            domain_b="software_engineering",
            account_id=UUID("00000000-0000-0000-0000-000000000001"),
        )

    def test_source_id_is_deterministic(self) -> None:
        a = job._evidence_source_id(self._make_pair())
        b = job._evidence_source_id(self._make_pair())
        assert a == b
        # Same pair flipped (would never happen — planner guarantees a<b)
        # so the source_id must include the company too:
        assert ":" in a
        assert "0000c0" in a

    def test_payload_serialises_compactly(self) -> None:
        payload = job._evidence_payload(self._make_pair())
        # No spaces: cap on jsonb is 4096 chars; compact serialisation matters.
        assert ", " not in payload
        assert ": " not in payload
        # Required fields present:
        for field in ("company_id", "company_name", "overlap_years", "team_a", "domain_a"):
            assert field in payload
