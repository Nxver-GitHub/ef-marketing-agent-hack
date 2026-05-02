"""Tests for `credence.onboarding.pipeline.run_onboarding_pipeline` (Wave B,
LP delegation msg 275 + 281).

Strategy: AsyncMock the 5 external dependencies (rep_resolver, scrape_company_site,
team_scraper, entity_resolver, find_warm_paths) plus a FakeConn/FakePool that
records every UPDATE/INSERT executed against onboarding_jobs +
account_team_members + persons. No live DB. No live external services.

Coverage (Contract 14 invariants + LP test list):
1.  Happy-path: all 4 stages mark, status flips to 'done'
2.  Stage 0 LinkedIn no-match → tier-0 person inserted, later stages still run
3.  Stage 1 employee_count<500 → strategy='all_employees'
4.  Stage 1 employee_count>=500 → strategy='gtm_only'
5.  Stage 2 mid-failure (entity_resolver crashes on one) → others survive
6.  Stage 3 partial failure (find_warm_paths raises) → still marks complete
7.  Idempotent retry: re-entering with same job_id past 'team' skips earlier
8.  Progress JSON merges (doesn't overwrite prior keys)
9.  completed_at set only when stage='complete'
10. find_warm_paths called with source_person_ids = team_ids
11. account_team_members role='owner' for rep, scrape_status='done'
12. account_id consistent across all writes
13. Stage 0 hard-fail (rep_resolver raises) → status='error', halt
14. Email-domain extraction edge cases
"""
from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from credence.onboarding import pipeline as pipeline_module


# ─── deterministic UUIDs / fixtures ────────────────────────────────────────


def _u(label: str) -> UUID:
    """Stable test UUID derived from a label string. Easier to read than uuid4()."""
    h = abs(hash(label)) % (10**32)
    return UUID(int=h)


def _make_job_row(
    job_id: UUID,
    account_id: UUID,
    *,
    stage: str | None = None,
    status: str = "pending",
    strategy: str | None = None,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": job_id,
        "account_id": account_id,
        "status": status,
        "stage": stage,
        "strategy": strategy,
        "progress": progress or {},
    }


# ─── FakeConn: records SQL + dispatches canned responses by substring ─────


