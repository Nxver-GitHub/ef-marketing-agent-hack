#!/usr/bin/env bash
# tests/orgchart/run.sh — convenience wrapper around pytest -m
#
# Usage:
#   ./run.sh                # default: unit only
#   ./run.sh unit
#   ./run.sh integration
#   ./run.sh dq             # data_quality
#   ./run.sh perf           # performance
#   ./run.sh snap           # snapshot
#   ./run.sh ci             # unit + data_quality (the safe-against-prod set)
#   ./run.sh full           # everything

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

mode="${1:-unit}"

case "$mode" in
  unit)        marker="unit" ;;
  integration) marker="integration" ;;
  dq|data)     marker="data_quality" ;;
  perf)        marker="performance" ;;
  snap)        marker="snapshot" ;;
  ci)          marker="unit or data_quality" ;;
  full)        marker="unit or integration or data_quality or performance or snapshot" ;;
  *)
    echo "Unknown mode: $mode"
    echo "Modes: unit | integration | dq | perf | snap | ci | full"
    exit 1
    ;;
esac

# Re-use the parent project's uv environment so `credence` imports work.
# Fall back to plain pytest if uv isn't available (e.g., CI image).
if command -v uv >/dev/null 2>&1; then
  exec uv run --project ../../server pytest -m "$marker" -v
else
  exec pytest -m "$marker" -v
fi
