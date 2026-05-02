"""Stream 3 — read-only pipeline state probe.

Connects via DATABASE_URL (the same path score_runner uses) and reports:
  1. score_weights rows per account, which is active
  2. score_records counts per account
  3. RPC `replace_active_score_weights` exists in pg_proc
  4. account-with-active-row vs account-without (the new-signup gap)
  5. Smoke test of the auto-account-creation gap by counting accounts
     missing a score_weights row

Pure read. Does not write or modify anything. Safe to run repeatedly.
Invoke from the server/ dir:

    uv run python scripts/s3_pipeline_check.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg


def _normalize_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def main() -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        env_path = Path(__file__).resolve().parents[2] / ".env.local"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("DATABASE_URL="):
                    dsn = line.split("=", 1)[1].strip()
                    break
    if not dsn:
        raise SystemExit("DATABASE_URL not set; cannot probe live state")

    print("=== Stream 3 pipeline state probe ===\n")

    conn = await asyncpg.connect(_normalize_dsn(dsn), statement_cache_size=0)
    try:
        # 1. score_weights per account
        rows = await conn.fetch(
            """
            SELECT a.id, a.display_name, a.slug,
                   sw.id AS sw_id, sw.is_active, sw.created_by, sw.authenticity_w,
                   sw.authority_w, sw.warmth_w, sw.created_at
            FROM accounts a
            LEFT JOIN score_weights sw ON sw.account_id = a.id
            ORDER BY a.created_at, sw.created_at
            """
        )
        print(f"-- score_weights rows by account ({len(rows)} total joined rows)")
        per_account: dict = {}
        for r in rows:
            per_account.setdefault(str(r["id"]), {"slug": r["slug"], "rows": []})
            if r["sw_id"] is not None:
                per_account[str(r["id"])]["rows"].append(dict(r))
        for aid, info in per_account.items():
            actives = [w for w in info["rows"] if w["is_active"]]
            print(
                f"  account {aid[:8]}… ({info['slug']}): "
                f"{len(info['rows'])} rows, {len(actives)} active"
            )
            for w in info["rows"]:
                star = "★" if w["is_active"] else " "
                print(
                    f"    {star} {str(w['sw_id'])[:8]}… "
                    f"auth={w['authenticity_w']} authority={w['authority_w']} "
                    f"warmth={w['warmth_w']} ({w['created_by']} @ {w['created_at']:%Y-%m-%d %H:%M})"
                )

        # 2. score_records counts per account
        rec_rows = await conn.fetch(
            """
            SELECT account_id, count(*) AS n
            FROM score_records
            GROUP BY account_id
            """
        )
        print(f"\n-- score_records counts ({len(rec_rows)} accounts have records)")
        for r in rec_rows:
            print(f"  account {str(r['account_id'])[:8]}…: {r['n']} records")
        if not rec_rows:
            print("  (none yet — expected; the route writes lazily)")

        # 3. RPC presence
        rpc = await conn.fetchrow(
            """
            SELECT proname, prosrc IS NOT NULL AS has_source
            FROM pg_proc
            WHERE proname = 'replace_active_score_weights'
            """
        )
        print("\n-- RPC replace_active_score_weights")
        if rpc:
            print(f"  ✓ exists ({rpc['proname']}, has_source={rpc['has_source']})")
        else:
            print("  ✗ MISSING — Settings save will fail on PostgREST .rpc() call")

        # 4. Accounts without an active score_weights row
        gap = await conn.fetch(
            """
            SELECT a.id, a.slug, a.created_at
            FROM accounts a
            WHERE NOT EXISTS (
              SELECT 1 FROM score_weights sw
              WHERE sw.account_id = a.id AND sw.is_active = TRUE
            )
            ORDER BY a.created_at DESC
            """
        )
        print(f"\n-- Accounts WITHOUT an active score_weights row ({len(gap)})")
        if gap:
            print("  ⚠ FINDING: these tenants will 503 on GET /score/{prospect_id}")
            for r in gap[:5]:
                print(f"    {str(r['id'])[:8]}… ({r['slug']}) created {r['created_at']:%Y-%m-%d}")
            print("    Cause: handle_new_user() trigger seeds account_settings but NOT score_weights.")
            print("    Fix: add an INSERT into score_weights inside handle_new_user().")
        else:
            print("  ✓ all accounts have an active row (Contract 6 seed covered them)")

        # 5. Sanity: ensure the unique-active invariant holds
        dup = await conn.fetch(
            """
            SELECT account_id, count(*) AS n
            FROM score_weights
            WHERE is_active = TRUE
            GROUP BY account_id
            HAVING count(*) > 1
            """
        )
        print("\n-- Unique-active invariant")
        if dup:
            print(f"  ✗ VIOLATED: {len(dup)} accounts have >1 active row")
            for r in dup:
                print(f"    {str(r['account_id'])[:8]}…: {r['n']} active")
        else:
            print("  ✓ each account has at most one active row")

    finally:
        await conn.close()
    print("\n=== done ===")


if __name__ == "__main__":
    asyncio.run(main())
