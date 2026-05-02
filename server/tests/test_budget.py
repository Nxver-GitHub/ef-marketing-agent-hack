"""Unit tests for the per-tenant budget enforcement helper (Wave 6 M4).

The module under test (`credence.enrichment.budget`) reads from the live
DB via `credence.db.fetchrow`. We monkeypatch that helper to control
both the configured cap and the MTD spend, isolating the assertions to
budget.py's pure logic.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from credence.enrichment import budget
from credence.enrichment.budget import (
    BudgetExceeded,
    BudgetState,
    assert_budget,
    get_budget_state,
    mtd_spent_cents,
    vendor_monthly_cap_cents,
)

# ─── Fixtures ────────────────────────────────────────────────────────────

ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")


class _FakeRow(dict):
    """Mimic asyncpg.Record's ``row["col"]`` access."""


def _patch_fetchrow(monkeypatch, *, cap: int | None = 0, total: int = 0) -> None:
    """Stub `credence.db.fetchrow` for both the cap query and the spend
    query.

    - ``cap=None`` simulates "no account_settings row exists for this
      account" (i.e., `fetchrow` returns ``None``).
    - ``cap=<int>`` simulates a real row with that cap value.
    - ``total`` becomes the SUM(cost_cents) returned by the spend query.

    Both queries return _FakeRow instances; the helper distinguishes them
    by which column the SQL projects.
    """

    async def _stub(sql: str, *args: Any) -> Any:
        if "SUM(cost_cents)" in sql:
            return _FakeRow(total=total)
        # cap query: SELECT <vendor_col> AS cap FROM account_settings WHERE ...
        if cap is None:
            return None
        return _FakeRow(cap=cap)

    monkeypatch.setattr(budget.db, "fetchrow", _stub)


# ─── BudgetState dataclass ───────────────────────────────────────────────


@pytest.mark.unit
def test_budget_state_unlimited_when_cap_zero() -> None:
    s = BudgetState(vendor="apollo", account_id=ACCOUNT, cap_cents=0, spent_cents=0)
    assert s.unlimited is True
    assert s.remaining_cents is None


@pytest.mark.unit
def test_budget_state_remaining_when_capped() -> None:
    s = BudgetState(vendor="apollo", account_id=ACCOUNT, cap_cents=1000, spent_cents=400)
    assert s.unlimited is False
    assert s.remaining_cents == 600


@pytest.mark.unit
def test_budget_state_remaining_clamped_at_zero_when_overspent() -> None:
    s = BudgetState(vendor="apollo", account_id=ACCOUNT, cap_cents=100, spent_cents=250)
    assert s.unlimited is False
    assert s.remaining_cents == 0  # not negative


# ─── vendor_monthly_cap_cents ────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_vendor_returns_zero_unlimited(monkeypatch) -> None:
    _patch_fetchrow(monkeypatch, cap=999)  # would have been cap=999 if known
    cap = await vendor_monthly_cap_cents(ACCOUNT, "unknown_vendor")
    assert cap == 0  # unknown vendor = unlimited (no row read)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_account_row_returns_zero_unlimited(monkeypatch) -> None:
    _patch_fetchrow(monkeypatch, cap=None)
    cap = await vendor_monthly_cap_cents(ACCOUNT, "apollo")
    assert cap == 0  # no account_settings row = unlimited


@pytest.mark.unit
@pytest.mark.asyncio
async def test_known_vendor_returns_configured_cap(monkeypatch) -> None:
    _patch_fetchrow(monkeypatch, cap=5000)
    cap = await vendor_monthly_cap_cents(ACCOUNT, "apollo")
    assert cap == 5000


@pytest.mark.unit
@pytest.mark.parametrize(
    "vendor",
    ["apollo", "pdl", "parallel", "firecrawl"],
)
@pytest.mark.asyncio
async def test_all_known_vendors_resolve_to_a_cap(monkeypatch, vendor: str) -> None:
    """Every vendor in the runner registry must have a budget column."""
    _patch_fetchrow(monkeypatch, cap=2500)
    cap = await vendor_monthly_cap_cents(ACCOUNT, vendor)
    assert cap == 2500


