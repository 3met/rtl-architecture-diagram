"""Route-quality measurements shared by optimization and linting."""

from __future__ import annotations

from typing import List, Sequence, Tuple

from .model import Point


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
    routes: Sequence[Sequence[Point]], warnings: Sequence[str] = ()
) -> Tuple[int, int, int, int, int, int, int]:
    """Score a routed diagram with crossings and bends as first-class costs."""
    crossings = sum(
        len(perpendicular_route_crossings(left, right))
        for left_index, left in enumerate(routes)
        for right in routes[left_index + 1:]
    )
    overlap = sum(
        collinear_route_overlap_length(left, right)
        for left_index, left in enumerate(routes)
        for right in routes[left_index + 1:]
    )
    corner_touches = sum(
        len(ambiguous_route_corner_touches(left, right))
        for left_index, left in enumerate(routes)
        for right in routes[left_index + 1:]
    )
    bends = sum(max(0, len(route) - 2) for route in routes)
    length = sum(
        abs(start.x - end.x) + abs(start.y - end.y)
        for route in routes
        for start, end in zip(route, route[1:])
    )
    return (
        len(warnings),
        crossings * 250
        + corner_touches * 220
        + bends * 60
        + overlap * 200
        + length,
        crossings,
        corner_touches,
        bends,
        overlap,
        length,
    )
