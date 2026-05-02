"""Background clustering jobs that materialize person↔person edges.

Each module in this package reads from one or more extractor outputs and
writes to ``person_connections`` + ``connection_evidence``. Jobs are
idempotent on retry: re-running over the same input produces the same row
content.

Per CLAUDE.md Decision 7, warm paths are pre-materialized in
``person_connections``; the BFS layer queries this table at read time
rather than computing strengths on the fly.
"""
