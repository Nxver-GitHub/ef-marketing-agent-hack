"""Tests for bulk_standards_ingest — pure helpers + CLI surface.

Live Firecrawl is not exercised here; the per-pair extractor in
``test_standards.py`` covers HTTP behavior. This file covers the bulk
runner's planning + dedupe + UPSERT shape.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from credence.jobs import bulk_standards_ingest as job


# ── Fixtures ────────────────────────────────────────────────────────────────


ACCOUNT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
P_WEI = UUID("00000000-0000-0000-0000-000000000001")
P_MARCUS = UUID("00000000-0000-0000-0000-000000000002")


def _person(pid: UUID, canonical: str, *variants: str) -> job.PersonRow:
    folded = {job._fold_name(canonical)}
    folded.update(job._fold_name(v) for v in variants)
    return job.PersonRow(
        id=pid, canonical_name=canonical, folded_names=frozenset(folded)
    )


# ── _safe_year ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("not-a-year", None),
        (1949, None),
        (1950, 1950),
        (2024, 2024),
        (2100, 2100),
        (2101, None),
    ],
)
def test_safe_year_squashes_out_of_range(raw: Any, expected: int | None) -> None:
    assert job._safe_year(raw) == expected


# ── _parse_years ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_years_range() -> None:
    assert job._parse_years("2018-2022") == (2018, 2022)
    assert job._parse_years("2018–2022") == (2018, 2022)  # en-dash
    assert job._parse_years("2018—2022") == (2018, 2022)  # em-dash


@pytest.mark.unit
def test_parse_years_present() -> None:
    assert job._parse_years("2015-present") == (2015, None)
    assert job._parse_years("2015-Present") == (2015, None)


@pytest.mark.unit
def test_parse_years_single() -> None:
    assert job._parse_years("2020") == (2020, None)


@pytest.mark.unit
def test_parse_years_unknown_or_garbage() -> None:
    assert job._parse_years(None) == (None, None)
    assert job._parse_years("") == (None, None)
    assert job._parse_years("unknown") == (None, None)
    assert job._parse_years("UNKNOWN") == (None, None)
    assert job._parse_years("garbage 1234567") == (None, None)


@pytest.mark.unit
def test_parse_years_squashes_pre_1950() -> None:
    """Years < 1950 must squash to NULL to satisfy the CHECK constraint."""
    assert job._parse_years("1900-1920") == (None, None)


# ── _build_name_index ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_build_name_index_uses_canonical_and_variants() -> None:
    people = [
        _person(P_WEI, "Wei Chen", "W. Chen", "Chen, Wei"),
        _person(P_MARCUS, "Marcus Aurelius"),
    ]
    idx = job._build_name_index(people)
    assert idx[job._fold_name("Wei Chen")] == P_WEI
    assert idx[job._fold_name("W. Chen")] == P_WEI
    assert idx[job._fold_name("Marcus Aurelius")] == P_MARCUS


@pytest.mark.unit
def test_build_name_index_collision_last_wins() -> None:
    """Documented behavior — colliding folded names overwrite."""
    people = [
        _person(P_WEI, "Wei Chen"),
        _person(P_MARCUS, "Wei Chen"),  # collision
    ]
    idx = job._build_name_index(people)
    # Last one in the list wins.
    assert idx[job._fold_name("Wei Chen")] == P_MARCUS


@pytest.mark.unit
def test_build_name_index_skips_empty() -> None:
    p = job.PersonRow(
        id=P_WEI,
        canonical_name="Wei Chen",
        folded_names=frozenset({"", "wei chen"}),
    )
    idx = job._build_name_index([p])
    assert "" not in idx


# ── _emissions_from_rosters ─────────────────────────────────────────────────


@pytest.mark.unit
def test_emissions_match_only_known_persons() -> None:
    idx = {job._fold_name("Wei Chen"): P_WEI}
    entries = [
        job.RosterEntry(
            body="JEDEC",
            committee="JC-42 (DRAM)",
            member_name="Wei Chen",
            years="2018-2022",
            source_url="https://www.jedec.org/committees",
        ),
        job.RosterEntry(
            body="JEDEC",
            committee="JC-42 (DRAM)",
            member_name="Stranger Person",  # not in index
            years="2018-2022",
            source_url="https://www.jedec.org/committees",
        ),
    ]
    emissions = job._emissions_from_rosters(entries, idx)
    assert len(emissions) == 1
    assert emissions[0].person_id == P_WEI
    assert emissions[0].organization == "JEDEC"
    assert emissions[0].committee == "JC-42 (DRAM)"
    assert emissions[0].start_year == 2018
    assert emissions[0].end_year == 2022


@pytest.mark.unit
def test_emissions_dedup_within_committee_year() -> None:
    """Same person + body + committee + start_year emits once.

    Mirrors the ``standards_memberships_uniq`` index's COALESCE behavior:
    NULL start_year collapses to 0 in the dedup key.
    """
    idx = {job._fold_name("Wei Chen"): P_WEI}
    entries = [
        job.RosterEntry(
            body="JEDEC", committee="JC-42 (DRAM)", member_name="Wei Chen",
            years="2018-2022", source_url="x",
        ),
        job.RosterEntry(  # duplicate — same start year
            body="JEDEC", committee="JC-42 (DRAM)", member_name="Wei Chen",
            years="2018-2022", source_url="x",
        ),
    ]
    emissions = job._emissions_from_rosters(entries, idx)
    assert len(emissions) == 1


@pytest.mark.unit
def test_emissions_distinct_when_start_year_differs() -> None:
    """Same person + committee at different years → two rows (different stints)."""
    idx = {job._fold_name("Wei Chen"): P_WEI}
    entries = [
        job.RosterEntry(
            body="JEDEC", committee="JC-42", member_name="Wei Chen",
            years="2018-2020", source_url="x",
        ),
        job.RosterEntry(
            body="JEDEC", committee="JC-42", member_name="Wei Chen",
            years="2022-present", source_url="x",
        ),
    ]
    emissions = job._emissions_from_rosters(entries, idx)
    assert len(emissions) == 2
    starts = sorted(e.start_year for e in emissions if e.start_year)
    assert starts == [2018, 2022]


@pytest.mark.unit
def test_emissions_drop_unmatchable_names_silently() -> None:
    idx: dict[str, UUID] = {}
    entries = [
        job.RosterEntry(
            body="JEDEC", committee="JC-42", member_name="Wei Chen",
            years="2018-2022", source_url="x",
        )
    ]
    assert job._emissions_from_rosters(entries, idx) == []


# ── _parse_bodies ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_bodies_default_returns_none() -> None:
    assert job._parse_bodies(None) is None
    assert job._parse_bodies("") is None
    assert job._parse_bodies("all") is None


@pytest.mark.unit
def test_parse_bodies_subset() -> None:
    out = job._parse_bodies("JEDEC, IEEE SA")
    assert out is not None
    assert set(out.keys()) == {"JEDEC", "IEEE SA"}


@pytest.mark.unit
def test_parse_bodies_rejects_invalid() -> None:
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        job._parse_bodies("FAKE BODY")


# ── CLI parser ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cli_requires_scope() -> None:
    parser = job._build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


@pytest.mark.unit
def test_cli_account_id_path() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(
        ["--account-id", str(ACCOUNT_ID), "--dry-run"]
    )
    assert args.account_id == ACCOUNT_ID
    assert args.dry_run is True
    assert args.bodies is None  # default: all bodies


@pytest.mark.unit
def test_cli_all_accounts_path() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(["--all-accounts"])
    assert args.all_accounts is True


@pytest.mark.unit
def test_cli_bodies_subset() -> None:
    parser = job._build_arg_parser()
    args = parser.parse_args(
        ["--all-accounts", "--bodies", "JEDEC,SEMI"]
    )
    assert args.bodies is not None
    assert set(args.bodies.keys()) == {"JEDEC", "SEMI"}
