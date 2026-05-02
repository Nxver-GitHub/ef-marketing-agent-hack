"""Apify orphan-recovery watch daemon (Wave 10 — SR delegation msg 225).

Polls Apify ``/v2/actor-runs`` every N seconds, finds SUCCEEDED runs of the
LinkedIn profile-scraper actor that the bulk caller abandoned, imports
:func:`scripts.apify_recover_datasets.recover_run` to do the actual fetch +
persist work. Idempotent via an on-disk run-id set so re-runs don't refetch
already-recovered datasets.

## Why a daemon instead of a one-shot

SR's one-shot ``apify_recover_datasets.py`` recovers explicitly-named run
ids. That solved the original 8-chunk timeout but doesn't catch the next
orphan that appears 90 minutes from now — the new caller has a 4hr
timeout but slow-proxy edge cases will still happen. This daemon
auto-detects new orphans without operator intervention.

## Stop conditions

The daemon exits cleanly on the first of:
  - ``--max-hours`` total wall time (default 24)
  - ``--max-runs`` total runs successfully recovered (default 30)
  - 3 consecutive polling cycles with **no new orphans**

The 3-cycle rule lets the daemon ship its work and exit instead of
hammering Apify's API forever. Operators can re-launch if a new wave
of orphans is expected.

## Defensive defaults

- Skip runs younger than 5 min (``--min-age-seconds``) — caller may still
  be processing. Avoid double-persist races.
- Skip runs whose ``stats.outputBodyLen`` (or fallback chargedEventCount)
  is zero — empty datasets don't help anyone.
- Skip runs already in the state file. Newly-discovered runs are added
  before the recovery call so a crash mid-recover-run leaves a marker
  (the underlying ``write_canonical_persons`` is idempotent on its own).
- ``APIFY_TOKEN`` missing → exit 2 with a clear error.

## CLI

::

    APIFY_TOKEN=... DATABASE_URL=... uv run python -m scripts.apify_orphan_watcher \\
        --account-id <uuid> --interval 60 --max-hours 6 --max-runs 30
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
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

# Ensure server/ is on path so the script can import recursively from
# `credence.*` and `scripts.*` regardless of cwd. Mirrors the path-shim
# pattern in apify_recover_datasets.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from credence.enrichment.apify import PROFILE_ACTOR_ID  # noqa: E402

# SR's recovery script — read-only import, do not modify per msg 225.
from scripts.apify_recover_datasets import (  # noqa: E402
    fetch_unenriched_prospects_with_linkedin,
    recover_run,
)
from credence.db import close_pool  # noqa: E402

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


APIFY_RUNS_URL = "https://api.apify.com/v2/actor-runs"

# State file lives in HOME so it survives across daemon restarts. The
# format is a JSON object ``{"recovered": ["<run_id>", ...]}``.
DEFAULT_STATE_FILE = Path.home() / ".apify_recovered_runs.json"

DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_MIN_AGE_SECONDS = 300  # 5 minutes
DEFAULT_MAX_HOURS = 24
DEFAULT_MAX_RUNS = 30
DEFAULT_LIST_LIMIT = 50

# Number of consecutive empty polling cycles after which we exit cleanly.
# Empty = "we polled and found zero new candidates to recover."
#
# Default 60 (= 60 min at the default 60s interval). The previous default
# of 3 was too aggressive: SR Wave 11 reported the watcher exited at
# iter=3 before any of 5 unwatched chunks SUCCEEDED, then ONrLD/Lrr5a
# SUCCEEDED ~5 min later and were missed. 60 cycles tolerates the actual
# 1-3hr Apify chunk runtimes while still bounding the daemon's lifetime
# under the --max-hours cap.
EMPTY_CYCLE_EXIT_THRESHOLD = 60


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ApifyRun:
    """Subset of /v2/actor-runs payload that the watcher needs."""

    run_id: str
    act_id: str
    status: str
    finished_at_unix: float | None
    charged_full_profile: int


@dataclass(slots=True)
class WatcherState:
    """In-memory + on-disk state for one daemon process."""

    recovered_run_ids: set[str] = field(default_factory=set)
    started_at_unix: float = field(default_factory=time.time)
    runs_recovered_this_session: int = 0
    consecutive_empty_cycles: int = 0


# ── State file IO ────────────────────────────────────────────────────────────


def _load_state(path: Path) -> set[str]:
    """Read the recovered-run-ids set; return empty set if the file is absent."""
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("orphan_watcher: state file %s unreadable; starting fresh", path)
        return set()
    if not isinstance(raw, dict):
        return set()
    recovered = raw.get("recovered")
    if not isinstance(recovered, list):
        return set()
    return {str(r) for r in recovered if isinstance(r, str)}


def _save_state(path: Path, recovered: set[str]) -> None:
    """Atomic write — write to ``<path>.tmp`` then rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {"recovered": sorted(recovered)}
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


