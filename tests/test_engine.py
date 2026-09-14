"""Engine-level tests: exact rational provenance through the edit graph."""

from fractions import Fraction

import pytest

from app.engine import GraphError, analyze, validate_submission
from app.models import EditSubmission


def make_edit(nodes, consents=None):
    return EditSubmission.model_validate({"nodes": nodes, "consents": consents or []})


def consent(source, start, end, audiences=("students",)):
    return {"source": source, "start": start, "end": end, "audiences": list(audiences)}


# ---------------------------------------------------------------------------
# Nested transforms and fractional arithmetic
# ---------------------------------------------------------------------------

def nested_edit(consent_segments):
    return make_edit(
        nodes=[
            {"type": "source", "id": "s", "duration": 100, "contributors": ["amy"]},
            {"type": "trim", "id": "t", "child": "s", "start": 10, "end": 70},
            {"type": "speed", "id": "v", "child": "t", "p": 2, "q": 3},
            {"type": "concat", "id": "c", "children": ["v", "s"]},
        ],
        consents=[consent("s", a, b) for a, b in consent_segments],
    )


def test_nested_trim_speed_exact_fractions():
    sub = nested_edit([(10, 70)])
    durations = validate_submission(sub)
    assert durations["t"] == 60
    assert durations["v"] == 90  # 60 * 3/2
    assert durations["c"] == 190

    report = analyze(sub, "c", "students")
    assert report["output_duration"] == 190
    first, second = report["segments"]

    # speed 2/3: output t -> input (2/3) t + 10 over [0, 90) -> [10, 70)
    assert first["out"] == {"start": 0, "end": 90}
    assert first["licensed"] is True
    assert first["sources"][0]["maps"] == [{"ratio": "2/3", "offset": 10}]
    assert first["sources"][0]["used"] == [{"start": 10, "end": 70}]

    # raw source appended at 90: uses [0, 100), consent only covers [10, 70)
    assert second["out"] == {"start": 90, "end": 190}
    assert second["sources"][0]["used"] == [{"start": 0, "end": 100}]
    assert second["sources"][0]["uncovered"] == [
        {"start": 0, "end": 10},
        {"start": 70, "end": 100},
    ]
    assert report["decision"] == "rejected"
    assert report["violation"]["out"] == {"start": 90, "end": 190}
    assert report["violation"]["unlicensed"] == [
        {
            "source": "s",
            "contributors": ["amy"],
            "uncovered": [{"start": 0, "end": 10}, {"start": 70, "end": 100}],
        }
    ]


def test_nested_fully_licensed_is_approved():
    report = analyze(nested_edit([(0, 100)]), "c", "students")
    assert report["decision"] == "approved"
    assert report["violation"] is None
    assert all(seg["licensed"] for seg in report["segments"])


def test_fractional_duration_and_concat_offset():
    sub = make_edit(
        nodes=[
            {"type": "source", "id": "s", "duration": 5},
            {"type": "speed", "id": "v", "child": "s", "p": 2, "q": 1},
            {"type": "concat", "id": "c", "children": ["v", "s"]},
        ],
        consents=[consent("s", 0, 5)],
    )
    report = analyze(sub, "c", "students")
    assert report["decision"] == "approved"
    assert report["output_duration"] == "15/2"
    first, second = report["segments"]
    assert first["out"] == {"start": 0, "end": "5/2"}
    assert first["sources"][0]["maps"] == [{"ratio": 2, "offset": 0}]
    assert first["sources"][0]["used"] == [{"start": 0, "end": 5}]
    # second half is the raw source shifted by a fractional 5/2 ticks
    assert second["out"] == {"start": "5/2", "end": "15/2"}
    assert second["sources"][0]["maps"] == [{"ratio": 1, "offset": "-5/2"}]
    assert second["sources"][0]["used"] == [{"start": 0, "end": 5}]


def test_slowdown_speed_maps_output_to_half_input():
    sub = make_edit(
        nodes=[
            {"type": "source", "id": "s", "duration": 10},
            {"type": "speed", "id": "v", "child": "s", "p": 1, "q": 2},
        ],
        consents=[consent("s", 0, 10)],
    )
    report = analyze(sub, "v", "students")
    assert report["output_duration"] == 20
    assert report["segments"][0]["sources"][0]["maps"] == [{"ratio": "1/2", "offset": 0}]
    assert report["segments"][0]["sources"][0]["used"] == [{"start": 0, "end": 10}]
    assert report["decision"] == "approved"


