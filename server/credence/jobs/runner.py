"""Bulk clustering job runner.

Orchestrates all five ``person_connections`` population jobs in dependency
order. Run this after:
  1. ``python -m credence.backfill_v3 --all``           (fills persons / employment_periods)
  2. USPTO + Semantic Scholar extractors               (fills patents + patent_inventors)
  3. PDL education enrichment                          (fills education_periods)
  4. Firecrawl standards scraper                       (fills standards_memberships / standards_committees)
  5. Conference program scraper                        (fills conference_attendances + events)

Each job emits its own log line on completion. The runner exits 0 only when
every job completes without failures.

## Jobs (run order)

1. ``career_overlap``    — scans employment_periods.  Highest yield (~50-100k rows).
2. ``patent``            — scans patent_inventors.    Highest strength (0.95).
3. ``education_cohort``  — scans education_periods.   Needs PDL enrichment.
4. ``standards``         — scans standards_memberships. Needs roster scraper.
5. ``conference``        — scans conference_attendances. Needs program scraper.

Jobs 1-5 are independent and can run in parallel (``--parallel`` flag).
Sequential mode is the default; it uses less DB connection pool pressure and
produces cleaner logs for debugging.

## CLI

::

    # Dry-run all jobs (no writes, just pair counts)
    uv run python -m credence.jobs.runner --all --dry-run

    # Run all jobs sequentially (writes)
    uv run python -m credence.jobs.runner --all

    # Run only the highest-yield jobs
    uv run python -m credence.jobs.runner --jobs career_overlap patent

    # Allow missing-year fallback (useful before full backfill)
    uv run python -m credence.jobs.runner --all --allow-missing-years

    # Run in parallel (faster, noisier logs)
    uv run python -m credence.jobs.runner --all --parallel
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass

from ..db import close_pool
from .career_overlap_clustering import ClusterRollup, cluster_career_overlaps
from .conference_clustering import ConferenceRollup, cluster_conference_co_presenters
from .education_cohort_clustering import EducationRollup, cluster_education_cohorts
from .paper_clustering import PaperRollup, cluster_paper_co_authors
from .patent_clustering import PatentRollup, cluster_patent_co_inventors
from .standards_clustering import StandardsRollup, cluster_standards_peers

log = logging.getLogger(__name__)

ALL_JOB_NAMES = ("career_overlap", "patent", "paper", "education_cohort", "standards", "conference")


@dataclass
class RunnerResult:
    career_overlap: ClusterRollup | None = None
    patent: PatentRollup | None = None
    paper: PaperRollup | None = None
    education_cohort: EducationRollup | None = None
    standards: StandardsRollup | None = None
    conference: ConferenceRollup | None = None

    def total_inserted(self) -> int:
        total = 0
        for rollup in (
            self.career_overlap,
            self.patent,
            self.paper,
            self.education_cohort,
            self.standards,
            self.conference,
        ):
            if rollup is not None:
                total += getattr(rollup, "pairs_inserted", 0)
        return total

    def total_failures(self) -> int:
        total = 0
        for rollup in (
            self.career_overlap,
            self.patent,
            self.paper,
            self.education_cohort,
            self.standards,
            self.conference,
        ):
            if rollup is not None:
                total += len(getattr(rollup, "failures", []))
        return total

    def summary(self) -> str:
        lines = ["── clustering runner summary ──────────────────────"]
        pairs = {
            "career_overlap": self.career_overlap,
            "patent": self.patent,
            "paper": self.paper,
            "education_cohort": self.education_cohort,
            "standards": self.standards,
            "conference": self.conference,
        }
        for name, rollup in pairs.items():
            if rollup is None:
                lines.append(f"  {name:<20} skipped")
            else:
                found = getattr(rollup, "pairs_found", 0)
                ins = getattr(rollup, "pairs_inserted", 0)
                upd = getattr(rollup, "pairs_updated", 0)
                fail = len(getattr(rollup, "failures", []))
                lines.append(
                    f"  {name:<20} found={found:>6}  inserted={ins:>6}  "
                    f"updated={upd:>6}  failures={fail}"
                )
        lines.append(
            f"  {'TOTAL':<20} inserted={self.total_inserted():>6}  "
            f"failures={self.total_failures()}"
        )
        return "\n".join(lines)


async def run_all(
    jobs: tuple[str, ...],
    *,
    dry_run: bool,
    allow_missing_years: bool,
    parallel: bool,
) -> RunnerResult:
    result = RunnerResult()

    async def _career() -> None:
        result.career_overlap = await cluster_career_overlaps(
            dry_run=dry_run,
            allow_missing_years=allow_missing_years,
        )

    async def _patent() -> None:
        result.patent = await cluster_patent_co_inventors(dry_run=dry_run)

    async def _paper() -> None:
        result.paper = await cluster_paper_co_authors(dry_run=dry_run)

    async def _education() -> None:
        result.education_cohort = await cluster_education_cohorts(
            dry_run=dry_run,
            allow_missing_years=allow_missing_years,
        )

    async def _standards() -> None:
        result.standards = await cluster_standards_peers(
            dry_run=dry_run,
            allow_missing_years=allow_missing_years,
        )

    async def _conference() -> None:
        result.conference = await cluster_conference_co_presenters(dry_run=dry_run)

    _job_map = {
        "career_overlap": _career,
        "patent": _patent,
        "paper": _paper,
        "education_cohort": _education,
        "standards": _standards,
        "conference": _conference,
    }

    coros = [_job_map[j]() for j in jobs if j in _job_map]
    if not coros:
        log.warning("No valid jobs selected — nothing to do.")
        return result

    if parallel:
        await asyncio.gather(*coros, return_exceptions=False)
    else:
        for coro in coros:
            await coro

    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="credence.jobs.runner",
        description="Run person_connections bulk clustering jobs.",
    )
    p.add_argument(
        "--jobs",
        nargs="+",
        choices=list(ALL_JOB_NAMES),
        metavar="JOB",
        help=(
            f"Jobs to run. Choices: {', '.join(ALL_JOB_NAMES)}. "
            "Use --all to run all."
        ),
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Run every job (equivalent to --jobs " + " ".join(ALL_JOB_NAMES) + ").",
    )
    p.add_argument("--dry-run", action="store_true", help="Count pairs only, write nothing.")
    p.add_argument(
        "--allow-missing-years",
        action="store_true",
        help=(
            "Pass --allow-missing-years to jobs that support it (career_overlap, "
            "education_cohort, standards). Useful before a full backfill."
        ),
    )
    p.add_argument(
        "--parallel",
        action="store_true",
        help="Run all selected jobs in parallel (asyncio.gather). Default is sequential.",
    )
    p.add_argument("--log-level", default="INFO", help="Python logging level (default INFO).")
    args = p.parse_args(argv)

    if not args.all and not args.jobs:
        p.error("specify --all or at least one --jobs value")

    selected: tuple[str, ...] = ALL_JOB_NAMES if args.all else tuple(args.jobs)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    async def _go() -> RunnerResult:
        try:
            return await run_all(
                selected,
                dry_run=args.dry_run,
                allow_missing_years=args.allow_missing_years,
                parallel=args.parallel,
            )
        finally:
            await close_pool()

    result = asyncio.run(_go())
    print(result.summary())
    return 0 if result.total_failures() == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
