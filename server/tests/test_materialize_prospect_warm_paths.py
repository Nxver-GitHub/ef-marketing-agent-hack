"""Tests for materialize_prospect_warm_paths.

Pure-function unit coverage for the helpers + CLI parser. The materialization
SQL itself is exercised by the live smoke run from the orchestrator session
(no DB integration test in this suite to keep it fast / hermetic).
"""
from __future__ import annotations

from uuid import UUID

import pytest

from credence.jobs import materialize_prospect_warm_paths as job


# ── _validate_top_k ─────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("k", [1, 5, 10, 20])
def test_validate_top_k_accepts_in_range(k: int) -> None:
    assert job._validate_top_k(k) == k


@pytest.mark.unit
def test_validate_top_k_rejects_zero() -> None:
    with pytest.raises(ValueError, match=">=.*1"):
        job._validate_top_k(0)


@pytest.mark.unit
def test_validate_top_k_rejects_negative() -> None:
    with pytest.raises(ValueError, match=">=.*1"):
        job._validate_top_k(-1)


@pytest.mark.unit
def test_validate_top_k_rejects_above_max() -> None:
    with pytest.raises(ValueError, match=f"<= {job.TOP_K_MAX}"):
        job._validate_top_k(job.TOP_K_MAX + 1)


@pytest.mark.unit
def test_validate_top_k_rejects_bool() -> None:
    """Bool is a subclass of int — defensive guard rejects it."""
    with pytest.raises(ValueError, match="must be int"):
        job._validate_top_k(True)  # type: ignore[arg-type]


@pytest.mark.unit
def test_validate_top_k_rejects_float() -> None:
    with pytest.raises(ValueError, match="must be int"):
        job._validate_top_k(1.5)  # type: ignore[arg-type]


# ── _parse_delete_count ─────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "status,expected",
    [
        ("DELETE 0", 0),
        ("DELETE 47349", 47349),
        ("INSERT 0 47537", 47537),
        ("UPDATE 5", 5),
    ],
)
def test_parse_delete_count_extracts_trailing_int(status: str, expected: int) -> None:
    assert job._parse_delete_count(status) == expected


@pytest.mark.unit
def test_parse_delete_count_handles_empty() -> None:
    assert job._parse_delete_count("") == 0


@pytest.mark.unit
def test_parse_delete_count_handles_unparseable() -> None:
    assert job._parse_delete_count("WEIRD STATUS NO COUNT") == 0


# ── Constants sanity ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_default_top_k_within_bounds() -> None:
    assert 1 <= job.DEFAULT_TOP_K <= job.TOP_K_MAX


@pytest.mark.unit
def test_top_k_max_matches_migration() -> None:
    """The CHECK constraint in the migration is `rank BETWEEN 1 AND 20`. If
    we ever change one, the other must follow."""
    assert job.TOP_K_MAX == 20


# ── SQL contains expected pieces ────────────────────────────────────────────


@pytest.mark.unit
class TestMaterializeSql:

    def test_query_unions_both_directions(self) -> None:
        """The SQL must materialize person_a→b AND person_b→a so each prospect
        sees their own top-K, not just the half they happen to land on."""
        assert "UNION ALL" in job.MATERIALIZE_SQL

    def test_query_filters_by_account_id(self) -> None:
        assert "pc.account_id = $1" in job.MATERIALIZE_SQL

    def test_query_filters_null_source_prospect_id(self) -> None:
        """Persons without source_prospect_id can't render in the UI; skip."""
        assert "pa.source_prospect_id IS NOT NULL" in job.MATERIALIZE_SQL
        assert "pb.source_prospect_id IS NOT NULL" in job.MATERIALIZE_SQL

    def test_query_excludes_self_edges(self) -> None:
        assert "pa.source_prospect_id <> pb.source_prospect_id" in job.MATERIALIZE_SQL

    def test_query_dedupes_by_prospect_partner(self) -> None:
        """The unique constraint allows one row per (prospect, partner)."""
        assert "DISTINCT ON (prospect_id, partner_prospect_id)" in job.MATERIALIZE_SQL

    def test_query_caps_at_top_k(self) -> None:
        assert "rank <= $2" in job.MATERIALIZE_SQL

    def test_query_orders_by_strength_desc(self) -> None:
        assert "computed_strength DESC" in job.MATERIALIZE_SQL

    def test_query_inserts_into_target_table(self) -> None:
        assert "INSERT INTO prospect_warm_paths" in job.MATERIALIZE_SQL

    def test_query_denormalizes_partner_display_fields(self) -> None:
        assert "partner_name" in job.MATERIALIZE_SQL
        assert "partner_company" in job.MATERIALIZE_SQL
        assert "partner_title" in job.MATERIALIZE_SQL