class FakeConn:
    """Mock asyncpg.Connection. Tests register canned responses per
    SQL substring; the conn records every call for assertions."""

    def __init__(self) -> None:
        self.fetchrow_responses: list[tuple[str, dict[str, Any] | None]] = []
        self.fetch_responses: list[tuple[str, list[dict[str, Any]]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    def on_fetchrow(self, substr: str, response: dict[str, Any] | None) -> None:
        self.fetchrow_responses.append((substr, response))

    def on_fetch(self, substr: str, response: list[dict[str, Any]]) -> None:
        self.fetch_responses.append((substr, response))

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((sql, args))
        for substr, resp in self.fetchrow_responses:
            if substr in sql:
                return resp
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((sql, args))
        for substr, resp in self.fetch_responses:
            if substr in sql:
                return resp
        return []

    async def execute(self, sql: str, *args: Any) -> None:
        self.execute_calls.append((sql, args))

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield  # no-op: tests inspect execute_calls directly


class FakePool:
    """Returns the SAME FakeConn each acquire so all stages write to one log."""

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self._conn


# ─── pytest fixture: install all 6 mocks ─────────────────────────────────


@pytest.fixture
def patched_pipeline(monkeypatch: pytest.MonkeyPatch):
    conn = FakeConn()
    pool = FakePool(conn)

    async def fake_get_pool() -> Any:
        return pool

    # Mock all 5 stage helpers via AsyncMock so individual tests can
    # configure return values / side effects per scenario.
    rep_mock = AsyncMock()
    company_mock = AsyncMock(return_value=[])  # scrape_company_site → [signals]
    team_mock = AsyncMock()
    entity_mock = AsyncMock()
    bfs_mock = AsyncMock(return_value={"paths_found": 0, "paths": []})

    monkeypatch.setattr(pipeline_module.db, "get_pool", fake_get_pool)
    monkeypatch.setattr(pipeline_module, "resolve_rep_linkedin", rep_mock)
    monkeypatch.setattr(pipeline_module, "scrape_company_site", company_mock)
    monkeypatch.setattr(pipeline_module, "scrape_team_for_account", team_mock)
    monkeypatch.setattr(pipeline_module, "resolve_or_insert_team_member", entity_mock)
    monkeypatch.setattr(pipeline_module, "find_warm_paths", bfs_mock)
    return {
        "conn": conn,
        "pool": pool,
        "rep": rep_mock,
        "company": company_mock,
        "team": team_mock,
        "entity": entity_mock,
        "bfs": bfs_mock,
    }


def _executes_matching(conn: FakeConn, substr: str) -> list[tuple[str, tuple]]:
    return [(sql, args) for sql, args in conn.execute_calls if substr in sql]


# ─── helpers to seed FakeConn for the happy path ─────────────────────────


def _seed_happy_path(
    conn: FakeConn,
    *,
    job_id: UUID,
    account_id: UUID,
    employee_count: int = 100,
) -> None:
    """Wire FakeConn so all 4 stages can complete."""
    job_row = _make_job_row(job_id, account_id, stage=None, status="pending")
    conn.on_fetchrow("FROM onboarding_jobs", job_row)
    conn.on_fetchrow(
        "INSERT INTO persons",
        {"id": _u("rep-person")},
    )
    conn.on_fetchrow(
        "FROM companies",
        {"employee_count_estimate": employee_count},
    )
    conn.on_fetchrow(
        "FROM account_team_members",
        {
            "company_id": _u("rep-company"),
            "canonical_name": "nvidia",
        },
    )
    conn.on_fetch(
        "FROM account_team_members\n        WHERE",
        [{"person_id": _u("rep-person")}],
    )
    conn.on_fetch(
        "FROM persons\n        WHERE id <> ALL",
        [{"id": _u("target-1")}, {"id": _u("target-2")}],
    )


# ─── tests ───────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_happy_path_drives_all_four_stages(patched_pipeline) -> None:
    job_id, account_id = _u("job-1"), _u("acct-1")
    conn = patched_pipeline["conn"]
    _seed_happy_path(conn, job_id=job_id, account_id=account_id)
    patched_pipeline["rep"].return_value = MagicMock(
        linkedin_url="https://linkedin.com/in/rep",
        current_title="VP Sales",
    )
    patched_pipeline["team"].return_value = MagicMock(
        employees=[],
        total_returned=0,
        cost_usd=0.0,
        strategy_used="all_employees",
    )

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-1"), email="rep@nvidia.com",
        full_name="Sarah Sales", account_id=account_id,
    )

    # Verify each stage advanced.
    stages_set = [
        args[1] for sql, args in conn.execute_calls
        if "UPDATE onboarding_jobs SET" in sql and "stage = $2" in sql
    ]
    assert "company" in stages_set
    assert "team" in stages_set
    assert "connections" in stages_set
    # Final mark_complete is its own UPDATE with stage='complete' literal.
    assert any("'complete'" in sql for sql, _ in conn.execute_calls)


@pytest.mark.unit
async def test_stage_0_no_linkedin_match_falls_back_to_tier_0_person(
    patched_pipeline,
) -> None:
    job_id, account_id = _u("job-2"), _u("acct-2")
    conn = patched_pipeline["conn"]
    _seed_happy_path(conn, job_id=job_id, account_id=account_id)
    patched_pipeline["rep"].return_value = None  # no LinkedIn match
    patched_pipeline["team"].return_value = MagicMock(
        employees=[], total_returned=0, cost_usd=0.0, strategy_used="gtm_only",
    )

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-2"), email="rep@nvidia.com",
        full_name="Anonymous Andy", account_id=account_id,
    )

    # Tier-0 INSERT path used (no linkedin_url, enrichment_tier=0).
    person_inserts = _executes_matching(conn, "INSERT INTO persons")
    # The rep was upserted via fetchrow, not execute (RETURNING id), so check
    # account_team_members got an upsert with NULL linkedin_url.
    atm_inserts = _executes_matching(conn, "INSERT INTO account_team_members")
    assert len(atm_inserts) >= 1
    # The 3rd positional arg is linkedin_url; should be None when no match.
    assert atm_inserts[0][1][2] is None