# ── Apify run-list parsing ───────────────────────────────────────────────────


def _parse_run_payload(item: dict[str, Any]) -> ApifyRun | None:
    """Pull the few fields we care about out of one /v2/actor-runs item.

    Returns None on any malformed shape — the caller skips silently.
    """
    if not isinstance(item, dict):
        return None
    run_id = item.get("id")
    act_id = item.get("actId")
    status = item.get("status")
    if not (
        isinstance(run_id, str) and isinstance(act_id, str) and isinstance(status, str)
    ):
        return None

    finished_at_unix: float | None = None
    finished_at_raw = item.get("finishedAt")
    if isinstance(finished_at_raw, (int, float)):
        finished_at_unix = float(finished_at_raw) / 1000.0  # ms → s
    elif isinstance(finished_at_raw, str) and finished_at_raw:
        # Apify returns ISO-8601 like "2026-05-01T22:30:00.000Z"; parse via
        # fromisoformat after normalizing the trailing Z.
        try:
            from datetime import datetime
            iso = finished_at_raw.replace("Z", "+00:00")
            finished_at_unix = datetime.fromisoformat(iso).timestamp()
        except (ValueError, ImportError):
            finished_at_unix = None

    # Actor-agnostic charged-items count. The previous implementation looked
    # for ``full-profile`` / ``fullProfile`` keys but the live SUCCEEDED runs
    # use ``profile`` (msg 232) — the per-actor key drifts across the
    # harvestapi suite. Sum any positive numeric values instead so we don't
    # have to track the key vocabulary.
    charged = 0
    charged_counts = item.get("chargedEventCounts")
    if isinstance(charged_counts, dict):
        for v in charged_counts.values():
            if isinstance(v, bool):  # bool is a subclass of int
                continue
            if isinstance(v, (int, float)) and v > 0:
                charged += int(v)

    return ApifyRun(
        run_id=run_id,
        act_id=act_id,
        status=status,
        finished_at_unix=finished_at_unix,
        charged_full_profile=charged,
    )


# ── Filter logic ─────────────────────────────────────────────────────────────


def _should_recover(
    run: ApifyRun,
    *,
    already_recovered: set[str],
    now_unix: float,
    min_age_seconds: int,
    profile_actor_id: str,
) -> tuple[bool, str]:
    """Pure: should this run be enqueued for recovery?

    Returns ``(decision, reason)`` so the caller can log skipped runs.
    """
    if run.status != "SUCCEEDED":
        return False, f"status={run.status}"
    if run.act_id != profile_actor_id:
        return False, f"act_id={run.act_id}"
    if run.run_id in already_recovered:
        return False, "already_recovered"
    if run.charged_full_profile <= 0:
        return False, "empty_dataset"
    if run.finished_at_unix is not None:
        age = now_unix - run.finished_at_unix
        if age < min_age_seconds:
            return False, f"too_recent({age:.0f}s)"
    # finished_at_unix == None: caller status SUCCEEDED w/ no timestamp
    # is rare; recover anyway. Not worth the false-negative.
    return True, "recover"


# ── Apify HTTP layer ─────────────────────────────────────────────────────────


