"""Interval provenance engine.

Every node of the edit DAG is compiled to a list of half-open output
segments. Each segment carries, per original source, the affine maps

    input_time = ratio * output_time + offset

that tell exactly which original ticks that output interval comes from.
All arithmetic uses fractions.Fraction, so speed changes and nested
transforms never lose precision.

Node semantics (ticks, half-open intervals):
  source            declares duration + contributors; identity map
  trim  [a, b)      keeps child range [a, b), output restarts at 0
  speed p/q         output t maps to input t * p / q
  concat            children back to back in array order
  mix               children overlaid, each shifted by its offset
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


class GraphError(Exception):
    """Edit-graph validation error, locatable inside the submitted document."""

    def __init__(self, msg: str, loc: list):
        super().__init__(msg)
        self.msg = msg
        self.loc = list(loc)


@dataclass(frozen=True)
class Mapping:
    """Affine map from output time t to source input time ratio*t + offset."""

    ratio: Fraction
    offset: Fraction


@dataclass
class Segment:
    """Half-open output interval [start, end) with per-source affine maps."""

    start: Fraction
    end: Fraction
    mappings: dict[str, tuple[Mapping, ...]]


def rat_json(value: Fraction) -> int | str:
    """Encode a Fraction as an int when whole, otherwise as a "p/q" string."""
    return value.numerator if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _canon(maps: dict[str, tuple[Mapping, ...]]) -> dict[str, tuple[Mapping, ...]]:
    """Sort and deduplicate each source's maps so equality is stable."""
    return {
        sid: tuple(sorted(set(ms), key=lambda m: (m.ratio, m.offset)))
        for sid, ms in maps.items()
    }


