"""Tests for `credence.orgchart.clustering` — Plan A Stage 1.1.

Covers both the pure clustering plan (no DB) and the DB-backed
``cluster_company`` orchestrator with monkeypatched ``fetch`` /
``acquire`` shims, mirroring the pattern in ``test_signals.py`` and
``test_score_runner.py``.

Coverage:
1. Pure plan: canonical-domain person → 0.95 main cluster
2. Pure plan: NLP-fallback person → 0.70 main cluster
3. Pure plan: unclassified title → person dropped (no uncategorized cluster)
4. Pure plan: sub-cluster requires ≥2 same-team members AND canonical domain
5. Pure plan: IC-track titles flagged via taxonomy.is_ic_track
6. Orchestrator: cluster_company writes one cluster + N member rows
7. Orchestrator: skips companies below MIN_CLUSTER_SIZE
8. Orchestrator: missing company → LookupError, no writes
9. Orchestrator: account_id flows through from employment_periods to inserts
10. Idempotency: re-running yields same cluster count, members upserted
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from credence import taxonomy
from credence.orgchart import clustering
from credence.orgchart.clustering import (
    MIN_CLUSTER_SIZE,
    SUB_CLUSTER_MIN_MEMBERS,
    _PersonRow,
    _build_cluster_plan,
    cluster_company,
)


COMPANY_A = UUID("00000000-0000-0000-0000-aaaa00000001")
ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")


def _person(
    *,
    pid: int,
    title: str | None = None,
    domain: str | None = None,
    team: str | None = None,
) -> _PersonRow:
    return _PersonRow(
        person_id=UUID(f"00000000-0000-0000-0000-cccc{pid:08d}"),
        account_id=ACCOUNT,
        canonical_title=title,
        canonical_domain=domain,
        canonical_team=team,
        is_ic_track=taxonomy.is_ic_track(title),
    )


# ── Pure clustering tests ────────────────────────────────────────────────────


@pytest.mark.unit
def test_canonical_domain_yields_high_confidence_main_cluster() -> None:
    """When the canonical column is set, the main cluster gets 0.95 conf."""
    persons = [
        _person(pid=1, title="Hardware Engineer", domain="hardware_engineering"),
        _person(pid=2, title="Sr Hardware Engineer", domain="hardware_engineering"),
        _person(pid=3, title="VP Hardware", domain="hardware_engineering"),
    ]
    plan = _build_cluster_plan(persons)

    assert ("hardware_engineering", None) in plan
    main = plan[("hardware_engineering", None)]
    assert len(main) == 3
    assert all(conf == 0.95 for _, conf in main)


@pytest.mark.unit
def test_nlp_fallback_yields_lower_confidence() -> None:
    """No canonical_domain → infer from title → 0.70 confidence."""
    persons = [
        _person(pid=1, title="Software Engineer"),  # no canonical
        _person(pid=2, title="Senior Backend Engineer"),
    ]
    plan = _build_cluster_plan(persons)

    assert ("software_engineering", None) in plan
    members = plan[("software_engineering", None)]
    assert len(members) == 2
    assert all(conf == 0.70 for _, conf in members)


@pytest.mark.unit
def test_unclassifiable_title_is_dropped() -> None:
    """No canonical, no NLP match → person not in any cluster."""
    persons = [
        _person(pid=1, title="Mystery Title"),  # NLP doesn't match
        _person(pid=2, title="Hardware Engineer", domain="hardware_engineering"),
    ]
    plan = _build_cluster_plan(persons)

    # Only the hardware person made it; mystery title was dropped
    main_members = plan[("hardware_engineering", None)]
    assert len(main_members) == 1
    pid_2 = UUID("00000000-0000-0000-0000-cccc00000002")
    assert main_members[0][0].person_id == pid_2


@pytest.mark.unit
def test_sub_cluster_requires_canonical_domain_and_min_members() -> None:
    """Sub-cluster only emitted when ≥2 share an inferred_team AND canonical_domain set."""
    persons = [
        # Three on same team with canonical domain → sub-cluster fires
        _person(pid=1, title="HW Engineer", domain="hardware_engineering", team="GPU"),
        _person(pid=2, title="Sr HW Engineer", domain="hardware_engineering", team="GPU"),
        _person(pid=3, title="HW Engineer", domain="hardware_engineering", team="GPU"),
        # One alone on different team → no sub-cluster
        _person(pid=4, title="HW Engineer", domain="hardware_engineering", team="CPU"),
        # Two with team but NO canonical_domain → not sub-clustered
        _person(pid=5, title="Software Engineer", team="Compiler"),
        _person(pid=6, title="Software Engineer", team="Compiler"),
    ]
    plan = _build_cluster_plan(persons)

    assert ("hardware_engineering", "GPU") in plan
    gpu = plan[("hardware_engineering", "GPU")]
    assert len(gpu) == 3
    assert all(conf == 0.90 for _, conf in gpu)

    # CPU team has only 1 → no sub-cluster
    assert ("hardware_engineering", "CPU") not in plan
    # Compiler sub-cluster requires canonical_domain, which is missing
    assert ("software_engineering", "Compiler") not in plan


@pytest.mark.unit
def test_ic_track_flag_set_via_taxonomy() -> None:
    """is_ic_track derived from title patterns at row build time."""
    persons = [
        _person(pid=1, title="Distinguished Engineer", domain="hardware_engineering"),
        _person(pid=2, title="Engineering Manager", domain="hardware_engineering"),
        _person(pid=3, title="Principal Architect", domain="hardware_engineering"),
        _person(pid=4, title="Director, Engineering", domain="hardware_engineering"),
    ]
    assert persons[0].is_ic_track is True   # Distinguished Engineer
    assert persons[1].is_ic_track is False  # Engineering Manager
    assert persons[2].is_ic_track is True   # Principal Architect
    assert persons[3].is_ic_track is False  # Director


@pytest.mark.unit
def test_persons_below_min_size_in_pure_plan_still_cluster() -> None:
    """The pure plan doesn't enforce MIN_CLUSTER_SIZE — that's the orchestrator's job."""
    persons = [
        _person(pid=1, title="HW Engineer", domain="hardware_engineering"),
    ]
    plan = _build_cluster_plan(persons)
    # Single-person main cluster is fine in pure logic; orchestrator gates above.
    assert plan == {("hardware_engineering", None): [(persons[0], 0.95)]}


