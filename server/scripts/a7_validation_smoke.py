"""Live smoke for A7 — `validate_all_accounts()` against real Supabase.

Runs the read-only validator against the materialized `org_reporting_edges`
graph and prints per-tenant violation counts. Useful as the final QA step
after A2 hierarchy + A3 scope + A8 propagation have populated the chart
(see `a2_a3_a8_pipeline_smoke.py`).

Read-only. No writes. Safe to run repeatedly.

Run:
    cd server && uv run python scripts/a7_validation_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

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
    print("=== A7 validation smoke ===")

    # ── Pre-state ─────────────────────────────────────────────────────────
    conn = await asyncpg.connect(_normalize_dsn(dsn), statement_cache_size=0)
    try:
        edge_count = await conn.fetchval(
            "SELECT count(*) FROM org_reporting_edges WHERE is_current = TRUE"
        )
        account_count = await conn.fetchval(
            "SELECT count(DISTINCT account_id) FROM org_reporting_edges"
        )
        print(f"\nGraph snapshot:")
        print(f"  current edges across all tenants  = {edge_count}")
        print(f"  distinct tenants with edges        = {account_count}")
    finally:
        await conn.close()

    if edge_count == 0:
        print(
            "\nNo edges to validate — run a2_a3_a8_pipeline_smoke.py first to "
            "populate org_reporting_edges, then re-run this smoke."
        )
        return

    # ── Run validator ────────────────────────────────────────────────────
    print("\n--- Running validate_all_accounts() ---")
    from credence.orgchart.validation import validate_all_accounts

    try:
        reports = await validate_all_accounts()
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        from credence.db import close_pool
        await close_pool()
        return

    # ── Per-tenant rollup ─────────────────────────────────────────────────
    total_span = 0
    total_cycle = 0
    total_ic = 0
    clean_tenants = 0
    dirty_tenants = 0

    print(f"\nValidator ran for {len(reports)} tenant(s):")
    for account_id, report in reports.items():
        if report.is_clean:
            clean_tenants += 1
            print(f"  {str(account_id)[:8]}...  ✅ clean")
            continue
        dirty_tenants += 1
        total_span += len(report.span_violations)
        total_cycle += len(report.cycle_violations)
        total_ic += len(report.ic_violations)
        print(
            f"  {str(account_id)[:8]}...  ⚠ "
            f"span={len(report.span_violations)} "
            f"cycles={len(report.cycle_violations)} "
            f"ic={len(report.ic_violations)}"
        )

    # ── Aggregate summary ────────────────────────────────────────────────
    print(f"\nTotals:")
    print(f"  clean tenants            = {clean_tenants}")
    print(f"  tenants with violations  = {dirty_tenants}")
    print(f"  span_violations          = {total_span}")
    print(f"  cycle_violations         = {total_cycle}")
    print(f"  ic_misclassifications    = {total_ic}")

    # ── Sample violations (first 3 from any dirty tenant) ────────────────
    sample_acct = next(
        (a for a, r in reports.items() if not r.is_clean),
        None,
    )
    if sample_acct is not None:
        report = reports[sample_acct]
        print(f"\nSample violations from {str(sample_acct)[:8]}...:")

        if report.span_violations:
            print("  Span:")
            for v in report.span_violations[:3]:
                print(
                    f"    manager {str(v.manager_id)[:8]}... "
                    f"{v.direct_report_count}/{v.span_cap} "
                    f"reports ({v.seniority_tier})"
                )

        if report.cycle_violations:
            print("  Cycles:")
            for v in report.cycle_violations[:3]:
                cycle_str = " → ".join(str(p)[:8] for p in v.cycle)
                print(f"    {cycle_str}")

        if report.ic_violations:
            print("  IC misclassifications:")
            for v in report.ic_violations[:3]:
                print(
                    f"    IC manager {str(v.manager_id)[:8]}... "
                    f"→ non-IC report {str(v.report_id)[:8]}..."
                )

    from credence.db import close_pool
    await close_pool()
    print("\n=== smoke complete ===")


if __name__ == "__main__":
    asyncio.run(main())
