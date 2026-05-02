"""Shared Parallel.ai async-task client.

Parallel.ai is task-based, not request/response: the caller submits a task
(prose + output schema), gets a `run_id`, then polls until the task reaches a
terminal state. Tasks typically take 10-60 seconds.

This module exposes a single high-level entry point — `run_parallel_task` —
that handles submit, poll, deadline-aware cancellation, and defensive
response parsing. Per-extractor modules import this; they don't reimplement
the lifecycle.

## API shape — built against the documented v1 schema

```
POST /v1/tasks/runs
  body: { "task_spec": { "input_schema": {...}, "output_schema": {...},
                         "description": "..." },
          "input": {...} }
  response: { "run_id": "...", "status": "queued" }

GET /v1/tasks/runs/{run_id}
  response: { "run_id": "...",
              "status": "queued" | "running" | "succeeded" | "failed" | "cancelled",
              "output": {...},          # present when status == "succeeded"
              "error":  {"message": ...},  # present when status == "failed"
              "cost_cents": int }
```

All shape access is defensive — missing keys collapse to None / empty
collections. Live integration is gated by `PARALLEL_API_KEY` and runs only
under `pytest -m integration`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

PARALLEL_BASE_URL = "https://api.parallel.ai/v1/"
DEFAULT_HTTP_TIMEOUT_SECONDS = 8.0  # per individual HTTP call (not whole task)
DEFAULT_POLL_INTERVAL_SECONDS = 3.0  # gap between status polls
DEFAULT_TASK_TIMEOUT_SECONDS = 60.0  # walltime cap per submit-and-poll cycle

TerminalStatus = Literal["succeeded", "failed", "cancelled"]
RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class ParallelRunResult:
    """Outcome of a Parallel task run.

    `output` is None unless `status == "succeeded"`. `cost_cents` is reported
    even on failure (the vendor still charges for partial work in some cases).
    """

    run_id: str
    status: TerminalStatus
    output: dict[str, Any] | None
    error_message: str | None
    cost_cents: int


def _api_key() -> str | None:
    """Pull the Parallel.ai API key from env. Returns None when missing.

    The caller decides whether a missing key is fatal (extractor returns [])
    or whether a no-op run is acceptable (test mode with a MockTransport).
    """
    return os.environ.get("PARALLEL_API_KEY")


def _auth_headers() -> dict[str, str]:
    key = _api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


async def _submit_task(
    client: httpx.AsyncClient,
    *,
    description: str,
    input_payload: dict[str, Any],
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
) -> str | None:
    """POST a new task; return the run_id on success, None on failure."""
    body = {
        "task_spec": {
            "description": description,
            "input_schema": input_schema,
            "output_schema": output_schema,
        },
        "input": input_payload,
    }
    url = urljoin(PARALLEL_BASE_URL, "tasks/runs")
    try:
        r = await client.post(
            url,
            json=body,
            headers=_auth_headers(),
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("Parallel submit failed: %s", exc)
        return None
    if r.status_code not in (200, 201, 202):
        logger.warning("Parallel submit HTTP %d: %s", r.status_code, r.text[:200])
        return None
    try:
        body_json = r.json()
    except ValueError:
        logger.warning("Parallel submit returned non-JSON")
        return None
    run_id = body_json.get("run_id") if isinstance(body_json, dict) else None
    return run_id if isinstance(run_id, str) and run_id else None


async def _fetch_run(
    client: httpx.AsyncClient, run_id: str
) -> dict[str, Any] | None:
    """GET the current state of a run; return parsed dict or None."""
    url = urljoin(PARALLEL_BASE_URL, f"tasks/runs/{run_id}")
    try:
        r = await client.get(
            url,
            headers=_auth_headers(),
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("Parallel poll failed: %s", exc)
        return None
    if r.status_code != 200:
        logger.warning("Parallel poll HTTP %d: %s", r.status_code, r.text[:200])
        return None
    try:
        body = r.json()
    except ValueError:
        logger.warning("Parallel poll returned non-JSON")
        return None
    return body if isinstance(body, dict) else None


async def run_parallel_task(
    *,
    description: str,
    input_payload: dict[str, Any],
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    deadline_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> ParallelRunResult | None:
    """Submit a task, poll until terminal, return the result.

    Returns None when:
    - submission fails (network, auth, malformed body)
    - the deadline is hit before a terminal status (callers treat this as a
      timeout — Contract 1 partial-results semantics: log + skip the source)

    Returns ParallelRunResult with `status="failed"` on terminal-failure runs;
    callers can then decide whether to surface `vendors_failed` or absorb.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()

    started = time.monotonic()

    try:
        run_id = await _submit_task(
            client,
            description=description,
            input_payload=input_payload,
            input_schema=input_schema,
            output_schema=output_schema,
        )
        if run_id is None:
            return None

        # Poll until terminal or deadline
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= deadline_seconds:
                logger.warning(
                    "Parallel task %s exceeded deadline %.1fs (still %s)",
                    run_id,
                    deadline_seconds,
                    "non-terminal",
                )
                return None

            body = await _fetch_run(client, run_id)
            if body is None:
                # Transient poll failure — sleep and retry until deadline
                await asyncio.sleep(poll_interval_seconds)
                continue

            status_raw = body.get("status")
            if status_raw not in {
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
            }:
                logger.warning("Parallel run %s has unknown status %r", run_id, status_raw)
                # Treat as transient; retry
                await asyncio.sleep(poll_interval_seconds)
                continue

            if status_raw in {"succeeded", "failed", "cancelled"}:
                output = body.get("output")
                if not isinstance(output, dict):
                    output = None
                err = body.get("error")
                err_msg: str | None = None
                if isinstance(err, dict):
                    msg_raw = err.get("message")
                    if isinstance(msg_raw, str):
                        err_msg = msg_raw
                cost_raw = body.get("cost_cents", 0)
                cost_cents = int(cost_raw) if isinstance(cost_raw, (int, float)) else 0
                return ParallelRunResult(
                    run_id=run_id,
                    status=status_raw,  # type: ignore[arg-type]
                    output=output if status_raw == "succeeded" else None,
                    error_message=err_msg,
                    cost_cents=cost_cents,
                )

            # Still queued/running — sleep and continue
            await asyncio.sleep(poll_interval_seconds)
    finally:
        if own_client:
            await client.aclose()
