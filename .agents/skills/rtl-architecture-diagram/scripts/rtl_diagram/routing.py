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

# ---------------------------------------------------------------------------
# Port assignment and orthogonal routing
# ---------------------------------------------------------------------------


def blocked_cells(boxes: Sequence[Box], ignore: set[str], width: int, height: int) -> set[Tuple[int, int]]:
    blocked = set()
    for b in boxes:
        # Endpoint blocks waive only the external clearance halo. Their actual
        # interiors remain blocked so a feedback route cannot cut back through
        # its own source or target after leaving the port.
        clearance = (
            0
            if b.id in ignore
            else (2 if b.id.startswith("__route_obstacle_") else ROUTE_CLEAR)
        )
        l = max(0, _floor_snap(b.left - clearance))
        r = min(width, _ceil_snap(b.right + clearance))
        t = max(0, _floor_snap(b.top - clearance))
        bot = min(height, _ceil_snap(b.bottom + clearance))
        for x in range(l, r + ROUTE_STEP, ROUTE_STEP):
            for y in range(t, bot + ROUTE_STEP, ROUTE_STEP):
                blocked.add((x, y))
    return blocked


def astar_route(start: Point, goal: Point, boxes: Sequence[Box], width: int, height: int,
                ignore: set[str], used: Dict[Tuple[int, int], int],
                preferred_y: Optional[int] = None,
                used_axes: Optional[Dict[Tuple[int, int], set[str]]] = None) -> Tuple[List[Point], bool]:
    start = Point(_snap(start.x), _snap(start.y))
    goal = Point(_snap(goal.x), _snap(goal.y))
    blocked = blocked_cells(boxes, ignore, width, height)
    blocked.discard((start.x, start.y))
    blocked.discard((goal.x, goal.y))

    # Preserve the visually strongest connection when aligned ports already
    # have a clear corridor. A* remains responsible for all obstructed and
    # non-aligned cases, but it should not introduce a dogleg merely to shave a
    # small soft occupancy cost.
    if start.x == goal.x or start.y == goal.y:
        if start.x == goal.x:
            cells = [
                (start.x, y)
                for y in range(
                    min(start.y, goal.y) + ROUTE_STEP,
                    max(start.y, goal.y),
                    ROUTE_STEP,
                )
            ]
        else:
            cells = [
                (x, start.y)
                for x in range(
                    min(start.x, goal.x) + ROUTE_STEP,
                    max(start.x, goal.x),
                    ROUTE_STEP,
                )
            ]
        if not any(cell in blocked or used.get(cell, 0) for cell in cells):
            return [start, goal], False

    dirs = [(ROUTE_STEP, 0), (-ROUTE_STEP, 0), (0, ROUTE_STEP), (0, -ROUTE_STEP)]
    # state=(x,y,dx,dy) retains direction to penalize bends.
    initial = (start.x, start.y, 0, 0)
    pq: List[Tuple[float, int, Tuple[int, int, int, int]]] = []
    counter = 0
    heapq.heappush(pq, (0.0, counter, initial))
    best = {initial: 0.0}
    parent: Dict[Tuple[int, int, int, int], Tuple[int, int, int, int]] = {}
    goal_state = None

    def h(x: int, y: int) -> float:
        return (abs(goal.x - x) + abs(goal.y - y)) / ROUTE_STEP

    max_nodes = max(20000, (width // ROUTE_STEP) * (height // ROUTE_STEP) * 4)
    visited = 0
    while pq and visited < max_nodes:
        _, _, st = heapq.heappop(pq)
        g = best.get(st)
        if g is None:
            continue
        x, y, pdx, pdy = st
        visited += 1
        if (x, y) == (goal.x, goal.y):
            goal_state = st
            break
        for dx, dy in dirs:
            nx, ny = x + dx, y + dy
            if nx < ROUTE_STEP or ny < ROUTE_STEP or nx > width - ROUTE_STEP or ny > height - ROUTE_STEP:
                continue
            if (nx, ny) in blocked:
                continue
            bend = 0.0 if (pdx, pdy) in {(0, 0), (dx, dy)} else 12.0
            axis = "h" if dx else "v"
            occupied_axes = used_axes.get((nx, ny), set()) if used_axes is not None else set()
            # Crossing an existing route is visually more ambiguous than
            # briefly sharing its direction, so keep a much stronger penalty
            # for perpendicular occupancy. Both remain soft constraints: A*
            # may still use a congested channel when geometry leaves no choice.
            crossing = 80.0 if occupied_axes and axis not in occupied_axes else 0.0
            sharing = 15.0 if axis in occupied_axes else 0.0
            occupancy = used.get((nx, ny), 0) * 1.8 + crossing + sharing
            corridor = 0.0
            if preferred_y is not None:
                corridor = min(3.0, abs(ny - preferred_y) / 120.0)
            ng = g + 1.0 + bend + occupancy + corridor
            ns = (nx, ny, dx, dy)
            if ng + 1e-9 < best.get(ns, float("inf")):
                best[ns] = ng
                parent[ns] = st
                counter += 1
                heapq.heappush(pq, (ng + h(nx, ny), counter, ns))

    if goal_state is None:
        # Deterministic fallback. The geometric lint pass reports any crossing so
        # callers can fix the IR or fail the render with --strict.
        midx = _snap((start.x + goal.x) / 2)
        return [start, Point(midx, start.y), Point(midx, goal.y), goal], True

    rev = []
    st = goal_state
    while True:
        rev.append(Point(st[0], st[1]))
        if st == initial:
            break
        st = parent[st]
    rev.reverse()
    return simplify_polyline(rev), False


def simplify_polyline(points: Sequence[Point]) -> List[Point]:
    deduped = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
    if len(deduped) <= 2:
        return deduped
    out = [deduped[0]]
    for i in range(1, len(deduped) - 1):
        a, b, c = out[-1], deduped[i], deduped[i + 1]
        if (a.x == b.x == c.x) or (a.y == b.y == c.y):
            continue
        out.append(b)
    out.append(deduped[-1])
    return out


def route_clear_of_boxes(
    points: Sequence[Point], boxes: Sequence[Box], ignore: set[str]
) -> bool:
    """Return whether an orthogonal candidate keeps normal block clearance."""
    for box in boxes:
        if box.id in ignore:
            continue
        clearance = 2 if box.id.startswith("__route_obstacle_") else ROUTE_CLEAR
        rect = (
            box.left - clearance,
            box.top - clearance,
            box.right + clearance,
            box.bottom + clearance,
        )
        for a, b in zip(points, points[1:]):
            if _segment_intersects_rect(a, b, rect, interior=True):
                return False
    return True


def _route_candidate_score(
    route: Sequence[Point],
    used: Dict[Tuple[int, int], int],
    used_axes: Dict[Tuple[int, int], set[str]],
    preferred_y: Optional[int],
    preferred_x: Optional[int] = None,
) -> float:
    """Score a clear orthogonal candidate by length, bends, and ambiguity."""
    length = 0
    occupancy = 0.0
    for a, b in zip(route, route[1:]):
        length += abs(a.x - b.x) + abs(a.y - b.y)
        axis = "h" if a.y == b.y else "v"
        if axis == "h":
            cells = (
                (_snap(x), _snap(a.y))
                for x in range(
                    _ceil_snap(min(a.x, b.x)),
                    _floor_snap(max(a.x, b.x)) + ROUTE_STEP,
                    ROUTE_STEP,
                )
            )
        else:
            cells = (
                (_snap(a.x), _snap(y))
                for y in range(
                    _ceil_snap(min(a.y, b.y)),
                    _floor_snap(max(a.y, b.y)) + ROUTE_STEP,
                    ROUTE_STEP,
                )
            )
        for cell in cells:
            axes = used_axes.get(cell, set())
            if axes and axis not in axes:
                occupancy += 180
            elif axis in axes:
                occupancy += 20
            occupancy += used.get(cell, 0) * 1.5
    bends = max(0, len(route) - 2)
    corridor = 0.0
    if preferred_y is not None:
        horizontal_ys = [
            a.y for a, b in zip(route, route[1:]) if a.y == b.y
        ]
        if horizontal_ys:
            corridor = min(abs(y - preferred_y) for y in horizontal_ys) * 0.2
    if preferred_x is not None:
        vertical_xs = [
            a.x for a, b in zip(route, route[1:]) if a.x == b.x
        ]
        if vertical_xs:
            corridor += min(abs(x - preferred_x) for x in vertical_xs) * 3.0
    return length + bends * 60 + occupancy + corridor


def direct_orthogonal_route(
    start: Point,
    goal: Point,
    boxes: Sequence[Box],
    ignore: set[str],
    width: int,
    height: int,
    used: Dict[Tuple[int, int], int],
    used_axes: Dict[Tuple[int, int], set[str]],
    preferred_y: Optional[int] = None,
    preferred_x: Optional[int] = None,
    candidate_cache: Optional[Dict[tuple, List[Tuple[tuple, List[Point]]]]] = None,
) -> Optional[List[Point]]:
    """Return the quietest clear route with at most two orthogonal bends.

    A* is useful in genuinely obstructed fields, but allowing soft wire costs
    to decide every connection can turn an otherwise obvious elbow into a
    many-jog detour.  Enumerate the small set of schematic-quality elbows and
    channels first; use A* only when none of them clears the blocks.
    """
    cache_key = (
        start.x,
        start.y,
        goal.x,
        goal.y,
        tuple(sorted(ignore)),
        width,
        height,
        tuple(
            (box.id, box.left, box.top, box.right, box.bottom)
            for box in boxes
        ),
    )
    cached = candidate_cache.get(cache_key) if candidate_cache is not None else None
    if cached is None:
        candidates: List[List[Point]] = []
        if start.x == goal.x or start.y == goal.y:
            candidates.append([start, goal])
        candidates.extend(
            [
                [start, Point(goal.x, start.y), goal],
                [start, Point(start.x, goal.y), goal],
            ]
        )

        x_channels = {
            _snap((start.x + goal.x) / 2),
            *(_floor_snap(box.left - ROUTE_CLEAR) for box in boxes),
            *(_ceil_snap(box.right + ROUTE_CLEAR) for box in boxes),
        }
        y_channels = {
            _snap((start.y + goal.y) / 2),
            *(_floor_snap(box.top - ROUTE_CLEAR) for box in boxes),
            *(_ceil_snap(box.bottom + ROUTE_CLEAR) for box in boxes),
        }
        # Offer parallel tracks up front so unrelated nets need not share the
        # single channel at each obstacle boundary and be repaired afterward.
        x_channels |= {x + offset for x in tuple(x_channels) for offset in (-ROUTE_STEP, ROUTE_STEP)}
        y_channels |= {y + offset for y in tuple(y_channels) for offset in (-ROUTE_STEP, ROUTE_STEP)}
        for x in sorted(x_channels):
            if ROUTE_STEP <= x <= width - ROUTE_STEP:
                candidates.append(
                    [start, Point(x, start.y), Point(x, goal.y), goal]
                )
        for y in sorted(y_channels):
            if ROUTE_STEP <= y <= height - ROUTE_STEP:
                candidates.append(
                    [start, Point(start.x, y), Point(goal.x, y), goal]
                )
        # A stepped escape can pass the end of a sibling trunk before crossing
        # toward the destination. Without these candidates, a dense fanout is
        # forced to choose between a short visual junction and falling all the
        # way back to A*, even when two quiet obstacle-boundary channels form a
        # clean schematic route.
        valid_xs = [
            x for x in x_channels if ROUTE_STEP <= x <= width - ROUTE_STEP
        ]
        valid_ys = [
            y for y in y_channels if ROUTE_STEP <= y <= height - ROUTE_STEP
        ]
        midpoint_x = (start.x + goal.x) / 2
        midpoint_y = (start.y + goal.y) / 2
        dogleg_xs = sorted(set(
            sorted(
                valid_xs,
                key=lambda x: (min(abs(x - start.x), abs(x - goal.x)), x),
            )[:10]
            + sorted(valid_xs, key=lambda x: (abs(x - midpoint_x), x))[:10]
        ))
        dogleg_ys = sorted(set(
            sorted(
                valid_ys,
                key=lambda y: (min(abs(y - start.y), abs(y - goal.y)), y),
            )[:10]
            + sorted(valid_ys, key=lambda y: (abs(y - midpoint_y), y))[:10]
        ))
        for x in dogleg_xs:
            for y in dogleg_ys:
                candidates.append([
                    start,
                    Point(x, start.y),
                    Point(x, y),
                    Point(goal.x, y),
                    goal,
                ])

        cached = []
        seen = set()
        for candidate in candidates:
            route = simplify_polyline(candidate)
            signature = tuple((point.x, point.y) for point in route)
            if signature in seen:
                continue
            seen.add(signature)
            if not all(
                a.x == b.x or a.y == b.y
                for a, b in zip(route, route[1:])
            ):
                continue
            if route_clear_of_boxes(route, boxes, ignore):
                cached.append((signature, route))
        if candidate_cache is not None:
            candidate_cache[cache_key] = cached

    scored = [
        (
            _route_candidate_score(
                route, used, used_axes, preferred_y, preferred_x
            ),
            signature,
            route,
        )
        for signature, route in cached
    ]
    return min(scored, default=(0, (), None))[2]


def _route_conflict_score(
    route: Sequence[Point], previous_routes: Sequence[Sequence[Point]]
) -> Tuple[int, int, int, int, int, Tuple[Tuple[int, int], ...]]:
    overlap = sum(
        collinear_route_overlap_length(route, previous)
        for previous in previous_routes
    )
    crossings = sum(
        len(perpendicular_route_crossings(route, previous))
        for previous in previous_routes
    )
    corner_touches = sum(
        len(ambiguous_route_corner_touches(route, previous))
        for previous in previous_routes
    )
    length = sum(
        abs(a.x - b.x) + abs(a.y - b.y)
        for a, b in zip(route, route[1:])
    )
    bends = max(0, len(route) - 2)
    return (
        overlap * 200 + crossings * 300 + corner_touches * 260 + bends * 40,
        crossings,
        overlap,
        corner_touches,
        length,
        tuple((point.x, point.y) for point in route),
    )


def _deoverlap_route(
    route: Sequence[Point],
    previous_routes: Sequence[Sequence[Point]],
    boxes: Sequence[Box],
    ignore: set[str],
) -> List[Point]:
    """Nudge interior tracks to reduce sharing and crossings, keeping port stubs."""
    best = list(route)
    def preserves_approaches(candidate: Sequence[Point]) -> bool:
        # Moving the first/last interior track must not collapse or reverse
        # the port stub. Otherwise an arrow can run along the block boundary.
        for original, changed in ((route, candidate), (route[::-1], candidate[::-1])):
            if len(changed) < 2:
                return False
            a, b = original[:2]
            c, d = changed[:2]
            dx, dy = b.x - a.x, b.y - a.y
            nx, ny = d.x - c.x, d.y - c.y
            if dx * nx + dy * ny <= 0 or dx * ny != dy * nx:
                return False
            if abs(nx) + abs(ny) < min(ROUTE_CLEAR, abs(dx) + abs(dy)):
                return False
        return True

    best_score = _route_conflict_score(best, previous_routes)
    for _ in range(3):
        variants: List[List[Point]] = []
        ambiguous_points = {
            point
            for previous in previous_routes
            for point in ambiguous_route_corner_touches(best, previous)
        }
        for segment_index, (a, b) in enumerate(zip(best, best[1:])):
            if segment_index == 0 or segment_index >= len(best) - 2:
                continue
            if not any(
                collinear_route_overlap_length([a, b], previous) > 0
                or perpendicular_route_crossings([a, b], previous)
                for previous in previous_routes
            ) and a not in ambiguous_points and b not in ambiguous_points:
                continue
            for offset in (
                -ROUTE_STEP,
                ROUTE_STEP,
                -2 * ROUTE_STEP,
                2 * ROUTE_STEP,
                -3 * ROUTE_STEP,
                3 * ROUTE_STEP,
            ):
                if a.x == b.x:
                    shifted_a = Point(a.x + offset, a.y)
                    shifted_b = Point(b.x + offset, b.y)
                elif a.y == b.y:
                    shifted_a = Point(a.x, a.y + offset)
                    shifted_b = Point(b.x, b.y + offset)
                else:
                    continue
                candidate = simplify_polyline(
                    [*best[:segment_index], shifted_a, shifted_b, *best[segment_index + 2:]]
                )
                if preserves_approaches(candidate) and route_clear_of_boxes(candidate, boxes, ignore):
                    variants.append(candidate)
        if not variants:
            break
        candidate = min(
            variants,
            key=lambda value: _route_conflict_score(value, previous_routes),
        )
        candidate_score = _route_conflict_score(candidate, previous_routes)
        if candidate_score >= best_score:
            break
        best = candidate
        best_score = candidate_score
        if best_score[1] == 0 and best_score[2] == 0 and best_score[3] == 0:
            break
    return best


def edge_sides(edges: List[Edge], boxes: Dict[str, Box]) -> List[Tuple[str, str]]:
    result = []
    north_control_targets = {
        edge.target
        for edge in edges
        if edge.kind in CONTROL_EDGE_KINDS
        and boxes[edge.source].cy < boxes[edge.target].cy
    }
    for e in edges:
        sb, tb = boxes[e.source], boxes[e.target]
        fs = e.from_side or infer_side(sb, tb, True)
        ts = e.to_side or infer_side(tb, sb, False)
        if (
            e.from_side is None
            and e.to_side is None
            and e.kind == "data"
            and sb.kind == "memory"
            and sb.cy > tb.cy
        ):
            # Support memories below (or on a fold shelf beside) their
            # consumers should use facing vertical ports. A west/east choice
            # creates a hook around the folded row even when the clear corridor
            # between rows is the most direct route.
            fs, ts = "n", "s"
        if (
            e.to_side is None
            and e.kind == "data"
            and sb.group
            and tb.group
            and sb.group != tb.group
            and sb.cy < tb.cy
            and tb.kind == "memory"
        ):
            # A state handoff from an upper component should enter the lower
            # component's memory from the north. Approaching its east/west side
            # tends to descend through that component's controller fanout and
            # creates avoidable crossovers.
            ts = "n"
        if (
            e.to_side is None
            and e.kind in CONTROL_EDGE_KINDS
            and tb.kind in {"memory", "fifo"}
            and tb.cy > sb.cy + ROW_GAP
        ):
            # A controller selecting a memory on a lower side shelf should
            # enter from above. Approaching the west side forces the select
            # wire through the return datapath occupying that shelf.
            ts = "n"
        if (
            e.to_side is None
            and e.kind == "data"
            and e.target in north_control_targets
            and abs(sb.cy - tb.cy) <= ROUTE_STEP
            and abs(tb.col - sb.col) > 2
        ):
            # A long data bypass into a block with control fan-in above should
            # approach from below. It stays out of both the north control track
            # and the direct west-side datapath connection.
            ts = "s"
        if e.from_side is None and e.kind in CONTROL_EDGE_KINDS:
            dx = tb.cx - sb.cx
            dy = tb.cy - sb.cy
            if (
                sb.kind in {"fsm", "arbiter"}
                and dy > ROW_GAP * 3
                and tb.col - sb.col >= 2
            ):
                # A far downstream control arc should leave through the south
                # side before turning toward its consumer. Reusing the east
                # fanout side makes its first horizontal stub cut across the
                # vertical trunk of a nearer sibling control connection.
                fs = "s"
            elif abs(dy) > ROW_GAP * 3 and abs(dx) >= abs(dy) * 0.6:
                # A long diagonal controller link should leave laterally before
                # descending; exiting through the bottom tends to encounter the
                # entire datapath and can send A* around the diagram perimeter.
                fs = "e" if dx >= 0 else "w"
        result.append((fs, ts))
    return result


def assign_ports(
    edges: List[Edge],
    sides: List[Tuple[str, str]],
    boxes: Dict[str, Box],
) -> List[Tuple[Point, Point, str, str]]:
    # Share a single ordering for incoming and outgoing connections on each
    # physical side. This prevents opposite-direction links from landing on
    # the exact same port coordinate.
    usage: Dict[Tuple[str, str], List[Tuple[int, str]]] = defaultdict(list)
    for i, (e, (fs, ts)) in enumerate(zip(edges, sides)):
        usage[(e.source, fs)].append((i, "out"))
        usage[(e.target, ts)].append((i, "in"))

    north_control_targets = {
        edge.target
        for edge, (_, target_side) in zip(edges, sides)
        if edge.kind in CONTROL_EDGE_KINDS and target_side == "n"
    }

    def connection_key(edge_i: int) -> Tuple[str, str, str, str, str, int]:
        e = edges[edge_i]
        # One edge-level identity is used at both endpoints. Reciprocal wires
        # therefore retain the same rank on both facing sides instead of being
        # independently sorted by their local port names and crossing midway.
        return (
            e.source,
            e.source_port,
            e.target,
            e.target_port,
            e.kind,
            edge_i,
        )

    ranks: Dict[Tuple[int, str], Tuple[int, int]] = {}
    for (node, side), items in usage.items():
        def key(item: Tuple[int, str]) -> Tuple[int, int, int, int, Tuple[str, str, str, str, str, int], int]:
            edge_i, direction = item
            e = edges[edge_i]
            here = boxes[node]
            other = boxes[e.target if direction == "out" else e.source]
            coord = other.cy if side in {"e", "w"} else other.cx
            distance = abs(other.cx - here.cx) + abs(other.cy - here.cy)
            lower_bypass = int(
                direction == "out"
                and side in {"e", "w"}
                and e.kind == "data"
                and e.target in north_control_targets
                and abs(boxes[e.target].col - boxes[e.source].col) > 2
            )
            outer_cross_group_handoff = int(not (
                direction == "in"
                and side == "n"
                and e.kind == "data"
                and boxes[e.source].group
                and boxes[e.target].group
                and boxes[e.source].group != boxes[e.target].group
                and boxes[e.target].kind == "memory"
            ))
            # When destinations share the same perpendicular coordinate, put
            # the farther one on the outer/top port. This prevents a short
            # local connection from crossing a longer same-row connection.
            return (
                outer_cross_group_handoff,
                coord,
                lower_bypass,
                -distance,
                connection_key(edge_i),
                0 if direction == "out" else 1,
            )
        items.sort(key=key)
        for rank, (edge_i, direction) in enumerate(items):
            ranks[(edge_i, direction)] = (rank, len(items))

    out = []
    for i, (e, (fs, ts)) in enumerate(zip(edges, sides)):
        sr, sc = ranks[(i, "out")]
        tr, tc = ranks[(i, "in")]
        p1 = port_point(boxes[e.source], fs, sr, sc)
        p2 = port_point(boxes[e.target], ts, tr, tc)
        out.append((p1, p2, fs, ts))

    # A side with only one connection does not need to insist on its geometric
    # midpoint. If its peer already has a sensible port coordinate within that
    # side, align to it and preserve a straight link. This removes tiny final
    # jogs without requiring cosmetic port hints in the IR.
    aligned = []
    for edge_i, (p1, p2, fs, ts) in enumerate(out):
        edge = edges[edge_i]
        source = boxes[edge.source]
        target = boxes[edge.target]
        source_count = len(usage[(edge.source, fs)])
        target_count = len(usage[(edge.target, ts)])
        if fs in {"n", "s"} and ts in {"n", "s"}:
            overlap_low = _ceil_snap(max(source.left, target.left) + 16)
            overlap_high = _floor_snap(min(source.right, target.right) - 16)
            if overlap_low <= overlap_high:
                shared_x = max(
                    overlap_low,
                    min(overlap_high, _snap((p1.x + p2.x) / 2)),
                )
                p1 = Point(shared_x, p1.y)
                p2 = Point(shared_x, p2.y)
        if fs in {"e", "w"} and ts in {"e", "w"} and p1.y != p2.y:
            if target_count == 1 and target.top + 14 <= p1.y <= target.bottom - 14:
                p2 = Point(p2.x, p1.y)
            elif source_count == 1 and source.top + 14 <= p2.y <= source.bottom - 14:
                p1 = Point(p1.x, p2.y)
        elif fs in {"n", "s"} and ts in {"n", "s"} and p1.x != p2.x:
            if target_count == 1 and target.left + 16 <= p1.x <= target.right - 16:
                p2 = Point(p1.x, p2.y)
            elif source_count == 1 and source.left + 16 <= p2.x <= source.right - 16:
                p1 = Point(p2.x, p1.y)
        aligned.append((p1, p2, fs, ts))
    out = aligned

    pair_edges: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    for edge_i, edge in enumerate(edges):
        pair_edges[tuple(sorted((edge.source, edge.target)))].append(edge_i)

    for (node_a, node_b), edge_indices in pair_edges.items():
        if len(edge_indices) < 2:
            continue
        pair_sides = [sides[edge_i] for edge_i in edge_indices]
        horizontal = all(fs in {"e", "w"} and ts in {"e", "w"} for fs, ts in pair_sides)
        vertical = all(fs in {"n", "s"} and ts in {"n", "s"} for fs, ts in pair_sides)
        ordered_edges = sorted(edge_indices, key=connection_key)
        if horizontal:
            low = max(boxes[node_a].top, boxes[node_b].top) + 16
            high = min(boxes[node_a].bottom, boxes[node_b].bottom) - 16
            if high - low < (len(ordered_edges) - 1) * ROUTE_STEP:
                continue
            for rank, edge_i in enumerate(ordered_edges):
                y = _snap(low + (rank + 1) * (high - low) / (len(ordered_edges) + 1))
                p1, p2, fs, ts = out[edge_i]
                out[edge_i] = (Point(p1.x, y), Point(p2.x, y), fs, ts)
        elif vertical:
            low = max(boxes[node_a].left, boxes[node_b].left) + 18
            high = min(boxes[node_a].right, boxes[node_b].right) - 18
            if high - low < (len(ordered_edges) - 1) * ROUTE_STEP:
                continue
            for rank, edge_i in enumerate(ordered_edges):
                x = _snap(low + (rank + 1) * (high - low) / (len(ordered_edges) + 1))
                p1, p2, fs, ts = out[edge_i]
                out[edge_i] = (Point(x, p1.y), Point(x, p2.y), fs, ts)
    # Port reordering/alignment may change the coordinate along a side. Snap
    # the normal coordinate back to the visible shape boundary afterwards;
    # this is essential for pill-shaped I/O blocks whose corners do not occupy
    # the full rectangular bounding box.
    return [
        (
            _rendered_boundary_point(boxes[edges[edge_i].source], fs, p1.x, p1.y),
            _rendered_boundary_point(boxes[edges[edge_i].target], ts, p2.x, p2.y),
            fs,
            ts,
        )
        for edge_i, (p1, p2, fs, ts) in enumerate(out)
    ]


def resolved_via(e: Edge, boxes: Dict[str, Box]) -> str:
    if e.via != "auto":
        return e.via
    backwards = boxes[e.target].col < boxes[e.source].col
    col_span = abs(boxes[e.target].col - boxes[e.source].col)
    # Exterior lanes are valuable for real feedback arcs, but a one-column
    # backward link—especially a diagonal controller connection—should stay
    # local. Explicit via hints remain available for exceptional layouts.
    if backwards and col_span >= 2 and e.kind == "response":
        return "bottom"
    if backwards and col_span >= 2 and e.kind in CONTROL_EDGE_KINDS:
        return "top"
    return "auto"


def route_edges(
    edges: List[Edge],
    boxes_list: List[Box],
    width: int,
    height: int,
    top_lane_base: int = TOP_LANE_Y,
    bottom_lane_base: Optional[int] = None,
    obstacle_rects: Sequence[Rect] = (),
    *,
    _source_port_coords: Optional[Dict[int, int]] = None,
    _optimize_ports: bool = True,
    _direct_cache: Optional[Dict[tuple, List[Tuple[tuple, List[Point]]]]] = None,
) -> Tuple[List[List[Point]], List[str]]:
    boxes = {b.id: b for b in boxes_list}
    routing_boxes = [*boxes_list]
    for obstacle_index, (left, top, right, bottom) in enumerate(obstacle_rects):
        obstacle_left = int(math.floor(left))
        obstacle_top = int(math.floor(top))
        routing_boxes.append(Box(
            id=f"__route_obstacle_{obstacle_index}",
            label="",
            kind="module",
            col=0,
            row=0,
            x=obstacle_left,
            y=obstacle_top,
            w=max(1, int(math.ceil(right)) - obstacle_left),
            h=max(1, int(math.ceil(bottom)) - obstacle_top),
        ))
    direct_cache = _direct_cache if _direct_cache is not None else {}
    group_members: Dict[str, List[Box]] = defaultdict(list)
    for box in boxes_list:
        if box.group:
            group_members[box.group].append(box)
    sides = edge_sides(edges, boxes)
    ports = assign_ports(edges, sides, boxes)
    if obstacle_rects:
        adjusted_ports = list(ports)
        adjusted_sides = list(sides)
        for edge_i, edge in enumerate(edges):
            p1, p2, from_side, to_side = adjusted_ports[edge_i]
            endpoint_specs = (
                (0, edge.source, edge.from_side, p1, p2, from_side),
                (1, edge.target, edge.to_side, p2, p1, to_side),
            )
            for (
                endpoint_index,
                block_id,
                explicit_side,
                point,
                other,
                side,
            ) in endpoint_specs:
                stub = outward(point, side)
                if explicit_side is not None or not any(
                    _segment_intersects_rect(point, stub, rect, 2)
                    for rect in obstacle_rects
                ):
                    continue
                alternatives = []
                for alternate_side in sorted(SIDES - {side}):
                    alternate_point = port_point(boxes[block_id], alternate_side, 0, 1)
                    alternate_stub = outward(alternate_point, alternate_side)
                    if any(
                        _segment_intersects_rect(
                            alternate_point, alternate_stub, rect, 2
                        )
                        for rect in obstacle_rects
                    ):
                        continue
                    alternatives.append((
                        abs(alternate_stub.x - other.x) + abs(alternate_stub.y - other.y),
                        alternate_side,
                        alternate_point,
                    ))
                if not alternatives:
                    continue
                _, side, point = min(alternatives)
                if endpoint_index == 0:
                    p1, from_side = point, side
                else:
                    p2, to_side = point, side
            adjusted_ports[edge_i] = (p1, p2, from_side, to_side)
            adjusted_sides[edge_i] = (from_side, to_side)
        ports = adjusted_ports
        sides = adjusted_sides
    if _source_port_coords:
        adjusted_ports = []
        for edge_i, (p1, p2, fs, ts) in enumerate(ports):
            if edge_i in _source_port_coords:
                coord = _source_port_coords[edge_i]
                p1 = (
                    Point(p1.x, coord)
                    if fs in {"e", "w"}
                    else Point(coord, p1.y)
                )
                p1 = _rendered_boundary_point(
                    boxes[edges[edge_i].source], fs, p1.x, p1.y
                )
            adjusted_ports.append((p1, p2, fs, ts))
        ports = adjusted_ports
    source_side_fanout: Dict[Tuple[str, str], int] = defaultdict(int)
    for edge, (_, _, from_side, _) in zip(edges, ports):
        source_side_fanout[(edge.source, from_side)] += 1
    used: Dict[Tuple[int, int], int] = defaultdict(int)
    used_axes: Dict[Tuple[int, int], set[str]] = defaultdict(set)
    routes: List[List[Point]] = []
    warnings: List[str] = []

    top_lane_base = _snap(top_lane_base)
    if bottom_lane_base is None:
        bottom_lane_base = _snap(height - 30)
    else:
        bottom_lane_base = _snap(bottom_lane_base)
    top_count = 0
    bottom_count = 0
    local_top_count: Dict[str, int] = defaultdict(int)
    local_bottom_count: Dict[str, int] = defaultdict(int)

    # Establish short datapath links before longer fanout and return paths.
    # This makes the visual backbone stable while later routes choose among
    # the remaining quiet channels.
    order = sorted(
        range(len(edges)),
        key=lambda i: (
            {"data": 0, "clock": 1, "control": 2, "response": 3}[edges[i].kind],
            (
                abs(boxes[edges[i].source].cx - boxes[edges[i].target].cx)
                + abs(boxes[edges[i].source].cy - boxes[edges[i].target].cy)
            ),
            i,
        ),
    )
    routed: Dict[int, List[Point]] = {}

    for i in order:
        e = edges[i]
        p1, p2, fs, ts = ports[i]
        source_stub = PORT_STUB if fs in {"e", "w"} else VERTICAL_PORT_STUB
        target_stub = PORT_STUB if ts in {"e", "w"} else VERTICAL_PORT_STUB
        if fs == "e" and ts == "w" and p1.x <= p2.x:
            available = max(2 * ROUTE_STEP, p2.x - p1.x - ROUTE_STEP)
            source_stub = target_stub = min(PORT_STUB, available // 2)
        elif fs == "w" and ts == "e" and p2.x <= p1.x:
            available = max(2 * ROUTE_STEP, p1.x - p2.x - ROUTE_STEP)
            source_stub = target_stub = min(PORT_STUB, available // 2)
        elif fs == "s" and ts == "n" and p1.y <= p2.y:
            available = max(2 * ROUTE_STEP, p2.y - p1.y - ROUTE_STEP)
            source_stub = target_stub = min(VERTICAL_PORT_STUB, available // 2)
        elif fs == "n" and ts == "s" and p2.y <= p1.y:
            available = max(2 * ROUTE_STEP, p1.y - p2.y - ROUTE_STEP)
            source_stub = target_stub = min(VERTICAL_PORT_STUB, available // 2)
        s = outward(p1, fs, source_stub)
        t = outward(p2, ts, target_stub)

        via = resolved_via(e, boxes)
        used_fallback = False
        endpoint_ids = {e.source, e.target}

        if via == "top":
            common_group = (
                boxes[e.source].group
                if boxes[e.source].group == boxes[e.target].group
                else None
            )
            nearby_pair = (
                abs(boxes[e.source].col - boxes[e.target].col) <= 2
                and abs(boxes[e.source].cy - boxes[e.target].cy)
                <= ROW_GAP + max(boxes[e.source].h, boxes[e.target].h)
            )
            if common_group or nearby_pair:
                lane_y = _floor_snap(
                    min(
                        box.top
                        for box in (
                            group_members[common_group]
                            if common_group
                            else (boxes[e.source], boxes[e.target])
                        )
                    )
                    - 2 * ROUTE_CLEAR
                    - local_top_count[common_group or "__nearby__"] * ROUTE_STEP
                )
                local_top_count[common_group or "__nearby__"] += 1
                lane_y = max(top_lane_base, lane_y)
            else:
                lane_y = top_lane_base + top_count * ROUTE_STEP
                top_count += 1
            exterior = simplify_polyline(
                [s, Point(s.x, lane_y), Point(t.x, lane_y), t]
            )
            if route_clear_of_boxes(exterior, routing_boxes, endpoint_ids):
                core = exterior
            else:
                first, fallback_a = astar_route(
                    s, Point(s.x, lane_y), routing_boxes, width, height, endpoint_ids, used, lane_y, used_axes
                )
                last, fallback_b = astar_route(
                    Point(t.x, lane_y), t, routing_boxes, width, height, endpoint_ids, used, lane_y, used_axes
                )
                core = simplify_polyline(first + [Point(t.x, lane_y)] + last[1:])
                used_fallback = fallback_a or fallback_b
        elif via == "bottom":
            common_group = (
                boxes[e.source].group
                if boxes[e.source].group == boxes[e.target].group
                else None
            )
            nearby_pair = (
                abs(boxes[e.source].col - boxes[e.target].col) <= 2
                and abs(boxes[e.source].cy - boxes[e.target].cy)
                <= ROW_GAP + max(boxes[e.source].h, boxes[e.target].h)
            )
            if common_group or nearby_pair:
                lane_y = _ceil_snap(
                    max(
                        box.bottom
                        for box in (
                            group_members[common_group]
                            if common_group
                            else (boxes[e.source], boxes[e.target])
                        )
                    )
                    + 2 * ROUTE_CLEAR
                    + local_bottom_count[common_group or "__nearby__"] * ROUTE_STEP
                )
                local_bottom_count[common_group or "__nearby__"] += 1
                lane_y = min(bottom_lane_base, lane_y)
            else:
                lane_y = bottom_lane_base + bottom_count * ROUTE_STEP
                bottom_count += 1
            exterior = simplify_polyline(
                [s, Point(s.x, lane_y), Point(t.x, lane_y), t]
            )
            if route_clear_of_boxes(exterior, routing_boxes, endpoint_ids):
                core = exterior
            else:
                first, fallback_a = astar_route(
                    s, Point(s.x, lane_y), routing_boxes, width, height, endpoint_ids, used, lane_y, used_axes
                )
                last, fallback_b = astar_route(
                    Point(t.x, lane_y), t, routing_boxes, width, height, endpoint_ids, used, lane_y, used_axes
                )
                core = simplify_polyline(first + [Point(t.x, lane_y)] + last[1:])
                used_fallback = fallback_a or fallback_b
        else:
            cross_group_handoff = None
            source_box = boxes[e.source]
            target_box = boxes[e.target]
            if (
                e.kind == "data"
                and source_box.group
                and target_box.group
                and source_box.group != target_box.group
                and source_box.cy < target_box.cy
                and ts == "n"
            ):
                source_group_bottom = max(
                    box.bottom for box in group_members[source_box.group]
                )
                target_group_top = min(
                    box.top for box in group_members[target_box.group]
                )
                corridor_low = _ceil_snap(source_group_bottom + ROUTE_CLEAR)
                corridor_high = _floor_snap(target_group_top - ROUTE_CLEAR)
                if corridor_low <= corridor_high:
                    corridor_ys = list(
                        range(corridor_low, corridor_high + 1, ROUTE_STEP)
                    )
                    corridor_ys.sort(
                        key=lambda y: abs(
                            y - (source_group_bottom + target_group_top) / 2
                        )
                    )
                    clear_handoffs = []
                    for corridor_y in corridor_ys:
                        candidate = simplify_polyline(
                            [
                                s,
                                Point(s.x, corridor_y),
                                Point(t.x, corridor_y),
                                t,
                            ]
                        )
                        if route_clear_of_boxes(
                            candidate, routing_boxes, endpoint_ids
                        ):
                            clear_handoffs.append((
                                _route_candidate_score(
                                    candidate, used, used_axes, corridor_y
                                ),
                                abs(
                                    corridor_y
                                    - (source_group_bottom + target_group_top) / 2
                                ),
                                tuple((point.x, point.y) for point in candidate),
                                candidate,
                            ))
                    if clear_handoffs:
                        cross_group_handoff = min(clear_handoffs)[3]

            # A controller directly above a nearby consumer reads best as a
            # compact, symmetric fanout in the row gap. Let A* handle longer
            # and obstructed control paths, but do not let soft wire occupancy
            # turn these local links into arbitrary-looking little doglegs.
            local_control = None
            long_control = None
            if (
                e.kind in CONTROL_EDGE_KINDS
                and fs == "s"
                and ts == "n"
                and 0 <= t.y - s.y <= ROW_GAP + 2 * PORT_STUB
            ):
                mid_y = _snap((s.y + t.y) / 2)
                local_candidates = []
                for corridor_y in range(
                    _ceil_snap(min(s.y, t.y)),
                    _floor_snap(max(s.y, t.y)) + ROUTE_STEP,
                    ROUTE_STEP,
                ):
                    candidate = simplify_polyline(
                        [s, Point(s.x, corridor_y), Point(t.x, corridor_y), t]
                    )
                    if route_clear_of_boxes(candidate, routing_boxes, endpoint_ids):
                        local_candidates.append((
                            _route_candidate_score(
                                candidate, used, used_axes, mid_y
                            ),
                            abs(corridor_y - mid_y),
                            tuple((point.x, point.y) for point in candidate),
                            candidate,
                        ))
                if local_candidates:
                    local_control = min(local_candidates)[3]

            if (
                local_control is None
                and e.kind in CONTROL_EDGE_KINDS
                and fs in {"e", "w"}
                and ts in {"n", "s"}
                and t.y > s.y
            ):
                low_x, high_x = sorted((s.x, t.x))
                desired_x = (
                    boxes[e.target].left - ROUTE_CLEAR - 2 * ROUTE_STEP
                    if t.x >= s.x
                    else boxes[e.target].right + ROUTE_CLEAR + 2 * ROUTE_STEP
                )
                # Keep the vertical trunk beyond every normal horizontal port
                # stub on this source side. Otherwise a later sibling edge can
                # leave the same block, turn at the stub end, and visually join
                # or cross this trunk immediately outside the source.
                if fs == "e" and t.x >= s.x:
                    desired_x = max(
                        desired_x,
                        boxes[e.source].right + PORT_STUB + ROUTE_STEP,
                    )
                elif fs == "w" and t.x <= s.x:
                    desired_x = min(
                        desired_x,
                        boxes[e.source].left - PORT_STUB - ROUTE_STEP,
                    )
                candidate_xs = list(range(_ceil_snap(low_x), _floor_snap(high_x) + 1, ROUTE_STEP))
                candidate_xs.sort(key=lambda x: (abs(x - desired_x), abs(x - t.x)))
                long_candidates = []
                for corridor_x in candidate_xs:
                    candidate = simplify_polyline(
                        [s, Point(corridor_x, s.y), Point(corridor_x, t.y), t]
                    )
                    if route_clear_of_boxes(candidate, routing_boxes, endpoint_ids):
                        long_candidates.append((
                            _route_candidate_score(
                                candidate, used, used_axes, None
                            ),
                            abs(corridor_x - desired_x),
                            tuple((point.x, point.y) for point in candidate),
                            candidate,
                        ))
                if long_candidates:
                    long_control = min(long_candidates)[3]

            preferred_y = None
            if cross_group_handoff is not None:
                core = cross_group_handoff
            elif local_control is not None:
                core = local_control
            elif long_control is not None:
                core = long_control
            elif (
                e.kind == "data"
                and abs(boxes[e.source].cy - boxes[e.target].cy) <= ROUTE_STEP
                and abs(boxes[e.source].col - boxes[e.target].col) > 1
            ):
                # Multiple same-row outputs receive ordered ports. Carry that
                # order into the bypass choice: a top port uses the upper lane
                # and a bottom port the lower lane. When the destination also
                # receives control from above, prefer the lower bypass so the
                # data route does not occupy the controller fan-in corridor.
                target_has_north_control = any(
                    j != i
                    and other.kind in CONTROL_EDGE_KINDS
                    and other.target == e.target
                    and sides[j][1] == "n"
                    for j, other in enumerate(edges)
                )
                if target_has_north_control:
                    preferred_y = _ceil_snap(
                        max(boxes[e.source].bottom, boxes[e.target].bottom) + 30
                    )
                elif p1.y < boxes[e.source].cy:
                    preferred_y = _floor_snap(
                        min(boxes[e.source].top, boxes[e.target].top) - 30
                    )
                elif p1.y > boxes[e.source].cy:
                    preferred_y = _ceil_snap(
                        max(boxes[e.source].bottom, boxes[e.target].bottom) + 30
                    )
            if (
                cross_group_handoff is None
                and local_control is None
                and long_control is None
            ):
                if (
                    preferred_y is None
                    and fs == "s"
                    and e.kind in CONTROL_EDGE_KINDS
                ):
                    nearer_siblings = [
                        boxes[other.target]
                        for other in edges
                        if other is not e
                        and other.source == e.source
                        and boxes[other.target].cy < boxes[e.target].cy
                    ]
                    if nearer_siblings:
                        preferred_y = _ceil_snap(
                            max(box.bottom for box in nearer_siblings)
                            + ROUTE_CLEAR
                        )
                preferred_trunk_x = None
                if source_side_fanout[(e.source, fs)] > 1:
                    if fs == "e":
                        preferred_trunk_x = _snap(
                            boxes[e.source].right + PORT_STUB + ROUTE_STEP
                        )
                    elif fs == "w":
                        preferred_trunk_x = _snap(
                            boxes[e.source].left - PORT_STUB - ROUTE_STEP
                        )
                direct = direct_orthogonal_route(
                    s,
                    t,
                    routing_boxes,
                    endpoint_ids,
                    width,
                    height,
                    used,
                    used_axes,
                    preferred_y,
                    preferred_trunk_x,
                    direct_cache,
                )
                if direct is not None:
                    core = direct
                else:
                    core, used_fallback = astar_route(
                        s, t, routing_boxes, width, height, endpoint_ids, used, preferred_y, used_axes
                    )

        if used_fallback:
            warnings.append(
                f"edge {i} ({e.source}->{e.target}) used fallback routing; inspect or adjust the IR"
            )

        route = simplify_polyline([p1, s] + core[1:-1] + [t, p2])
        route = _deoverlap_route(
            route,
            list(routed.values()),
            routing_boxes,
            endpoint_ids,
        )
        routed[i] = route
        for a, b in zip(route, route[1:]):
            if a.x == b.x:
                y0, y1 = sorted((a.y, b.y))
                for y in range(_snap(y0), _snap(y1) + ROUTE_STEP, ROUTE_STEP):
                    used[(_snap(a.x), y)] += 1
                    used_axes[(_snap(a.x), y)].add("v")
            elif a.y == b.y:
                x0, x1 = sorted((a.x, b.x))
                for x in range(_snap(x0), _snap(x1) + ROUTE_STEP, ROUTE_STEP):
                    used[(x, _snap(a.y))] += 1
                    used_axes[(x, _snap(a.y))].add("h")

    for i in range(len(edges)):
        routes.append(routed[i])

    if _optimize_ports and len(edges) > 1:
        source_side_edges: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        base_coords: Dict[int, int] = {}
        for edge_i, (p1, _, fs, _) in enumerate(ports):
            source_side_edges[(edges[edge_i].source, fs)].append(edge_i)
            base_coords[edge_i] = p1.y if fs in {"e", "w"} else p1.x

        current_routes = routes
        current_warnings = warnings
        current_overrides = dict(_source_port_coords or {})
        current_score = route_quality_score(current_routes, current_warnings)
        for _ in range(2):
            improved = False
            for _, edge_indices in sorted(source_side_edges.items()):
                if not 2 <= len(edge_indices) <= 4:
                    continue
                current_coords = tuple(
                    current_overrides.get(edge_i, base_coords[edge_i])
                    for edge_i in edge_indices
                )
                best = None
                for permutation in sorted(set(itertools.permutations(current_coords))):
                    if permutation == current_coords:
                        continue
                    trial_overrides = dict(current_overrides)
                    for edge_i, coord in zip(edge_indices, permutation):
                        trial_overrides[edge_i] = coord
                    trial_routes, trial_warnings = route_edges(
                        edges,
                        boxes_list,
                        width,
                        height,
                        top_lane_base,
                        bottom_lane_base,
                        obstacle_rects,
                        _source_port_coords=trial_overrides,
                        _optimize_ports=False,
                        _direct_cache=direct_cache,
                        )
                    trial_score = route_quality_score(
                        trial_routes, trial_warnings
                    )
                    if trial_score < current_score and (
                        best is None or trial_score < best[0]
                    ):
                        best = (
                            trial_score,
                            trial_routes,
                            trial_warnings,
                            trial_overrides,
                        )
                if best is None:
                    continue
                current_score, current_routes, current_warnings, current_overrides = best
                improved = True
            if not improved:
                break
        routes, warnings = current_routes, current_warnings
    return routes, warnings


def longest_segment_mid(route: Sequence[Point]) -> Tuple[int, int, bool]:
    best = (-1, 0, 0, True)
    # Prefer horizontal segments for readable labels.
    for a, b in zip(route, route[1:]):
        length = abs(a.x - b.x) + abs(a.y - b.y)
        horizontal = a.y == b.y
        score = length + (24 if horizontal else 0)
        if score > best[0]:
            best = (score, (a.x + b.x) // 2, (a.y + b.y) // 2, horizontal)
    return best[1], best[2], best[3]

__all__ = [name for name in globals() if not name.startswith("__")]
