"""Internal renderer implementation; use ``render.py`` as the public facade."""

from __future__ import annotations

import argparse
import bisect
import copy
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


def _optimize_global_placement(
    boxes: List[Box], edges: List[Edge]
) -> Tuple[int, int]:
    """Target high-cost nets with block and group moves, rerouting each trial."""
    width, height = layout_boxes(boxes, edges)
    if len(boxes) < 2 or not edges:
        return width, height

    routes, warnings = _route_edges_once(
        edges, boxes, width, height, _optimize_ports=False
    )
    score = route_quality_score(
        routes, warnings, edges, boxes, width * height
    )
    def group_of(box: Box) -> str:
        return box.group or "__ungrouped__"

    initial_by_id = {box.id: box for box in boxes}
    semantic_directions = {
        edge_index: (
            (initial_by_id[edge.target].col > initial_by_id[edge.source].col)
            - (initial_by_id[edge.target].col < initial_by_id[edge.source].col)
        )
        for edge_index, edge in enumerate(edges)
        if edge.kind == "data"
        and group_of(initial_by_id[edge.source])
        == group_of(initial_by_id[edge.target])
    }

    def preserves_semantic_order(trial: Sequence[Box]) -> bool:
        trial_by_id = {box.id: box for box in trial}
        for edge_index, direction in semantic_directions.items():
            if direction == 0:
                continue
            edge = edges[edge_index]
            delta = (
                trial_by_id[edge.target].col
                - trial_by_id[edge.source].col
            )
            if delta * direction <= 0:
                return False
        return True

    def can_move_group(group_id: str) -> bool:
        return group_id != "__ungrouped__" and all(
            not box.position_fixed
            for box in boxes
            if group_of(box) == group_id
        )

    def apply_operation(trial: List[Box], operation: tuple) -> None:
        trial_by_id = {box.id: box for box in trial}
        kind = operation[0]
        if kind == "move":
            _, box_id, col, row = operation
            trial_by_id[box_id].col = max(0, col)
            trial_by_id[box_id].row = row
        elif kind == "swap":
            _, left_id, right_id = operation
            left, right = trial_by_id[left_id], trial_by_id[right_id]
            left.col, right.col = right.col, left.col
            left.row, right.row = right.row, left.row
        elif kind == "shift_group":
            _, group_id, col_delta, row_delta = operation
            for box in trial:
                if group_of(box) == group_id:
                    box.col = max(0, box.col + col_delta)
                    box.row += row_delta
        elif kind == "swap_groups":
            _, left_group, right_group = operation
            left = [box for box in trial if group_of(box) == left_group]
            right = [box for box in trial if group_of(box) == right_group]
            left_col, left_row = min(b.col for b in left), min(b.row for b in left)
            right_col, right_row = min(b.col for b in right), min(b.row for b in right)
            for box in left:
                box.col += right_col - left_col
                box.row += right_row - left_row
            for box in right:
                box.col += left_col - right_col
                box.row += left_row - right_row

    for _ in range(4):
        by_id = {box.id: box for box in boxes}
        edge_costs = []
        conflicts = [0] * len(edges)
        for left_index, left_route in enumerate(routes):
            for right_index in range(left_index + 1, len(routes)):
                severity = (
                    8 * len(perpendicular_route_crossings(
                        left_route, routes[right_index]
                    ))
                    + 5 * len(ambiguous_route_corner_touches(
                        left_route, routes[right_index]
                    ))
                    + int(collinear_route_overlap_length(
                        left_route, routes[right_index]
                    ) > 0) * 10
                )
                conflicts[left_index] += severity
                conflicts[right_index] += severity
        for edge_index, (edge, route) in enumerate(zip(edges, routes)):
            route_length = sum(
                abs(start.x - end.x) + abs(start.y - end.y)
                for start, end in zip(route, route[1:])
            )
            bends = max(0, len(route) - 2)
            cost = effective_edge_importance(edge, by_id) * (
                route_length + bends * BEND_COST + conflicts[edge_index] * 1000
            )
            edge_costs.append((cost, edge_index))

        candidates: Dict[tuple, float] = {}

        def offer(priority: float, operation: tuple) -> None:
            candidates[operation] = min(priority, candidates.get(operation, priority))

        for negative_cost, edge_index in (
            (-cost, edge_index)
            for cost, edge_index in sorted(edge_costs, reverse=True)[:12]
        ):
            edge = edges[edge_index]
            source, target = by_id[edge.source], by_id[edge.target]
            same_group = group_of(source) == group_of(target)
            for moving, other in ((source, target), (target, source)):
                if moving.position_fixed:
                    continue
                col_step = (other.col > moving.col) - (other.col < moving.col)
                row_step = (other.row > moving.row) - (other.row < moving.row)
                if col_step:
                    offer(negative_cost, (
                        "move", moving.id, moving.col + col_step, moving.row
                    ))
                if row_step and same_group:
                    offer(negative_cost, (
                        "move", moving.id, moving.col, moving.row + row_step
                    ))
                offer(negative_cost + 1, (
                    "move", moving.id,
                    max(0, round((moving.col + other.col) / 2)),
                    (
                        round((moving.row + other.row) / 2)
                        if same_group else moving.row
                    ),
                ))
                swap_targets = sorted(
                    (
                        candidate for candidate in boxes
                        if candidate.id != moving.id
                        and not candidate.position_fixed
                        and group_of(candidate) == group_of(moving)
                    ),
                    key=lambda candidate: (
                        abs(candidate.col - other.col)
                        + abs(candidate.row - other.row),
                        candidate.id,
                    ),
                )[:2]
                for candidate in swap_targets:
                    offer(
                        negative_cost + 2,
                        ("swap", moving.id, candidate.id),
                    )

            source_group, target_group = group_of(source), group_of(target)
            if source_group != target_group:
                col_step = (target.col > source.col) - (target.col < source.col)
                row_step = (target.row > source.row) - (target.row < source.row)
                if can_move_group(source_group):
                    if col_step:
                        offer(negative_cost + 3, (
                            "shift_group", source_group, col_step, 0
                        ))
                    if row_step:
                        offer(negative_cost + 3, (
                            "shift_group", source_group, 0, row_step
                        ))
                if can_move_group(target_group):
                    if col_step:
                        offer(negative_cost + 3, (
                            "shift_group", target_group, -col_step, 0
                        ))
                    if row_step:
                        offer(negative_cost + 3, (
                            "shift_group", target_group, 0, -row_step
                        ))
                if can_move_group(source_group) and can_move_group(target_group):
                    offer(negative_cost + 4, (
                        "swap_groups", source_group, target_group
                    ))

        by_lane: Dict[Tuple[str, int], List[Box]] = defaultdict(list)
        for box in boxes:
            by_lane[(group_of(box), box.row)].append(box)
        for members in by_lane.values():
            ordered = sorted(members, key=lambda box: (box.col, box.id))
            for left, right in zip(ordered, ordered[1:]):
                if not left.position_fixed and not right.position_fixed:
                    offer(0.0, ("swap", left.id, right.id))

        operations = [
            operation
            for operation, _ in sorted(
                candidates.items(), key=lambda item: (item[1], item[0])
            )[:24]
        ]
        best = None
        for operation_index, operation in enumerate(operations):
            trial = copy.deepcopy(boxes)
            apply_operation(trial, operation)
            if not preserves_semantic_order(trial):
                continue
            trial_width, trial_height = layout_boxes(trial, edges)
            trial_routes, trial_warnings = _route_edges_once(
                edges, trial, trial_width, trial_height,
                _optimize_ports=False,
            )
            trial_score = route_quality_score(
                trial_routes, trial_warnings, edges, trial,
                trial_width * trial_height,
            )
            if trial_score < score and (
                best is None or (trial_score, operation_index) < (best[0], best[1])
            ):
                best = (
                    trial_score, operation_index, trial,
                    trial_width, trial_height, trial_routes, trial_warnings,
                )
        if best is None:
            break
        score = best[0]
        chosen = {box.id: box for box in best[2]}
        for box in boxes:
            box.col, box.row = chosen[box.id].col, chosen[box.id].row
        width, height, routes, warnings = best[3:7]
        layout_boxes(boxes, edges)
    rightmost = max(box.right for box in boxes)
    label_gutter = 0
    by_id = {box.id: box for box in boxes}
    for edge in edges:
        label = edge_label_text(edge)
        if not label:
            continue
        source, target = by_id[edge.source], by_id[edge.target]
        if min(source.bottom, target.bottom) <= max(source.top, target.top):
            continue
        gap = max(source.left, target.left) - min(source.right, target.right)
        if (
            gap < estimate_edge_label_width(label) + 2 * LABEL_BLOCK_CLEAR
            and max(source.right, target.right) >= rightmost - COL_GAP
        ):
            label_gutter = max(
                label_gutter,
                _ceil_snap(estimate_edge_label_width(label) + 24),
            )
    width += label_gutter
    return width, height

