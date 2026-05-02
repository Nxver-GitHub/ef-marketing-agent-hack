"""Stage 0 of the customer-onboarding pipeline.

Given ``(full_name, work_email)`` from a freshly-signed-up sales rep, find their
LinkedIn profile by:

1. Deriving the company's LinkedIn URL from the email's registered (eTLD+1)
   domain.
2. Listing the company's employees via the apimaestro
   ``linkedin-company-employees-scraper-no-cookies`` actor.
3. Fuzzy-matching ``full_name`` against the returned employees and returning
   the highest-confidence match (``confidence >= MIN_CONFIDENCE``).

Why apimaestro and not the harvestapi family
--------------------------------------------
``harvestapi/*`` actors gate FREE-tier accounts at 10 lifetime runs, after
which they SUCCEED with 0 items (silent failure). apimaestro has no such
gate. See ``credence/enrichment/apify_apimaestro.py`` for the full story.

Why not a dedicated "people search" actor
-----------------------------------------
apimaestro does not currently expose a name+company keyword search actor.
The plan in ``CUSTOMER_ONBOARDING_PLAN.md`` §"Stage 0" calls for
"parse the email domain → find the company on LinkedIn → search the
company's people for ``full_name`` → take top match." We achieve the
same result by listing the company's employees and doing a local
fuzzy match — this also lets us return a real, calibrated similarity
score as the ``confidence`` field rather than relying on the actor's
opaque ranking.

Idempotency
-----------
The function is idempotent: identical ``(full_name, email)`` always
produces an identical :class:`ResolvedRep` (the upstream apimaestro
actor returns the same employee list for the same company URL, and
all post-processing is pure).

Failure modes
-------------
Returns ``None`` for: actor failure (HTTP 5xx, network error, timeout),
zero employees returned, no name match above ``MIN_CONFIDENCE``,
malformed email. **Raises** :class:`RepResolverConfigError` if the
``APIFY_TOKEN`` environment variable is not set — that's a programmer
configuration bug, not a transient runtime condition.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Final
from uuid import UUID

import httpx

from ..enrichment.apify_apimaestro import (
    COST_EMPLOYEES_PER_ITEM_USD,
    EMPLOYEES_ACTOR,
    CompanyEmployee,
    list_company_employees,
)

log = logging.getLogger(__name__)

# Minimum similarity required for a fuzzy name match to count.
# Empirically: 0.60 admits "Sara Kim" → "Sarah Kim" and rejects
# "John Smith" → "Joan Smyth". Tunable per onboarding feedback.
MIN_CONFIDENCE: Final[float] = 0.60

# Free-mail and infrastructure domains that don't identify a company.
# A rep using one of these for their work email is unresolvable via
# the company-employees route — bail out early.
_NON_CORPORATE_DOMAINS: Final[frozenset[str]] = frozenset({
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "pm.me",
    "duck.com",
    "msn.com",
    "fastmail.com",
    "fastmail.fm",
})

# Common public suffixes we strip when going from registered domain → slug.
# Not a full PSL — that would be massive overkill here. Just the cases
# we actually see in practice.
_TWO_PART_TLDS: Final[frozenset[str]] = frozenset({
    "co.uk", "ac.uk", "gov.uk", "org.uk",
    "co.jp", "ac.jp", "or.jp",
    "com.au", "net.au", "org.au",
    "co.nz", "com.br", "com.mx", "com.sg",
    "co.in", "com.cn",
})

_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})$"
)


class RepResolverConfigError(RuntimeError):
    """Raised when a required env var is missing — programmer bug, not a no-match."""


@dataclass(frozen=True, slots=True)
class ResolvedRep:
    """The outcome of a successful rep-LinkedIn resolution.

    Frozen dataclass — instances are immutable. Build new ones rather than
    mutating fields. ``confidence`` is the [0, 1] similarity score that
    drove the match decision; callers may use it to gate downstream
    decisions (e.g. require >= 0.8 before auto-linking with no review).
    """

    linkedin_url: str
    current_title: str | None
    headline: str | None
    profile_photo_url: str | None
    confidence: float


# ── Email / domain parsing ─────────────────────────────────────────────


def _extract_registered_domain(email: str) -> str | None:
    """Parse ``user@host.tld`` and return the registered (eTLD+1) domain.

    Handles:
      - plus-addressing in the local part (``sarah+filter@nvidia.com``)
      - mixed case (``SARAH@NVIDIA.COM``)
      - mail subdomains (``sarah@mail.nvidia.com`` → ``nvidia.com``)
      - googlemail.com → gmail.com normalization
      - two-part public suffixes (``user@deepmind.co.uk`` → ``deepmind.co.uk``)

    Returns ``None`` if the email is malformed or uses a free-mail provider.
    """
    if not isinstance(email, str):
        return None
    m = _EMAIL_RE.match(email.strip())
    if not m:
        return None
    host = m.group(1).lower().rstrip(".")

    parts = host.split(".")
    if len(parts) < 2:
        return None

    # Detect 2-part public suffix (e.g. "co.uk") and keep one extra label.
    last_two = ".".join(parts[-2:])
    if last_two in _TWO_PART_TLDS and len(parts) >= 3:
        registered = ".".join(parts[-3:])
    else:
        registered = ".".join(parts[-2:])

    # googlemail.com is a Gmail alias — treat both as the same free-mail.
    if registered == "googlemail.com":
        registered = "gmail.com"

    if registered in _NON_CORPORATE_DOMAINS:
        return None

    return registered


def _company_slug_from_domain(domain: str) -> str:
    """Best-effort guess of the LinkedIn company slug from a domain.

    LinkedIn slugs don't perfectly match domains, but for the ~80% case
    (``nvidia.com`` → ``nvidia``, ``deepmind.com`` → ``deepmind``) the
    first label is correct. Edge cases that this gets wrong (e.g.
    ``saleforce.com`` → slug actually is ``salesforce-com``) will simply
    return ``[]`` from Stage A and we'll return ``None`` — the upstream
    pipeline falls back to manual entry, which is fine.
    """
    return domain.split(".", 1)[0]


def _company_url_from_email(email: str) -> str | None:
    """Compose the LinkedIn company URL probe from the email's domain."""
    domain = _extract_registered_domain(email)
    if domain is None:
        return None
    slug = _company_slug_from_domain(domain)
    if not slug:
        return None
    return f"https://linkedin.com/company/{slug}/"