# ---------------------------------------------------------------------------
# Mix overlaps
# ---------------------------------------------------------------------------

def test_mix_overlap_splits_segments_at_boundaries():
    sub = make_edit(
        nodes=[
            {"type": "source", "id": "a", "duration": 10, "contributors": ["amy"]},
            {"type": "source", "id": "b", "duration": 4, "contributors": ["bo"]},
            {"type": "mix", "id": "m",
             "children": [{"node": "a", "offset": 0}, {"node": "b", "offset": 6}]},
        ],
        consents=[consent("a", 0, 10), consent("b", 0, 2)],
    )
    report = analyze(sub, "m", "students")
    assert [s["out"] for s in report["segments"]] == [
        {"start": 0, "end": 6},
        {"start": 6, "end": 10},
    ]
    solo, overlap = report["segments"]
    assert solo["licensed"] is True
    assert [s["source"] for s in solo["sources"]] == ["a"]
    assert [s["source"] for s in overlap["sources"]] == ["a", "b"]
    assert overlap["licensed"] is False
    # b is shifted by 6: output [6, 10) consumes b[0, 4), consent covers [0, 2)
    assert report["decision"] == "rejected"
    assert report["violation"]["out"] == {"start": 6, "end": 10}
    assert report["violation"]["unlicensed"] == [
        {"source": "b", "contributors": ["bo"], "uncovered": [{"start": 2, "end": 4}]}
    ]


def test_mix_leading_offset_creates_silent_segment():
    sub = make_edit(
        nodes=[
            {"type": "source", "id": "a", "duration": 5},
            {"type": "mix", "id": "m", "children": [{"node": "a", "offset": 3}]},
        ],
        consents=[consent("a", 0, 5)],
    )
    report = analyze(sub, "m", "students")
    assert report["output_duration"] == 8
    assert report["segments"][0] == {
        "out": {"start": 0, "end": 3},
        "licensed": True,
        "sources": [],
    }
    assert report["segments"][1]["out"] == {"start": 3, "end": 8}
    assert report["decision"] == "approved"


def test_same_source_mixed_twice_unions_used_intervals():
    sub = make_edit(
        nodes=[
            {"type": "source", "id": "s", "duration": 10, "contributors": ["amy"]},
            {"type": "mix", "id": "m",
             "children": [{"node": "s", "offset": 0}, {"node": "s", "offset": 5}]},
        ],
        consents=[consent("s", 2, 10)],
    )
    report = analyze(sub, "m", "students")
    assert report["output_duration"] == 15
    assert [s["out"] for s in report["segments"]] == [
        {"start": 0, "end": 5},
        {"start": 5, "end": 10},
        {"start": 10, "end": 15},
    ]
    mid = report["segments"][1]
    # echo overlap: output [5, 10) consumes s[0, 5) and s[5, 10) -> union [0, 10)
    assert mid["sources"][0]["used"] == [{"start": 0, "end": 10}]
    assert mid["sources"][0]["uncovered"] == [{"start": 0, "end": 2}]
    # earliest violating segment wins
    assert report["violation"]["out"] == {"start": 0, "end": 5}


# ---------------------------------------------------------------------------
# Half-open endpoints
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "consent_segments,expected_uncovered",
    [
        ([(0, 50)], []),                          # exact coverage, end exclusive
        ([(0, 25), (25, 50)], []),                # adjacent half-open ranges join
        ([(0, 49)], [(49, 50)]),                  # last tick uncovered
        ([(1, 50)], [(0, 1)]),                    # first tick uncovered
        ([(10, 40)], [(0, 10), (40, 50)]),        # hole in the middle
    ],
)
def test_half_open_endpoints(consent_segments, expected_uncovered):
    sub = make_edit(
        nodes=[{"type": "source", "id": "s", "duration": 50}],
        consents=[consent("s", a, b) for a, b in consent_segments],
    )
    report = analyze(sub, "s", "students")
    seg = report["segments"][0]
    assert seg["out"] == {"start": 0, "end": 50}
    assert seg["sources"][0]["used"] == [{"start": 0, "end": 50}]
    assert seg["sources"][0]["uncovered"] == [
        {"start": a, "end": b} for a, b in expected_uncovered
    ]
    assert report["decision"] == ("approved" if not expected_uncovered else "rejected")


