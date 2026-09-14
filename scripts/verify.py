"""One-shot verification for the podcast club.

Submits the club's full episode edit, asks the analysis endpoint whether the
whole program is licensed for the target audience, and reads the stored
snapshot back. Exits non-zero unless the decision is "approved".
"""

import json
import os
import sys
import urllib.request

API_URL = os.environ.get("API_URL", "http://localhost:8000")
AUDIENCE = os.environ.get("AUDIENCE", "students")

# The full episode: licensed intro music, a trimmed + 1.25x interview whose
# used range [60, 540) is covered by the guest's consent, and an ambience bed
# mixed in at tick 100.
EPISODE_EDIT = {
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


def call(method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        API_URL + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def main() -> int:
    created = call("POST", "/edits", EPISODE_EDIT)
    edit_id = created["edit_id"]
    print(f"edit stored as {edit_id}")
    print(f"node durations: {json.dumps(created['durations'])}")

    report = call(
        "POST",
        f"/edits/{edit_id}/analyses",
        {"output": "episode", "audience": AUDIENCE},
    )
    print(
        f"analysis {report['analysis_id']}: decision={report['decision']} "
        f"duration={report['output_duration']} segments={len(report['segments'])}"
    )

    snapshot = call("GET", f"/edits/{edit_id}/analyses/{report['analysis_id']}")
    assert snapshot["report"]["decision"] == report["decision"], "snapshot mismatch"

    if report["decision"] != "approved":
        print(json.dumps(report["violation"], indent=2, ensure_ascii=False))
        print(f"VERIFY FAILED: episode is not fully licensed for '{AUDIENCE}'")
        return 1
    print(f"VERIFY OK: full episode licensed for audience '{AUDIENCE}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
