"""Internal renderer implementation; use ``render.py`` as the public facade."""

from __future__ import annotations

import argparse
import bisect
import heapq
import itertools
import json
import math
import sys
from collections import defaultdict
from html import escape
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .geometry import *
from .metrics import *
from .model import *
from .core import *
from .ir import *
from .layout import *
from .routing import *
from .labels import *
from .svg import *

def render(
    title: str,
    boxes: List[Box],
    edges: List[Edge],
    groups: List[dict],
    diagnostics: Optional[List[str]] = None,
) -> str:
    width, height = layout_boxes(boxes, edges)
    width = max(width, _ceil_snap(MARGIN_X * 2 + len(title) * TITLE_FONT * 0.58))
    grects = group_rects(boxes, groups)
    if grects:
        min_top = min(y for _, _, _, y, _, _ in grects)
        minimum_group_top = _title_rect(title, width)[3] + TITLE_CONTENT_GAP
        if min_top < minimum_group_top:
            shift = _ceil_snap(minimum_group_top - min_top)
            for b in boxes:
                b.y += shift
            height += shift
            grects = group_rects(boxes, groups)

    # Reserve lanes for explicit hints and automatically exterior-routed return
    # paths. The first top lane stays below the title; blocks shift as lanes grow.
    by_id = {b.id: b for b in boxes}
    vias = [resolved_via(e, by_id) for e in edges]
    top_extra = sum(via == "top" for via in vias) * ROUTE_STEP
    bottom_extra = sum(via == "bottom" for via in vias) * ROUTE_STEP
    height += top_extra + bottom_extra
    if top_extra:
        for b in boxes:
            b.y += top_extra
        grects = group_rects(boxes, groups)

    bottom_lane_base = _snap(max(b.bottom for b in boxes) + 30)
    routes, route_warnings = route_edges(
        edges, boxes, width, height, TOP_LANE_Y, bottom_lane_base
    )
    placements, label_warnings = place_edge_labels(
        title, boxes, edges, routes, grects, width, height
    )
    geometry_warnings = lint_geometry(
        boxes, edges, routes, placements, title, grects, width
    )
    if diagnostics is not None:
        diagnostics.extend(route_warnings)
        diagnostics.extend(label_warnings)
        diagnostics.extend(geometry_warnings)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="diagram-title" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<title id="diagram-title">{escape(title)}</title>',
        "<defs>",
        f"<style>{SVG_CSS}</style>",
        "</defs>",
        f'<rect x="0" y="0" width="{width}" height="{height}" class="bg"/>',
        f'<text x="{width/2:.1f}" y="{TITLE_Y}" class="title">{escape(title)}</text>',
    ]

    for group_index, (_, label, x, y, w, h) in enumerate(grects):
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" class="group group-{group_index % 4}"/>')
        parts.append(f'<text x="{x+10}" y="{y+17}" class="group-label">{escape(label)}</text>')

    # Memory stack shadows are behind routes, while the actual node faces stay
    # in front. An arrow approaching the rear/right side of a memory therefore
    # remains visible until it reaches the real block boundary.
    for b in boxes:
        shadow = svg_block_shadow(b)
        if shadow:
            parts.append(shadow)

    # Routes stay behind node faces. Labels are placed last so valid labels are
    # never occluded; collision-aware placement keeps them off the nodes.
    for e, route in zip(edges, routes):
        parts.append(svg_edge_shaft(e, route))
    for b in boxes:
        parts.append(svg_block(b))
    # Arrowheads sit above block outlines, but their tips stop exactly on the
    # boundary. This keeps memory faces and other node strokes from dulling the
    # point while the trimmed shaft remains safely behind the nodes.
    for e, route in zip(edges, routes):
        arrowhead = svg_edge_arrowhead(e, route)
        if arrowhead:
            parts.append(arrowhead)
    for e, placement in zip(edges, placements):
        rendered_label = svg_edge_label(e, placement)
        if rendered_label:
            parts.append(rendered_label)
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Geometry lint and command-line interface
# ---------------------------------------------------------------------------