def test_trim_endpoints_are_half_open():
    sub = make_edit(
        nodes=[
            {"type": "source", "id": "s", "duration": 100},
            {"type": "trim", "id": "t", "child": "s", "start": 10, "end": 50},
        ],
        consents=[consent("s", 10, 50)],
    )
    report = analyze(sub, "t", "students")
    assert report["output_duration"] == 40
    assert report["segments"][0]["sources"][0]["used"] == [{"start": 10, "end": 50}]
    assert report["decision"] == "approved"


def test_audience_must_match():
    sub = make_edit(
        nodes=[{"type": "source", "id": "s", "duration": 10}],
        consents=[consent("s", 0, 10, audiences=("staff",))],
    )
    assert analyze(sub, "s", "staff")["decision"] == "approved"
    rejected = analyze(sub, "s", "students")
    assert rejected["decision"] == "rejected"
    assert rejected["violation"]["unlicensed"][0]["uncovered"] == [
        {"start": 0, "end": 10}
    ]


# ---------------------------------------------------------------------------
# Segment merging and violation ordering
# ---------------------------------------------------------------------------

def test_adjacent_segments_merge_only_when_maps_match():
    # concat of two adjacent trims of the same source collapses to one segment
    merged = make_edit(
        nodes=[
            {"type": "source", "id": "s", "duration": 10},
            {"type": "trim", "id": "t1", "child": "s", "start": 0, "end": 5},
            {"type": "trim", "id": "t2", "child": "s", "start": 5, "end": 10},
            {"type": "concat", "id": "c", "children": ["t1", "t2"]},
        ],
        consents=[consent("s", 0, 10)],
    )
    report = analyze(merged, "c", "students")
    assert len(report["segments"]) == 1
    assert report["segments"][0]["out"] == {"start": 0, "end": 10}
    assert report["segments"][0]["sources"][0]["maps"] == [{"ratio": 1, "offset": 0}]

    # concat of the same source twice keeps two segments (maps differ)
    kept = make_edit(
        nodes=[
            {"type": "source", "id": "s", "duration": 10},
            {"type": "concat", "id": "c", "children": ["s", "s"]},
        ],
        consents=[consent("s", 0, 10)],
    )
    report = analyze(kept, "c", "students")
    assert [s["out"] for s in report["segments"]] == [
        {"start": 0, "end": 10},
        {"start": 10, "end": 20},
    ]


def test_violation_is_earliest_segment():
    sub = make_edit(
        nodes=[
            {"type": "source", "id": "s", "duration": 10, "contributors": ["amy"]},
            {"type": "concat", "id": "c", "children": ["s", "s"]},
        ],
        consents=[consent("s", 2, 8)],
    )
    report = analyze(sub, "c", "students")
    assert report["decision"] == "rejected"
    assert report["violation"]["out"] == {"start": 0, "end": 10}
    assert report["violation"]["unlicensed"][0]["uncovered"] == [
        {"start": 0, "end": 2},
        {"start": 8, "end": 10},
    ]


# ---------------------------------------------------------------------------
# Bad graphs
# ---------------------------------------------------------------------------

def test_cycle_error_is_locatable():
    sub = make_edit(
        nodes=[
            {"type": "trim", "id": "x", "child": "y", "start": 0, "end": 1},
            {"type": "trim", "id": "y", "child": "x", "start": 0, "end": 1},
        ]
    )
    with pytest.raises(GraphError) as excinfo:
        validate_submission(sub)
    assert "cycle" in excinfo.value.msg
    assert excinfo.value.loc[0] == "nodes"


def test_dangling_reference_is_locatable():
    sub = make_edit(
        nodes=[
            {"type": "source", "id": "a", "duration": 10},
            {"type": "trim", "id": "t", "child": "ghost", "start": 0, "end": 5},
        ]
    )
    with pytest.raises(GraphError) as excinfo:
        validate_submission(sub)
    assert "ghost" in excinfo.value.msg
    assert excinfo.value.loc == ["nodes", 1, "child"]


def test_trim_out_of_bounds_is_locatable():
    sub = make_edit(
        nodes=[
            {"type": "source", "id": "a", "duration": 10},
            {"type": "trim", "id": "t", "child": "a", "start": 0, "end": 11},
        ]
    )
    with pytest.raises(GraphError) as excinfo:
        validate_submission(sub)
    assert "exceeds" in excinfo.value.msg
    assert excinfo.value.loc == ["nodes", 1, "end"]


def test_analysis_output_must_exist():
    sub = make_edit(nodes=[{"type": "source", "id": "a", "duration": 10}])
    with pytest.raises(GraphError) as excinfo:
        analyze(sub, "ghost", "students")
    assert excinfo.value.loc == ["output"]