# ── CLI parser ──────────────────────────────────────────────────────────────


ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.unit
def test_cli_requires_scope() -> None:
    parser = job._build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


@pytest.mark.unit
def test_cli_account_id_path() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(["--account-id", str(ACCOUNT_ID), "--top-k", "10"])
    assert args.account_id == ACCOUNT_ID
    assert args.top_k == 10
    assert args.dry_run is False
    assert args.all_accounts is False


@pytest.mark.unit
def test_cli_all_accounts_path() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(["--all-accounts", "--dry-run"])
    assert args.all_accounts is True
    assert args.dry_run is True
    assert args.top_k == job.DEFAULT_TOP_K


@pytest.mark.unit
def test_cli_account_id_and_all_accounts_mutually_exclusive() -> None:
    parser = job._build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--account-id", str(ACCOUNT_ID), "--all-accounts"]
        )


# ── Watch-mode helpers ──────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "seconds,valid",
    [
        (5, True),
        (60, True),
        (3600, True),
        (4, False),
        (0, False),
        (-1, False),
    ],
)
def test_validate_poll_seconds(seconds: int, valid: bool) -> None:
    if valid:
        assert job._validate_poll_seconds(seconds) == seconds
    else:
        with pytest.raises(ValueError):
            job._validate_poll_seconds(seconds)


@pytest.mark.unit
def test_validate_poll_seconds_rejects_bool() -> None:
    with pytest.raises(ValueError, match="must be int"):
        job._validate_poll_seconds(True)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(
    "threshold,valid",
    [(1, True), (100, True), (10000, True), (0, False), (-5, False)],
)
def test_validate_threshold(threshold: int, valid: bool) -> None:
    if valid:
        assert job._validate_threshold(threshold) == threshold
    else:
        with pytest.raises(ValueError):
            job._validate_threshold(threshold)


@pytest.mark.unit
def test_cli_watch_flag_parses() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(
        [
            "--account-id", str(ACCOUNT_ID),
            "--watch",
            "--poll-seconds", "30",
            "--threshold", "50",
        ]
    )
    assert args.watch is True
    assert args.poll_seconds == 30
    assert args.threshold == 50


@pytest.mark.unit
def test_cli_watch_default_poll_and_threshold() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(["--account-id", str(ACCOUNT_ID), "--watch"])
    assert args.poll_seconds == job.DEFAULT_WATCH_POLL_SECONDS
    assert args.threshold == job.DEFAULT_WATCH_THRESHOLD


