"""Request/response contracts (Pydantic v2).

Edit documents are DAGs of typed nodes. All durations, endpoints and
offsets are non-negative integer ticks; durations are strictly positive.
All intervals are half-open [start, end).
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, NonNegativeInt, PositiveInt, model_validator


# ---------------------------------------------------------------------------
# Edit graph nodes (discriminated union on "type")
# ---------------------------------------------------------------------------

class SourceNode(BaseModel):
    """Raw recording: declares its duration and its contributors."""

    type: Literal["source"]
    id: str = Field(min_length=1)
    duration: PositiveInt
    contributors: list[str] = Field(default_factory=list)


class TrimNode(BaseModel):
    """Keeps the child's half-open range [start, end); output restarts at 0."""

    type: Literal["trim"]
    id: str = Field(min_length=1)
    child: str = Field(min_length=1)
    start: NonNegativeInt
    end: NonNegativeInt

    @model_validator(mode="after")
    def _non_empty_half_open(self):
        if not self.start < self.end:
            raise ValueError("trim requires start < end (half-open [start, end))")
        return self


class SpeedNode(BaseModel):
    """Rate p/q: output time t maps to input time t * p / q."""

    type: Literal["speed"]
    id: str = Field(min_length=1)
    child: str = Field(min_length=1)
    p: PositiveInt
    q: PositiveInt


class ConcatNode(BaseModel):
    """Plays children back to back in array order."""

    type: Literal["concat"]
    id: str = Field(min_length=1)
    children: list[str] = Field(min_length=1)


class MixChild(BaseModel):
    node: str = Field(min_length=1)
    offset: NonNegativeInt = 0


class MixNode(BaseModel):
    """Overlays children, each shifted by its tick offset."""

    type: Literal["mix"]
    id: str = Field(min_length=1)
    children: list[MixChild] = Field(min_length=1)


Node = Annotated[
    Union[SourceNode, TrimNode, SpeedNode, ConcatNode, MixNode],
    Field(discriminator="type"),
]


class ConsentEntry(BaseModel):
    """One half-open licensed range of one source, for the named audiences."""

    source: str = Field(min_length=1)
    start: NonNegativeInt
    end: NonNegativeInt
    audiences: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _non_empty_half_open(self):
        if not self.start < self.end:
            raise ValueError("consent segment requires start < end (half-open [start, end))")
        return self


class EditSubmission(BaseModel):
    title: str | None = None
    nodes: list[Node] = Field(min_length=1)
    consents: list[ConsentEntry] = Field(default_factory=list)


class AnalysisRequest(BaseModel):
    """Selects the single output node and the target audience."""

    output: str = Field(min_length=1)
    audience: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

# Exact rationals serialize as an int when whole, otherwise as a "p/q" string.
Rational = Union[int, str]


class EditCreated(BaseModel):
    edit_id: str
    durations: dict[str, Rational]


class EditSummary(BaseModel):
    edit_id: str
    created_at: str
    title: str | None
    node_count: int
    consent_count: int


class StoredEdit(BaseModel):
    edit_id: str
    created_at: str
    edit: EditSubmission


class IntervalOut(BaseModel):
    start: Rational
    end: Rational


class AffineMapOut(BaseModel):
    """input_time = ratio * output_time + offset for one source."""

    ratio: Rational
    offset: Rational


class SourceEvidence(BaseModel):
    source: str
    maps: list[AffineMapOut]
    used: list[IntervalOut]
    licensed: bool
    uncovered: list[IntervalOut]


class SegmentReport(BaseModel):
    out: IntervalOut
    licensed: bool
    sources: list[SourceEvidence]


class UnlicensedSource(BaseModel):
    source: str
    contributors: list[str]
    uncovered: list[IntervalOut]


class ViolationReport(BaseModel):
    out: IntervalOut
    unlicensed: list[UnlicensedSource]


class AnalysisReport(BaseModel):
    analysis_id: str
    edit_id: str
    output: str
    audience: str
    decision: Literal["approved", "rejected"]
    output_duration: Rational
    segments: list[SegmentReport]
    violation: ViolationReport | None


class AnalysisSummary(BaseModel):
    analysis_id: str
    created_at: str
    output: str
    audience: str
    decision: str


class AnalysisSnapshot(BaseModel):
    analysis_id: str
    edit_id: str
    created_at: str
    request: AnalysisRequest
    report: AnalysisReport
