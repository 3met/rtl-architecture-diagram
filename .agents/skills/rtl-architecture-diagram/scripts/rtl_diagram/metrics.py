"""Route-quality measurements shared by optimization and linting."""

from __future__ import annotations

from typing import List, Sequence, Tuple

from .model import Point


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
) -> Tuple[int, int, int, int, int, int]:
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
    bends = sum(max(0, len(route) - 2) for route in routes)
    length = sum(
        abs(start.x - end.x) + abs(start.y - end.y)
        for route in routes
        for start, end in zip(route, route[1:])
    )
    return (
        len(warnings),
        crossings * 250 + bends * 60 + overlap * 200 + length,
        crossings,
        bends,
        overlap,
        length,
    )
