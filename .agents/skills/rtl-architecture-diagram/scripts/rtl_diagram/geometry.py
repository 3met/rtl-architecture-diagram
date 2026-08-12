"""Dependency-free orthogonal geometry primitives used throughout rendering."""

from __future__ import annotations

from typing import Tuple

from .model import Box, Point


Rect = Tuple[float, float, float, float]


def box_rect(box: Box, pad: float = 0) -> Rect:
    return (box.left - pad, box.top - pad, box.right + pad, box.bottom + pad)


def rects_overlap(left: Rect, right: Rect, pad: float = 0) -> bool:
    return not (
        left[2] + pad <= right[0]
        or right[2] + pad <= left[0]
        or left[3] + pad <= right[1]
        or right[3] + pad <= left[1]
    )


def rect_contains(outer: Rect, inner: Rect, inset: float = 0) -> bool:
    return (
        inner[0] >= outer[0] + inset
        and inner[1] >= outer[1] + inset
        and inner[2] <= outer[2] - inset
        and inner[3] <= outer[3] - inset
    )


def segment_intersects_rect(
    start: Point, end: Point, rect: Rect, pad: float = 0, interior: bool = False
) -> bool:
    left, top, right, bottom = (
        rect[0] - pad,
        rect[1] - pad,
        rect[2] + pad,
        rect[3] + pad,
    )
    if start.x == end.x:
        low, high = sorted((start.y, end.y))
        if interior:
            return left < start.x < right and max(low, top) < min(high, bottom)
        return left <= start.x <= right and max(low, top) <= min(high, bottom)
    if start.y == end.y:
        low, high = sorted((start.x, end.x))
        if interior:
            return top < start.y < bottom and max(low, left) < min(high, right)
        return top <= start.y <= bottom and max(low, left) <= min(high, right)
    return rects_overlap(
        (min(start.x, end.x), min(start.y, end.y), max(start.x, end.x), max(start.y, end.y)),
        (left, top, right, bottom),
    )


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Return true for crossings, touches, and collinear overlap."""
    def orientation(p: Point, q: Point, r: Point) -> float:
        return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)

    def on_segment(p: Point, q: Point, r: Point) -> bool:
        return (
            min(p.x, r.x) <= q.x <= max(p.x, r.x)
            and min(p.y, r.y) <= q.y <= max(p.y, r.y)
        )

    o1, o2 = orientation(a, b, c), orientation(a, b, d)
    o3, o4 = orientation(c, d, a), orientation(c, d, b)
    if (o1 > 0 > o2 or o2 > 0 > o1) and (o3 > 0 > o4 or o4 > 0 > o3):
        return True
    return (
        (o1 == 0 and on_segment(a, c, b))
        or (o2 == 0 and on_segment(a, d, b))
        or (o3 == 0 and on_segment(c, a, d))
        or (o4 == 0 and on_segment(c, b, d))
    )
