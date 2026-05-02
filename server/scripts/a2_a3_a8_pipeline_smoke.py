"""Live pipeline smoke: A2 hierarchy → A8 propagation → A3 scope.

Builds on the A1 clustering smoke (msg 152). Now that org_functional_clusters
+ org_cluster_members are populated, walks the rest of Stage 1 + Stage 3.3:

1. **A2** — `infer_company_hierarchy` for each company that has clusters →
   populates `org_reporting_edges`
2. **A8** — confidence propagation extending hierarchy.py → fills
   `path_confidence` on the edges
3. **A3** — `estimate_account_scopes` → populates `person_scope_estimates`

Pure read-and-write smoke. Idempotent on retry. Safe to repeat.

Run:
    cd server && uv run python scripts/a2_a3_a8_pipeline_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

# Make `credence` importable
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
                os.environ.setdefault("SUPABASE_JWT_SECRET", "scratch")
                os.environ.setdefault("SUPABASE_URL", "http://localhost")
                return value
    raise SystemExit("DATABASE_URL not set")


async def main() -> None:
    dsn = _load_dsn()
    print("=== A2 + A8 + A3 live pipeline smoke ===\n")

    conn = await asyncpg.connect(_normalize_dsn(dsn), statement_cache_size=0)
    try:
        # Snapshot pre-state for the full pipeline tables
        pre_edges = await conn.fetchval(
            "SELECT count(*) FROM org_reporting_edges"
        )
        pre_scope = await conn.fetchval(
            "SELECT count(*) FROM person_scope_estimates"
        )
        # Find companies that have clusters → eligible for A2
        rows = await conn.fetch(
            """
            SELECT DISTINCT c.id, c.canonical_name,
                   (SELECT count(*) FROM org_cluster_members ocm
                    JOIN org_functional_clusters ofc ON ofc.id = ocm.cluster_id
                    WHERE ofc.company_id = c.id) AS member_count
            FROM org_functional_clusters ofc
            JOIN companies c ON c.id = ofc.company_id
            ORDER BY member_count DESC
            LIMIT 5
            """
        )
        if not rows:
            print("No companies with cluster rows. Run A1 smoke first.")
            return

        print("Companies with clusters:")
        for r in rows:
            print(f"  {r['canonical_name']:35s} members={r['member_count']}")
        print(f"\nPre-state: org_reporting_edges={pre_edges} "
              f"person_scope_estimates={pre_scope}")
    finally:
        await conn.close()

    # ── A2 hierarchy ──────────────────────────────────────────────────────
    from credence.orgchart.hierarchy import infer_company_hierarchy

    print("\n--- A2 hierarchy inference ---")
    for r in rows:
        try:
            rollups = await infer_company_hierarchy(r["id"])
            edges_written = sum(rl.edges_written for rl in rollups)
            no_cand = sum(rl.edges_skipped_no_candidate for rl in rollups)
            span_resolved = sum(rl.span_violations_resolved for rl in rollups)
            print(
                f"  {r['canonical_name']:35s} "
                f"clusters_processed={len(rollups)} "
                f"edges_written={edges_written} "
                f"skipped_no_candidate={no_cand} "
                f"span_resolved={span_resolved}"
            )
        except Exception as exc:
            print(f"  {r['canonical_name']:35s} FAILED: {type(exc).__name__}: {exc}")

    # ── A8 confidence propagation ──────────────────────────────────────────
    print("\n--- A8 confidence propagation ---")
    try:
        from credence.orgchart.propagation import propagate_all_accounts
        rollups = await propagate_all_accounts()
        total_edges = sum(rp.edges_total for rp in rollups)
        total_propagated = sum(rp.edges_propagated for rp in rollups)
        total_cycle_skipped = sum(rp.cycle_skipped for rp in rollups)
        total_orphan_skipped = sum(rp.orphan_skipped for rp in rollups)
        print(
            f"  tenants={len(rollups)} edges_total={total_edges} "
            f"propagated={total_propagated} "
            f"cycle_skipped={total_cycle_skipped} "
            f"orphan_skipped={total_orphan_skipped}"
        )
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")

    # ── A3 scope estimation ────────────────────────────────────────────────
    print("\n--- A3 scope estimation ---")
    try:
        from credence.orgchart.scope import estimate_all_scopes
        n_scopes = await estimate_all_scopes()
        print(f"  scope estimates written: {n_scopes}")
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")

    # ── Post-state ────────────────────────────────────────────────────────
    conn2 = await asyncpg.connect(_normalize_dsn(dsn), statement_cache_size=0)
    try:
        post_edges = await conn2.fetchval(
            "SELECT count(*) FROM org_reporting_edges"
        )
        post_scope = await conn2.fetchval(
            "SELECT count(*) FROM person_scope_estimates"
        )
        with_path_conf = await conn2.fetchval(
            "SELECT count(*) FROM org_reporting_edges WHERE path_confidence IS NOT NULL"
        )
        print(f"\nPost-state:")
        print(f"  org_reporting_edges       = {post_edges} (Δ {post_edges - pre_edges})")
        print(f"    with path_confidence    = {with_path_conf}")
        print(f"  person_scope_estimates    = {post_scope} (Δ {post_scope - pre_scope})")

        # Sample edges
        if post_edges:
            sample = await conn2.fetch(
                """
                SELECT mp.canonical_name AS manager_name,
                       rp.canonical_name AS report_name,
                       e.confidence,
                       e.path_confidence,
                       e.inference_method
                FROM org_reporting_edges e
                JOIN persons mp ON mp.id = e.manager_id
                JOIN persons rp ON rp.id = e.report_id
                LIMIT 5
                """
            )
            print("\nSample edges:")
            for s in sample:
                pc = (
                    f"{s['path_confidence']:.2f}"
                    if s["path_confidence"] is not None else "NULL"
                )
                print(
                    f"  {s['manager_name'][:24]:24s} → {s['report_name'][:24]:24s} "
                    f"conf={s['confidence']:.2f} path={pc} "
                    f"({s['inference_method']})"
                )

        # Sample scopes
        if post_scope:
            sample = await conn2.fetch(
                """
                SELECT p.canonical_name, s.team_size_min, s.team_size_max,
                       s.budget_authority_level, s.owns_functions
                FROM person_scope_estimates s
                JOIN persons p ON p.id = s.person_id
                ORDER BY s.team_size_max DESC NULLS LAST
                LIMIT 5
                """
            )
            print("\nSample scopes (top by team_size_max):")
            for s in sample:
                print(
                    f"  {s['canonical_name'][:24]:24s} "
                    f"team={s['team_size_min']}–{s['team_size_max']} "
                    f"budget={s['budget_authority_level']} "
                    f"functions={list(s['owns_functions'])}"
                )
    finally:
        await conn2.close()

    from credence.db import close_pool
    await close_pool()
    print("\n=== smoke complete ===")


if __name__ == "__main__":
    asyncio.run(main())