@pytest.mark.unit
async def test_stage_1_small_company_uses_all_employees_strategy(
    patched_pipeline,
) -> None:
    job_id, account_id = _u("job-3"), _u("acct-3")
    conn = patched_pipeline["conn"]
    _seed_happy_path(conn, job_id=job_id, account_id=account_id, employee_count=120)
    patched_pipeline["rep"].return_value = MagicMock(
        linkedin_url="https://linkedin.com/in/r", current_title="t",
    )
    patched_pipeline["team"].return_value = MagicMock(
        employees=[], total_returned=0, cost_usd=0.0, strategy_used="all_employees",
    )

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-3"), email="r@small.com",
        full_name="Small Co Sam", account_id=account_id,
    )

    # The advance-to-team UPDATE includes strategy as $3 positional.
    team_advances = [
        args for sql, args in conn.execute_calls
        if "UPDATE onboarding_jobs SET" in sql and "strategy = $3" in sql
    ]
    assert len(team_advances) >= 1
    assert team_advances[0][2] == "all_employees"


@pytest.mark.unit
async def test_stage_1_large_company_uses_gtm_only_strategy(patched_pipeline) -> None:
    job_id, account_id = _u("job-4"), _u("acct-4")
    conn = patched_pipeline["conn"]
    _seed_happy_path(conn, job_id=job_id, account_id=account_id, employee_count=8000)
    patched_pipeline["rep"].return_value = MagicMock(
        linkedin_url="https://linkedin.com/in/r", current_title="t",
    )
    patched_pipeline["team"].return_value = MagicMock(
        employees=[], total_returned=0, cost_usd=0.0, strategy_used="gtm_only",
    )

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-4"), email="r@bigco.com",
        full_name="Big Co Bob", account_id=account_id,
    )

    team_advances = [
        args for sql, args in conn.execute_calls
        if "UPDATE onboarding_jobs SET" in sql and "strategy = $3" in sql
    ]
    assert team_advances and team_advances[0][2] == "gtm_only"


@pytest.mark.unit
async def test_stage_2_entity_resolver_failure_does_not_abort(patched_pipeline) -> None:
    job_id, account_id = _u("job-5"), _u("acct-5")
    conn = patched_pipeline["conn"]
    _seed_happy_path(conn, job_id=job_id, account_id=account_id)
    patched_pipeline["rep"].return_value = MagicMock(
        linkedin_url="https://linkedin.com/in/r", current_title="t",
    )
    employees = [MagicMock(id=f"emp-{i}") for i in range(3)]
    patched_pipeline["team"].return_value = MagicMock(
        employees=employees, total_returned=3, cost_usd=0.03,
        strategy_used="gtm_only",
    )
    # Middle employee crashes resolution; others succeed.
    patched_pipeline["entity"].side_effect = [
        MagicMock(person_id=_u("p-good-1"), was_new=True,
                  account_team_member_id=_u("atm-1")),
        Exception("entity resolution crash"),
        MagicMock(person_id=_u("p-good-2"), was_new=False,
                  account_team_member_id=_u("atm-2")),
    ]

    # Should NOT raise — pipeline continues.
    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-5"), email="r@co.com",
        full_name="Resilient Rep", account_id=account_id,
    )

    # Three entity calls attempted.
    assert patched_pipeline["entity"].await_count == 3
    # Stage advances to 'connections'.
    assert any(
        "stage = $2" in sql and args[1] == "connections"
        for sql, args in conn.execute_calls
        if "UPDATE onboarding_jobs SET" in sql
    )


@pytest.mark.unit
async def test_stage_3_find_warm_paths_failure_still_marks_complete(
    patched_pipeline,
) -> None:
    job_id, account_id = _u("job-6"), _u("acct-6")
    conn = patched_pipeline["conn"]
    _seed_happy_path(conn, job_id=job_id, account_id=account_id)
    patched_pipeline["rep"].return_value = MagicMock(
        linkedin_url="https://linkedin.com/in/r", current_title="t",
    )
    patched_pipeline["team"].return_value = MagicMock(
        employees=[], total_returned=0, cost_usd=0.0, strategy_used="gtm_only",
    )
    patched_pipeline["bfs"].side_effect = Exception("BFS exploded")

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-6"), email="r@co.com",
        full_name="Warm Path Wally", account_id=account_id,
    )

    # Pipeline still hits the mark_complete UPDATE.
    assert any(
        "stage = 'complete'" in sql and "status = 'done'" in sql
        for sql, _ in conn.execute_calls
    )