@pytest.mark.unit
async def test_watch_and_refresh_cold_start_always_materializes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First iteration treats startup as a cold delta and refreshes."""
    counts = iter([1000, 1010])  # first read → cold; second read won't be hit

    async def fake_count(account_id: UUID) -> int:
        return next(counts)

    refresh_calls: list[tuple[UUID, int]] = []

    async def fake_materialize(
        account_id: UUID, *, top_k: int, dry_run: bool,
    ) -> job.MaterializeRollup:
        refresh_calls.append((account_id, top_k))
        return job.MaterializeRollup(
            account_id=account_id, top_k=top_k, rows_inserted=42,
        )

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(job, "_count_person_connections", fake_count)
    monkeypatch.setattr(
        job, "materialize_prospect_warm_paths_account", fake_materialize,
    )

    rollups = await job.watch_and_refresh_account(
        ACCOUNT_ID,
        top_k=20,
        poll_seconds=10,
        threshold=100,
        max_iterations=1,
        sleep_func=fake_sleep,
    )

    assert len(rollups) == 1
    assert refresh_calls == [(ACCOUNT_ID, 20)]
    assert sleeps == [10.0]


@pytest.mark.unit
async def test_watch_skips_refresh_when_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If person_connections grew by less than `threshold`, no refresh."""
    counts = iter([1000, 1050])  # delta 50 < threshold 100

    async def fake_count(account_id: UUID) -> int:
        return next(counts)

    refresh_calls: list[UUID] = []

    async def fake_materialize(
        account_id: UUID, *, top_k: int, dry_run: bool,
    ) -> job.MaterializeRollup:
        refresh_calls.append(account_id)
        return job.MaterializeRollup(account_id=account_id, top_k=top_k)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(job, "_count_person_connections", fake_count)
    monkeypatch.setattr(
        job, "materialize_prospect_warm_paths_account", fake_materialize,
    )

    rollups = await job.watch_and_refresh_account(
        ACCOUNT_ID,
        top_k=20,
        poll_seconds=10,
        threshold=100,
        max_iterations=2,
        sleep_func=fake_sleep,
    )

    # Iter 1: cold start → refresh. Iter 2: delta=50 < 100 → no refresh.
    assert len(rollups) == 1
    assert len(refresh_calls) == 1


@pytest.mark.unit
async def test_watch_refreshes_when_delta_meets_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delta ≥ threshold on the second iteration triggers a refresh."""
    counts = iter([1000, 1500])  # delta 500 >> threshold 100

    async def fake_count(account_id: UUID) -> int:
        return next(counts)

    refresh_calls: list[UUID] = []

    async def fake_materialize(
        account_id: UUID, *, top_k: int, dry_run: bool,
    ) -> job.MaterializeRollup:
        refresh_calls.append(account_id)
        return job.MaterializeRollup(account_id=account_id, top_k=top_k)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(job, "_count_person_connections", fake_count)
    monkeypatch.setattr(
        job, "materialize_prospect_warm_paths_account", fake_materialize,
    )

    rollups = await job.watch_and_refresh_account(
        ACCOUNT_ID,
        top_k=20,
        poll_seconds=10,
        threshold=100,
        max_iterations=2,
        sleep_func=fake_sleep,
    )

    assert len(rollups) == 2  # cold + delta-triggered
    assert len(refresh_calls) == 2


@pytest.mark.unit
async def test_watch_swallows_count_query_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the count query fails, the watch loop logs + retries instead of dying."""

    call_count = {"n": 0}

    async def fake_count(account_id: UUID) -> int:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated DB hiccup")
        return 1000

    refresh_calls: list[UUID] = []

    async def fake_materialize(
        account_id: UUID, *, top_k: int, dry_run: bool,
    ) -> job.MaterializeRollup:
        refresh_calls.append(account_id)
        return job.MaterializeRollup(account_id=account_id, top_k=top_k)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(job, "_count_person_connections", fake_count)
    monkeypatch.setattr(
        job, "materialize_prospect_warm_paths_account", fake_materialize,
    )

    rollups = await job.watch_and_refresh_account(
        ACCOUNT_ID,
        top_k=20,
        poll_seconds=10,
        threshold=100,
        max_iterations=2,
        sleep_func=fake_sleep,
    )

    # Iter 1: count failed, retried, no refresh.
    # Iter 2: count succeeded, cold delta, refreshed.
    assert call_count["n"] == 2
    assert len(rollups) == 1
