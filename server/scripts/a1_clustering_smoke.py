"""A1 clustering live smoke test.

Picks the top companies by current employee count (those with most
`employment_periods.is_current=TRUE` rows) and runs `cluster_company` on
each. Reports the rollup + queries the resulting `org_functional_clusters`
to confirm rows landed.

Idempotent — re-running upserts the same cluster rows. Safe to run
repeatedly. Reads + writes only to org_* tables created by the just-applied
A0 migration (no impact on existing v2 tables).

Run:
    cd server && uv run python scripts/a1_clustering_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

# Make `credence` importable when run from server/scripts
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg


def _normalize_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _load_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return dsn
    env_path = Path(__file__).resolve().parents[2] / ".env.local"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                value = line.split("=", 1)[1].strip()
                os.environ.setdefault("DATABASE_URL", value)
                # Stub other settings the credence config demands
                os.environ.setdefault("SUPABASE_JWT_SECRET", "scratch")
                os.environ.setdefault("SUPABASE_URL", "http://localhost")
                return value
    raise SystemExit("DATABASE_URL not set")


async def main() -> None:
    dsn = _load_dsn()
    print("=== A1 clustering live smoke test ===\n")

    # Pick the top 5 companies by current employee count
    conn = await asyncpg.connect(_normalize_dsn(dsn), statement_cache_size=0)
    try:
        rows = await conn.fetch(
            """
            SELECT c.id, c.canonical_name, count(*) AS n_current
            FROM employment_periods ep
            JOIN companies c ON c.id = ep.company_id
            WHERE ep.is_current = TRUE
            GROUP BY c.id, c.canonical_name
            ORDER BY n_current DESC
            LIMIT 5
            """
        )
        if not rows:
            print("No companies with current employees in employment_periods.")
            return

        print("Top 5 companies by current employee count:")
        for r in rows:
            print(f"  {r['canonical_name']:30s} n={r['n_current']}")
        print()

        # Snapshot pre-state
        pre_clusters = await conn.fetchval(
            "SELECT count(*) FROM org_functional_clusters"
        )
        pre_members = await conn.fetchval(
            "SELECT count(*) FROM org_cluster_members"
        )
        print(f"Pre-state: org_functional_clusters={pre_clusters}, "
              f"org_cluster_members={pre_members}")
    finally:
        await conn.close()

    # Run cluster_company on each — uses the real asyncpg pool
    from credence.orgchart.clustering import cluster_company, ClusterRollup

    rollups: list[ClusterRollup] = []
    for r in rows:
        company_id = r["id"]
        try:
            rollup = await cluster_company(company_id)
            rollups.append(rollup)
            print(f"\n{rollup.company_name}: clusters={rollup.cluster_count}, "
                  f"members={rollup.member_count}, ic={rollup.ic_track_count}")
        except Exception as exc:
            print(f"\n{r['canonical_name']}: FAILED — {exc}")

    # Re-query post-state
    conn2 = await asyncpg.connect(_normalize_dsn(dsn), statement_cache_size=0)
    try:
        post_clusters = await conn2.fetchval(
            "SELECT count(*) FROM org_functional_clusters"
        )
        post_members = await conn2.fetchval(
            "SELECT count(*) FROM org_cluster_members"
        )
        print(f"\nPost-state: org_functional_clusters={post_clusters} "
              f"(Δ {post_clusters - pre_clusters}), "
              f"org_cluster_members={post_members} (Δ {post_members - pre_members})")

        # Show the cluster shapes for the first company
        if rollups and rollups[0].cluster_count > 0:
            first_id = rollups[0].company_id
            print(f"\nClusters for {rollups[0].company_name}:")
            cluster_rows = await conn2.fetch(
                """
                SELECT functional_domain, sub_domain, member_count
                FROM org_functional_clusters
                WHERE company_id = $1
                ORDER BY functional_domain, sub_domain NULLS FIRST
                """,
                first_id,
            )
            for cr in cluster_rows:
                sub = f"/{cr['sub_domain']}" if cr["sub_domain"] else ""
                print(f"  {cr['functional_domain']}{sub:25s} n={cr['member_count']}")

        # IC track members
        ic_count = await conn2.fetchval(
            "SELECT count(*) FROM org_cluster_members WHERE is_ic_track = TRUE"
        )
        print(f"\nTotal IC-track members across all clusters: {ic_count}")
    finally:
        await conn2.close()

    # Close the credence pool
    from credence.db import close_pool
    await close_pool()

    print("\n=== smoke test complete ===")


if __name__ == "__main__":
    asyncio.run(main())