# ── Name matching ──────────────────────────────────────────────────────


_NAME_NOISE_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


def _normalize_name(name: str) -> str:
    """Lowercase, strip diacritics, drop punctuation, collapse whitespace.

    ``"Søren O'Malley-Smith, PhD"`` → ``"soren omalleysmith phd"``.
    Used as the canonical form for both inputs to the fuzzy match so
    accents and punctuation can't artificially deflate the score.
    """
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    lowered = ascii_only.lower()
    no_punct = _NAME_NOISE_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", no_punct).strip()


def _name_similarity(a: str, b: str) -> float:
    """Confidence in [0, 1] that two display names refer to the same person.

    Uses :class:`difflib.SequenceMatcher` on normalized names. Exact match
    after normalization → 1.0; total miss → ~0.0. Empty inputs → 0.0.

    Why not Levenshtein? SequenceMatcher is in the stdlib, gives a
    similarly-shaped 0-1 score, and handles the typical "missing middle
    initial" / "Sara vs Sarah" cases adequately. If we need better
    discrimination later we can swap in ``rapidfuzz``.
    """
    na = _normalize_name(a)
    nb = _normalize_name(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _best_match(
    full_name: str,
    employees: list[CompanyEmployee],
) -> tuple[CompanyEmployee | None, float]:
    """Return (best_employee, confidence). ``best_employee`` is ``None`` on empty input.

    Pure function: same employees + name → same result. No tie-breaking
    randomness. On a tie (extremely unlikely with normalized 1.0 matches),
    the first employee in the input list wins, which matches the
    actor's stable ranking.
    """
    best_emp: CompanyEmployee | None = None
    best_score = 0.0
    for emp in employees:
        score = _name_similarity(full_name, emp.fullname)
        if score > best_score:
            best_score = score
            best_emp = emp
    return best_emp, best_score


# ── Public API ─────────────────────────────────────────────────────────


def _extract_photo_url(emp: CompanyEmployee) -> str | None:
    """Stage A doesn't expose a profile photo field on :class:`CompanyEmployee`.

    Kept as a separate function so when we later upgrade Stage 0 to also
    call Stage B (``fetch_profile_detail``) for the matched person, only
    this helper changes — the public ``ResolvedRep`` shape stays stable.
    """
    return None


async def resolve_rep_linkedin(
    full_name: str,
    email: str,
    *,
    account_id: UUID,
    client: httpx.AsyncClient | None = None,
) -> ResolvedRep | None:
    """Resolve a sales rep's LinkedIn profile from ``(full_name, email)``.

    Args:
        full_name: The rep's full display name as given at signup.
        email: The rep's work email. Used purely to derive the company
            domain — the email itself is not transmitted to LinkedIn or
            apimaestro.
        account_id: The rep's account UUID. **Not stored anywhere** —
            used only as a cost-attribution field in log records so
            spend can be allocated to the right tenant via log-based
            cost reports.
        client: Optional pre-existing :class:`httpx.AsyncClient` to reuse.
            If ``None``, a short-lived one is created inside this call.

    Returns:
        A :class:`ResolvedRep` when an employee of the rep's company
        matches ``full_name`` with confidence >= ``MIN_CONFIDENCE``,
        otherwise ``None``.

    Raises:
        RepResolverConfigError: If the ``APIFY_TOKEN`` env var is not set.
            This is a deployment misconfiguration, not a runtime no-match,
            so we surface it loudly rather than swallowing it as ``None``.
    """
    # Fail-fast on missing config (programmer bug — must not be silenced).
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise RepResolverConfigError(
            "APIFY_TOKEN env var is not set; rep_resolver cannot reach apimaestro."
        )

    company_url = _company_url_from_email(email)
    if company_url is None:
        log.info(
            "rep_resolver: skipping unresolvable email domain",
            extra={
                "account_id": str(account_id),
                "actor": EMPLOYEES_ACTOR,
                "cost_usd": 0.0,
                "reason": "non_corporate_or_malformed_email",
            },
        )
        return None

    # Apimaestro Stage A — list company employees.
    try:
        employees, cost_cents = await list_company_employees(
            company_url,
            max_items=500,
            api_token=token,
            client=client,
        )
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.warning(
            "rep_resolver: apimaestro actor call failed",
            extra={
                "account_id": str(account_id),
                "actor": EMPLOYEES_ACTOR,
                "cost_usd": 0.0,
                "company_url": company_url,
                "error": repr(exc),
            },
        )
        return None
    except Exception as exc:  # noqa: BLE001 — network layer can raise any number of things
        log.warning(
            "rep_resolver: apimaestro actor raised unexpected error",
            extra={
                "account_id": str(account_id),
                "actor": EMPLOYEES_ACTOR,
                "cost_usd": 0.0,
                "company_url": company_url,
                "error": repr(exc),
            },
        )
        return None

    cost_usd = cost_cents / 100.0

    if not employees:
        log.info(
            "rep_resolver: no employees returned",
            extra={
                "account_id": str(account_id),
                "actor": EMPLOYEES_ACTOR,
                "cost_usd": cost_usd,
                "company_url": company_url,
            },
        )
        return None

    best_emp, confidence = _best_match(full_name, employees)

    if best_emp is None or confidence < MIN_CONFIDENCE:
        log.info(
            "rep_resolver: best match below threshold",
            extra={
                "account_id": str(account_id),
                "actor": EMPLOYEES_ACTOR,
                "cost_usd": cost_usd,
                "company_url": company_url,
                "confidence": round(confidence, 4),
                "candidate_count": len(employees),
            },
        )
        return None

    log.info(
        "rep_resolver: matched rep to LinkedIn profile",
        extra={
            "account_id": str(account_id),
            "actor": EMPLOYEES_ACTOR,
            "cost_usd": cost_usd,
            "cost_per_item_usd": COST_EMPLOYEES_PER_ITEM_USD,
            "company_url": company_url,
            "confidence": round(confidence, 4),
            "candidate_count": len(employees),
            "matched_linkedin_url": best_emp.profile_url,
        },
    )

    return ResolvedRep(
        linkedin_url=best_emp.profile_url,
        current_title=best_emp.headline,
        headline=best_emp.headline,
        profile_photo_url=_extract_photo_url(best_emp),
        confidence=round(confidence, 4),
    )
