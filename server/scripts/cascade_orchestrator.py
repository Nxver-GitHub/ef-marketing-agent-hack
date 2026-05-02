"""Cascade orchestrator (Wave 11.2 — SR delegation msg 228).

When Apify lands new ``persons`` rows the downstream pipelines (signals
extraction, clustering, materialization) don't auto-fire. This daemon
closes the loop:

1. Poll ``persons WHERE created_at > last_marker`` every ``--interval`` min.
2. When the delta crosses ``--threshold`` new persons, run the cascade:
   a. ``bulk_career_overlap_signals --account-id <X>``
   b. ``bulk_education_signals --account-id <X>``
   c. ``career_overlap_clustering --all --allow-missing-years --db-concurrency <X>``
3. Each step is a subprocess. We wait for one to exit before starting the
   next so the Supabase pool never sees concurrent heavy writers from this
   daemon (the pool is already shared with Tier-1 + recovery2).
4. Log each step's stdout to the daemon log so the rollup numbers are
   captured for the orchestration thread.
5. After all three steps complete, advance ``last_marker`` to ``NOW()`` so
   the next poll only sees genuinely-new persons.

The ``materialize_prospect_warm_paths`` watch daemon (Wave 9, PID
``bftl0lgq4``) auto-refreshes the read-cache after person_connections
grows by ``--threshold`` rows, so we don't run it from here.

## Stop conditions

- ``--max-hours`` total wall time (default 24)
- ``--max-runs`` cascade invocations (default 30)
- 60 consecutive polls below threshold (lets the daemon exit instead of
  polling forever; operator can re-launch)

## State file

``~/.cascade_orchestrator.json``:

```json
{
  "last_marker_ts": "2026-05-01T22:00:00+00:00",
  "cascades_run": 12,
  "last_run_persons_count": 18432
}
```

Atomic write via ``<path>.tmp`` + rename. Survives daemon restarts.

## CLI

::

    DATABASE_URL=... uv run python -m scripts.cascade_orchestrator \\
        --account-id <uuid> --interval 5 --threshold 100 --max-hours 6
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import UUID

# Ensure server/ is on path for the credence imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from credence.db import acquire, close_pool  # noqa: E402

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_THRESHOLD = 100
DEFAULT_MAX_HOURS = 24
DEFAULT_MAX_RUNS = 30
DEFAULT_DB_CONCURRENCY = 4  # for the clustering step

# Number of below-threshold polls in a row before clean exit. With the
# default 5-min interval that's 5hr of quiet; combined with the
# --max-hours cap this gives the daemon a clean exit envelope.
EMPTY_POLL_EXIT_THRESHOLD = 60

DEFAULT_STATE_FILE = Path.home() / ".cascade_orchestrator.json"


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class OrchestratorState:
    """Runtime state for one daemon process."""

    last_marker_ts: datetime
    cascades_run_total: int = 0
    cascades_run_session: int = 0
    consecutive_empty_polls: int = 0
    started_at_unix: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class CascadeStep:
    """One subprocess step in the cascade."""

    name: str
    argv: list[str]


@dataclass(slots=True)
class CascadeResult:
    """Outcome of one cascade run."""

    steps: list[tuple[str, int]] = field(default_factory=list)
    aborted_at: str | None = None


# ── Cascade step planning ────────────────────────────────────────────────────


def _build_cascade_steps(
    account_id: UUID,
    *,
    db_concurrency: int,
    allow_missing_years: bool = True,
) -> list[CascadeStep]:
    """Pure: which subprocess steps run, in order, for one cascade.

    Sequential by design — the Supabase pool is shared with the Tier-1
    enrichment + recovery2 + paper_clustering re-runs, so we never want
    two cascade steps writing concurrently from the same daemon.
    """
    py = sys.executable
    common = [py, "-m"]
    return [
        CascadeStep(
            name="bulk_career_overlap_signals",
            argv=[*common, "credence.jobs.bulk_career_overlap_signals",
                  "--account-id", str(account_id)],
        ),
        CascadeStep(
            name="bulk_education_signals",
            argv=[*common, "credence.jobs.bulk_education_signals",
                  "--account-id", str(account_id)],
        ),
        CascadeStep(
            name="career_overlap_clustering",
            argv=[*common, "credence.jobs.career_overlap_clustering",
                  "--all",
                  *(["--allow-missing-years"] if allow_missing_years else []),
                  "--db-concurrency", str(db_concurrency)],
        ),
    ]


# ── State file IO ────────────────────────────────────────────────────────────


def _load_state(path: Path) -> OrchestratorState:
    """Read state, or return a fresh state pointing at epoch."""
    fresh = OrchestratorState(
        last_marker_ts=datetime(1970, 1, 1, tzinfo=timezone.utc)
    )
    if not path.exists():
        return fresh
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("cascade: state file %s unreadable; starting fresh", path)
        return fresh
    if not isinstance(raw, dict):
        return fresh
    marker_str = raw.get("last_marker_ts")
    cascades_run = raw.get("cascades_run") or 0
    if not isinstance(marker_str, str):
        return fresh
    try:
        marker = datetime.fromisoformat(marker_str.replace("Z", "+00:00"))
    except ValueError:
        return fresh
    if marker.tzinfo is None:
        marker = marker.replace(tzinfo=timezone.utc)
    return OrchestratorState(
        last_marker_ts=marker,
        cascades_run_total=int(cascades_run) if isinstance(cascades_run, int) else 0,
    )


def _save_state(
    path: Path,
    state: OrchestratorState,
    *,
    last_run_persons_count: int | None = None,
) -> None:
    """Atomic write — temp file + rename."""
    payload: dict[str, Any] = {
        "last_marker_ts": state.last_marker_ts.isoformat(),
        "cascades_run": state.cascades_run_total,
    }
    if last_run_persons_count is not None:
        payload["last_run_persons_count"] = last_run_persons_count
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


# ── DB poll ──────────────────────────────────────────────────────────────────


SELECT_NEW_PERSONS_COUNT_SQL = """
SELECT count(*) AS new_persons
FROM persons
WHERE account_id = $1
  AND created_at > $2
