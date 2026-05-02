"""Audit which clustering-eligible companies produced zero clusters.

After the full-fleet Step A pipeline ran (157 of 169 eligible companies
clustered, 12 didn't), operators need to know *why* the 12 dropped out.
The likely cause is `_build_cluster_plan` skipping every person because
`domain_from_title` couldn't map their title — those companies have
employees in the DB but no canonical_domain set and no NLP-classifiable
titles. Catching those companies is the difference between "we don't
have data" and "the title taxonomy needs a new bucket."

## What this prints

For every company that:
  * has ≥ MIN_CLUSTER_SIZE current `persons` rows (and so was eligible
    for clustering), AND
  * has zero rows in `org_functional_clusters`

…we print the company name, current-person count, the top 5 most common
unclassified titles, and the proportion of titles that lack
canonical_domain. Operators use this to decide whether to:

  1. Add new title patterns to `taxonomy.domain_from_title`,
  2. Backfill `persons.canonical_domain` from a different signal, or
  3. Accept that the company genuinely has no usable signal and stop
     wasting cluster-pipeline cycles on it.

## Why this is read-only

The script touches the same tables as the live pipeline but writes
nothing. It's safe to run against production whenever curiosity strikes
(or after every full-fleet pipeline pass, as a regression check).

## Usage

    DATABASE_URL=... python -m scripts.orgchart_audit_zero_cluster_companies

The output is plain text suitable for piping into a report or grep.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import dataclass

# Allow running as either `python -m scripts.orgchart_audit_zero_cluster_companies`
# from `server/` or `python server/scripts/orgchart_audit_zero_cluster_companies.py`
# from the repo root — the latter needs an explicit sys.path nudge.
if __package__ in (None, ""):
    import os

    _SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _SERVER_DIR not in sys.path:
        sys.path.insert(0, _SERVER_DIR)

from credence.db import close_pool, fetch  # noqa: E402
from credence.orgchart.clustering import MIN_CLUSTER_SIZE  # noqa: E402
from credence.taxonomy import domain_from_title  # noqa: E402


# ── Audit shapes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CompanyAudit:
    """One row per zero-cluster company in the printed report."""

    company_id: str
    company_name: str
    current_person_count: int
    persons_with_canonical_domain: int
    persons_with_classifiable_title: int
    top_unclassified_titles: list[tuple[str, int]]


# ── Audit logic ──────────────────────────────────────────────────────────────


async def _zero_cluster_companies(min_size: int) -> list[dict]:
    """Companies the pipeline considered eligible but produced 0 clusters.

    Eligibility mirrors `clustering._eligible_company_ids` exactly: we count
    *current employment_periods rows*, not `persons.current_company_id` —
    the clustering pipeline only loads people via employment_periods so an
    orphaned `current_company_id` (no current period) doesn't get clustered.

    Earlier versions of this audit used `persons.current_company_id` which
    over-counted by ~50 companies because the persons table has 50+ rows
    where current_company_id is set but no `is_current=TRUE`
    employment_period exists for that pair (legacy backfill artifact).
    """
    rows = await fetch(
        """
        WITH eligible AS (
            SELECT company_id, COUNT(*) AS n
            FROM employment_periods
            WHERE is_current = TRUE
            GROUP BY company_id
            HAVING COUNT(*) >= $1
        )
        SELECT
            c.id              AS company_id,
            c.canonical_name  AS company_name,
            e.n               AS current_person_count
        FROM eligible e
        JOIN companies c ON c.id = e.company_id
        LEFT JOIN org_functional_clusters ofc ON ofc.company_id = c.id
        WHERE ofc.id IS NULL
        ORDER BY e.n DESC
        """,
        min_size,
    )
    return [dict(row) for row in rows]


async def _company_titles(company_id: str) -> list[tuple[str | None, str | None]]:
    """All (title, domain) pairs for one company's current persons.

    Mirrors `clustering._load_current_persons` exactly so the audit's
    classification view matches what the pipeline actually sees: titles
    come from `persons.current_title` with fallback to `employment_periods.title`,
    likewise for the domain hint. Joining on `employment_periods is_current`
    keeps the result aligned with the eligibility query above.
    """
    rows = await fetch(
        """
        SELECT
          COALESCE(p.current_title, ep.title)                          AS title,
          COALESCE(p.current_functional_domain, ep.functional_domain)  AS domain
        FROM employment_periods ep
        JOIN persons p ON p.id = ep.person_id
        WHERE ep.company_id = $1
          AND ep.is_current = TRUE
        """,
        company_id,
    )
    return [(row["title"], row["domain"]) for row in rows]


def _summarize_titles(
    title_pairs: list[tuple[str | None, str | None]],
) -> tuple[int, int, list[tuple[str, int]]]:
    """Pure: bucket titles into "domain set" vs "classifiable via NLP" vs
    "unclassified" and surface the top unclassified title strings.

    Returns ``(with_domain, classifiable, top_unclassified_pairs)``.
    """
    with_domain = 0
    classifiable = 0
    unclassified_titles: Counter[str] = Counter()

    for title, domain in title_pairs:
        if domain:
            with_domain += 1
            continue
        # No canonical_domain set — try the NLP fallback that clustering.py uses.
        inferred = domain_from_title(title) if title else None
        if inferred is not None:
            classifiable += 1
        else:
            label = (title or "<no title>").strip() or "<empty title>"
            unclassified_titles[label] += 1

    return with_domain, classifiable, unclassified_titles.most_common(5)


async def audit() -> list[CompanyAudit]:
    """Run the full audit and return one CompanyAudit per zero-cluster row."""
    candidates = await _zero_cluster_companies(MIN_CLUSTER_SIZE)
    out: list[CompanyAudit] = []
    for row in candidates:
        title_pairs = await _company_titles(row["company_id"])
        with_domain, classifiable, top_unclassified = _summarize_titles(title_pairs)
        out.append(
            CompanyAudit(
                company_id=str(row["company_id"]),
                company_name=row["company_name"],
                current_person_count=row["current_person_count"],
                persons_with_canonical_domain=with_domain,
                persons_with_classifiable_title=classifiable,
                top_unclassified_titles=top_unclassified,
            )
        )
    return out


# ── Output formatting ────────────────────────────────────────────────────────


def _format_report(audits: list[CompanyAudit]) -> str:
    """Render the audit list as plain text. One block per company.

    Format choice: plain text over CSV because the top-titles column is a
    list and CSV escaping of nested commas/quotes is ugly. Operators eyeball
    this; nothing downstream parses it.
    """
    if not audits:
        return "No zero-cluster companies found. Pipeline coverage is complete.\n"

    lines: list[str] = []
    lines.append(
        f"Found {len(audits)} clustering-eligible companies with 0 clusters.\n"
    )
    lines.append(
        "(eligible = at least %d current persons; min_size matches clustering.MIN_CLUSTER_SIZE)\n"
        % MIN_CLUSTER_SIZE
    )
    for a in audits:
        lines.append("")
        lines.append(f"## {a.company_name}  ({a.company_id})")
        lines.append(f"  current_persons:        {a.current_person_count}")
        lines.append(f"  with canonical_domain:  {a.persons_with_canonical_domain}")
        lines.append(
            f"  classifiable via NLP:   {a.persons_with_classifiable_title}"
        )
        unclassified = (
            a.current_person_count
            - a.persons_with_canonical_domain
            - a.persons_with_classifiable_title
        )
        lines.append(f"  unclassified:           {unclassified}")
        if a.top_unclassified_titles:
            lines.append("  top unclassified titles:")
            for title, count in a.top_unclassified_titles:
                lines.append(f"    {count:>4} × {title}")
    return "\n".join(lines) + "\n"


# ── Entrypoint ───────────────────────────────────────────────────────────────


async def _amain(args: argparse.Namespace) -> int:
    try:
        audits = await audit()
    finally:
        await close_pool()
    sys.stdout.write(_format_report(audits))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit clustering-eligible companies that produced zero clusters."
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
