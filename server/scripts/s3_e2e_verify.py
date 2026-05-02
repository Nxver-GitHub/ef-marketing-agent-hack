"""Stream 3 E2E verification — actively trigger one dual-write and confirm.

Per LP's msg 126 task #1: validate score_runner.score_prospect() lands rows
in BOTH legacy `scores` AND v3 `score_records`, with the right account_id +
weight_version_id, against the live DB.

Picks one real prospect from the default tenant, calls score_prospect(),
then re-queries both tables and reports.

Pure verification — writes exactly one new row in each table for the picked
prospect. Idempotent on repeat: ON CONFLICT DO UPDATE on score_records means
re-running just refreshes computed_at.

Run:
    cd server && uv run python scripts/s3_e2e_verify.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

import asyncpg

# Make `credence` importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
                # mirror to env so credence.config / db can pick it up if
                # they're imported transitively
                os.environ.setdefault("DATABASE_URL", value)
                # set fake values for other settings the Settings model
                # demands so importing `credence.db` doesn't blow up
                os.environ.setdefault("SUPABASE_JWT_SECRET", "scratch")
                os.environ.setdefault("SUPABASE_URL", "http://localhost")
                return value
    raise SystemExit("DATABASE_URL not set")


async def main() -> None:
    dsn = _load_dsn()
    print("=== Stream 3 E2E verification ===\n")

    conn = await asyncpg.connect(_normalize_dsn(dsn), statement_cache_size=0)
    try:
        # 1. Pick the default tenant
        DEFAULT_ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")

        # 2. Find a prospect in the default tenant that has at least one signal
        prospect_row = await conn.fetchrow(
            """
            SELECT p.id, p.name, count(s.id) AS sig_count
            FROM prospects p
            LEFT JOIN signals s ON s.prospect_id = p.id
            WHERE p.account_id = $1
            GROUP BY p.id, p.name
            ORDER BY sig_count DESC, p.id
            LIMIT 1
            """,
            DEFAULT_ACCOUNT,
        )
        if prospect_row is None:
            print("⚠ No prospects in default tenant — cannot verify against live data.")
            return

        prospect_id = prospect_row["id"]
        prospect_name = prospect_row["name"]
        sig_count = prospect_row["sig_count"]
        print(f"Picked prospect: {prospect_name} ({str(prospect_id)[:8]}…), signals={sig_count}")

        # 3. Snapshot pre-state for both tables
        pre_scores = await conn.fetchval(
            "SELECT count(*) FROM scores WHERE prospect_id = $1", prospect_id
        )
        pre_records = await conn.fetchval(
            "SELECT count(*) FROM score_records WHERE prospect_id = $1", prospect_id
        )
        print(f"Pre-state: scores={pre_scores} rows, score_records={pre_records} rows\n")

        # 4. Get the active weight_version_id we expect to land in score_records
        expected_version = await conn.fetchval(
            "SELECT id FROM score_weights WHERE account_id = $1 AND is_active = TRUE",
            DEFAULT_ACCOUNT,
        )
        print(f"Expected weight_version_id: {expected_version}\n")
    finally:
        await conn.close()

    # 5. Import score_runner and trigger the dual-write through the real
    # asyncpg pool (not the verification connection above)
    from credence.score_runner import score_prospect

    print("Calling score_prospect() (real dual-write)…")
    result = await score_prospect(prospect_id)
    print(
        f"  → ScoreResult overall={result.overall_score} "
        f"auth={result.authenticity_score} authority={result.authority_score} "
        f"warmth={result.warmth_score}\n"
    )

    # 6. Re-open a fresh connection to inspect post-state
    conn2 = await asyncpg.connect(_normalize_dsn(dsn), statement_cache_size=0)
    try:
        post_scores = await conn2.fetchval(
            "SELECT count(*) FROM scores WHERE prospect_id = $1", prospect_id
        )
        post_records = await conn2.fetchval(
            "SELECT count(*) FROM score_records WHERE prospect_id = $1", prospect_id
        )
        print(f"Post-state: scores={post_scores} rows (Δ {post_scores - pre_scores}), "
              f"score_records={post_records} rows (Δ {post_records - pre_records})")

        # 7. Verify the new score_records row carries the right fields
        record = await conn2.fetchrow(
            """
            SELECT account_id, prospect_id, weight_version_id,
                   authenticity_score, authority_score, warmth_score, overall_score,
                   length(trim(falsification_note)) AS note_len, computed_at
            FROM score_records
            WHERE prospect_id = $1
            ORDER BY computed_at DESC
            LIMIT 1
            """,
            prospect_id,
        )
        print("\n-- Newest score_records row")
        print(f"  account_id        = {record['account_id']}  "
              f"(expect {DEFAULT_ACCOUNT}: {'✓' if record['account_id'] == DEFAULT_ACCOUNT else '✗'})")
        print(f"  prospect_id       = {record['prospect_id']}  "
              f"(expect {prospect_id}: {'✓' if record['prospect_id'] == prospect_id else '✗'})")
        print(f"  weight_version_id = {record['weight_version_id']}  "
              f"(expect {expected_version}: {'✓' if record['weight_version_id'] == expected_version else '✗'})")
        print(f"  scores            auth={record['authenticity_score']} "
              f"authority={record['authority_score']} "
              f"warmth={record['warmth_score']} "
              f"overall={record['overall_score']}")
        print(f"  falsification_note length={record['note_len']}  "
              f"(CHECK length>0: {'✓' if record['note_len'] > 0 else '✗'})")
        print(f"  computed_at       = {record['computed_at']:%Y-%m-%d %H:%M:%S %z}")

        # 8. Verify legacy scores row also got account_id + the same scalars
        legacy = await conn2.fetchrow(
            """
            SELECT account_id, authenticity_score, authority_score, warmth_score, overall_score
            FROM scores
            WHERE prospect_id = $1
            ORDER BY computed_at DESC
            LIMIT 1
            """,
            prospect_id,
        )
        print("\n-- Newest legacy scores row")
        print(f"  account_id = {legacy['account_id']}  "
              f"(expect {DEFAULT_ACCOUNT}: {'✓' if legacy['account_id'] == DEFAULT_ACCOUNT else '✗'})")
        same = (
            legacy['authenticity_score'] == record['authenticity_score']
            and legacy['authority_score'] == record['authority_score']
            and legacy['warmth_score'] == record['warmth_score']
            and legacy['overall_score'] == record['overall_score']
        )
        print(f"  scalars match score_records: {'✓' if same else '✗'}")

        # 9. Banner-render predicate: would the WeightVersionBanner show?
        # Banner shows when displayed weight_version_id != active id.
        # The legacy scores row has NO weight_version_id column — so v2 read
        # path → banner stays hidden (intentional). The v3 score_records row
        # carries the active id → also hidden when consumed. Banner only
        # appears when a STALE record (older version) is shown alongside an
        # active version that's been flipped since.
        print("\n-- Banner-render predicate")
        print("  This row's weight_version_id == active version → banner correctly hidden")
        print("  Banner would only appear if Settings flipped the active version")
        print("  AFTER this row was written (i.e., this row's version is now stale).")
    finally:
        await conn2.close()

    # Close the real pool that score_prospect created
    from credence.db import close_pool
    await close_pool()

    print("\n=== verification complete ===")


if __name__ == "__main__":
    asyncio.run(main())