async def _fetch_recent_runs(
    client: httpx.AsyncClient,
    *,
    token: str,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[ApifyRun]:
    """GET /v2/actor-runs?desc=true&limit=N — return parsed runs.

    Network/parse failures collapse to ``[]`` so the daemon keeps polling
    instead of crashing on a transient API blip.
    """
    try:
        r = await client.get(
            APIFY_RUNS_URL,
            params={"token": token, "desc": "true", "limit": str(limit)},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        log.warning("orphan_watcher: list-runs HTTP error: %r", exc)
        return []
    if r.status_code != 200:
        log.warning("orphan_watcher: list-runs returned %s", r.status_code)
        return []
    try:
        body = r.json()
    except ValueError:
        return []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []
    runs: list[ApifyRun] = []
    for item in items:
        parsed = _parse_run_payload(item)
        if parsed is not None:
            runs.append(parsed)
    return runs


# ── Main poll loop ───────────────────────────────────────────────────────────


def _should_stop(
    state: WatcherState,
    *,
    max_hours: float,
    max_runs: int,
    max_empty_cycles: int,
    now_unix: float,
) -> tuple[bool, str]:
    """Pure stop predicate; returns ``(stop, reason)``."""
    elapsed_hours = (now_unix - state.started_at_unix) / 3600.0
    if elapsed_hours >= max_hours:
        return True, f"max_hours({elapsed_hours:.2f}>={max_hours})"
    if state.runs_recovered_this_session >= max_runs:
        return True, f"max_runs({state.runs_recovered_this_session}>={max_runs})"
    if state.consecutive_empty_cycles >= max_empty_cycles:
        return True, f"empty_cycles({state.consecutive_empty_cycles}>={max_empty_cycles})"
    return False, ""


async def _poll_once(
    state: WatcherState,
    *,
    client: httpx.AsyncClient,
    token: str,
    account_id: UUID,
    url_to_prospect: dict[str, Any],
    state_path: Path,
    profile_actor_id: str,
    min_age_seconds: int,
    list_limit: int,
    max_runs: int,
) -> int:
    """One polling cycle; returns count of runs recovered this cycle."""
    runs = await _fetch_recent_runs(client, token=token, limit=list_limit)
    now_unix = time.time()
    candidates: list[ApifyRun] = []
    for run in runs:
        decision, reason = _should_recover(
            run,
            already_recovered=state.recovered_run_ids,
            now_unix=now_unix,
            min_age_seconds=min_age_seconds,
            profile_actor_id=profile_actor_id,
        )
        if decision:
            candidates.append(run)
        else:
            log.debug("orphan_watcher: skip %s — %s", run.run_id, reason)

    if not candidates:
        log.info("orphan_watcher: cycle empty — 0 new orphans")
        return 0

    log.info(
        "orphan_watcher: cycle has %d candidate(s) — recovering …",
        len(candidates),
    )
    recovered_this_cycle = 0
    for run in candidates:
        if state.runs_recovered_this_session >= max_runs:
            log.info(
                "orphan_watcher: max_runs (%d) reached — deferring rest of cycle",
                max_runs,
            )
            break
        # Mark as recovered BEFORE the call so a mid-call crash doesn't
        # cause a re-fetch on the next start. The underlying writer is
        # idempotent (markers + write_canonical_persons), so a re-run is
        # safe but wasteful — this is the correct ordering.
        state.recovered_run_ids.add(run.run_id)
        _save_state(state_path, state.recovered_run_ids)
        try:
            counts = await recover_run(run.run_id, account_id, url_to_prospect)
        except Exception as exc:  # noqa: BLE001 — keep daemon alive
            log.exception("orphan_watcher: recover_run(%s) crashed: %r",
                          run.run_id, exc)
            continue
        log.info(
            "orphan_watcher: recovered %s — %s",
            run.run_id,
            json.dumps(counts, separators=(",", ":")),
        )
        recovered_this_cycle += 1
        state.runs_recovered_this_session += 1
    return recovered_this_cycle


async def watch_loop(
    account_id: UUID,
    *,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
    max_hours: float = DEFAULT_MAX_HOURS,
    max_runs: int = DEFAULT_MAX_RUNS,
    max_empty_cycles: int = EMPTY_CYCLE_EXIT_THRESHOLD,
    list_limit: int = DEFAULT_LIST_LIMIT,
    state_path: Path = DEFAULT_STATE_FILE,
    profile_actor_id: str = PROFILE_ACTOR_ID,
    token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> WatcherState:
    """Top-level daemon loop. Returns final ``WatcherState`` on clean exit."""
    api_key = token if token is not None else os.environ.get("APIFY_TOKEN")
    if not api_key:
        raise RuntimeError(
            "APIFY_TOKEN environment variable is required for the orphan watcher"
        )

    state = WatcherState(recovered_run_ids=_load_state(state_path))
    log.info(
        "orphan_watcher: start account=%s interval=%ds min_age=%ds "
        "max_hours=%.1f max_runs=%d list_limit=%d known_recovered=%d",
        account_id, interval_seconds, min_age_seconds, max_hours,
        max_runs, list_limit, len(state.recovered_run_ids),
    )

    log.info("orphan_watcher: loading url→prospect index for account=%s", account_id)
    url_to_prospect = await fetch_unenriched_prospects_with_linkedin(account_id)
    log.info("orphan_watcher: indexed %d unenriched prospects", len(url_to_prospect))

    own_client = client is None
    http = client if client is not None else httpx.AsyncClient()
    try:
        iteration = 0
        while True:
            iteration += 1
            recovered_this_cycle = await _poll_once(
                state,
                client=http,
                token=api_key,
                account_id=account_id,
                url_to_prospect=url_to_prospect,
                state_path=state_path,
                profile_actor_id=profile_actor_id,
                min_age_seconds=min_age_seconds,
                list_limit=list_limit,
                max_runs=max_runs,
            )
            if recovered_this_cycle == 0:
                state.consecutive_empty_cycles += 1
            else:
                state.consecutive_empty_cycles = 0

            stop, reason = _should_stop(
                state,
                max_hours=max_hours,
                max_runs=max_runs,
                max_empty_cycles=max_empty_cycles,
                now_unix=time.time(),
            )
            if stop:
                log.info("orphan_watcher: stop — %s", reason)
                break
            log.info(
                "orphan_watcher: iter=%d recovered=%d empty_cycles=%d "
                "session_recovered=%d sleep=%ds",
                iteration, recovered_this_cycle, state.consecutive_empty_cycles,
                state.runs_recovered_this_session, interval_seconds,
            )
            await asyncio.sleep(interval_seconds)
    finally:
        if own_client:
            await http.aclose()

    return state


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts.apify_orphan_watcher",
        description=(
            "Watch daemon: poll Apify /v2/actor-runs and auto-recover any "
            "SUCCEEDED LinkedIn-profile-scraper runs the bulk caller abandoned."
        ),
    )
    p.add_argument(
        "--account-id",
        type=UUID,
        default=UUID(os.environ.get("ACCOUNT_ID", "00000000-0000-0000-0000-000000000001")),
        help="Tenant scope (default: the standard demo account).",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"Polling interval in seconds (default {DEFAULT_INTERVAL_SECONDS}).",
    )
    p.add_argument(
        "--min-age-seconds",
        type=int,
        default=DEFAULT_MIN_AGE_SECONDS,
        help=(
            f"Skip runs whose finishedAt is younger than this many seconds "
            f"(default {DEFAULT_MIN_AGE_SECONDS} = 5 min)."
        ),
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
        help=f"Total runs cap (default {DEFAULT_MAX_RUNS}).",
    )
    p.add_argument(
        "--max-empty-cycles",
        type=int,
        default=EMPTY_CYCLE_EXIT_THRESHOLD,
        help=(
            f"Exit after N consecutive empty polling cycles "
            f"(default {EMPTY_CYCLE_EXIT_THRESHOLD}). Pass a large value "
            f"(e.g. --max-empty-cycles 999) to keep the watcher alive while "
            f"long Apify chunks (1-3hr) finish in the background."
        ),
    )
    p.add_argument(
        "--list-limit",
        type=int,
        default=DEFAULT_LIST_LIMIT,
        help=f"How many recent runs to fetch per poll (default {DEFAULT_LIST_LIMIT}).",
    )
    p.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=f"Recovered-run-ids JSON file (default {DEFAULT_STATE_FILE}).",
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

    async def _go() -> WatcherState:
        try:
            return await watch_loop(
                args.account_id,
                interval_seconds=args.interval,
                min_age_seconds=args.min_age_seconds,
                max_hours=args.max_hours,
                max_runs=args.max_runs,
                max_empty_cycles=args.max_empty_cycles,
                list_limit=args.list_limit,
                state_path=args.state_file,
            )
        finally:
            await close_pool()

    try:
        final = asyncio.run(_go())
    except RuntimeError as exc:
        log.error("orphan_watcher: %s", exc)
        return 2

    print(
        f"orphan_watcher exit — recovered_this_session={final.runs_recovered_this_session} "
        f"known_recovered_total={len(final.recovered_run_ids)} "
        f"consecutive_empty_cycles={final.consecutive_empty_cycles}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