# ─── mtd_spent_cents ─────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mtd_spent_zero_when_no_rows(monkeypatch) -> None:
    _patch_fetchrow(monkeypatch, total=0)
    spent = await mtd_spent_cents(ACCOUNT, "apollo")
    assert spent == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mtd_spent_returns_sum(monkeypatch) -> None:
    _patch_fetchrow(monkeypatch, total=4200)
    spent = await mtd_spent_cents(ACCOUNT, "apollo")
    assert spent == 4200


# ─── get_budget_state ────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_budget_state_bundles_cap_and_spent(monkeypatch) -> None:
    _patch_fetchrow(monkeypatch, cap=1000, total=300)
    state = await get_budget_state(ACCOUNT, "apollo")
    assert state.vendor == "apollo"
    assert state.account_id == ACCOUNT
    assert state.cap_cents == 1000
    assert state.spent_cents == 300
    assert state.remaining_cents == 700


# ─── assert_budget ───────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("projected", [0, -1, -100])
async def test_assert_budget_noop_for_nonpositive_projected(
    monkeypatch, projected: int
) -> None:
    """Cache hits and free vendors short-circuit before any DB call."""
    # Patch fetchrow to raise so we'd notice if it got called.
    async def _fail(*_args: Any) -> Any:
        raise AssertionError("fetchrow should not be called for projected<=0")

    monkeypatch.setattr(budget.db, "fetchrow", _fail)
    await assert_budget(ACCOUNT, "apollo", projected)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_assert_budget_noop_when_unlimited(monkeypatch) -> None:
    _patch_fetchrow(monkeypatch, cap=0, total=10_000)  # huge spend, cap=0
    await assert_budget(ACCOUNT, "apollo", 500)  # should not raise


@pytest.mark.unit
@pytest.mark.asyncio
async def test_assert_budget_noop_when_under_cap(monkeypatch) -> None:
    _patch_fetchrow(monkeypatch, cap=1000, total=200)
    await assert_budget(ACCOUNT, "apollo", 500)  # 700 <= 1000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_assert_budget_noop_at_exact_cap(monkeypatch) -> None:
    """The cap is inclusive: spent + projected == cap is allowed."""
    _patch_fetchrow(monkeypatch, cap=1000, total=400)
    await assert_budget(ACCOUNT, "apollo", 600)  # 400 + 600 == 1000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_assert_budget_raises_on_overrun(monkeypatch) -> None:
    _patch_fetchrow(monkeypatch, cap=1000, total=900)
    with pytest.raises(BudgetExceeded) as ei:
        await assert_budget(ACCOUNT, "apollo", 200)  # 900 + 200 > 1000

    exc = ei.value
    assert exc.vendor == "apollo"
    assert exc.account_id == ACCOUNT
    assert exc.cap_cents == 1000
    assert exc.spent_cents == 900
    assert exc.projected_cents == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_assert_budget_raises_when_already_overspent(monkeypatch) -> None:
    """If spend already exceeds cap, any positive projection raises."""
    _patch_fetchrow(monkeypatch, cap=1000, total=1500)
    with pytest.raises(BudgetExceeded):
        await assert_budget(ACCOUNT, "apollo", 1)


# ─── BudgetExceeded message format ───────────────────────────────────────


@pytest.mark.unit
def test_budget_exceeded_message_contains_breakdown() -> None:
    exc = BudgetExceeded(
        vendor="apollo",
        account_id=ACCOUNT,
        cap_cents=500,
        spent_cents=450,
        projected_cents=100,
    )
    msg = str(exc)
    assert "apollo" in msg
    assert "cap=500c" in msg
    assert "spent=450c" in msg
    assert "projected=100c" in msg
