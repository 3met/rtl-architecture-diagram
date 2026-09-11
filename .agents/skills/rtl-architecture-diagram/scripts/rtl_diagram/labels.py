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
from .layout import *
from .routing import *

# ---------------------------------------------------------------------------
# Edge-label geometry and placement
# ---------------------------------------------------------------------------


def _point_on_boundary(p: Point, b: Box) -> bool:
    if b.kind == "io":
        for side in ("e", "w", "n", "s"):
            expected = _rendered_boundary_point(b, side, p.x, p.y)
            if abs(expected.x - p.x) <= 1 and abs(expected.y - p.y) <= 1:
                return True
        return False
    return (
        (p.x in {b.left, b.right} and b.top <= p.y <= b.bottom)
        or (p.y in {b.top, b.bottom} and b.left <= p.x <= b.right)
    )


def _title_text_width(title: str) -> float:
    return max(40.0, len(title) * TITLE_FONT * 0.58)


def _title_rect(title: str, canvas_width: int) -> Rect:
    text_width = _title_text_width(title)
    center = canvas_width / 2
    return (
        center - text_width / 2,
        TITLE_Y - TITLE_FONT,
        center + text_width / 2,
        TITLE_Y + 5,
    )


def _group_label_rects(grects: Sequence[Tuple[str, str, int, int, int, int]]) -> List[Rect]:
    result = []
    for _, label, x, y, _, _ in grects:
        result.append((x + 7, y + 3, x + 14 + len(label) * 7.2, y + 22))
    return result


def _ordered_positions(low: int, high: int) -> List[int]:
    midpoint = int(round((low + high) / 2))
    result = [midpoint]
    max_offset = max(midpoint - low, high - midpoint)
    for offset in range(ROUTE_STEP, max_offset + ROUTE_STEP, ROUTE_STEP):
        if midpoint - offset >= low:
            result.append(midpoint - offset)
        if midpoint + offset <= high:
            result.append(midpoint + offset)
    return result