def _combine_maps(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for sid, ms in extra.items():
        merged[sid] = tuple(merged.get(sid, ())) + tuple(ms)
    return _canon(merged)


def _remap(maps: dict, ratio: Fraction, shift: Fraction) -> dict:
    """Apply output transform t_new = (t_old - shift) * ... to every map.

    If the node's output is shifted by `shift` ticks (t_old = t_new + shift)
    and/or stretched so that t_old = t_new * ratio, then each affine map
    input = r * t_old + o becomes input = (r*ratio) * t_new + (o + r*shift).
    """
    return _canon(
        {
            sid: tuple(
                Mapping(m.ratio * ratio, m.offset + m.ratio * shift) for m in ms
            )
            for sid, ms in maps.items()
        }
    )


def _merge_adjacent(segments: list[Segment]) -> list[Segment]:
    """Merge adjacent segments whose source sets and affine maps match."""
    merged: list[Segment] = []
    for seg in sorted(segments, key=lambda s: (s.start, s.end)):
        if (
            merged
            and merged[-1].end == seg.start
            and merged[-1].mappings == seg.mappings
        ):
            merged[-1].end = seg.end
        else:
            merged.append(Segment(seg.start, seg.end, dict(seg.mappings)))
    return merged


def _sweep(shifted: list[Segment], duration: Fraction) -> list[Segment]:
    """Partition [0, duration) at every child boundary and union the maps.

    Regions no child covers become silent segments (empty source set), so
    the result always tiles the whole output duration.
    """
    points = {Fraction(0), duration}
    for seg in shifted:
        points.add(seg.start)
        points.add(seg.end)
    ordered = sorted(points)
    out: list[Segment] = []
    for a, b in zip(ordered, ordered[1:]):
        mappings: dict = {}
        for seg in shifted:
            if seg.start <= a and seg.end >= b:
                mappings = _combine_maps(mappings, seg.mappings)
        out.append(Segment(a, b, mappings))
    return _merge_adjacent(out)


class Graph:
    """Compiles edit nodes to durations and provenance segments (memoized)."""

    def __init__(self, nodes):
        self.index: dict[str, tuple[int, object]] = {}
        for i, node in enumerate(nodes):
            if node.id in self.index:
                raise GraphError(f"duplicate node id '{node.id}'", ["nodes", i, "id"])
            self.index[node.id] = (i, node)
        self.durations: dict[str, Fraction] = {}
        self.segments: dict[str, list[Segment]] = {}
        self._state: dict[str, str] = {}
        self._stack: list[str] = []

    def _loc(self, node_id: str, *extra) -> list:
        return ["nodes", self.index[node_id][0], *extra]

    def _resolve(self, ref: str, loc: list) -> None:
        if ref not in self.index:
            raise GraphError(f"reference to unknown node '{ref}'", loc)

    def compute(self, node_id: str) -> None:
        state = self._state.get(node_id)
        if state == "done":
            return
        if state == "active":
            chain = " -> ".join([*self._stack, node_id])
            raise GraphError(
                f"dependency cycle detected: {chain}", self._loc(node_id, "id")
            )
        self._state[node_id] = "active"
        self._stack.append(node_id)
        try:
            self._compute(node_id)
        finally:
            self._stack.pop()
        self._state[node_id] = "done"

    def _compute(self, node_id: str) -> None:
        _, node = self.index[node_id]
        kind = node.type

        if kind == "source":
            duration = Fraction(node.duration)
            segments = [
                Segment(
                    Fraction(0),
                    duration,
                    {node.id: (Mapping(Fraction(1), Fraction(0)),)},
                )
            ]

        elif kind == "trim":
            self._resolve(node.child, self._loc(node_id, "child"))
            self.compute(node.child)
            child_duration = self.durations[node.child]
            if Fraction(node.end) > child_duration:
                raise GraphError(
                    f"trim end {node.end} exceeds child duration {child_duration}",
                    self._loc(node_id, "end"),
                )
            a, b = Fraction(node.start), Fraction(node.end)
            duration = b - a
            segments = []
            for seg in self.segments[node.child]:
                s, e = max(seg.start, a), min(seg.end, b)
                if s < e:
                    # t_child = t_new + a
                    segments.append(
                        Segment(s - a, e - a, _remap(seg.mappings, Fraction(1), a))
                    )
            segments = _merge_adjacent(segments)

        elif kind == "speed":
            self._resolve(node.child, self._loc(node_id, "child"))
            self.compute(node.child)
            p, q = Fraction(node.p), Fraction(node.q)
            duration = self.durations[node.child] * q / p
            # t_child = t_new * p / q
            segments = _merge_adjacent(
                [
                    Segment(
                        seg.start * q / p,
                        seg.end * q / p,
                        _remap(seg.mappings, p / q, Fraction(0)),
                    )
                    for seg in self.segments[node.child]
                ]
            )

        elif kind == "concat":
            segments = []
            duration = Fraction(0)
            for k, ref in enumerate(node.children):
                self._resolve(ref, self._loc(node_id, "children", k))
                self.compute(ref)
                for seg in self.segments[ref]:
                    # t_child = t_new - duration
                    segments.append(
                        Segment(
                            seg.start + duration,
                            seg.end + duration,
                            _remap(seg.mappings, Fraction(1), -duration),
                        )
                    )
                duration += self.durations[ref]
            segments = _merge_adjacent(segments)

        elif kind == "mix":
            shifted: list[Segment] = []
            duration = Fraction(0)
            for k, child in enumerate(node.children):
                self._resolve(child.node, self._loc(node_id, "children", k, "node"))
                self.compute(child.node)
                offset = Fraction(child.offset)
                duration = max(duration, offset + self.durations[child.node])
                for seg in self.segments[child.node]:
                    # t_child = t_new - offset
                    shifted.append(
                        Segment(
                            seg.start + offset,
                            seg.end + offset,
                            _remap(seg.mappings, Fraction(1), -offset),
                        )
                    )
            segments = _sweep(shifted, duration)

        else:  # pragma: no cover - discriminated union prevents this
            raise GraphError(f"unknown node type '{kind}'", self._loc(node_id, "type"))

        self.durations[node_id] = duration
        self.segments[node_id] = segments


# ---------------------------------------------------------------------------
# Interval algebra on half-open [start, end) ranges
# ---------------------------------------------------------------------------

def union_intervals(intervals) -> list[tuple[Fraction, Fraction]]:
    """Normalize to sorted disjoint intervals; touching ranges merge."""
    merged: list[list[Fraction]] = []
    for s, e in sorted(intervals):
        if not s < e:
            continue
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def subtract_intervals(used, permitted) -> list[tuple[Fraction, Fraction]]:
    """Return the parts of `used` not covered by `permitted` (both normalized)."""
    uncovered: list[tuple[Fraction, Fraction]] = []
    for s, e in used:
        cur = s
        for ps, pe in permitted:
            if pe <= cur:
                continue
            if ps >= e:
                break
            if ps > cur:
                uncovered.append((cur, min(ps, e)))
            cur = max(cur, pe)
            if cur >= e:
                break
        if cur < e:
            uncovered.append((cur, e))
    return uncovered


def intersect_intervals(left, right) -> list[tuple[Fraction, Fraction]]:
    """Intersection of two normalized interval lists (normalized output)."""
    out: list[tuple[Fraction, Fraction]] = []
    for s, e in left:
        for ps, pe in right:
            lo, hi = max(s, ps), min(e, pe)
            if lo < hi:
                out.append((lo, hi))
    return union_intervals(out)


def _permitted_intervals(consents, audience: str):
    """Union of consent segments per source, for the target audience only."""
    per_source: dict[str, list] = {}
    for consent in consents:
        if audience in consent.audiences:
            per_source.setdefault(consent.source, []).append(
                (Fraction(consent.start), Fraction(consent.end))
            )
    return {sid: union_intervals(ivs) for sid, ivs in per_source.items()}


def _used_intervals(mappings, start: Fraction, end: Fraction):
    """Original source intervals consumed by output range [start, end)."""
    return {
        sid: union_intervals(
            (m.ratio * start + m.offset, m.ratio * end + m.offset) for m in ms
        )
        for sid, ms in mappings.items()
    }


# ---------------------------------------------------------------------------
# Submission validation and analysis
# ---------------------------------------------------------------------------

def validate_submission(sub) -> dict[str, Fraction]:
    """Fully validate the edit document; return exact node durations.

    Raises GraphError (locatable) on duplicate ids, dangling references,
    cycles, out-of-bounds trims and out-of-bounds/dangling consents.
    Nothing is persisted by the caller when this raises.
    """
    graph = Graph(sub.nodes)
    for node_id in list(graph.index):
        graph.compute(node_id)
    for j, consent in enumerate(sub.consents):
        loc = ["consents", j, "source"]
        if consent.source not in graph.index:
            raise GraphError(
                f"consent references unknown source '{consent.source}'", loc
            )
        node = graph.index[consent.source][1]
        if node.type != "source":
            raise GraphError(
                f"consent target '{consent.source}' is a {node.type} node, not a source",
                loc,
            )
        if Fraction(consent.end) > Fraction(node.duration):
            raise GraphError(
                f"consent segment end {consent.end} exceeds source duration {node.duration}",
                ["consents", j, "end"],
            )
    return {nid: graph.durations[nid] for nid in graph.index}


def _pinpoint_violation(
    item: dict, uncovered_by_source: dict, contributors: dict
) -> dict:
    """Trim a violating segment to its exact unlicensed output range.

    Each uncovered source interval is mapped back through the source's affine
    maps to output time (t = (input - offset) / ratio). The earliest maximal
    unlicensed output interval is reported, so already-licensed program at
    the segment's start is never flagged; inside it, every unlicensed source
    is listed with the original intervals consumed there, sorted by source id.
    """
    output_ranges: list[tuple[Fraction, Fraction]] = []
    for sid, uncovered in uncovered_by_source.items():
        if not uncovered:
            continue
        for mapping in item["mappings"][sid]:
            in_lo = mapping.ratio * item["start"] + mapping.offset
            in_hi = mapping.ratio * item["end"] + mapping.offset
            for us, ue in uncovered:
                lo, hi = max(us, in_lo), min(ue, in_hi)
                if lo < hi:
                    output_ranges.append(
                        (
                            (lo - mapping.offset) / mapping.ratio,
                            (hi - mapping.offset) / mapping.ratio,
                        )
                    )
    v_start, v_end = union_intervals(output_ranges)[0]

    unlicensed: list[dict] = []
    for sid in sorted(uncovered_by_source):
        uncovered = uncovered_by_source[sid]
        if not uncovered:
            continue
        consumed = union_intervals(
            (m.ratio * v_start + m.offset, m.ratio * v_end + m.offset)
            for m in item["mappings"][sid]
        )
        precise = intersect_intervals(consumed, uncovered)
        if precise:
            unlicensed.append(
                {
                    "source": sid,
                    "contributors": contributors.get(sid, []),
                    "uncovered": [
                        {"start": rat_json(a), "end": rat_json(b)} for a, b in precise
                    ],
                }
            )
    return {
        "out": {"start": rat_json(v_start), "end": rat_json(v_end)},
        "unlicensed": unlicensed,
    }


def analyze(sub, output_id: str, audience: str) -> dict:
    """Backtrack the output node's timeline to original source intervals and
    check them against the consent union for the target audience."""
    graph = Graph(sub.nodes)
    if output_id not in graph.index:
        raise GraphError(
            f"analysis output references unknown node '{output_id}'", ["output"]
        )
    graph.compute(output_id)
    permitted = _permitted_intervals(sub.consents, audience)
    contributors = {
        n.id: list(n.contributors) for n in sub.nodes if n.type == "source"
    }

    # License each segment, then merge adjacent segments only when the source
    # set, the affine maps AND the licensing conclusion all match.
    merged: list[dict] = []
    for seg in graph.segments[output_id]:
        used = _used_intervals(seg.mappings, seg.start, seg.end)
        licensed = all(
            not subtract_intervals(ivs, permitted.get(sid, []))
            for sid, ivs in used.items()
        )
        if (
            merged
            and merged[-1]["end"] == seg.start
            and merged[-1]["mappings"] == seg.mappings
            and merged[-1]["licensed"] == licensed
        ):
            merged[-1]["end"] = seg.end
        else:
            merged.append(
                {
                    "start": seg.start,
                    "end": seg.end,
                    "mappings": seg.mappings,
                    "licensed": licensed,
                }
            )

    segments_out: list[dict] = []
    violation = None
    for item in merged:
        used = _used_intervals(item["mappings"], item["start"], item["end"])
        sources_out: list[dict] = []
        uncovered_by_source: dict[str, list] = {}
        for sid in sorted(item["mappings"]):
            uncovered = subtract_intervals(used[sid], permitted.get(sid, []))
            uncovered_by_source[sid] = uncovered
            sources_out.append(
                {
                    "source": sid,
                    "maps": [
                        {"ratio": rat_json(m.ratio), "offset": rat_json(m.offset)}
                        for m in item["mappings"][sid]
                    ],
                    "used": [
                        {"start": rat_json(a), "end": rat_json(b)} for a, b in used[sid]
                    ],
                    "licensed": not uncovered,
                    "uncovered": [
                        {"start": rat_json(a), "end": rat_json(b)} for a, b in uncovered
                    ],
                }
            )
        out_span = {"start": rat_json(item["start"]), "end": rat_json(item["end"])}
        segments_out.append(
            {"out": out_span, "licensed": item["licensed"], "sources": sources_out}
        )
        if violation is None and any(uncovered_by_source.values()):
            # Earliest violating segment, pinpointed to its unlicensed range.
            violation = _pinpoint_violation(item, uncovered_by_source, contributors)

    return {
        "output": output_id,
        "audience": audience,
        "decision": "approved" if violation is None else "rejected",
        "output_duration": rat_json(graph.durations[output_id]),
        "segments": segments_out,
        "violation": violation,
    }
