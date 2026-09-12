"""Route-quality measurements shared by optimization and linting."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .model import Box, Edge, Point


def ambiguous_route_corner_touches(
    route_a: Sequence[Point], route_b: Sequence[Point]
) -> List[Point]:
    """Return complementary elbow touches that look like four-way junctions.

    These are not mathematical crossings: each wire merely turns at the same
    point. Visually, however, a north-to-east elbow touching a west-to-south
    elbow is indistinguishable from a connected four-way junction.
    """
    def elbow_directions(route: Sequence[Point]) -> dict[Point, frozenset[str]]:
        result = {}
        for previous, point, following in zip(route, route[1:], route[2:]):
            directions = set()
            for adjacent in (previous, following):
                if adjacent.x < point.x:
                    directions.add("w")
                elif adjacent.x > point.x:
                    directions.add("e")
                elif adjacent.y < point.y:
                    directions.add("n")
                elif adjacent.y > point.y:
                    directions.add("s")
            if len(directions) == 2:
                result[point] = frozenset(directions)
        return result

    left = elbow_directions(route_a)
    right = elbow_directions(route_b)
    all_directions = frozenset({"n", "s", "e", "w"})
    return sorted(
        (
            point
            for point in left.keys() & right.keys()
            if left[point].isdisjoint(right[point])
            and left[point] | right[point] == all_directions
        ),
        key=lambda point: (point.y, point.x),
    )


def perpendicular_route_crossings(
    route_a: Sequence[Point], route_b: Sequence[Point]
) -> List[Point]:
    """Return true interior crossings, excluding endpoint touches and sharing."""
    crossings = set()
    for a1, a2 in zip(route_a, route_a[1:]):
        for b1, b2 in zip(route_b, route_b[1:]):
            if (
                a1.y == a2.y
                and b1.x == b2.x
                and min(a1.x, a2.x) < b1.x < max(a1.x, a2.x)
                and min(b1.y, b2.y) < a1.y < max(b1.y, b2.y)
            ):
                crossings.add(Point(b1.x, a1.y))
            elif (
                a1.x == a2.x
                and b1.y == b2.y
                and min(b1.x, b2.x) < a1.x < max(b1.x, b2.x)
                and min(a1.y, a2.y) < b1.y < max(a1.y, a2.y)
            ):
                crossings.add(Point(a1.x, b1.y))
    return sorted(crossings, key=lambda point: (point.y, point.x))


def collinear_route_overlap_length(
    route_a: Sequence[Point], route_b: Sequence[Point]
) -> int:
    """Return the total positive-length overlap between two routed wires."""
    overlap = 0
    for a1, a2 in zip(route_a, route_a[1:]):
        for b1, b2 in zip(route_b, route_b[1:]):
            if a1.y == a2.y == b1.y == b2.y:
                overlap += max(
                    0,
                    min(max(a1.x, a2.x), max(b1.x, b2.x))
                    - max(min(a1.x, a2.x), min(b1.x, b2.x)),
                )
            elif a1.x == a2.x == b1.x == b2.x:
                overlap += max(
                    0,
                    min(max(a1.y, a2.y), max(b1.y, b2.y))
                    - max(min(a1.y, a2.y), min(b1.y, b2.y)),
                )
    return overlap


def route_quality_score(
    routes: Sequence[Sequence[Point]],
    warnings: Sequence[str] = (),
    edges: Optional[Sequence[Edge]] = None,
    boxes: Optional[Sequence[Box]] = None,
    area: int = 0,
) -> Tuple[int, int, int, int, float, float, int, int]:
    """Lexicographically score ambiguity before weighted geometry and area."""
    by_id: Dict[str, Box] = {box.id: box for box in boxes or ()}
    weights = [1.0] * len(routes)
    if edges is not None:
        weights = [
            edge.importance
            * ((by_id.get(edge.source).importance if edge.source in by_id else 1.0)
               * (by_id.get(edge.target).importance if edge.target in by_id else 1.0)) ** 0.5
            for edge in edges
        ]
    crossings = overlap = corner_touches = bends = length = 0
    weighted_conflicts = 0.0
    weighted_length_and_bends = 0.0
    for left_index, left in enumerate(routes):
        bends_i = max(0, len(left) - 2)
        length_i = sum(
            abs(start.x - end.x) + abs(start.y - end.y)
            for start, end in zip(left, left[1:])
        )
        bends += bends_i
        length += length_i
        weighted_length_and_bends += weights[left_index] * (
            length_i + bends_i * 120
        )
        for right_index in range(left_index + 1, len(routes)):
            right = routes[right_index]
            pair_weight = max(weights[left_index], weights[right_index])
            cross = len(perpendicular_route_crossings(left, right))
            shared = collinear_route_overlap_length(left, right)
            corners = len(ambiguous_route_corner_touches(left, right))
            crossings += cross
            overlap += shared
            corner_touches += corners
            weighted_conflicts += pair_weight * (
                cross * 1000 + corners * 500 + shared * 200
            )
    return (
        len(warnings),
        crossings,
        corner_touches,
        overlap,
        weighted_conflicts + weighted_length_and_bends,
        float(area),
        bends,
        length,
    )