def _label_center_offsets(text_width: float, vertical: bool = False) -> List[int]:
    """Offer near-center leader attachment without making it a hard constraint."""
    if vertical:
        return [0, -6, 6, -12, 12, -24, 24, -36, 36, -48, 48]
    # Wider offsets let a collision-aware leader carry a label out of a busy
    # route corridor. This is especially useful on short links bracketed by
    # feedback wires, where centering the pill can never be clear.
    max_offset = min(120, max(0, int((text_width / 2 + 96) // 12) * 12))
    offsets = [0]
    for offset in range(12, max_offset + 1, 12):
        offsets.extend((-offset, offset))
    return offsets


def _fit_label_inside(placement: LabelPlacement, bounds: Rect) -> LabelPlacement:
    """Shift a last-resort label inside its shared group when dimensions allow."""
    left, top, right, bottom = placement.rect
    inner_left = bounds[0] + LABEL_GROUP_INSET
    inner_top = bounds[1] + LABEL_GROUP_INSET
    inner_right = bounds[2] - LABEL_GROUP_INSET
    inner_bottom = bounds[3] - LABEL_GROUP_INSET
    dx = 0.0
    dy = 0.0
    if right - left <= inner_right - inner_left:
        if left < inner_left:
            dx = inner_left - left
        elif right > inner_right:
            dx = inner_right - right
    if bottom - top <= inner_bottom - inner_top:
        if top < inner_top:
            dy = inner_top - top
        elif bottom > inner_bottom:
            dy = inner_bottom - bottom
    return LabelPlacement(
        placement.x + dx,
        placement.y + dy,
        placement.width,
        placement.height,
        placement.leader_start,
        placement.leader_end,
        placement.fallback,
        placement.leader_bend,
    )


def _leader_segments(placement: LabelPlacement) -> List[Tuple[Point, Point]]:
    if not placement.leader_start or not placement.leader_end:
        return []
    points = [placement.leader_start]
    if placement.leader_bend and placement.leader_bend not in {
        placement.leader_start, placement.leader_end
    }:
        points.append(placement.leader_bend)
    points.append(placement.leader_end)
    return list(zip(points, points[1:]))


def _attach_leader_to_label(placement: LabelPlacement) -> LabelPlacement:
    """Attach a leader to the nearest clear point on any label edge."""
    if placement.leader_start is None:
        return placement
    start = placement.leader_start
    left, top, right, bottom = placement.rect
    inset = 5
    candidates = [
        Point(
            int(round(max(left + inset, min(right - inset, start.x)))),
            int(round(top)),
        ),
        Point(
            int(round(max(left + inset, min(right - inset, start.x)))),
            int(round(bottom)),
        ),
        Point(
            int(round(left)),
            int(round(max(top + inset, min(bottom - inset, start.y)))),
        ),
        Point(
            int(round(right)),
            int(round(max(top + inset, min(bottom - inset, start.y)))),
        ),
    ]
    end = min(
        candidates,
        key=lambda point: (
            abs(point.x - start.x) + abs(point.y - start.y),
            point.y,
            point.x,
        ),
    )
    bend = None
    if start.x != end.x and start.y != end.y:
        on_horizontal_edge = end.y in {int(round(top)), int(round(bottom))}
        bend = (
            Point(start.x, end.y)
            if on_horizontal_edge
            else Point(end.x, start.y)
        )
    return LabelPlacement(
        placement.x,
        placement.y,
        placement.width,
        placement.height,
        start,
        end,
        placement.fallback,
        bend,
    )


def _leader_route_attachments(
    placement: LabelPlacement,
    route: Sequence[Point],
) -> List[LabelPlacement]:
    """Rank useful connections between a placed label and its owning wire.

    Candidate generation decides where the label belongs.  Once that decision is
    made, the leader is free to originate on any segment of the owning route; it
    should not remain tied to the segment position that happened to produce the
    label candidate.  The caller can then select the first attachment that also
    clears foreign geometry.
    """
    if placement.leader_start is None:
        return [placement]

    left, top, right, bottom = placement.rect
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    original = _attach_leader_to_label(placement)
    candidates: List[LabelPlacement] = [original]
    seen: set[Point] = {placement.leader_start}

    for a, b in zip(route, route[1:]):
        if a.y == b.y:
            low, high = sorted((a.x, b.x))
            start = Point(int(round(max(low, min(high, center_x)))), a.y)
        elif a.x == b.x:
            low, high = sorted((a.y, b.y))
            start = Point(a.x, int(round(max(low, min(high, center_y)))))
        else:
            continue
        if start in seen:
            continue
        seen.add(start)
        candidates.append(_attach_leader_to_label(LabelPlacement(
            placement.x,
            placement.y,
            placement.width,
            placement.height,
            start,
            placement.leader_end,
            placement.fallback,
            placement.leader_bend,
        )))

    if not candidates:
        return [_attach_leader_to_label(placement)]

    def leader_score(candidate: LabelPlacement) -> Tuple[int, int, int, int]:
        segments = _leader_segments(candidate)
        length = sum(
            abs(start.x - end.x) + abs(start.y - end.y)
            for start, end in segments
        )
        return (
            length,
            len(segments),
            candidate.leader_start.y if candidate.leader_start else 0,
            candidate.leader_start.x if candidate.leader_start else 0,
        )

    return sorted(candidates, key=leader_score)


def _attach_leader_to_route(
    placement: LabelPlacement,
    route: Sequence[Point],
) -> LabelPlacement:
    """Return the shortest route attachment, independent of other geometry."""
    return _leader_route_attachments(placement, route)[0]


def _placement_is_clear(
    placement: LabelPlacement,
    obstacles: Sequence[Rect],
    routes: Sequence[Sequence[Point]],
    edge_index: int,
    placed: Sequence[LabelPlacement],
    width: int,
    height: int,
    containment: Optional[Rect] = None,
) -> bool:
    rect = placement.rect
    if rect[0] < 4 or rect[1] < 4 or rect[2] > width - 4 or rect[3] > height - 4:
        return False
    if any(_rects_overlap(rect, obstacle, 2) for obstacle in obstacles):
        return False
    if any(_rects_overlap(rect, other.rect, 3) for other in placed):
        return False
    if any(
        _segment_intersects_rect(start, end, rect, 1)
        for other in placed
        for start, end in _leader_segments(other)
    ):
        return False
    if containment is not None and not _rect_contains(
        containment, rect, LABEL_GROUP_INSET
    ):
        return False
    for route_index, route in enumerate(routes):
        if route_index == edge_index:
            continue
        if any(_segment_intersects_rect(a, b, rect, 1) for a, b in zip(route, route[1:])):
            return False
    leader_segments = _leader_segments(placement)
    if leader_segments:
        if any(
            _segment_intersects_rect(start, end, obstacle, interior=True)
            for start, end in leader_segments
            for obstacle in obstacles
        ):
            return False
        for route_index, route in enumerate(routes):
            if route_index == edge_index:
                continue
            if any(
                _segments_intersect(start, end, a, b)
                for start, end in leader_segments
                for a, b in zip(route, route[1:])
            ):
                return False
        for other in placed:
            if any(
                _segment_intersects_rect(start, end, other.rect, 1)
                for start, end in leader_segments
            ):
                return False
            if any(
                _segments_intersect(start, end, other_start, other_end)
                for start, end in leader_segments
                for other_start, other_end in _leader_segments(other)
            ):
                return False
    return True


def _label_route_congestion(
    placement: LabelPlacement,
    routes: Sequence[Sequence[Point]],
    edge_index: int,
) -> int:
    """Count nearby foreign segments so labels favor the quieter wire half."""
    return sum(
        _segment_intersects_rect(a, b, placement.rect, 18)
        for route_index, route in enumerate(routes)
        if route_index != edge_index
        for a, b in zip(route, route[1:])
    )


def _compile_foreign_route_segments(
    routes: Sequence[Sequence[Point]],
    edge_index: int,
) -> Tuple[
    List[int],
    List[Tuple[int, int, int]],
    List[int],
    List[Tuple[int, int, int]],
]:
    """Flatten foreign orthogonal segments for repeated label scoring.

    Each tuple is ``(horizontal, fixed_coordinate, low, high)``. Label
    placement evaluates hundreds of thousands of candidates, so avoiding
    Point traversal and generic rectangle construction in that inner loop is
    material while retaining exactly the same inclusive intersection rules.
    """
    flattened = [
        (
            start.y == end.y,
            start.y if start.y == end.y else start.x,
            min(start.x, end.x) if start.y == end.y else min(start.y, end.y),
            max(start.x, end.x) if start.y == end.y else max(start.y, end.y),
        )
        for route_index, route in enumerate(routes)
        if route_index != edge_index
        for start, end in zip(route, route[1:])
    ]
    horizontal = sorted(
        (fixed, low, high)
        for is_horizontal, fixed, low, high in flattened
        if is_horizontal
    )
    vertical = sorted(
        (fixed, low, high)
        for is_horizontal, fixed, low, high in flattened
        if not is_horizontal
    )
    return (
        [fixed for fixed, _, _ in horizontal],
        horizontal,
        [fixed for fixed, _, _ in vertical],
        vertical,
    )


def _compiled_label_route_congestion(
    placement: LabelPlacement,
    segments: Tuple[
        Sequence[int],
        Sequence[Tuple[int, int, int]],
        Sequence[int],
        Sequence[Tuple[int, int, int]],
    ],
) -> int:
    left, top, right, bottom = placement.rect
    left -= 18
    top -= 18
    right += 18
    bottom += 18
    horizontal_fixed, horizontal, vertical_fixed, vertical = segments
    count = 0
    start_index = bisect.bisect_left(horizontal_fixed, top)
    end_index = bisect.bisect_right(horizontal_fixed, bottom)
    for index in range(start_index, end_index):
        _, low, high = horizontal[index]
        count += low <= right and high >= left
    start_index = bisect.bisect_left(vertical_fixed, left)
    end_index = bisect.bisect_right(vertical_fixed, right)
    for index in range(start_index, end_index):
        _, low, high = vertical[index]
        count += low <= bottom and high >= top
    return count


def place_edge_labels(
    title: str,
    boxes: Sequence[Box],
    edges: Sequence[Edge],
    routes: Sequence[Sequence[Point]],
    grects: Sequence[Tuple[str, str, int, int, int, int]],
    width: int,
    height: int,
) -> Tuple[List[Optional[LabelPlacement]], List[str]]:
    obstacles = [_box_rect(b, LABEL_BLOCK_CLEAR) for b in boxes]
    obstacles.append(_title_rect(title, width))
    obstacles.extend(_group_label_rects(grects))
    boxes_by_id = {b.id: b for b in boxes}
    group_bounds = {
        group_id: (x, y, x + group_width, y + group_height)
        for group_id, _, x, y, group_width, group_height in grects
    }
    placed: List[LabelPlacement] = []
    result: List[Optional[LabelPlacement]] = [None] * len(edges)
    warnings: List[str] = []

    # Short local routes have far fewer sensible label sites than long buses.
    # Give the constrained labels first choice, then let long routes use their
    # greater freedom. Output remains indexed in original edge order.
    label_order = sorted(
        (i for i, edge in enumerate(edges) if edge_label_text(edge)),
        key=lambda i: (
            0 if longest_segment_mid(routes[i])[2] else 1,
            sum(
                abs(a.x - b.x) + abs(a.y - b.y)
                for a, b in zip(routes[i], routes[i][1:])
            ),
            i,
        ),
    )

    for edge_index in label_order:
        edge = edges[edge_index]
        route = routes[edge_index]
        label = edge_label_text(edge)
        text_width = estimate_edge_label_width(label)
        source_group = boxes_by_id[edge.source].group if edge.source in boxes_by_id else None
        target_group = boxes_by_id[edge.target].group if edge.target in boxes_by_id else None
        containment = (
            group_bounds.get(source_group)
            if source_group is not None and source_group == target_group
            else None
        )
        candidates = []
        foreign_route_segments = _compile_foreign_route_segments(
            routes, edge_index
        )
        route_left = min(point.x for point in route)
        route_right = max(point.x for point in route)
        route_top = min(point.y for point in route)
        route_bottom = max(point.y for point in route)
        if (
            len(route) >= 4
            and route_right - route_left >= text_width + 2 * LABEL_BLOCK_CLEAR
            and route_bottom - route_top <= 2 * ROUTE_STEP
        ):
            # A compact dogleg between adjacent blocks often has no useful
            # "above" or "below": both sides are occupied by those blocks.
            # Center the pill in the inter-block channel and let its opaque
            # background interrupt the owning wire, as a hand-drawn inline
            # net label would.
            inline_baselines = (
                (route_top + route_bottom) / 2 + 4,
                route_top - LABEL_HEIGHT + 12,
                route_bottom + 12,
            )
            for inline_rank, baseline in enumerate(inline_baselines):
                candidates.append((
                    (
                        0,
                        0,
                        inline_rank,
                        0,
                        -(route_right - route_left),
                        -1,
                        inline_rank,
                    ),
                    LabelPlacement(
                        (route_left + route_right) / 2,
                        baseline,
                        text_width,
                    ),
                ))
        for segment_index, (a, b) in enumerate(zip(route, route[1:])):
            horizontal = a.y == b.y
            length = abs(a.x - b.x) + abs(a.y - b.y)
            if not horizontal and a.x != b.x:
                continue
            low, high = sorted((a.x, b.x) if horizontal else (a.y, b.y))
            for position in _ordered_positions(low, high):
                midpoint_distance = abs(position - (low + high) / 2)
                for gap in LABEL_SEARCH_GAPS:
                    if horizontal:
                        above_bottom = a.y - gap
                        above_y = above_bottom - LABEL_HEIGHT + 12
                        below_top = a.y + gap
                        below_y = below_top + 12
                        for center_offset in _label_center_offsets(text_width):
                            label_x = position + center_offset
                            label_left = label_x - text_width / 2
                            label_right = label_x + text_width / 2
                            leader_x = int(round(max(
                                label_left + 8,
                                min(label_right - 8, position),
                            )))
                            needs_leader = gap >= 24 or abs(center_offset) > 12
                            candidates.append((
                                (
                                    gap,
                                    0,
                                    midpoint_distance,
                                    abs(center_offset),
                                    -length,
                                    segment_index,
                                    0,
                                ),
                                LabelPlacement(
                                    label_x,
                                    above_y,
                                    text_width,
                                    leader_start=Point(position, a.y) if needs_leader else None,
                                    leader_end=Point(leader_x, int(above_bottom)) if needs_leader else None,
                                    leader_bend=(
                                        Point(position, int(above_bottom))
                                        if needs_leader and leader_x != position else None
                                    ),
                                ),
                            ))
                            candidates.append((
                                (
                                    gap,
                                    0,
                                    midpoint_distance,
                                    abs(center_offset),
                                    -length,
                                    segment_index,
                                    1,
                                ),
                                LabelPlacement(
                                    label_x,
                                    below_y,
                                    text_width,
                                    leader_start=Point(position, a.y) if needs_leader else None,
                                    leader_end=Point(leader_x, int(below_top)) if needs_leader else None,
                                    leader_bend=(
                                        Point(position, int(below_top))
                                        if needs_leader and leader_x != position else None
                                    ),
                                ),
                            ))
                    else:
                        right_left = a.x + gap
                        right_x = right_left + text_width / 2
                        left_right = a.x - gap
                        left_x = left_right - text_width / 2
                        for center_offset in _label_center_offsets(text_width, vertical=True):
                            label_center_y = position + center_offset
                            baseline = label_center_y + 4
                            label_top = baseline - 12
                            label_bottom = label_top + LABEL_HEIGHT
                            leader_y = int(round(max(
                                label_top + 5,
                                min(label_bottom - 5, position),
                            )))
                            needs_leader = gap >= 24 or abs(center_offset) > 12
                            candidates.append((
                                (
                                    gap,
                                    1,
                                    midpoint_distance,
                                    abs(center_offset),
                                    -length,
                                    segment_index,
                                    0,
                                ),
                                LabelPlacement(
                                    right_x,
                                    baseline,
                                    text_width,
                                    leader_start=Point(a.x, position) if needs_leader else None,
                                    leader_end=Point(int(right_left), leader_y) if needs_leader else None,
                                    leader_bend=(
                                        Point(int(right_left), position)
                                        if needs_leader and leader_y != position else None
                                    ),
                                ),
                            ))
                            candidates.append((
                                (
                                    gap,
                                    1,
                                    midpoint_distance,
                                    abs(center_offset),
                                    -length,
                                    segment_index,
                                    1,
                                ),
                                LabelPlacement(
                                    left_x,
                                    baseline,
                                    text_width,
                                    leader_start=Point(a.x, position) if needs_leader else None,
                                    leader_end=Point(int(left_right), leader_y) if needs_leader else None,
                                    leader_bend=(
                                        Point(int(left_right), position)
                                        if needs_leader and leader_y != position else None
                                    ),
                                ),
                            ))

        selected = None
        congestion_cache: Dict[Rect, int] = {}

        def candidate_congestion(candidate: LabelPlacement) -> int:
            rect = candidate.rect
            cached = congestion_cache.get(rect)
            if cached is None:
                cached = _compiled_label_route_congestion(
                    candidate, foreign_route_segments
                )
                # Congestion is a count and therefore non-negative, so None
                # remains an unambiguous cache-miss sentinel.
                congestion_cache[rect] = cached
            return cached

        for _, candidate in sorted(
            candidates,
            key=lambda item: (
                item[0][0],
                candidate_congestion(item[1]),
                *item[0][1:],
            ),
        ):
            for attachment in _leader_route_attachments(candidate, route):
                if _placement_is_clear(
                    attachment,
                    obstacles,
                    routes,
                    edge_index,
                    placed,
                    width,
                    height,
                    containment,
                ):
                    selected = attachment
                    break
            if selected is not None:
                break

        if selected is None:
            mx, my, horizontal = longest_segment_mid(route)
            if horizontal:
                selected = LabelPlacement(mx, my - 7, text_width, fallback=True)
            else:
                selected = LabelPlacement(
                    mx + 7 + text_width / 2, my + 4, text_width, fallback=True
                )
            if containment is not None:
                selected = _fit_label_inside(selected, containment)
            warnings.append(
                f"edge {edge_index} ({edge.source}->{edge.target}) has no collision-free label position"
            )

        placed.append(selected)
        result[edge_index] = selected

    return result, warnings

__all__ = [name for name in globals() if not name.startswith("__")]