@pytest.mark.unit
async def test_idempotent_retry_skips_completed_stages(patched_pipeline) -> None:
    job_id, account_id = _u("job-7"), _u("acct-7")
    conn = patched_pipeline["conn"]
    # Job has already completed stage='team' — should pick up at 'connections'.
    job_row = _make_job_row(job_id, account_id, stage="connections", status="running")
    conn.on_fetchrow("FROM onboarding_jobs", job_row)
    conn.on_fetch(
        "FROM account_team_members\n        WHERE",
        [{"person_id": _u("rep-person")}],
    )
    conn.on_fetch(
        "FROM persons\n        WHERE id <> ALL",
        [],
    )

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-7"), email="r@co.com",
        full_name="Retry Rita", account_id=account_id,
    )

    # Stage 0 (rep_resolver) NOT called.
    assert patched_pipeline["rep"].await_count == 0
    # Stage 1 (scrape_company_site) NOT called.
    assert patched_pipeline["company"].await_count == 0
    # Stage 2 (team_scraper) NOT called.
    assert patched_pipeline["team"].await_count == 0


@pytest.mark.unit
async def test_progress_json_merge_preserves_prior_keys(patched_pipeline) -> None:
    """The `progress = progress || $jsonb` SQL pattern is a JSONB merge.
    We assert that the orchestrator passes the merged dict (cost rollup +
    stage delta), not a replacement that would drop earlier keys."""
    job_id, account_id = _u("job-8"), _u("acct-8")
    conn = patched_pipeline["conn"]
    _seed_happy_path(conn, job_id=job_id, account_id=account_id)
    patched_pipeline["rep"].return_value = MagicMock(
        linkedin_url="https://linkedin.com/in/r", current_title="t",
    )
    patched_pipeline["team"].return_value = MagicMock(
        employees=[], total_returned=42, cost_usd=0.42, strategy_used="gtm_only",
    )

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-8"), email="r@co.com",
        full_name="JSON Merge Jen", account_id=account_id,
    )

    # The Stage 2 progress UPDATE carries scraped+matched counts AND the
    # cost rollup nested under 'cost'. Find the write that has both keys —
    # later writes (advance_stage to 'connections', mark_complete) only
    # carry the cost ledger snapshot.
    progress_writes = [
        args[1] for sql, args in conn.execute_calls
        if "UPDATE onboarding_jobs SET" in sql
        and "progress = COALESCE(progress, '{}'::jsonb) || $2::jsonb" in sql
        and isinstance(args[1], dict)
    ]
    merged_with_scraped = next(
        (m for m in progress_writes if m.get("scraped") == 42), None
    )
    assert merged_with_scraped is not None, (
        "no progress write carried the scrape count alongside the cost rollup"
    )
    assert "cost" in merged_with_scraped


@pytest.mark.unit
async def test_completed_at_set_only_at_complete_stage(patched_pipeline) -> None:
    job_id, account_id = _u("job-9"), _u("acct-9")
    conn = patched_pipeline["conn"]
    _seed_happy_path(conn, job_id=job_id, account_id=account_id)
    patched_pipeline["rep"].return_value = MagicMock(
        linkedin_url="https://linkedin.com/in/r", current_title="t",
    )
    patched_pipeline["team"].return_value = MagicMock(
        employees=[], total_returned=0, cost_usd=0.0, strategy_used="gtm_only",
    )

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-9"), email="r@co.com",
        full_name="Complete Carl", account_id=account_id,
    )

    completed_writes = [
        sql for sql, _ in conn.execute_calls
        if "completed_at = now()" in sql
    ]
    assert len(completed_writes) == 1


@pytest.mark.unit
async def test_find_warm_paths_called_with_team_ids_as_source_filter(
    patched_pipeline,
) -> None:
    job_id, account_id = _u("job-10"), _u("acct-10")
    conn = patched_pipeline["conn"]
    _seed_happy_path(conn, job_id=job_id, account_id=account_id)
    patched_pipeline["rep"].return_value = MagicMock(
        linkedin_url="https://linkedin.com/in/r", current_title="t",
    )
    patched_pipeline["team"].return_value = MagicMock(
        employees=[], total_returned=0, cost_usd=0.0, strategy_used="gtm_only",
    )

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-10"), email="r@co.com",
        full_name="Source Filter Sam", account_id=account_id,
    )

    # Each find_warm_paths call must pass source_person_ids.
    bfs_calls = patched_pipeline["bfs"].await_args_list
    for call in bfs_calls:
        kwargs = call.kwargs
        assert "source_person_ids" in kwargs
        assert kwargs["source_person_ids"]  # non-empty list


