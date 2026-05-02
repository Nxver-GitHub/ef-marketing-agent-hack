"""Target company list — 60 companies for the Tier-1 bulk enrichment.

Sourced from PROSPECT_ENRICHMENT_TASK.md tiers (semis, defense, aerospace)
plus the highest-prospect-count companies in the live DB that aren't on
the task-list. LinkedIn company slugs verified against
``api.apify.com/v2/acts/harvestapi~linkedin-company-employees`` — bad
slugs cause the actor to silently return 0 results (verified live with
"marvell-semiconductor" → 404, "marvell" → 8,839 employees).

When adding a new company:
1. Find the slug by visiting the company's LinkedIn page
2. Add a 1-line smoke test in tests/test_target_companies.py if you
   want belt-and-suspenders (optional)
3. Add the canonical_name + slug here
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier = Literal["semiconductor", "defense", "aerospace", "research_lab"]


@dataclass(frozen=True, slots=True)
class TargetCompany:
    canonical_name: str           # what we want in companies.canonical_name
    linkedin_slug: str            # the slug after /company/ in the LinkedIn URL
    tier: Tier
    priority: int                 # 0 = P0, 1 = P1, etc.

    @property
    def linkedin_url(self) -> str:
        return f"https://www.linkedin.com/company/{self.linkedin_slug}/"


# 60 companies. Order: priority asc, then alphabetical.
TARGET_COMPANIES: tuple[TargetCompany, ...] = (
    # ── Tier 1 — Semiconductor (20) ────────────────────────────────────
    TargetCompany("NVIDIA", "nvidia", "semiconductor", 0),
    TargetCompany("Intel", "intel-corporation", "semiconductor", 0),  # validated 2026-04-30
    TargetCompany("AMD", "amd", "semiconductor", 0),
    TargetCompany("Qualcomm", "qualcomm", "semiconductor", 1),
    TargetCompany("TSMC", "tsmc", "semiconductor", 1),
    TargetCompany("ASML", "asml", "semiconductor", 1),
    TargetCompany("Broadcom", "broadcom", "semiconductor", 1),
    TargetCompany("Marvell Technology", "marvell", "semiconductor", 1),
    TargetCompany("Micron Technology", "micron-technology", "semiconductor", 2),
    TargetCompany("Applied Materials", "applied-materials", "semiconductor", 2),
    TargetCompany("Lam Research", "lam-research", "semiconductor", 2),
    TargetCompany("KLA Corporation", "klacorp", "semiconductor", 2),
    TargetCompany("Synopsys", "synopsys", "semiconductor", 2),
    TargetCompany("Cadence Design Systems", "cadence-design-systems", "semiconductor", 2),
    TargetCompany("Texas Instruments", "texas-instruments", "semiconductor", 3),
    TargetCompany("NXP Semiconductors", "nxp-semiconductors", "semiconductor", 3),
    TargetCompany("Infineon Technologies", "infineon-technologies", "semiconductor", 3),
    TargetCompany("Arm Holdings", "arm", "semiconductor", 3),
    TargetCompany("SK Hynix", "sk-hynix", "semiconductor", 3),  # validated 2026-04-30
    TargetCompany("Samsung Semiconductor", "samsungsemiconductor", "semiconductor", 3),

    # ── Tier 2 — Defense (13) ──────────────────────────────────────────
    TargetCompany("Lockheed Martin", "lockheed-martin", "defense", 0),
    TargetCompany("RTX", "rtx", "defense", 0),
    TargetCompany("Northrop Grumman", "northrop-grumman-corporation", "defense", 0),
    TargetCompany("General Dynamics", "general-dynamics", "defense", 1),
    TargetCompany("L3Harris Technologies", "l3harris-technologies", "defense", 1),  # validated 2026-04-30
    TargetCompany("BAE Systems", "bae-systems", "defense", 1),
    TargetCompany("Leidos", "leidos", "defense", 1),
    TargetCompany("SAIC", "saicinc", "defense", 2),  # validated 2026-04-30
    TargetCompany("Booz Allen Hamilton", "booz-allen-hamilton", "defense", 2),
    TargetCompany("Palantir Technologies", "palantir-technologies", "defense", 2),
    TargetCompany("Anduril Industries", "andurilindustries", "defense", 2),  # validated 2026-04-30
    TargetCompany("Shield AI", "shieldai", "defense", 3),
    TargetCompany("MITRE Corporation", "mitre", "defense", 3),  # validated 2026-04-30

    # ── Tier 3 — Aerospace (10; Archer Aviation dropped — no findable LI slug) ──
    TargetCompany("Boeing", "boeing", "aerospace", 0),
    TargetCompany("Airbus", "airbus", "aerospace", 0),
    TargetCompany("SpaceX", "spacex", "aerospace", 1),
    TargetCompany("Rocket Lab", "rocket-lab-limited", "aerospace", 1),
    TargetCompany("Aerojet Rocketdyne", "aerojet-rocketdyne", "aerospace", 2),
    TargetCompany("Textron Aviation", "textron-aviation", "aerospace", 2),
    TargetCompany("Honeywell Aerospace", "honeywell-aerospace", "aerospace", 2),  # validated 2026-04-30
    TargetCompany("Collins Aerospace", "collins-aerospace", "aerospace", 2),  # validated 2026-04-30
    TargetCompany("GE Aerospace", "geaerospace", "aerospace", 2),
    TargetCompany("Joby Aviation", "joby-aviation", "aerospace", 3),

    # ── Additional — high-DB-prospect-count, not on task list (16) ─────
    TargetCompany("GlobalFoundries", "globalfoundries", "semiconductor", 2),
    TargetCompany("STMicroelectronics", "stmicroelectronics", "semiconductor", 3),
    TargetCompany("Microchip Technology", "microchip-technology", "semiconductor", 3),
    TargetCompany("ON Semiconductor", "onsemi", "semiconductor", 3),
    TargetCompany("Sandia National Laboratories", "sandia-national-laboratories", "research_lab", 1),
    TargetCompany("Argonne National Laboratory", "argonne-national-laboratory", "research_lab", 2),
    TargetCompany("Oak Ridge National Laboratory", "oak-ridge-national-laboratory", "research_lab", 2),
    TargetCompany("Lawrence Livermore National Laboratory", "lawrence-livermore-national-laboratory", "research_lab", 2),
    TargetCompany("Los Alamos National Laboratory", "los-alamos-national-laboratory", "research_lab", 2),
    TargetCompany("Brookhaven National Laboratory", "brookhavenlab", "research_lab", 3),  # validated 2026-04-30
    TargetCompany("Pratt & Whitney", "pratt-&-whitney", "aerospace", 2),
    TargetCompany("Parker Aerospace", "parker-aerospace", "aerospace", 2),
    TargetCompany("Blue Origin", "blue-origin", "aerospace", 1),
    TargetCompany("Sierra Space", "sierraspace", "aerospace", 2),
    TargetCompany("Maxar Technologies", "maxar", "aerospace", 2),  # validated 2026-04-30
    TargetCompany("Iridium", "iridium", "aerospace", 3),  # validated 2026-04-30
)


# Convenience exports
TIER_1_SEMICONDUCTOR = tuple(c for c in TARGET_COMPANIES if c.tier == "semiconductor")
TIER_2_DEFENSE = tuple(c for c in TARGET_COMPANIES if c.tier == "defense")
TIER_3_AEROSPACE = tuple(c for c in TARGET_COMPANIES if c.tier == "aerospace")


def by_priority(tier: Tier | None = None) -> list[TargetCompany]:
    """Return target companies sorted by priority (P0 first), optionally filtered."""
    items = TARGET_COMPANIES if tier is None else [c for c in TARGET_COMPANIES if c.tier == tier]
    return sorted(items, key=lambda c: (c.priority, c.canonical_name))


__all__ = [
    "TargetCompany",
    "TARGET_COMPANIES",
    "TIER_1_SEMICONDUCTOR",
    "TIER_2_DEFENSE",
    "TIER_3_AEROSPACE",
    "by_priority",
]


# Sanity check at import time — if someone edits the list and breaks the
# count, it'll fail loudly rather than silently shipping a wrong-sized run.
assert len(TARGET_COMPANIES) == 59, (
    f"Expected 59 target companies; got {len(TARGET_COMPANIES)}"
)