"""


async def _count_new_persons(account_id: UUID, marker: datetime) -> int:
    """How many ``persons`` rows have appeared since ``marker``?"""
    async with acquire() as conn:
        row = await conn.fetchval(
            SELECT_NEW_PERSONS_COUNT_SQL, account_id, marker
        )
    return int(row or 0)


# ── Cascade execution ────────────────────────────────────────────────────────


# Indirection so tests can monkeypatch the subprocess invocation without
# touching asyncio internals.
async def _run_subprocess(
    step: CascadeStep,
    *,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> int:
    """Invoke one cascade step as a subprocess; return the exit code.

    Stdout + stderr are streamed to the parent process via inheritance so
    each step's rollup line lands in the daemon's log without us having
    to buffer + re-emit it.
    """
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    log.info("cascade step start — %s", step.name)
    proc = await asyncio.create_subprocess_exec(
        *step.argv,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=None,  # inherit
        stderr=None,
    )
    rc = await proc.wait()
    log.info("cascade step done  — %s (exit=%d)", step.name, rc)
    return rc


async def _run_one_cascade(
    steps: list[CascadeStep],
    *,
    runner: Callable[[CascadeStep], Awaitable[int]] | None = None,
) -> CascadeResult:
    """Run all steps sequentially; abort on first non-zero exit."""
    result = CascadeResult()
    run = runner if runner is not None else _run_subprocess
    for step in steps:
        rc = await run(step)
        result.steps.append((step.name, rc))
        if rc != 0:
            result.aborted_at = step.name
            log.warning(
                "cascade aborted at step=%s (exit=%d) — remaining steps skipped",
                step.name, rc,
            )
            return result
    return result


# ── Stop predicate ───────────────────────────────────────────────────────────


def _should_stop(
    state: OrchestratorState,
    *,
    max_hours: float,
    max_runs: int,
    max_empty_polls: int,
    now_unix: float,
) -> tuple[bool, str]:
    """Pure stop check; returns ``(stop, reason)``."""
    elapsed_hours = (now_unix - state.started_at_unix) / 3600.0
    if elapsed_hours >= max_hours:
        return True, f"max_hours({elapsed_hours:.2f}>={max_hours})"
    if state.cascades_run_session >= max_runs:
        return True, f"max_runs({state.cascades_run_session}>={max_runs})"
    if state.consecutive_empty_polls >= max_empty_polls:
        return True, f"empty_polls({state.consecutive_empty_polls}>={max_empty_polls})"
    return False, ""


# ── Main loop ────────────────────────────────────────────────────────────────


async def watch_loop(
    account_id: UUID,
    *,
    interval_seconds: int = DEFAULT_INTERVAL_MINUTES * 60,
    threshold: int = DEFAULT_THRESHOLD,
    max_hours: float = DEFAULT_MAX_HOURS,
    max_runs: int = DEFAULT_MAX_RUNS,
    max_empty_polls: int = EMPTY_POLL_EXIT_THRESHOLD,
    db_concurrency: int = DEFAULT_DB_CONCURRENCY,
    state_path: Path = DEFAULT_STATE_FILE,
    count_persons: Callable[[UUID, datetime], Awaitable[int]] = _count_new_persons,
    runner: Callable[[CascadeStep], Awaitable[int]] | None = None,
) -> OrchestratorState:
    """Top-level cascade-orchestrator daemon loop."""
    state = _load_state(state_path)
    state.started_at_unix = time.time()

    log.info(
        "cascade start account=%s interval=%ds threshold=%d max_hours=%.1f "
        "max_runs=%d marker=%s",
        account_id, interval_seconds, threshold, max_hours, max_runs,
        state.last_marker_ts.isoformat(),
    )

    iteration = 0
    while True:
        iteration += 1
        new_persons = await count_persons(account_id, state.last_marker_ts)
        log.info(
            "cascade iter=%d new_persons=%d threshold=%d cascades_session=%d",
            iteration, new_persons, threshold, state.cascades_run_session,
        )

        if new_persons >= threshold:
            steps = _build_cascade_steps(
                account_id, db_concurrency=db_concurrency, allow_missing_years=True
            )
            cascade_started_at = datetime.now(timezone.utc)
            result = await _run_one_cascade(steps, runner=runner)
            state.cascades_run_session += 1
            state.cascades_run_total += 1
            state.consecutive_empty_polls = 0
            # Advance marker only if all steps succeeded — partial cascades
            # leave the marker where it was so the next poll re-fires.
            if result.aborted_at is None:
                state.last_marker_ts = cascade_started_at
            _save_state(state_path, state, last_run_persons_count=new_persons)
            log.info(
                "cascade complete steps=%s aborted_at=%s",
                result.steps, result.aborted_at,
            )
        else:
            state.consecutive_empty_polls += 1
            _save_state(state_path, state, last_run_persons_count=new_persons)

        stop, reason = _should_stop(
            state,
            max_hours=max_hours,
            max_runs=max_runs,
            max_empty_polls=max_empty_polls,
            now_unix=time.time(),
        )
        if stop:
            log.info("cascade stop — %s", reason)
            break
        await asyncio.sleep(interval_seconds)

    return state


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts.cascade_orchestrator",
        description=(
            "Daemon that auto-cascades signals → clustering when Apify "
            "lands ≥ threshold new persons. Sequential subprocess steps "
            "to avoid Supabase pool collisions."
        ),
    )
    p.add_argument(
        "--account-id",
        type=UUID,
        default=UUID(os.environ.get("ACCOUNT_ID", "00000000-0000-0000-0000-000000000001")),
        help="Tenant scope (default: standard demo account).",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_MINUTES,
        help=f"Polling interval in MINUTES (default {DEFAULT_INTERVAL_MINUTES}).",
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"New-persons delta that triggers a cascade (default {DEFAULT_THRESHOLD}).",
    )
    p.add_argument(
        "--max-hours",
        type=float,
        default=DEFAULT_MAX_HOURS,
        help=f"Daemon wall-clock cap in hours (default {DEFAULT_MAX_HOURS}).",
    )
    p.add_argument(
        "--max-runs",
        type=int,
        default=DEFAULT_MAX_RUNS,
        help=f"Cascade invocation cap (default {DEFAULT_MAX_RUNS}).",
    )
    p.add_argument(
        "--max-empty-polls",
        type=int,
        default=EMPTY_POLL_EXIT_THRESHOLD,
        help=(
            f"Exit after N consecutive below-threshold polls "
            f"(default {EMPTY_POLL_EXIT_THRESHOLD})."
        ),
    )
    p.add_argument(
        "--db-concurrency",
        type=int,
        default=DEFAULT_DB_CONCURRENCY,
        help=(
            f"--db-concurrency value passed to career_overlap_clustering "
            f"(default {DEFAULT_DB_CONCURRENCY})."
        ),
    )
    p.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=f"State JSON file (default {DEFAULT_STATE_FILE}).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    async def _go() -> OrchestratorState:
        try:
            return await watch_loop(
                args.account_id,
                interval_seconds=args.interval * 60,  # CLI is minutes; loop is seconds
                threshold=args.threshold,
                max_hours=args.max_hours,
                max_runs=args.max_runs,
                max_empty_polls=args.max_empty_polls,
                db_concurrency=args.db_concurrency,
                state_path=args.state_file,
            )
        finally:
            await close_pool()

    final = asyncio.run(_go())
    print(
        f"cascade_orchestrator exit — cascades_session={final.cascades_run_session} "
        f"cascades_total={final.cascades_run_total} "
        f"empty_polls={final.consecutive_empty_polls} "
        f"marker={final.last_marker_ts.isoformat()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
