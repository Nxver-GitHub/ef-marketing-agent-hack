"""Refresh the org chart for a single company end-to-end.

The "refresh the chart for prospect X" button the frontend assumes exists.
Runs the full Stage 1 pipeline against one company, in dependency order:

  1. **A1** clustering — `cluster_company(company_id)` populates
     `org_functional_clusters` + `org_cluster_members` for this company's
     current employees.
  2. **A2** hierarchy — `infer_company_hierarchy(company_id)` walks every
     cluster of this company and writes manager → report edges to
     `org_reporting_edges`.
  3. **A8** confidence propagation — `propagate_account(account_id)` fills
     `path_confidence` on every current edge for this company's tenant.
  4. **A3** scope estimation — `estimate_account_scopes(account_id)` writes
     `person_scope_estimates` rows.
  5. **A7** validation — `validate_account(account_id)` runs the read-only
     audit (span / cycle / IC misclassification) and prints any violations.

Note: A3 / A7 / A8 are account-scoped (operate on every cluster for the
tenant), not company-scoped — running them touches all companies the tenant
owns. That's intentional and idempotent: A2's edge writes are isolated to
this company's clusters, and A3/A8 just refresh the per-tenant materialized
views. Re-running on the same data is a no-op aside from `updated_at`
bumps and (in A8's case) recomputing path_confidence on edges whose
component edges may have shifted.

## Usage

    cd server && uv run python scripts/orgchart_refresh_for_company.py \\
        --company-id 00000000-0000-0000-0000-000000000123

The company must exist in `companies` AND have at least
`MIN_CLUSTER_SIZE` (default 3) current employees in `employment_periods`,
or A1 will skip clustering and the rest of the pipeline produces nothing
new.

## Output

A summary block at the end:

    === orgchart refresh complete ===
    company:                Acme Semiconductors (00000000-…-0123)
    tenant:                 00000000-…-0001
    clusters_written:       4
    cluster_members:        17
    ic_track_members:       3
    edges_written:          12
    edges_skipped:          5         (no candidate manager above min_confidence)
    span_violations:        0
    propagation_edges:      89        (across the full tenant)
    propagation_skipped:    2         (cycle members)
    scope_rows:             156       (across the full tenant)
    validation:
      span_violations:      0
      cycle_violations:     0
      ic_misclassifications:0
      → ✅ tenant is clean
    stubs_created_in_run:   0         (no new persons.is_unresolved_target rows)

Read-and-write smoke. Idempotent. Safe to repeat.
"""
from __future__ import annotations

import argparse
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


async def _resolve_account_id(conn: asyncpg.Connection, company_id: UUID) -> UUID | None:
    """Resolve company → tenant via any current employee's `account_id`.

    Returns None when the company has no current employees (A1 will skip,
    making the whole refresh a no-op anyway).
    """
    row = await conn.fetchrow(
        """
        SELECT ep.account_id
        FROM employment_periods ep
        WHERE ep.company_id = $1 AND ep.is_current = TRUE
        LIMIT 1
        """,
        company_id,
    )
    return row["account_id"] if row else None


async def _company_name(conn: asyncpg.Connection, company_id: UUID) -> str:
    row = await conn.fetchrow(
        "SELECT canonical_name FROM companies WHERE id = $1", company_id,
    )
    return row["canonical_name"] if row else "<unknown>"


