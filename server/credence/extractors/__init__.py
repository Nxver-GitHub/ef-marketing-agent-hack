"""Connection extractors — each module finds documented relationships between
two persons from a single external source.

Common extractor signature:

    async def find_<kind>(person_a: PersonRef, person_b: PersonRef) -> list[ExtractedConnection]

`PersonRef` is the minimal identifier set we need to query the source
(canonical name, optional LinkedIn URL, optional ORCID, optional USPTO inventor ID).

`ExtractedConnection` is a stub-rich dict that the `signals` route turns into
a `ConnectionRecord` (Contract 1) before persisting.

Extractors are pure I/O wrappers — they do not write to the DB. The signals
route does the persistence so the timeout / partial-results contract is in
one place.
"""
from __future__ import annotations

from .career import find_career_overlaps
from .conference import find_conference_program_appearances
from .education import find_education_overlaps
from .parallel_conference import find_conference_co_appearances
from .parallel_standards import find_standards_committee_peers
from .patents import find_patent_co_inventions
from .scholar import find_paper_co_authorships
from .standards import find_standards_roster_memberships

__all__ = [
    "find_career_overlaps",
    "find_conference_co_appearances",
    "find_conference_program_appearances",
    "find_education_overlaps",
    "find_paper_co_authorships",
    "find_patent_co_inventions",
    "find_standards_committee_peers",
    "find_standards_roster_memberships",
]