# ── Orchestrator tests (DB-shimmed) ──────────────────────────────────────────


@pytest.fixture
def stub_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Programmable shim over clustering's fetch / acquire helpers."""
    state: dict[str, Any] = {
        "company_present": True,
        "company_name": "AMD",
        "persons": [
            # Three hardware engineers, all canonical, two on same team
            {
                "person_id": UUID("00000000-0000-0000-0000-cccc00000001"),
                "account_id": ACCOUNT,
                "title": "Hardware Engineer",
                "domain": "hardware_engineering",
                "team": "Zen",
            },
            {
                "person_id": UUID("00000000-0000-0000-0000-cccc00000002"),
                "account_id": ACCOUNT,
                "title": "Senior Hardware Engineer",
                "domain": "hardware_engineering",
                "team": "Zen",
            },
            {
                "person_id": UUID("00000000-0000-0000-0000-cccc00000003"),
                "account_id": ACCOUNT,
                "title": "Distinguished Engineer",
                "domain": "hardware_engineering",
                "team": None,
            },
        ],
        "writes_clusters": [],
        "writes_members": [],
        "next_cluster_id": 1,
    }

    async def fake_fetch(sql: str, *args: Any) -> list[dict]:
        sql_upper = sql.upper()
        if "FROM COMPANIES WHERE ID" in sql_upper:
            if not state["company_present"]:
                return []
            return [{"id": args[0], "name": state["company_name"]}]
        if "FROM EMPLOYMENT_PERIODS" in sql_upper and "WHERE EP.COMPANY_ID" in sql_upper:
            return list(state["persons"])
        if "FROM EMPLOYMENT_PERIODS" in sql_upper and "GROUP BY COMPANY_ID" in sql_upper:
            return [{"company_id": args[0], "n": len(state["persons"])}]
        return []

    class _FakeConn:
        def transaction(self):
            class _Tx:
                async def __aenter__(self_):
                    return None

                async def __aexit__(self_, *_a):
                    return None

            return _Tx()

        async def fetchrow(self, sql: str, *args: Any) -> dict:
            # Cluster upsert returns a synthesized id and captures the args.
            cluster_id = UUID(f"00000000-0000-0000-0000-dddd{state['next_cluster_id']:08d}")
            state["next_cluster_id"] += 1
            state["writes_clusters"].append(
                {
                    "id": cluster_id,
                    "account_id": args[0],
                    "company_id": args[1],
                    "functional_domain": args[2],
                    "sub_domain": args[3],
                    "member_count": args[4],
                }
            )
            return {"id": cluster_id}

        async def execute(self, sql: str, *args: Any) -> str:
            # Bulk path now does CREATE TEMP TABLE / TRUNCATE / INSERT SELECT
            # — these come through `execute` with no positional args. The
            # actual rows are written by `copy_records_to_table` below.
            # The legacy single-row INSERT path (5 args) still works for any
            # callers using `_upsert_member` directly.
            if len(args) == 5:
                state["writes_members"].append(
                    {
                        "account_id": args[0],
                        "cluster_id": args[1],
                        "person_id": args[2],
                        "membership_confidence": args[3],
                        "is_ic_track": args[4],
                    }
                )
            return "ok"

        async def copy_records_to_table(
            self,
            table_name: str,
            *,
            records: list,
            columns: list[str],
        ) -> None:
            """Stub for the bulk path — record each tuple as if it were a
            single-row insert so existing assertions on `writes_members`
            continue to hold without rewriting them.
            """
            assert table_name == "_cluster_member_chunk"
            assert columns == [
                "account_id",
                "cluster_id",
                "person_id",
                "membership_confidence",
                "is_ic_track",
            ]
            for rec in records:
                state["writes_members"].append(
                    {
                        "account_id": rec[0],
                        "cluster_id": rec[1],
                        "person_id": rec[2],
                        "membership_confidence": rec[3],
                        "is_ic_track": rec[4],
                    }
                )

    class _AcquireCtx:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr(clustering, "fetch", fake_fetch)
    monkeypatch.setattr(clustering, "acquire", lambda: _AcquireCtx())
    return state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cluster_company_writes_main_and_sub_cluster(stub_db) -> None:
    """One main cluster + one sub-cluster (Zen team has 2 canonical members)."""
    rollup = await cluster_company(COMPANY_A)

    # Main hardware_engineering cluster + Zen sub-cluster = 2 cluster rows
    assert rollup.cluster_count == 2
    # 3 persons in main + 2 (re-emitted) in Zen sub = 5 member rows
    assert rollup.member_count == 5
    assert rollup.ic_track_count == 1  # Distinguished Engineer
    assert rollup.company_name == "AMD"

    # Verify the cluster shapes — one main, one sub
    main_clusters = [c for c in stub_db["writes_clusters"] if c["sub_domain"] is None]
    sub_clusters = [c for c in stub_db["writes_clusters"] if c["sub_domain"] is not None]
    assert len(main_clusters) == 1
    assert main_clusters[0]["functional_domain"] == "hardware_engineering"
    assert main_clusters[0]["member_count"] == 3
    assert len(sub_clusters) == 1
    assert sub_clusters[0]["sub_domain"] == "Zen"
    assert sub_clusters[0]["member_count"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cluster_company_below_min_size_skipped(stub_db) -> None:
    """Companies under MIN_CLUSTER_SIZE produce zero rows."""
    stub_db["persons"] = stub_db["persons"][:2]  # only 2 < MIN_CLUSTER_SIZE
    rollup = await cluster_company(COMPANY_A)

    assert rollup.cluster_count == 0
    assert rollup.member_count == 0
    assert stub_db["writes_clusters"] == []
    assert stub_db["writes_members"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cluster_company_missing_raises(stub_db) -> None:
    stub_db["company_present"] = False

    with pytest.raises(LookupError, match="company .* not found"):
        await cluster_company(COMPANY_A)

    assert stub_db["writes_clusters"] == []
    assert stub_db["writes_members"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_account_id_flows_to_inserts(stub_db) -> None:
    """All cluster + member writes carry the account_id from employment_periods."""
    other_account = UUID("00000000-0000-0000-0000-000000000fff")
    for p in stub_db["persons"]:
        p["account_id"] = other_account

    await cluster_company(COMPANY_A)

    assert all(c["account_id"] == other_account for c in stub_db["writes_clusters"])
    assert all(m["account_id"] == other_account for m in stub_db["writes_members"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ic_track_member_carries_flag(stub_db) -> None:
    """Distinguished Engineer member row has is_ic_track=True."""
    await cluster_company(COMPANY_A)

    de_id = UUID("00000000-0000-0000-0000-cccc00000003")
    de_members = [m for m in stub_db["writes_members"] if m["person_id"] == de_id]
    # Distinguished Engineer is in the main hardware cluster only (no team set)
    assert len(de_members) == 1
    assert de_members[0]["is_ic_track"] is True

    # Non-IC member
    non_ic_id = UUID("00000000-0000-0000-0000-cccc00000001")
    ne_members = [m for m in stub_db["writes_members"] if m["person_id"] == non_ic_id]
    assert all(not m["is_ic_track"] for m in ne_members)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_membership_confidence_propagated_canonical_vs_sub(stub_db) -> None:
    """Main = 0.95, sub-cluster = 0.90."""
    await cluster_company(COMPANY_A)

    # Identify which writes were main vs sub by looking at cluster shape
    main_cluster = next(
        c for c in stub_db["writes_clusters"] if c["sub_domain"] is None
    )
    sub_cluster = next(
        c for c in stub_db["writes_clusters"] if c["sub_domain"] is not None
    )

    main_writes = [
        m for m in stub_db["writes_members"] if m["cluster_id"] == main_cluster["id"]
    ]
    sub_writes = [
        m for m in stub_db["writes_members"] if m["cluster_id"] == sub_cluster["id"]
    ]

    assert all(m["membership_confidence"] == 0.95 for m in main_writes)
    assert all(m["membership_confidence"] == 0.90 for m in sub_writes)