def lint_geometry(
    boxes: List[Box],
    edges: List[Edge],
    routes: Optional[Sequence[Sequence[Point]]] = None,
    placements: Optional[Sequence[Optional[LabelPlacement]]] = None,
    title: str = "",
    grects: Sequence[Tuple[str, str, int, int, int, int]] = (),
    canvas_width: Optional[int] = None,
) -> List[str]:
    warnings = []
    ids = {b.id for b in boxes}
    isolated = ids - {e.source for e in edges} - {e.target for e in edges}
    for bid in sorted(isolated):
        warnings.append(f"block {bid} is isolated")
    # Too many edge labels is a strong predictor of visual clutter.
    labeled = sum(bool(edge_label_text(e)) for e in edges)
    if labeled > 24:
        warnings.append(f"{labeled} labeled edges: consider labeling only architecturally important buses")
    # Warn about multiple very long spans.
    by_id = {b.id: b for b in boxes}
    long_spans = sum(abs(by_id[e.source].col - by_id[e.target].col) >= 4 for e in edges)
    if long_spans >= 5:
        warnings.append(f"{long_spans} edges span 4+ columns; consider a hierarchy split or bus aggregation")

    for b in boxes:
        if len(b.label.splitlines()) > 2:
            warnings.append(f"block {b.id} has more than two label lines; only two are rendered")
        if any(len(line) > 28 for line in b.label.splitlines()[:2]):
            warnings.append(f"block {b.id} has a long label that may need a manual line break")
        if len(b.subtitle.splitlines()) > 2:
            warnings.append(f"block {b.id} has more than two subtitle lines; only two are rendered")
        subtitle_room = b.w - (22 if b.kind in {"reg", "counter"} else 26)
        if any(
            estimate_ui_text_width(line, _subtitle_font_size(b.prominence)) > subtitle_room
            for line in block_subtitle_lines(b.subtitle, subtitle_room, b.prominence)
        ):
            warnings.append(f"block {b.id} has unbreakable subtitle text that may overflow")

    if routes is None:
        return warnings
    if len(routes) != len(edges):
        warnings.append("route count does not match edge count")
        return warnings

    if canvas_width is None:
        canvas_width = _snap(max((b.right for b in boxes), default=MARGIN_X) + MARGIN_X)
    title_rect = _title_rect(title, canvas_width) if title else None
    group_labels = _group_label_rects(grects)
    degree: Dict[str, int] = defaultdict(int)
    route_sides = edge_sides(edges, by_id)
    for edge in edges:
        degree[edge.source] += 1
        degree[edge.target] += 1
    for edge_index, (edge, route) in enumerate(zip(edges, routes)):
        if len(route) < 2:
            warnings.append(f"edge {edge_index} ({edge.source}->{edge.target}) has no usable route")
            continue
        if not _point_on_boundary(route[0], by_id[edge.source]):
            warnings.append(f"edge {edge_index} does not start on block {edge.source}")
        if not _point_on_boundary(route[-1], by_id[edge.target]):
            warnings.append(f"edge {edge_index} does not end on block {edge.target}")
        for name, endpoint, neighbor, side in (
            ("source", route[0], route[1], route_sides[edge_index][0]),
            ("target", route[-1], route[-2], route_sides[edge_index][1]),
        ):
            dx, dy = neighbor.x - endpoint.x, neighbor.y - endpoint.y
            normal = {"e": (1, 0), "w": (-1, 0), "n": (0, -1), "s": (0, 1)}[side]
            if dx * normal[1] != dy * normal[0] or dx * normal[0] + dy * normal[1] <= 0:
                warnings.append(f"edge {edge_index} has an invalid {name} port approach")

        route_length = sum(
            abs(a.x - b.x) + abs(a.y - b.y)
            for a, b in zip(route, route[1:])
        )
        direct_length = abs(route[0].x - route[-1].x) + abs(route[0].y - route[-1].y)
        same_group = by_id[edge.source].group == by_id[edge.target].group
        has_leaf_endpoint = degree[edge.source] == 1 or degree[edge.target] == 1
        if (
            edge.kind == "data"
            and same_group
            and has_leaf_endpoint
            and route_length >= 4 * COL_GAP
            and route_length - direct_length >= 6 * ROUTE_STEP
        ):
            warnings.append(
                f"edge {edge_index} is a long detour to a leaf block; move the "
                "terminal/status block nearer its producer"
            )

        crossed = set()
        segments = list(zip(route, route[1:]))
        for segment_index, (a, b) in enumerate(segments):
            if a.x != b.x and a.y != b.y:
                warnings.append(f"edge {edge_index} contains a non-orthogonal segment")
            for block in boxes:
                if block.id in crossed:
                    continue
                if block.id == edge.source and segment_index == 0:
                    continue
                if block.id == edge.target and segment_index == len(segments) - 1:
                    continue
                if _segment_intersects_rect(a, b, _box_rect(block), interior=True):
                    warnings.append(f"edge {edge_index} crosses block {block.id}")
                    crossed.add(block.id)
            if title_rect and _segment_intersects_rect(a, b, title_rect, 2):
                warnings.append(f"edge {edge_index} crosses the diagram title")
                title_rect = None  # Emit at most one title warning per lint pass.

    for left_index, left_route in enumerate(routes):
        for right_index, right_route in enumerate(
            routes[left_index + 1:], left_index + 1
        ):
            overlap = collinear_route_overlap_length(left_route, right_route)
            if overlap > 0:
                warnings.append(
                    f"edges {left_index} and {right_index} share {overlap} units of wire"
                )

    if placements is None:
        return warnings

    group_bounds = {
        group_id: (x, y, x + group_width, y + group_height)
        for group_id, _, x, y, group_width, group_height in grects
    }
    label_rects: List[Tuple[int, Rect]] = []
    for edge_index, placement in enumerate(placements):
        if placement is None:
            continue
        rect = placement.rect
        label_rects.append((edge_index, rect))
        edge = edges[edge_index]
        source_group = by_id[edge.source].group
        target_group = by_id[edge.target].group
        if source_group is not None and source_group == target_group:
            parent_bounds = group_bounds.get(source_group)
            if parent_bounds is not None and not _rect_contains(
                parent_bounds, rect, LABEL_GROUP_INSET
            ):
                warnings.append(
                    f"edge {edge_index} label leaves group {source_group}"
                )
        for block in boxes:
            if _rects_overlap(rect, _box_rect(block), 1):
                warnings.append(f"edge {edge_index} label overlaps block {block.id}")
        if title and _rects_overlap(rect, _title_rect(title, canvas_width), 1):
            warnings.append(f"edge {edge_index} label overlaps the diagram title")
        for group_index, group_rect in enumerate(group_labels):
            if _rects_overlap(rect, group_rect, 1):
                warnings.append(f"edge {edge_index} label overlaps group label {group_index}")
        for route_index, route in enumerate(routes):
            if route_index == edge_index:
                continue
            if any(_segment_intersects_rect(a, b, rect, 1) for a, b in zip(route, route[1:])):
                warnings.append(f"edge {edge_index} label overlaps edge {route_index}")
                break
        leader_segments = _leader_segments(placement)
        if leader_segments:
            for block in boxes:
                if any(
                    _segment_intersects_rect(
                        start, end, _box_rect(block), interior=True
                    )
                    for start, end in leader_segments
                ):
                    warnings.append(f"edge {edge_index} label leader crosses block {block.id}")
            for route_index, route in enumerate(routes):
                if route_index == edge_index:
                    continue
                if any(
                    _segments_intersect(start, end, a, b)
                    for start, end in leader_segments
                    for a, b in zip(route, route[1:])
                ):
                    warnings.append(
                        f"edge {edge_index} label leader overlaps edge {route_index}"
                    )
                    break

    for i, (edge_a, rect_a) in enumerate(label_rects):
        for edge_b, rect_b in label_rects[i + 1:]:
            if _rects_overlap(rect_a, rect_b, 2):
                warnings.append(f"edge {edge_a} label overlaps edge {edge_b} label")
    for edge_a, placement_a in enumerate(placements):
        if placement_a is None or not _leader_segments(placement_a):
            continue
        for edge_b, placement_b in enumerate(placements[edge_a + 1:], edge_a + 1):
            if placement_b is None:
                continue
            if any(
                _segment_intersects_rect(start, end, placement_b.rect, 1)
                for start, end in _leader_segments(placement_a)
            ):
                warnings.append(f"edge {edge_a} label leader overlaps edge {edge_b} label")
            if any(
                _segments_intersect(start_a, end_a, start_b, end_b)
                for start_a, end_a in _leader_segments(placement_a)
                for start_b, end_b in _leader_segments(placement_b)
            ):
                warnings.append(f"edge {edge_a} label leader overlaps edge {edge_b} label leader")
    return warnings


