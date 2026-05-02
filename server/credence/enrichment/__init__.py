"""Per-prospect enrichment vendors — Wave 5 (Apollo, PDL, Parallel, Firecrawl).

Distinct from `credence.extractors.*` which implements Contract 1's
pair-based connection discovery. Enrichment vendors here implement Contract
8's per-prospect enrichment: given a single prospect, pull contact / phone
/ employment-history / etc. from a paid third-party source.

Common interface — every enrichment module exports:

    async def enrich(
        prospect: ProspectRef,
        *,
        client: httpx.AsyncClient | None = None,
        max_cost_cents: int = 100,
    ) -> EnrichmentResult | None

`EnrichmentResult` carries `fields` (the vendor-specific payload),
`cost_cents`, `confidence`, and `cache_hit`. Returns `None` when the vendor
declines / has no match / would exceed cost ceiling. The route layer
(`server/credence/routes/enrich.py`) fans out to every enabled vendor in
parallel and returns Contract-8-shaped `EnrichResponse`.
"""
from __future__ import annotations

from .apollo import (
    ApolloFields,
    ProspectRef,
)
from .apollo import (
    enrich as apollo_enrich,
)

__all__ = [
    "ApolloFields",
    "ProspectRef",
    "apollo_enrich",
]
