"""API-level tests: contracts, persistence, locatable 422s, snapshots."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def episode_edit():
    """A full episode: intro + trimmed/sped-up interview + ambience bed."""
    return {
        "title": "episode-12",
        "nodes": [
            {"type": "source", "id": "intro", "duration": 120,
             "contributors": ["intro-music"]},
            {"type": "source", "id": "interview", "duration": 600,
             "contributors": ["guest", "host"]},
            {"type": "trim", "id": "itrim", "child": "interview",
             "start": 60, "end": 540},
            {"type": "speed", "id": "isped", "child": "itrim", "p": 5, "q": 4},
            {"type": "concat", "id": "body", "children": ["intro", "isped"]},
            {"type": "source", "id": "ambience", "duration": 300,
             "contributors": ["field-rec"]},
            {"type": "mix", "id": "episode",
             "children": [{"node": "body", "offset": 0},
                          {"node": "ambience", "offset": 100}]},
        ],
        "consents": [
            {"source": "intro", "start": 0, "end": 120, "audiences": ["students"]},
            {"source": "interview", "start": 60, "end": 540,
             "audiences": ["students"]},
            {"source": "ambience", "start": 0, "end": 300, "audiences": ["students"]},
        ],
    }


# ---------------------------------------------------------------------------
# Happy path: submit -> analyze -> snapshot read-back
# ---------------------------------------------------------------------------

def test_full_flow_approved_and_snapshot_persisted(client):
    created = client.post("/edits", json=episode_edit())
    assert created.status_code == 201
    edit_id = created.json()["edit_id"]
    # intro 120 + interview 480 * 4/5 = 384 -> body 504; ambience ends at 400
    assert created.json()["durations"]["episode"] == 504
    assert created.json()["durations"]["isped"] == 384

    analyzed = client.post(
        f"/edits/{edit_id}/analyses",
        json={"output": "episode", "audience": "students"},
    )
    assert analyzed.status_code == 201
    report = analyzed.json()
    assert report["decision"] == "approved"
    assert report["violation"] is None
    assert report["output_duration"] == 504
    # mix boundaries at 100 (ambience in), 120 (intro out), 400 (ambience out)
    assert [s["out"] for s in report["segments"]] == [
        {"start": 0, "end": 100},
        {"start": 100, "end": 120},
        {"start": 120, "end": 400},
        {"start": 400, "end": 504},
    ]
    assert all(s["licensed"] for s in report["segments"])

    # snapshot is stored and reads back identically
    analysis_id = report["analysis_id"]
    snapshot = client.get(f"/edits/{edit_id}/analyses/{analysis_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["report"] == report
    assert snapshot.json()["request"] == {"output": "episode", "audience": "students"}

    summaries = client.get(f"/edits/{edit_id}/analyses").json()
    assert [s["decision"] for s in summaries] == ["approved"]


def test_get_edit_roundtrip(client):
    payload = episode_edit()
    edit_id = client.post("/edits", json=payload).json()["edit_id"]
    got = client.get(f"/edits/{edit_id}")
    assert got.status_code == 200
    assert got.json()["edit"]["title"] == "episode-12"
    assert got.json()["edit"]["nodes"] == payload["nodes"]
    listed = client.get("/edits").json()
    assert [e["edit_id"] for e in listed] == [edit_id]
    assert listed[0]["node_count"] == len(payload["nodes"])


def test_rejected_report_lists_unlicensed_sorted_by_source(client):
    edit = {
        "nodes": [
            {"type": "source", "id": "zeta", "duration": 10,
             "contributors": ["zoe"]},
            {"type": "source", "id": "alpha", "duration": 10,
             "contributors": ["ann", "amy"]},
            {"type": "mix", "id": "m",
             "children": [{"node": "zeta"}, {"node": "alpha"}]},
        ],
        "consents": [],
    }
    edit_id = client.post("/edits", json=edit).json()["edit_id"]
    report = client.post(
        f"/edits/{edit_id}/analyses", json={"output": "m", "audience": "students"}
    ).json()
    assert report["decision"] == "rejected"
    violation = report["violation"]
    assert violation["out"] == {"start": 0, "end": 10}
    assert [u["source"] for u in violation["unlicensed"]] == ["alpha", "zeta"]
    assert violation["unlicensed"][0]["contributors"] == ["ann", "amy"]
    assert violation["unlicensed"][0]["uncovered"] == [{"start": 0, "end": 10}]


# ---------------------------------------------------------------------------
# Locatable 422s; a rejected submission is never stored
# ---------------------------------------------------------------------------

BAD_EDITS = [
    ({"nodes": [
        {"type": "trim", "id": "x", "child": "y", "start": 0, "end": 1},
        {"type": "trim", "id": "y", "child": "x", "start": 0, "end": 1}]},
     "cycle"),
    ({"nodes": [{"type": "concat", "id": "c", "children": ["c"]}]},
     "cycle"),
    ({"nodes": [
        {"type": "source", "id": "a", "duration": 10},
        {"type": "trim", "id": "t", "child": "ghost", "start": 0, "end": 5}]},
     "unknown node"),
    ({"nodes": [
        {"type": "source", "id": "a", "duration": 10},
        {"type": "trim", "id": "t", "child": "a", "start": 0, "end": 11}]},
     "exceeds"),
    ({"nodes": [
        {"type": "source", "id": "a", "duration": 10},
        {"type": "source", "id": "a", "duration": 5}]},
     "duplicate"),
    ({"nodes": [{"type": "source", "id": "a", "duration": 10}],
      "consents": [{"source": "a", "start": 0, "end": 11, "audiences": ["students"]}]},
     "exceeds"),
    ({"nodes": [{"type": "source", "id": "a", "duration": 10}],
      "consents": [{"source": "ghost", "start": 0, "end": 5,
                    "audiences": ["students"]}]},
     "unknown source"),
    ({"nodes": [
        {"type": "source", "id": "a", "duration": 10},
        {"type": "trim", "id": "t", "child": "a", "start": 0, "end": 5}],
      "consents": [{"source": "t", "start": 0, "end": 5, "audiences": ["students"]}]},
     "not a source"),
]


@pytest.mark.parametrize("payload,needle", BAD_EDITS)
def test_bad_edits_422_locatable_and_not_saved(client, payload, needle):
    response = client.post("/edits", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert needle in detail["msg"]
    assert detail["loc"][0] == "body"
    assert client.get("/edits").json() == []  # whole submission rejected


@pytest.mark.parametrize(
    "node",
    [
        {"type": "speed", "id": "v", "child": "a", "p": 0, "q": 1},
        {"type": "speed", "id": "v", "child": "a", "p": 1, "q": 0},
        {"type": "speed", "id": "v", "child": "a", "p": -2, "q": 1},
        {"type": "source", "id": "v", "duration": 0},
        {"type": "trim", "id": "v", "child": "a", "start": 5, "end": 5},
        {"type": "trim", "id": "v", "child": "a", "start": -1, "end": 5},
        {"type": "trim", "id": "v", "child": "a", "start": 0, "end": 3.5},
    ],
)
def test_field_validation_422_and_not_saved(client, node):
    payload = {"nodes": [{"type": "source", "id": "a", "duration": 10}, node]}
    response = client.post("/edits", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][0] == "body"
    assert client.get("/edits").json() == []


@pytest.mark.parametrize(
    "node",
    [
        # strings and floats must not be silently coerced to integer ticks
        {"type": "source", "id": "v", "duration": "100"},
        {"type": "source", "id": "v", "duration": 100.0},
        {"type": "source", "id": "v", "duration": True},
        {"type": "trim", "id": "v", "child": "a", "start": "0", "end": 5},
        {"type": "trim", "id": "v", "child": "a", "start": 0, "end": 5.0},
        {"type": "speed", "id": "v", "child": "a", "p": "2", "q": 1},
        {"type": "speed", "id": "v", "child": "a", "p": 2, "q": 1.0},
        {"type": "mix", "id": "v", "children": [{"node": "a", "offset": "3"}]},
        {"type": "mix", "id": "v", "children": [{"node": "a", "offset": 1.5}]},
    ],
)
def test_non_integer_ticks_rejected_not_coerced(client, node):
    payload = {"nodes": [{"type": "source", "id": "a", "duration": 10}, node]}
    response = client.post("/edits", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][0] == "body"
    assert client.get("/edits").json() == []


def test_non_integer_consent_ticks_rejected(client):
    payload = {
        "nodes": [{"type": "source", "id": "a", "duration": 10}],
        "consents": [{"source": "a", "start": "0", "end": 5,
                      "audiences": ["students"]}],
    }
    assert client.post("/edits", json=payload).status_code == 422
    payload["consents"][0]["start"] = 0
    payload["consents"][0]["end"] = 5.0
    assert client.post("/edits", json=payload).status_code == 422
    assert client.get("/edits").json() == []


def test_analysis_unknown_output_422_and_not_saved(client):
    edit_id = client.post(
        "/edits", json={"nodes": [{"type": "source", "id": "a", "duration": 10}]}
    ).json()["edit_id"]
    response = client.post(
        f"/edits/{edit_id}/analyses", json={"output": "ghost", "audience": "students"}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "output"]
    assert client.get(f"/edits/{edit_id}/analyses").json() == []


def test_404s(client):
    assert client.get("/edits/nope").status_code == 404
    assert client.post(
        "/edits/nope/analyses", json={"output": "a", "audience": "s"}
    ).status_code == 404
    edit_id = client.post(
        "/edits", json={"nodes": [{"type": "source", "id": "a", "duration": 1}]}
    ).json()["edit_id"]
    assert client.get(f"/edits/{edit_id}/analyses/nope").status_code == 404