async def _count_stubs(conn: asyncpg.Connection, company_id: UUID) -> int:
    """Stubs are persons rows with is_unresolved_target=TRUE for the company.

    Returns 0 if the column doesn't exist yet (migration
    20260501_v3_persons_unresolved_target.sql not applied) — the script
    is forward-compatible.
    """
    try:
        row = await conn.fetchrow(
            """
            SELECT count(*) AS n
            FROM persons
            WHERE current_company_id = $1 AND is_unresolved_target = TRUE
            """,
            company_id,
        )
        return int(row["n"]) if row else 0
    except asyncpg.UndefinedColumnError:
        return -1  # sentinel: column not present


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--company-id",
        required=True,
        type=UUID,
        help="UUID of the company to refresh.",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip A7 validation step (faster; useful in tight loops).",
    )
    args = parser.parse_args()

    dsn = _load_dsn()
    company_id: UUID = args.company_id

    print(f"=== orgchart refresh for company {company_id} ===")

    # ── Pre-flight: confirm company exists + resolve tenant ──────────────────
    conn = await asyncpg.connect(_normalize_dsn(dsn), statement_cache_size=0)
    try:
        company_name = await _company_name(conn, company_id)
        if company_name == "<unknown>":
            print(f"  ❌ company {company_id} not found in `companies`. Aborting.")
            await conn.close()
            return 1

        account_id = await _resolve_account_id(conn, company_id)
        if account_id is None:
            print(
                f"  ⚠ company {company_name} has no current employees in "
                "`employment_periods`. Pipeline will be a no-op."
            )
            await conn.close()
            return 0

        stubs_before = await _count_stubs(conn, company_id)
        print(f"  company:   {company_name} ({company_id})")
        print(f"  tenant:    {account_id}")
        if stubs_before == -1:
            print(
                "  ⚠ persons.is_unresolved_target column not present — Task 1-C "
                "stub creation will fail at runtime if explicit edges land. "
                "Apply 20260501_v3_persons_unresolved_target.sql to fix."
            )
            stubs_before = 0
    finally:
        await conn.close()

    # ── A1 clustering ────────────────────────────────────────────────────────
    print("\n--- A1 clustering ---")
    from credence.orgchart.clustering import cluster_company

    try:
        cluster_rollup = await cluster_company(company_id)
        print(
            f"  clusters_written = {cluster_rollup.cluster_count} "
            f"members = {cluster_rollup.member_count} "
            f"ic_track = {cluster_rollup.ic_track_count}"
        )
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the script
        print(f"  ❌ FAILED: {type(exc).__name__}: {exc}")
        from credence.db import close_pool
        await close_pool()
        return 1

    if cluster_rollup.cluster_count == 0:
        print(
            "  Below MIN_CLUSTER_SIZE — no clusters written. Pipeline halts; "
            "downstream stages have nothing to operate on."
        )
        from credence.db import close_pool
        await close_pool()
        return 0

    # ── A2 hierarchy ─────────────────────────────────────────────────────────
    print("\n--- A2 hierarchy ---")
    from credence.orgchart.hierarchy import infer_company_hierarchy

    try:
        hierarchy_rollups = await infer_company_hierarchy(company_id)
        edges_written = sum(r.edges_written for r in hierarchy_rollups)
        edges_skipped = sum(r.edges_skipped_no_candidate for r in hierarchy_rollups)
        span_resolved = sum(r.span_violations_resolved for r in hierarchy_rollups)
        print(
            f"  clusters_processed = {len(hierarchy_rollups)} "
            f"edges_written = {edges_written} "
            f"skipped_no_candidate = {edges_skipped} "
            f"span_violations = {span_resolved}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ FAILED: {type(exc).__name__}: {exc}")
        from credence.db import close_pool
        await close_pool()
        return 1

    # ── A8 propagation ───────────────────────────────────────────────────────
    print("\n--- A8 confidence propagation ---")
    from credence.orgchart.propagation import propagate_account

    try:
        prop_rollup = await propagate_account(account_id)
        print(
            f"  edges_total = {prop_rollup.edges_total} "
            f"propagated = {prop_rollup.edges_propagated} "
            f"cycle_skipped = {prop_rollup.cycle_skipped} "
            f"orphan_skipped = {prop_rollup.orphan_skipped}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ FAILED: {type(exc).__name__}: {exc}")
        from credence.db import close_pool
        await close_pool()
        return 1

    # ── A3 scope estimation ──────────────────────────────────────────────────
    print("\n--- A3 scope estimation ---")
    from credence.orgchart.scope import estimate_account_scopes

    try:
        scope_count = await estimate_account_scopes(account_id)
        print(f"  scope_rows_written = {scope_count}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ FAILED: {type(exc).__name__}: {exc}")
        from credence.db import close_pool
        await close_pool()
        return 1

    # ── A7 validation (optional) ─────────────────────────────────────────────
    if not args.skip_validation:
        print("\n--- A7 validation ---")
        from credence.orgchart.validation import validate_account

        try:
            report = await validate_account(account_id)
            print(
                f"  span_violations      = {len(report.span_violations)}\n"
                f"  cycle_violations     = {len(report.cycle_violations)}\n"
                f"  ic_misclassifications= {len(report.ic_violations)}"
            )
            if report.is_clean:
                print("  → ✅ tenant is clean")
            else:
                print(f"  → ⚠ {report.total_violations} violation(s) for triage")
                # First-3 sample of each violation kind
                for v in report.span_violations[:3]:
                    print(
                        f"    span: manager {str(v.manager_id)[:8]}… "
                        f"{v.direct_report_count}/{v.span_cap} ({v.seniority_tier})"
                    )
                for v in report.cycle_violations[:3]:
                    cycle_str = " → ".join(str(p)[:8] for p in v.cycle)
                    print(f"    cycle: {cycle_str}")
                for v in report.ic_violations[:3]:
                    print(
                        f"    ic: IC manager {str(v.manager_id)[:8]}… "
                        f"→ non-IC report {str(v.report_id)[:8]}…"
                    )
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ FAILED: {type(exc).__name__}: {exc}")

    # ── Stub delta ───────────────────────────────────────────────────────────
    conn = await asyncpg.connect(_normalize_dsn(dsn), statement_cache_size=0)
    try:
        stubs_after = await _count_stubs(conn, company_id)
        if stubs_after >= 0:
            print(f"\nstubs_total_for_company = {stubs_after} "
                  f"(Δ {stubs_after - stubs_before})")
    finally:
        await conn.close()

    from credence.db import close_pool
    await close_pool()
    print("\n=== orgchart refresh complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
