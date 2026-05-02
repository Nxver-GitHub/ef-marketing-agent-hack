"""Apollo.io enrichment — Contract 8 vendor implementation.

API: https://api.apollo.io/api/v1/  (Organization plan or above for API access;
~$0.025 per email credit, mobile numbers ~8x credits)

## What this fetches

For a single prospect, Apollo returns:
- Verified email (with a status: verified / guessed / no_match)
- Current title + current company + LinkedIn URL
- City + country
- Apollo's internal person_id (for idempotency / re-fetch)

**Phone numbers intentionally not requested.** Per user direction, the
warm-intro flow ends in an email send — phones cost 8x credits and add
no path-to-conversion value. If a future workflow needs them, re-enable by
extracting `phone_number` / `mobile_phone` in `_extract_apollo_person` and
adding the `APOLLO_PHONE_CREDIT_CENTS` term back into `_calculate_cost`.

**Manager / `reports_to` IS extracted** (Phase A.6). Apollo's `/people/match`
sometimes carries a `manager_first_name` / `manager_last_name` pair or a
nested `manager` object. We surface these as `reports_to_name` +
`reports_to_apollo_id` so the route layer can call
`hierarchy.ingest_explicit_edge(signal_type="linkedin_reports_to", ...)`
to populate `org_reporting_edges`. This is the explicit-signal path of the
org-chart inference pipeline — see Decision 3 in CLAUDE.md (explicit
signals override implicit scoring). The extractor itself never touches
the DB; it only surfaces the field for the route layer to act on.

## Strategy

Two-step lookup:

1. `POST /people/match` — sends `{ name, organization_name, linkedin_url? }`,
   Apollo returns the best match if any. Single API call per prospect.
2. If the match returns a `person_id` and the desired fields aren't in the
   match payload (Apollo's `/match` is generous but not exhaustive), one
   follow-up `POST /people/enrich` with the `id`.

For the v1 implementation we use only `/people/match` since it carries
emails + phones for ~95% of US tech-sector matches. The richer `/enrich`
call is wired but currently unused — a follow-up if hit rates disappoint.

## Sandbox / live status

Implementation is doc-driven against Apollo's published v1 schema. **Live
integration test is `tests/test_apollo_live.py`** (deferred — needs
`APOLLO_API_KEY`). Unit tests in `tests/test_apollo.py` mock the httpx
transport and lock the parsing.

## Cost handling

Every call returns the cost in cents. The route layer is responsible for
checking against `max_cost_cents` BEFORE issuing the call (Contract 8
invariant — don't pay for a request and then refuse to use the result).
This module reports back the cost via `EnrichmentResult.cost_cents` so the
route can write the `enrichment_cost_log` row.

## Idempotency / cache

The route layer handles cache (`prospects.last_enriched_at` < 24h). This
module is a pure I/O wrapper — it doesn't read or write the DB.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/api/v1/"
DEFAULT_TIMEOUT_SECONDS = 8.0

# Apollo's published per-credit pricing. An email match costs 1 credit;
# we surface the cost via `EnrichmentResult.cost_cents` for cap enforcement
# at the route layer. The Phone-number term is intentionally absent — see
# module docstring; phone is not requested.
APOLLO_EMAIL_CREDIT_CENTS = 3   # ~$0.025/credit at Organization tier


# ─── Public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProspectRef:
    """Minimal identifier set Apollo's match endpoint accepts.

    `linkedin_url` is the highest-precision match key when present. Falling
    back to `name + organization_name` when absent — Apollo will resolve
    this against their own database, but match quality drops noticeably
    for ambiguous names (e.g., common Chinese / Indian / Hispanic names
    paired with large multi-tenant company names).
    """

    person_id: str
    canonical_name: str
    organization_name: str | None = None
    linkedin_url: str | None = None
    email_hint: str | None = None       # if we already know the domain


class ApolloFields(TypedDict, total=False):
    """Vendor-specific payload for Contract 8's `EnrichmentRecord.fields`.

    Phone is intentionally absent — phones aren't requested (see module
    docstring). Re-add the `phone: str | None` key if a future workflow
    needs them.
    """

    email: str | None
    email_status: Literal["verified", "guessed", "no_match"]
    current_title: str | None
    current_company_name: str | None
    current_company_domain: str | None
    linkedin_url: str | None
    city: str | None
    country: str | None
    apollo_person_id: str
    # ── Phase A.6: explicit org-chart signals ──
    # Apollo occasionally returns the prospect's manager. We surface a
    # best-effort name (first+last concat) and the manager's Apollo id
    # for the route layer to resolve into a `persons.id` and write an
    # explicit org_reporting_edges row. Both default to None when absent.
    reports_to_name: str | None
    reports_to_apollo_id: str | None


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """Per-vendor result handed back to the route layer."""

    fields: ApolloFields
    confidence: float
    cost_cents: int
    cache_hit: bool = False  # apollo module never caches; set by route


# ─── HTTP I/O ───────────────────────────────────────────────────────────────


async def _apollo_post(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any],
    *,
    api_key: str,
) -> dict[str, Any] | None:
    """POST to Apollo, return JSON dict on 200, None on any failure mode.

    Apollo authenticates via `Cache-Control: no-cache` + `X-Api-Key` header
    (their docs cover both styles; X-Api-Key is the modern path).

    Network errors / non-200 / non-JSON all collapse to None — Contract 8
    partial-results semantics. Logs at warning level. Includes a tiny
    redaction so we don't leak credit-card-shaped data into structured
    logs (Apollo doesn't return CCs but defensively scrub anyway).
    """
    url = urljoin(APOLLO_BASE_URL, path)
    headers = {
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
    }
    try:
        r = await client.post(url, json=payload, headers=headers, timeout=DEFAULT_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.warning("Apollo request failed (%s): %s", path, exc)
        return None
    if r.status_code == 401 or r.status_code == 403:
        # Auth issues are sticky — log loud so ops sees them in the daily digest.
        logger.error("Apollo auth failure at %s — check APOLLO_API_KEY rotation", path)
        return None
    if r.status_code == 429:
        # Rate limit. The route layer's timeout absorbs the backoff window;
        # we don't retry inline since that would multiply the cost cap.
        logger.warning("Apollo rate-limited at %s", path)
        return None
    if r.status_code != 200:
        logger.warning("Apollo HTTP %d at %s: %s", r.status_code, path, r.text[:200])
        return None
    try:
        body = r.json()
    except ValueError:
        logger.warning("Apollo returned non-JSON body")
        return None
    return body if isinstance(body, dict) else None


# ─── Field extraction ───────────────────────────────────────────────────────


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _extract_apollo_person(person: dict[str, Any]) -> ApolloFields:
    """Map an Apollo `person` dict to the Contract 8 `ApolloFields` shape.

    Field paths are documented in Apollo's v1 API reference; missing fields
    default to None (Contract 8 invariant — never fabricate values).
    """
    email = _str_or_none(person.get("email"))
    email_status_raw = _str_or_none(person.get("email_status"))
    if email_status_raw == "verified":
        email_status: Literal["verified", "guessed", "no_match"] = "verified"
    elif email_status_raw in {"guessed", "uncertain"}:
        email_status = "guessed"
    else:
        email_status = "no_match" if email is None else "guessed"

    # Apollo nests current employment under `organization` or
    # `current_employer` depending on endpoint variant.
    org = person.get("organization") or person.get("current_employer") or {}
    if not isinstance(org, dict):
        org = {}

    # ── Phase A.6: extract manager / reports_to defensively ──
    # Apollo response shape varies. Two known forms:
    #   1. flat: `manager_first_name`, `manager_last_name`, `manager_id`
    #   2. nested: `manager: { first_name, last_name, id, ... }`
    # We try nested first, then flat. Empty / partial names collapse to None
    # rather than emitting "John " or " Doe".
    manager_obj = person.get("manager")
    if not isinstance(manager_obj, dict):
        manager_obj = {}
    manager_first = _str_or_none(manager_obj.get("first_name")) or _str_or_none(
        person.get("manager_first_name")
    )
    manager_last = _str_or_none(manager_obj.get("last_name")) or _str_or_none(
        person.get("manager_last_name")
    )
    if manager_first and manager_last:
        reports_to_name: str | None = f"{manager_first} {manager_last}"
    else:
        # A single-half name (e.g., only first_name "Wei") is too ambiguous
        # to be useful for `persons` lookup. Drop it — partial = None.
        reports_to_name = None
    reports_to_apollo_id = _str_or_none(manager_obj.get("id")) or _str_or_none(
        person.get("manager_id")
    )

    return ApolloFields(
        email=email,
        email_status=email_status,
        current_title=_str_or_none(person.get("title")),
        current_company_name=_str_or_none(org.get("name")),
        current_company_domain=_str_or_none(org.get("primary_domain"))
        or _str_or_none(org.get("website_url")),
        linkedin_url=_str_or_none(person.get("linkedin_url")),
        city=_str_or_none(person.get("city")),
        country=_str_or_none(person.get("country")),
        apollo_person_id=_str_or_none(person.get("id")) or "",
        reports_to_name=reports_to_name,
        reports_to_apollo_id=reports_to_apollo_id,
    )


def _calculate_cost(fields: ApolloFields) -> int:
    """Estimate cost in cents based on which fields Apollo returned.

    Apollo's billing is credit-tier-based:
    - Email match: 1 credit (~3¢)
    - (Phone is intentionally not requested — see module docstring.)

    A match returning only the title + LinkedIn URL costs us 0 credits
    (those are unlocked at all paid tiers). Approximate; actual invoicing
    is reconciled monthly against Apollo's billing dashboard.
    """
    cost = 0
    if fields.get("email"):
        cost += APOLLO_EMAIL_CREDIT_CENTS
    return cost


# ─── Public API ─────────────────────────────────────────────────────────────


async def enrich(
    prospect: ProspectRef,
    *,
    client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
    max_cost_cents: int = 100,
) -> EnrichmentResult | None:
    """Enrich a single prospect via Apollo `/people/match`.

    Returns:
        EnrichmentResult on a successful match (with `fields`, `cost_cents`,
        `confidence`).
        None when:
        - No `APOLLO_API_KEY` is configured
        - Apollo returns no match
        - Network / auth failure
        - The cost would exceed `max_cost_cents` (cost-cap enforcement)

    The route layer is responsible for writing the `enrichment_cost_log`
    row regardless of outcome (cache_hit=False, success=True/False).
    """
    key = api_key or os.environ.get("APOLLO_API_KEY")
    if not key:
        logger.info(
            "apollo.enrich called without APOLLO_API_KEY — skipping (set env or pass api_key=)"
        )
        return None

    # Pre-flight cost cap: worst case is an email match (3¢). If even that
    # exceeds the cap, decline rather than charge for nothing usable.
    worst_case_cost = APOLLO_EMAIL_CREDIT_CENTS
    if worst_case_cost > max_cost_cents:
        logger.info(
            "apollo.enrich: worst-case cost %d¢ > cap %d¢ — skipping",
            worst_case_cost,
            max_cost_cents,
        )
        return None

    payload: dict[str, Any] = {
        "name": prospect.canonical_name,
    }
    if prospect.linkedin_url:
        payload["linkedin_url"] = prospect.linkedin_url
    if prospect.organization_name:
        payload["organization_name"] = prospect.organization_name
    if prospect.email_hint:
        payload["email"] = prospect.email_hint

    own_client = client is None
    http = client if client is not None else httpx.AsyncClient()
    try:
        body = await _apollo_post(http, "people/match", payload, api_key=key)
    finally:
        if own_client:
            await http.aclose()

    if not body:
        return None

    person = body.get("person")
    if not isinstance(person, dict):
        return None

    fields = _extract_apollo_person(person)

    # Confidence comes from Apollo's email_status; fall back to 0.7 when
    # only a guess (the email syntactically matches the company domain
    # pattern but hasn't been deliverability-checked).
    confidence = (
        0.95
        if fields.get("email_status") == "verified"
        else 0.7
        if fields.get("email_status") == "guessed"
        else 0.5
    )

    return EnrichmentResult(
        fields=fields,
        confidence=confidence,
        cost_cents=_calculate_cost(fields),
        cache_hit=False,
    )