def render(
    title: str,
    boxes: List[Box],
    edges: List[Edge],
    groups: List[dict],
    diagnostics: Optional[List[str]] = None,
) -> str:
    width, height = _optimize_global_placement(boxes, edges)
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
    grects = _expand_groups_for_label_clearance(
        grects, boxes, edges, width
    )
    group_label_rects = _group_label_rects(grects, boxes, edges)
    routes, route_warnings = route_edges(
        edges,
        boxes,
        width,
        height,
        TOP_LANE_Y,
        bottom_lane_base,
        [*group_label_rects, _title_rect(title, width)],
    )
    placements, label_warnings = place_edge_labels(
        title, boxes, edges, routes, grects, width, height, group_label_rects
    )
    geometry_warnings = lint_geometry(
        boxes, edges, routes, placements, title, grects, width, group_label_rects
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

    for group_index, (_, _, x, y, w, h) in enumerate(grects):
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" class="group group-{group_index % 4}"/>')

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
    for (_, label, _, _, _, _), rect in zip(grects, group_label_rects):
        parts.append(
            f'<rect x="{rect[0]:.1f}" y="{rect[1]:.1f}" '
            f'width="{rect[2]-rect[0]:.1f}" height="{rect[3]-rect[1]:.1f}" '
            'rx="3" class="group-label-bg"/>'
        )
        parts.append(
            f'<text x="{rect[0]+3:.1f}" y="{rect[1]+14:.1f}" '
            f'class="group-label">{escape(label)}</text>'
        )
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
    group_label_rects: Optional[Sequence[Rect]] = None,
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
    group_labels = list(
        group_label_rects
        if group_label_rects is not None
        else _group_label_rects(grects, boxes, edges)
    )
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
        from_side, to_side = route_sides[edge_index]
        if edge.from_side is None:
            from_side = _side_on_boundary(route[0], by_id[edge.source]) or from_side
        if edge.to_side is None:
            to_side = _side_on_boundary(route[-1], by_id[edge.target]) or to_side
        for name, endpoint, neighbor, side in (
            ("source", route[0], route[1], from_side),
            ("target", route[-1], route[-2], to_side),
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
        crossed_group_labels = set()
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
            for group_label_index, group_label_rect in enumerate(group_labels):
                if (
                    group_label_index not in crossed_group_labels
                    and _segment_intersects_rect(a, b, group_label_rect, 2)
                ):
                    warnings.append(
                        f"edge {edge_index} crosses group label {group_label_index}"
                    )
                    crossed_group_labels.add(group_label_index)

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
            if any(
                collinear_route_overlap_length(
                    [start, end], routes[edge_index]
                ) > 0
                for start, end in leader_segments
            ):
                warnings.append(
                    f"edge {edge_index} label leader runs along its owning edge"
                )
            for block in boxes:
                if any(
                    _segment_intersects_rect(
                        start, end, _box_rect(block), interior=True
                    )
                    for start, end in leader_segments
                ):
                    warnings.append(f"edge {edge_index} label leader crosses block {block.id}")
            if not placement.fallback:
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
            {"from": "in.out", "to": "alu.in", "label": "data", "count": 16, "width": 4},
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
