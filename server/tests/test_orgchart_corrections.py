"""Tests for `credence.orgchart.corrections` + the POST /orgchart/correction route.

Coverage:
1. CorrectionInput validation — keyspace, required correct_value
2. record_correction with edge_id resolves account+method from edge
3. record_correction without edge_id falls back to person.account_id
4. EdgeNotFoundError raised when edge_id doesn't exist
5. POST /orgchart/correction route — happy path returns correction_id
6. Route returns 400 on invalid correction_type
7. Route returns 400 when correct_value missing for reports_to_other
8. Route returns 404 when edge_id is unknown
9. submitted_by reflects session shape (demo / service / user)
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from credence.api import app
from credence.orgchart import corrections as corr_mod
from credence.orgchart.corrections import (
    CorrectionInput,
    CorrectionPersistError,
    EdgeNotFoundError,
    VALID_ATTRIBUTION_COMPONENTS,
    VALID_CORRECTION_TYPES,
    record_correction,
)


PERSON_A = UUID("00000000-0000-0000-0000-aaaa00000001")
PERSON_B = UUID("00000000-0000-0000-0000-bbbb00000002")
EDGE_ID = UUID("00000000-0000-0000-0000-eeee00000001")
ACCOUNT = UUID("00000000-0000-0000-0000-000000000fff")  # demo tenant


# ─── 1. CorrectionInput validation ──────────────────────────────────────────


@pytest.mark.unit
def test_correction_input_accepts_valid_type() -> None:
    inp = CorrectionInput(
        person_a_id=PERSON_A,
        correction_type="not_reports_to",
        submitted_by="demo",
    )
    assert inp.correction_type == "not_reports_to"


@pytest.mark.unit
def test_correction_input_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="correction_type must be one of"):
        CorrectionInput(
            person_a_id=PERSON_A,
            correction_type="something_made_up",
            submitted_by="demo",
        )


@pytest.mark.unit
def test_correction_input_requires_correct_value_for_reports_to_other() -> None:
    with pytest.raises(ValueError, match="requires correct_value"):
        CorrectionInput(
            person_a_id=PERSON_A,
            correction_type="reports_to_other",
            submitted_by="demo",
            correct_value=None,
        )


@pytest.mark.unit
def test_correction_input_requires_correct_value_for_team_wrong() -> None:
    with pytest.raises(ValueError, match="requires correct_value"):
        CorrectionInput(
            person_a_id=PERSON_A,
            correction_type="team_wrong",
            submitted_by="demo",
        )


@pytest.mark.unit
def test_correction_input_rejects_empty_submitted_by() -> None:
    with pytest.raises(ValueError, match="submitted_by"):
        CorrectionInput(
            person_a_id=PERSON_A,
            correction_type="not_reports_to",
            submitted_by="   ",
        )


@pytest.mark.unit
def test_valid_correction_types_keyspace_matches_a0_migration() -> None:
    """The 4 values must match the A0 schema CHECK constraint exactly."""
    assert VALID_CORRECTION_TYPES == frozenset({
        "not_reports_to", "reports_to_other", "are_peers", "team_wrong",
    })


# ─── 1b. component_attributions validation ──────────────────────────────────


@pytest.mark.unit
def test_component_attributions_defaults_to_none() -> None:
    inp = CorrectionInput(
        person_a_id=PERSON_A,
        correction_type="not_reports_to",
        submitted_by="demo",
    )
    assert inp.component_attributions is None


@pytest.mark.unit
def test_component_attributions_accepts_full_seven_component_dict() -> None:
    full = {key: 0.5 for key in VALID_ATTRIBUTION_COMPONENTS}
    inp = CorrectionInput(
        person_a_id=PERSON_A,
        correction_type="not_reports_to",
        submitted_by="demo",
        component_attributions=full,
    )
    assert inp.component_attributions == full
    assert len(inp.component_attributions) == 7


@pytest.mark.unit
def test_component_attributions_accepts_partial_dict() -> None:
    partial = {"seniority_gap": 0.8, "domain_match": 0.2}
    inp = CorrectionInput(
        person_a_id=PERSON_A,
        correction_type="not_reports_to",
        submitted_by="demo",
        component_attributions=partial,
    )
    assert inp.component_attributions == partial


@pytest.mark.unit
def test_component_attributions_accepts_empty_dict() -> None:
    inp = CorrectionInput(
        person_a_id=PERSON_A,
        correction_type="not_reports_to",
        submitted_by="demo",
        component_attributions={},
    )
    assert inp.component_attributions == {}


@pytest.mark.unit
def test_component_attributions_accepts_boundary_values() -> None:
    """0.0 and 1.0 are inclusive bounds."""
    inp = CorrectionInput(
        person_a_id=PERSON_A,
        correction_type="not_reports_to",
        submitted_by="demo",
        component_attributions={"seniority_gap": 0.0, "domain_match": 1.0},
    )
    assert inp.component_attributions["seniority_gap"] == 0.0
    assert inp.component_attributions["domain_match"] == 1.0


@pytest.mark.unit
def test_component_attributions_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="unknown keys.*made_up_signal"):
        CorrectionInput(
            person_a_id=PERSON_A,
            correction_type="not_reports_to",
            submitted_by="demo",
            component_attributions={"made_up_signal": 0.5},
        )


@pytest.mark.unit
def test_component_attributions_rejects_unknown_key_among_valid() -> None:
    with pytest.raises(ValueError, match="unknown keys"):
        CorrectionInput(
            person_a_id=PERSON_A,
            correction_type="not_reports_to",
            submitted_by="demo",
            component_attributions={"seniority_gap": 0.5, "bogus": 0.3},
        )


@pytest.mark.unit
@pytest.mark.parametrize("bad_value", ["high", None, [0.5], {"nested": 1}])
def test_component_attributions_rejects_non_numeric_value(bad_value: Any) -> None:
    with pytest.raises(ValueError, match="must be a number"):
        CorrectionInput(
            person_a_id=PERSON_A,
            correction_type="not_reports_to",
            submitted_by="demo",
            component_attributions={"seniority_gap": bad_value},
        )


@pytest.mark.unit
def test_component_attributions_rejects_value_below_zero() -> None:
    with pytest.raises(ValueError, match=r"must be in \[0, 1\]"):
        CorrectionInput(
            person_a_id=PERSON_A,
            correction_type="not_reports_to",
            submitted_by="demo",
            component_attributions={"seniority_gap": -0.01},
        )


@pytest.mark.unit
def test_component_attributions_rejects_value_above_one() -> None:
    with pytest.raises(ValueError, match=r"must be in \[0, 1\]"):
        CorrectionInput(
            person_a_id=PERSON_A,
            correction_type="not_reports_to",
            submitted_by="demo",
            component_attributions={"domain_match": 1.5},
        )


@pytest.mark.unit
@pytest.mark.parametrize("bad_type", [["seniority_gap"], "seniority_gap", 42, 0.5])
def test_component_attributions_rejects_non_dict_type(bad_type: Any) -> None:
    with pytest.raises(ValueError, match="must be a dict"):
        CorrectionInput(
            person_a_id=PERSON_A,
            correction_type="not_reports_to",
            submitted_by="demo",
            component_attributions=bad_type,
        )


@pytest.mark.unit
def test_valid_attribution_components_keyspace() -> None:
    """The 7 keys must match the migration CHECK constraint exactly."""
    assert VALID_ATTRIBUTION_COMPONENTS == frozenset({
        "seniority_gap", "domain_match", "subdomain_match", "manager_title",
        "span_capacity", "patent_cluster", "geographic_scope",
    })


# ─── 2-4. record_correction ─────────────────────────────────────────────────


@pytest.fixture
def stub_corr_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "edge_present": True,
        "edge_account_id": ACCOUNT,
        "edge_inference_method": "implicit_scoring",
        "person_present": True,
        "person_account_id": ACCOUNT,
        "writes": [],
        "missing_edge": False,
        "missing_person": False,
    }

    async def fake_fetchrow(sql: str, *args: Any) -> Any:
        sql_norm = " ".join(sql.split()).upper()
        if "FROM ORG_REPORTING_EDGES WHERE ID" in sql_norm:
            if state["missing_edge"]:
                return None
            return {
                "account_id": state["edge_account_id"],
                "inference_method": state["edge_inference_method"],
            }
        if "FROM PERSONS WHERE ID" in sql_norm:
            if state["missing_person"]:
                return None
            return {"account_id": state["person_account_id"]}
        return None

    class _FakeConn:
        async def fetchrow(self, sql: str, *args: Any) -> dict:
            new_id = uuid4()
            state["writes"].append({
                "id": new_id,
                "account_id": args[0],
                "person_a_id": args[1],
                "person_b_id": args[2],
                "edge_id": args[3],
                "correction_type": args[4],
                "correct_value": args[5],
                "submitted_by": args[6],
                "inference_method": args[7],
            })
            return {"id": new_id}

    class _AcquireCtx:
        async def __aenter__(self): return _FakeConn()
        async def __aexit__(self, *_a): return None

    monkeypatch.setattr(corr_mod, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(corr_mod, "acquire", lambda: _AcquireCtx())
    return state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_correction_with_edge_resolves_account_and_method(stub_corr_db) -> None:
    inp = CorrectionInput(
        person_a_id=PERSON_A,
        person_b_id=PERSON_B,
        edge_id=EDGE_ID,
        correction_type="not_reports_to",
        submitted_by="user:test",
    )
    correction_id = await record_correction(inp)
    assert correction_id is not None
    assert len(stub_corr_db["writes"]) == 1
    write = stub_corr_db["writes"][0]
    assert write["account_id"] == ACCOUNT
    assert write["person_a_id"] == PERSON_A
    assert write["person_b_id"] == PERSON_B
    assert write["edge_id"] == EDGE_ID
    assert write["correction_type"] == "not_reports_to"
    assert write["submitted_by"] == "user:test"
    assert write["inference_method"] == "implicit_scoring"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_correction_without_edge_falls_back_to_person_account(stub_corr_db) -> None:
    other_account = UUID("00000000-0000-0000-0000-000000000001")
    stub_corr_db["person_account_id"] = other_account

    inp = CorrectionInput(
        person_a_id=PERSON_A,
        correction_type="are_peers",
        submitted_by="demo",
    )
    await record_correction(inp)
    write = stub_corr_db["writes"][0]
    assert write["account_id"] == other_account
    assert write["edge_id"] is None
    assert write["inference_method"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_correction_raises_edge_not_found(stub_corr_db) -> None:
    stub_corr_db["missing_edge"] = True
    inp = CorrectionInput(
        person_a_id=PERSON_A,
        edge_id=EDGE_ID,
        correction_type="not_reports_to",
        submitted_by="demo",
    )
    with pytest.raises(EdgeNotFoundError, match="not found"):
        await record_correction(inp)
    assert stub_corr_db["writes"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_correction_raises_person_not_found(stub_corr_db) -> None:
    stub_corr_db["missing_person"] = True
    inp = CorrectionInput(
        person_a_id=PERSON_A,
        correction_type="not_reports_to",
        submitted_by="demo",
    )
    with pytest.raises(CorrectionPersistError, match="person.*not found"):
        await record_correction(inp)


# ─── 5-9. POST /orgchart/correction route ──────────────────────────────────


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Credence-Demo": "true"},
    ) as c:
        yield c


@pytest.mark.unit
async def test_post_correction_happy_path(client, stub_corr_db) -> None:
    resp = await client.post(
        "/orgchart/correction",
        json={
            "person_a_id": str(PERSON_A),
            "person_b_id": str(PERSON_B),
            "edge_id": str(EDGE_ID),
            "correction_type": "not_reports_to",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "correction_id" in body
    assert UUID(body["correction_id"])  # parses
    assert len(stub_corr_db["writes"]) == 1
    # Demo session → submitted_by = 'demo'
    assert stub_corr_db["writes"][0]["submitted_by"] == "demo"


@pytest.mark.unit
async def test_post_correction_400_on_invalid_type(client, stub_corr_db) -> None:
    resp = await client.post(
        "/orgchart/correction",
        json={
            "person_a_id": str(PERSON_A),
            "correction_type": "made_up_type",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_correction"
    assert stub_corr_db["writes"] == []


@pytest.mark.unit
async def test_post_correction_400_when_value_missing_for_reports_to_other(
    client, stub_corr_db,
) -> None:
    resp = await client.post(
        "/orgchart/correction",
        json={
            "person_a_id": str(PERSON_A),
            "correction_type": "reports_to_other",
            # no correct_value
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_correction"
    assert stub_corr_db["writes"] == []


@pytest.mark.unit
async def test_post_correction_404_on_unknown_edge(client, stub_corr_db) -> None:
    stub_corr_db["missing_edge"] = True
    resp = await client.post(
        "/orgchart/correction",
        json={
            "person_a_id": str(PERSON_A),
            "edge_id": str(EDGE_ID),
            "correction_type": "not_reports_to",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "edge_not_found"


@pytest.mark.unit
async def test_post_correction_succeeds_without_edge_id(client, stub_corr_db) -> None:
    """Caller can submit a correction without naming a specific edge."""
    resp = await client.post(
        "/orgchart/correction",
        json={
            "person_a_id": str(PERSON_A),
            "person_b_id": str(PERSON_B),
            "correction_type": "are_peers",
        },
    )
    assert resp.status_code == 200, resp.text
    write = stub_corr_db["writes"][0]
    assert write["edge_id"] is None
    assert write["inference_method"] is None
