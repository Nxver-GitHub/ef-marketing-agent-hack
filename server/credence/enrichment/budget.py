"""Per-tenant enrichment budget enforcement (Wave 6 M4).

Reads `account_settings.<vendor>_monthly_cents` against MTD spend in
`enrichment_cost_log` to decide whether a vendor call is in-budget.

**Convention:** a budget cap of ``0`` means **UNLIMITED** (free tier or
unconfigured tenant). Any positive value is the hard cap; if MTD spend
plus the projected call cost would exceed it, the call is denied with
``BudgetExceeded``. Cap values are non-negative integers per the schema
``CONSTRAINT account_settings_caps_nonneg``.

Per CONTRACTS.md Contract 9 §"Wave 5 budget integration" — enrichment
routes pre-flight check budget before invoking vendor runners; over-budget
vendors are surfaced via ``vendors_skipped_for_cost`` in the
``EnrichResponse`` rather than billed-then-rejected.

Public surface:

- ``BudgetExceeded`` — raised when the projected call would breach the cap
- ``BudgetState`` — frozen snapshot of (cap, spent, remaining) for one
  (account, vendor) pair at one moment
- ``mtd_spent_cents`` — sum of cost_cents charged this calendar month
- ``vendor_monthly_cap_cents`` — read the configured cap (0 = unlimited)
- ``get_budget_state`` — both above in one call, returns BudgetState
- ``assert_budget`` — pre-flight predicate; raises BudgetExceeded on
  overrun, no-op otherwise
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from .. import db

logger = logging.getLogger(__name__)


# Vendor name → account_settings column. Centralized so adding a vendor
# requires only (a) a migration column and (b) one row in this dict.
# Keys must match the vendor strings used in EnrichResponse.vendor_name
# and the runner registry in enrich.py:_VENDOR_RUNNERS.
_VENDOR_BUDGET_COLUMNS: Final[dict[str, str]] = {
    "apollo": "apollo_monthly_cents",
    "pdl": "pdl_monthly_cents",
    "parallel": "parallel_monthly_cents",
    "firecrawl": "firecrawl_monthly_cents",
}


class BudgetExceeded(Exception):  # noqa: N818 — established public API; renaming would ripple
    """Raised when a projected vendor call would exceed the monthly cap.

    Carries the snapshot fields used by ``enrich.py`` to log the skip and
    surface a meaningful ``error_message`` in the cost-log audit trail.
    """

    def __init__(
        self,
        *,
        vendor: str,
        account_id: UUID,
        cap_cents: int,
        spent_cents: int,
        projected_cents: int,
    ) -> None:
        self.vendor = vendor
        self.account_id = account_id
        self.cap_cents = cap_cents
        self.spent_cents = spent_cents
        self.projected_cents = projected_cents
        super().__init__(
            f"budget exceeded: account={account_id} vendor={vendor} "
            f"cap={cap_cents}c spent={spent_cents}c projected={projected_cents}c"
        )


@dataclass(frozen=True)
class BudgetState:
    """Snapshot of a vendor budget for a given account at one moment."""

    vendor: str
    account_id: UUID
    cap_cents: int  # 0 = unlimited
    spent_cents: int

    @property
    def unlimited(self) -> bool:
        return self.cap_cents == 0

    @property
    def remaining_cents(self) -> int | None:
        """``None`` when unlimited; else ``max(0, cap - spent)``."""
        if self.unlimited:
            return None
        return max(0, self.cap_cents - self.spent_cents)


async def mtd_spent_cents(account_id: UUID, vendor: str) -> int:
    """Sum of ``cost_cents`` charged to (account, vendor) since the start
    of the current calendar month.

    Cache hits (``cost_cents=0``) and skipped/budget-blocked rows
    (``cost_cents=0``) contribute nothing to the sum. Returns ``0`` when
    no rows exist.
    """
    row = await db.fetchrow(
        """
        SELECT COALESCE(SUM(cost_cents), 0) AS total
        FROM enrichment_cost_log
        WHERE account_id = $1
          AND vendor = $2
          AND called_at >= date_trunc('month', now())
        """,
        account_id,
        vendor,
    )
    return int(row["total"]) if row else 0


async def vendor_monthly_cap_cents(account_id: UUID, vendor: str) -> int:
    """Per-vendor monthly cap in cents (``0`` = unlimited).

    Returns ``0`` when:
    - The vendor name is unknown (defensive — caller's runner registry is
      the source of truth for which vendors actually run)
    - The account has no ``account_settings`` row (treated as the seeded
      default of zeros, i.e., unlimited)
    """
    column = _VENDOR_BUDGET_COLUMNS.get(vendor)
    if column is None:
        logger.warning(
            "budget: unknown vendor %s — treating as unlimited", vendor
        )
        return 0

    # `column` comes from a whitelist defined in this module — safe to
    # f-string into the SQL. Account_id is parameterized.
    row = await db.fetchrow(
        f"SELECT {column} AS cap FROM account_settings WHERE account_id = $1",
        account_id,
    )
    return int(row["cap"]) if row else 0


async def get_budget_state(account_id: UUID, vendor: str) -> BudgetState:
    """Fetch (cap, spent) in two queries and bundle into a snapshot.

    Use when the caller wants the full picture for downstream reporting
    (cost dashboards, banner UIs). For pure check-and-go,
    ``assert_budget`` is the convenience.
    """
    cap = await vendor_monthly_cap_cents(account_id, vendor)
    spent = await mtd_spent_cents(account_id, vendor)
    return BudgetState(
        vendor=vendor,
        account_id=account_id,
        cap_cents=cap,
        spent_cents=spent,
    )


async def assert_budget(
    account_id: UUID, vendor: str, projected_cents: int
) -> None:
    """Pre-flight check: raise ``BudgetExceeded`` if the call would push
    MTD spend past the cap.

    No-op cases (always returns ``None`` without raising):
    - ``projected_cents <= 0`` — cache hits and free vendor calls
    - The cap is ``0`` (unlimited)
    - ``spent + projected <= cap``

    ## Known race (v3.1 candidate, surfaced 2026-04-30 via Stream 4)

    Two concurrent ``/enrich/{id}`` calls on the same (account, vendor)
    both read the same ``spent_cents`` baseline and both pass the check
    even when their *combined* projected cost exceeds the cap. Net
    overshoot is bounded by ``(N-1) × projected_cents`` for ``N``
    concurrent callers — typically ≤ 1 vendor call's worth of credit.

    A correct fix requires a reservation pattern: insert a "pending"
    cost-log row at pre-flight (cost_cents = projected), reject if
    committed + pending > cap, then update to actual cost on completion
    or delete on failure. Advisory locks across the vendor HTTP call
    would serialize all enrichment for that tenant — net worse.

    Out of scope here; locked by ``test_concurrent_enrich_no_lost_cost_logs``
    so future budget refactors must consciously address this.
    """
    if projected_cents <= 0:
        return
    state = await get_budget_state(account_id, vendor)
    if state.unlimited:
        return
    if state.spent_cents + projected_cents > state.cap_cents:
        raise BudgetExceeded(
            vendor=vendor,
            account_id=account_id,
            cap_cents=state.cap_cents,
            spent_cents=state.spent_cents,
            projected_cents=projected_cents,
        )