def example_json() -> str:
    return json.dumps({
        "title": "Datapath",
        "blocks": [
            {"id": "in", "label": "Input FIFO", "kind": "fifo"},
            {"id": "alu", "label": "Execute", "kind": "logic"},
            {"id": "ram", "label": "BRAM", "kind": "memory"},
            {"id": "out", "label": "Output", "kind": "io"},
            {"id": "ctrl", "label": "Control", "kind": "fsm"},
        ],
        "edges": [
            {"from": "in.out", "to": "alu.in", "label": "data", "width": 64},
            {"from": "alu.mem", "to": "ram.req"},
            {"from": "ram.data", "to": "alu.data", "kind": "response"},
            {"from": "alu.out", "to": "out.in", "label": "result"},
            {"from": "ctrl.en", "to": "alu.ctrl", "kind": "control"},
        ]
    }, indent=2)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render compact hardware architecture JSON to SVG")
    ap.add_argument("input", nargs="?", type=Path, help="diagram JSON")
    ap.add_argument("-o", "--output", type=Path, help="output SVG (default: INPUT with .svg suffix)")
    ap.add_argument("--lint", action="store_true", help="print architecture and geometry warnings")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="print warnings and return exit status 1 when any warning remains",
    )
    ap.add_argument("--example", action="store_true", help="print a minimal example JSON and exit")
    args = ap.parse_args(argv)

    if args.example:
        print(example_json())
        return 0
    if args.input is None:
        ap.error("INPUT is required unless --example is used")

    try:
        title, boxes, edges, groups, warnings = load_diagram(args.input)
        svg = render(title, boxes, edges, groups, warnings)
    except (OSError, DiagramError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    out = args.output or args.input.with_suffix(".svg")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(svg)
    except OSError as e:
        print(f"error: could not write {out}: {e}", file=sys.stderr)
        return 2
    if args.lint or args.strict:
        if warnings:
            for w in warnings:
                print(f"warning: {w}", file=sys.stderr)
        else:
            print("lint: ok", file=sys.stderr)
    print(out)
    return 1 if args.strict and warnings else 0


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = [name for name in globals() if not name.startswith("__")]
