"""Pydantic models — mirror src/lib/database.types.ts and src/lib/graph.ts.

These shapes are the contract with the frontend. Keep field names identical
to the TS side; the frontend can `JSON.parse` server responses straight into
its existing types.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ─── Domain ──────────────────────────────────────────────────────────────────


class Prospect(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    name: str
    company: str
    role: str
    industry: str
    linkedin_url: str | None = None
    created_at: datetime
    updated_at: datetime


class Education(BaseModel):
    school: str
    degree: str | None = None
    year: int | None = None


class Talk(BaseModel):
    venue: str
    year: int | None = None
    topic: str | None = None


class CareerStint(BaseModel):
    company: str
    title: str | None = None
    start_year: int | None = None
    end_year: int | None = None


class ProspectEnriched(Prospect):
    """Prospect + roll-ups from signals. Read from `prospects_enriched` view."""

    past_companies: list[str] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    talks: list[Talk] = Field(default_factory=list)
    career_history: list[CareerStint] = Field(default_factory=list)


class Signal(BaseModel):
    id: UUID
    prospect_id: UUID
    source: str
    signal_type: str
    value: Any
    raw_data: Any | None = None
    weight: float = 1.0
    confidence: float
    collected_at: datetime


class Score(BaseModel):
    id: UUID
    prospect_id: UUID
    authenticity_score: float
    authority_score: float
    warmth_score: float
    overall_score: float
    falsification_notes: list[str] = Field(default_factory=list)
    computed_at: datetime


class SignalWeight(BaseModel):
    id: UUID
    signal_type: str
    authenticity_weight: float
    authority_weight: float
    warmth_weight: float


# ─── Graph (mirrors src/lib/graph.ts) ────────────────────────────────────────

NodeKind = Literal[
    "person",
    "company",
    "role",
    "city",
    "school",
    "conference",
    "industry",
    "past_employer",
    "partnership",
]

EdgeKind = Literal[
    "works_at",
    "colleague",
    "reports_to",
    "located_in",
    "past_employer",
    "partnership",
    "education",
    "scope_signal",
    "vertical",
    "evidence_cited",
]


class GraphNode(BaseModel):
    id: str
    kind: NodeKind
    name: str
    score: float | None = None
    confidence: float | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    kind: EdgeKind
    weight: float | None = None


# ─── Chat ────────────────────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    snapshot: dict[str, Any] | None = None  # current visible-set, selectedId, etc.


class ToolResult(BaseModel):
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class ChatResponse(BaseModel):
    messages: list[ChatMessage]
    tool_results: list[ToolResult] = Field(default_factory=list)