@pytest.mark.unit
async def test_account_team_members_role_owner_for_rep(patched_pipeline) -> None:
    job_id, account_id = _u("job-11"), _u("acct-11")
    conn = patched_pipeline["conn"]
    _seed_happy_path(conn, job_id=job_id, account_id=account_id)
    patched_pipeline["rep"].return_value = MagicMock(
        linkedin_url="https://linkedin.com/in/r", current_title="t",
    )
    patched_pipeline["team"].return_value = MagicMock(
        employees=[], total_returned=0, cost_usd=0.0, strategy_used="gtm_only",
    )

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-11"), email="r@co.com",
        full_name="Role Owner Rita", account_id=account_id,
    )

    atm_inserts = _executes_matching(conn, "INSERT INTO account_team_members")
    assert atm_inserts
    # The INSERT has 'owner' as a SQL literal in the VALUES clause.
    assert any("'owner'" in sql for sql, _ in atm_inserts)


@pytest.mark.unit
async def test_account_id_consistent_across_writes(patched_pipeline) -> None:
    job_id, account_id = _u("job-12"), _u("acct-12")
    conn = patched_pipeline["conn"]
    _seed_happy_path(conn, job_id=job_id, account_id=account_id)
    patched_pipeline["rep"].return_value = MagicMock(
        linkedin_url="https://linkedin.com/in/r", current_title="t",
    )
    patched_pipeline["team"].return_value = MagicMock(
        employees=[], total_returned=0, cost_usd=0.0, strategy_used="gtm_only",
    )

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-12"), email="r@co.com",
        full_name="Account Consistency", account_id=account_id,
    )

    atm_inserts = _executes_matching(conn, "INSERT INTO account_team_members")
    for sql, args in atm_inserts:
        # account_id is the first positional arg in INSERT.
        assert args[0] == account_id


@pytest.mark.unit
async def test_stage_0_hard_fail_aborts_with_error_status(patched_pipeline) -> None:
    job_id, account_id = _u("job-13"), _u("acct-13")
    conn = patched_pipeline["conn"]
    _seed_happy_path(conn, job_id=job_id, account_id=account_id)
    patched_pipeline["rep"].side_effect = Exception("Apify down")

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-13"), email="r@co.com",
        full_name="Stage Zero Sue", account_id=account_id,
    )

    # The error-persist UPDATE writes status='error' on Stage 0 halt.
    error_writes = [
        sql for sql, _ in conn.execute_calls
        if "UPDATE onboarding_jobs SET" in sql
        and "status = $3" in sql
        and "error_message = $4" in sql
    ]
    assert error_writes
    # Stage 1+ never run.
    assert patched_pipeline["company"].await_count == 0
    assert patched_pipeline["team"].await_count == 0
    assert patched_pipeline["bfs"].await_count == 0


@pytest.mark.unit
async def test_email_domain_extraction() -> None:
    assert pipeline_module._extract_email_domain("sarah@nvidia.com") == "nvidia.com"
    assert pipeline_module._extract_email_domain("Sarah@NVIDIA.com") == "nvidia.com"
    assert pipeline_module._extract_email_domain("a@b.c.d") == "b.c.d"
    with pytest.raises(ValueError):
        pipeline_module._extract_email_domain("no-at-sign")


@pytest.mark.unit
async def test_should_skip_stage_logic() -> None:
    # No prior stage → don't skip.
    assert not pipeline_module._should_skip_stage({"stage": None}, "identity")
    # Same stage → don't skip (we re-run that stage).
    assert not pipeline_module._should_skip_stage({"stage": "company"}, "company")
    # Past stage → skip.
    assert pipeline_module._should_skip_stage({"stage": "team"}, "company")
    assert pipeline_module._should_skip_stage({"stage": "complete"}, "team")
    # Unknown stage label → don't skip (defensive).
    assert not pipeline_module._should_skip_stage({"stage": "unknown"}, "team")


@pytest.mark.unit
async def test_already_done_job_is_no_op_retry(patched_pipeline) -> None:
    job_id, account_id = _u("job-15"), _u("acct-15")
    conn = patched_pipeline["conn"]
    job_row = _make_job_row(job_id, account_id, stage="complete", status="done")
    conn.on_fetchrow("FROM onboarding_jobs", job_row)

    await pipeline_module.run_onboarding_pipeline(
        job_id=job_id, user_id=_u("user-15"), email="r@co.com",
        full_name="Done Already", account_id=account_id,
    )

    # No stage helpers called.
    assert patched_pipeline["rep"].await_count == 0
    assert patched_pipeline["company"].await_count == 0
    assert patched_pipeline["team"].await_count == 0
