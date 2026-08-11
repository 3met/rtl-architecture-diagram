#!/usr/bin/env python3
"""Deterministic compact SVG renderer for the rtl-architecture-diagram skill.

Input is a deliberately small JSON IR. The renderer owns geometry, port
placement, orthogonal routing, labels, groups, and block symbols.
No third-party packages are required.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


BLOCK_KINDS = {"module", "logic", "memory", "fifo", "mux", "reg", "fsm", "arbiter", "io"}
EDGE_KINDS = {"data", "control", "response", "clock"}
SIDES = {"n", "s", "e", "w"}

# Geometry. Kept compact on purpose.
MARGIN_X = 62
MARGIN_TOP = 72
MARGIN_BOTTOM = 62
COL_GAP = 88
ROW_GAP = 64
GROUP_PAD = 22
ROUTE_CLEAR = 14
ROUTE_STEP = 10
PORT_STUB = 18
FONT = 14
SMALL_FONT = 11
TITLE_FONT = 18
TITLE_Y = 30
TOP_LANE_Y = 50
LABEL_HEIGHT = 16
LABEL_GAP = 4


@dataclass
class Box:
    id: str
    label: str
    kind: str
    col: int
    row: int
    subtitle: str = ""
    group: Optional[str] = None
    w: int = 140
    h: int = 64
    x: int = 0
    y: int = 0

    @property
    def left(self) -> int:
        return self.x

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def top(self) -> int:
        return self.y

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


@dataclass
class Edge:
    source: str
    target: str
    source_port: str = ""
    target_port: str = ""
    label: str = ""
    width: Optional[int] = None
    kind: str = "data"
    from_side: Optional[str] = None
    to_side: Optional[str] = None
    via: str = "auto"


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class LabelPlacement:
    x: float
    y: float
    width: float
    height: float = LABEL_HEIGHT
    leader_start: Optional[Point] = None
    leader_end: Optional[Point] = None
    fallback: bool = False

    @property
    def rect(self) -> Tuple[float, float, float, float]:
        return (
            self.x - self.width / 2,
            self.y - 12,
            self.x + self.width / 2,
            self.y - 12 + self.height,
        )


class DiagramError(Exception):
    pass


def _split_endpoint(value: str) -> Tuple[str, str]:
    if "." in value:
        node, port = value.split(".", 1)
        return node.strip(), port.strip()
    return value.strip(), ""


def _auto_size(label: str, subtitle: str, kind: str) -> Tuple[int, int]:
    longest = max([len(x) for x in label.split("\n")] + ([len(subtitle)] if subtitle else [0]))
    if kind == "mux":
        return max(86, min(150, longest * 7 + 34)), 60
    if kind == "reg":
        return max(90, min(150, longest * 7 + 30)), 54
    if kind == "io":
        return max(104, min(180, longest * 7 + 34)), 54
    w = max(126, min(210, longest * 7 + 38))
    h = 72 if subtitle else 62
    return w, h


def load_diagram(path: Path) -> Tuple[str, List[Box], List[Edge], List[dict], List[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise DiagramError(f"invalid JSON: {e}") from e

    warnings: List[str] = []
    if not isinstance(data, dict):
        raise DiagramError("top-level JSON must be an object")

    title = str(data.get("title", "Architecture"))
    raw_blocks = data.get("blocks", [])
    raw_edges = data.get("edges", [])
    raw_groups = data.get("groups", [])
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise DiagramError("'blocks' must be a non-empty array")
    if not isinstance(raw_edges, list):
        raise DiagramError("'edges' must be an array")
    if not isinstance(raw_groups, list):
        raise DiagramError("'groups' must be an array")

    boxes: List[Box] = []
    seen = set()
    occupied_at = {}
    for i, b in enumerate(raw_blocks):
        if not isinstance(b, dict):
            raise DiagramError(f"block {i} must be an object")
        bid = str(b.get("id", "")).strip()
        label = str(b.get("label", bid)).strip()
        kind = str(b.get("kind", "module"))
        at = b.get("at")
        if not bid:
            raise DiagramError(f"block {i} has no id")
        if "." in bid:
            raise DiagramError(f"block {bid!r}: ids cannot contain '.' because dots delimit port names")
        if bid in seen:
            raise DiagramError(f"duplicate block id: {bid}")
        seen.add(bid)
        if kind not in BLOCK_KINDS:
            raise DiagramError(f"block {bid}: unknown kind {kind!r}")
        if not (
            isinstance(at, list)
            and len(at) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in at)
        ):
            raise DiagramError(f"block {bid}: 'at' must be [column,row] integers")
        col, row = at
        if (col, row) in occupied_at:
            raise DiagramError(f"blocks {occupied_at[(col,row)]} and {bid} share grid position {at}")
        occupied_at[(col, row)] = bid
        subtitle = str(b.get("subtitle", "")).strip()
        group = str(b["group"]) if b.get("group") is not None else None
        if "size" in b:
            size = b["size"]
            if not (
                isinstance(size, list)
                and len(size) == 2
                and all(
                    isinstance(v, (int, float))
                    and not isinstance(v, bool)
                    and math.isfinite(v)
                    and v > 20
                    for v in size
                )
            ):
                raise DiagramError(f"block {bid}: size must be [width,height] > 20")
            w, h = int(size[0]), int(size[1])
        else:
            w, h = _auto_size(label, subtitle, kind)
        boxes.append(Box(bid, label, kind, col, row, subtitle, group, w, h))

    by_id = {b.id: b for b in boxes}
    edges: List[Edge] = []
    for i, e in enumerate(raw_edges):
        if not isinstance(e, dict):
            raise DiagramError(f"edge {i} must be an object")
        if "from" not in e or "to" not in e:
            raise DiagramError(f"edge {i} requires 'from' and 'to'")
        src, sp = _split_endpoint(str(e["from"]))
        dst, tp = _split_endpoint(str(e["to"]))
        if src not in by_id:
            raise DiagramError(f"edge {i}: unknown source block {src!r}")
        if dst not in by_id:
            raise DiagramError(f"edge {i}: unknown target block {dst!r}")
        kind = str(e.get("kind", "data"))
        if kind not in EDGE_KINDS:
            raise DiagramError(f"edge {i}: unknown kind {kind!r}")
        fs = e.get("from_side")
        ts = e.get("to_side")
        if fs is not None and fs not in SIDES:
            raise DiagramError(f"edge {i}: from_side must be n/s/e/w")
        if ts is not None and ts not in SIDES:
            raise DiagramError(f"edge {i}: to_side must be n/s/e/w")
        via = str(e.get("via", "auto"))
        if via not in {"auto", "top", "bottom"}:
            raise DiagramError(f"edge {i}: via must be auto/top/bottom")
        width = e.get("width")
        if width is not None and (
            not isinstance(width, int) or isinstance(width, bool) or width <= 0
        ):
            raise DiagramError(f"edge {i}: width must be a positive integer")
        edges.append(Edge(src, dst, sp, tp, str(e.get("label", "")).strip(), width, kind, fs, ts, via))

    group_ids = set()
    for i, g in enumerate(raw_groups):
        if not isinstance(g, dict) or not str(g.get("id", "")).strip():
            raise DiagramError(f"group {i} requires an id")
        gid = str(g["id"])
        if gid in group_ids:
            raise DiagramError(f"duplicate group id: {gid}")
        group_ids.add(gid)
    for b in boxes:
        if b.group and b.group not in group_ids:
            warnings.append(f"block {b.id} references undeclared group {b.group!r}; group will still be drawn")
            group_ids.add(b.group)
            raw_groups.append({"id": b.group, "label": b.group})

    if len(boxes) > 25:
        warnings.append(f"{len(boxes)} blocks: consider splitting the diagram (recommended <=25)")
    if len(edges) > 45:
        warnings.append(f"{len(edges)} edges: consider aggregating buses or splitting the diagram (recommended <=45)")

    return title, boxes, edges, raw_groups, warnings


def layout_boxes(boxes: List[Box]) -> Tuple[int, int]:
    cols = sorted({b.col for b in boxes})
    rows = sorted({b.row for b in boxes})
    col_w = {c: max(b.w for b in boxes if b.col == c) for c in cols}
    row_h = {r: max(b.h for b in boxes if b.row == r) for r in rows}

    col_x: Dict[int, int] = {}
    x = MARGIN_X
    last_c = cols[0]
    for c in cols:
        if c != cols[0]:
            # Preserve intentional semantic gaps without making them enormous.
            x += COL_GAP + min(2, max(0, c - last_c - 1)) * 34
        col_x[c] = x
        x += col_w[c]
        last_c = c

    row_y: Dict[int, int] = {}
    y = MARGIN_TOP
    last_r = rows[0]
    for r in rows:
        if r != rows[0]:
            y += ROW_GAP + min(2, max(0, r - last_r - 1)) * 28
        row_y[r] = y
        y += row_h[r]
        last_r = r

    for b in boxes:
        b.x = _snap(col_x[b.col] + (col_w[b.col] - b.w) // 2)
        b.y = _snap(row_y[b.row] + (row_h[b.row] - b.h) // 2)

    width = max(b.right for b in boxes) + MARGIN_X
    height = max(b.bottom for b in boxes) + MARGIN_BOTTOM
    return _snap(width), _snap(height)


def _snap(v: float, step: int = ROUTE_STEP) -> int:
    return int(round(v / step) * step)


def _floor_snap(v: float, step: int = ROUTE_STEP) -> int:
    return int(math.floor(v / step) * step)


def _ceil_snap(v: float, step: int = ROUTE_STEP) -> int:
    return int(math.ceil(v / step) * step)


def infer_side(a: Box, b: Box, outgoing: bool = True) -> str:
    # Use the side of `a` that faces `b`. Direction does not change geometry;
    # the argument is retained for compatibility with older IR/render calls.
    dx = b.cx - a.cx
    dy = b.cy - a.cy
    if abs(dx) >= abs(dy) * 0.72:
        return "e" if dx >= 0 else "w"
    return "s" if dy >= 0 else "n"


def port_point(b: Box, side: str, index: int, count: int) -> Point:
    # Spread multiple wires across a side. Keep the normal coordinate exactly
    # on the box boundary; only the coordinate along the side is grid-snapped.
    if count <= 1:
        frac = 0.5
    else:
        frac = (index + 1) / (count + 1)
    if side in {"e", "w"}:
        y = _snap(b.top + max(16, min(b.h - 16, b.h * frac)))
        x = b.right if side == "e" else b.left
    else:
        x = _snap(b.left + max(18, min(b.w - 18, b.w * frac)))
        y = b.bottom if side == "s" else b.top
    return Point(int(x), int(y))


def outward(p: Point, side: str, dist: int = PORT_STUB) -> Point:
    if side == "e":
        return Point(_snap(p.x + dist), p.y)
    if side == "w":
        return Point(_snap(p.x - dist), p.y)
    if side == "n":
        return Point(p.x, _snap(p.y - dist))
    return Point(p.x, _snap(p.y + dist))


def group_rects(boxes: List[Box], groups: List[dict]) -> List[Tuple[str, str, int, int, int, int]]:
    labels = {str(g["id"]): str(g.get("label", g["id"])) for g in groups}
    members: Dict[str, List[Box]] = defaultdict(list)
    for b in boxes:
        if b.group:
            members[b.group].append(b)
    result = []
    for gid, bs in members.items():
        left = min(b.left for b in bs) - GROUP_PAD
        top = min(b.top for b in bs) - GROUP_PAD - 10
        right = max(b.right for b in bs) + GROUP_PAD
        bottom = max(b.bottom for b in bs) + GROUP_PAD
        result.append((gid, labels.get(gid, gid), left, top, right - left, bottom - top))
    return result


def _inside_rect(x: int, y: int, rect: Tuple[int, int, int, int]) -> bool:
    l, t, r, b = rect
    return l <= x <= r and t <= y <= b


def blocked_cells(boxes: Sequence[Box], ignore: set[str], width: int, height: int) -> set[Tuple[int, int]]:
    blocked = set()
    for b in boxes:
        if b.id in ignore:
            continue
        l = max(0, _floor_snap(b.left - ROUTE_CLEAR))
        r = min(width, _ceil_snap(b.right + ROUTE_CLEAR))
        t = max(0, _floor_snap(b.top - ROUTE_CLEAR))
        bot = min(height, _ceil_snap(b.bottom + ROUTE_CLEAR))
        for x in range(l, r + ROUTE_STEP, ROUTE_STEP):
            for y in range(t, bot + ROUTE_STEP, ROUTE_STEP):
                blocked.add((x, y))
    return blocked


def astar_route(start: Point, goal: Point, boxes: Sequence[Box], width: int, height: int,
                ignore: set[str], used: Dict[Tuple[int, int], int],
                preferred_y: Optional[int] = None) -> Tuple[List[Point], bool]:
    start = Point(_snap(start.x), _snap(start.y))
    goal = Point(_snap(goal.x), _snap(goal.y))
    blocked = blocked_cells(boxes, ignore, width, height)
    blocked.discard((start.x, start.y))
    blocked.discard((goal.x, goal.y))

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

    max_nodes = max(15000, (width // ROUTE_STEP) * (height // ROUTE_STEP) * 2)
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
            bend = 0.0 if (pdx, pdy) in {(0, 0), (dx, dy)} else 5.0
            occupancy = used.get((nx, ny), 0) * 2.8
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


def edge_sides(edges: List[Edge], boxes: Dict[str, Box]) -> List[Tuple[str, str]]:
    result = []
    for e in edges:
        sb, tb = boxes[e.source], boxes[e.target]
        fs = e.from_side or infer_side(sb, tb, True)
        ts = e.to_side or infer_side(tb, sb, False)
        result.append((fs, ts))
    return result


def assign_ports(edges: List[Edge], sides: List[Tuple[str, str]], boxes: Dict[str, Box]) -> List[Tuple[Point, Point, str, str]]:
    # Share a single ordering for incoming and outgoing connections on each
    # physical side. This prevents opposite-direction links from landing on
    # the exact same port coordinate.
    usage: Dict[Tuple[str, str], List[Tuple[int, str]]] = defaultdict(list)
    for i, (e, (fs, ts)) in enumerate(zip(edges, sides)):
        usage[(e.source, fs)].append((i, "out"))
        usage[(e.target, ts)].append((i, "in"))

    ranks: Dict[Tuple[int, str], Tuple[int, int]] = {}
    for (node, side), items in usage.items():
        def key(item: Tuple[int, str]) -> Tuple[int, str, int, int]:
            edge_i, direction = item
            e = edges[edge_i]
            other = boxes[e.target if direction == "out" else e.source]
            coord = other.cy if side in {"e", "w"} else other.cx
            port_name = e.source_port if direction == "out" else e.target_port
            # Port names make otherwise-equivalent connections stable while
            # geometry remains the primary crossing-minimizing sort key.
            return (coord, port_name, 0 if direction == "out" else 1, edge_i)
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
    return out


def resolved_via(e: Edge, boxes: Dict[str, Box]) -> str:
    if e.via != "auto":
        return e.via
    backwards = boxes[e.target].col < boxes[e.source].col
    if backwards and e.kind == "response":
        return "bottom"
    if backwards and e.kind in {"control", "clock"}:
        return "top"
    return "auto"


def route_edges(
    edges: List[Edge],
    boxes_list: List[Box],
    width: int,
    height: int,
    top_lane_base: int = TOP_LANE_Y,
    bottom_lane_base: Optional[int] = None,
) -> Tuple[List[List[Point]], List[str]]:
    boxes = {b.id: b for b in boxes_list}
    sides = edge_sides(edges, boxes)
    ports = assign_ports(edges, sides, boxes)
    used: Dict[Tuple[int, int], int] = defaultdict(int)
    routes: List[List[Point]] = []
    warnings: List[str] = []

    top_lane_base = _snap(top_lane_base)
    if bottom_lane_base is None:
        bottom_lane_base = _snap(height - 30)
    else:
        bottom_lane_base = _snap(bottom_lane_base)
    top_count = 0
    bottom_count = 0

    # Route main data first, then control/response. A* sees earlier routes and
    # mildly avoids them without making diagrams balloon outward.
    order = sorted(range(len(edges)), key=lambda i: ({"data": 0, "clock": 1, "control": 2, "response": 3}[edges[i].kind], i))
    routed: Dict[int, List[Point]] = {}

    for i in order:
        e = edges[i]
        p1, p2, fs, ts = ports[i]
        s = outward(p1, fs)
        t = outward(p2, ts)

        via = resolved_via(e, boxes)
        used_fallback = False

        if via == "top":
            lane_y = top_lane_base + top_count * ROUTE_STEP
            top_count += 1
            first, fallback_a = astar_route(
                s, Point(s.x, lane_y), boxes_list, width, height, set(), used, lane_y
            )
            last, fallback_b = astar_route(
                Point(t.x, lane_y), t, boxes_list, width, height, set(), used, lane_y
            )
            core = simplify_polyline(first + [Point(t.x, lane_y)] + last[1:])
            used_fallback = fallback_a or fallback_b
        elif via == "bottom":
            lane_y = bottom_lane_base + bottom_count * ROUTE_STEP
            bottom_count += 1
            first, fallback_a = astar_route(
                s, Point(s.x, lane_y), boxes_list, width, height, set(), used, lane_y
            )
            last, fallback_b = astar_route(
                Point(t.x, lane_y), t, boxes_list, width, height, set(), used, lane_y
            )
            core = simplify_polyline(first + [Point(t.x, lane_y)] + last[1:])
            used_fallback = fallback_a or fallback_b
        else:
            preferred_y = None
            if e.kind in {"control", "clock"}:
                preferred_y = top_lane_base
            elif e.kind == "response":
                preferred_y = bottom_lane_base
            core, used_fallback = astar_route(
                s, t, boxes_list, width, height, set(), used, preferred_y
            )

        if used_fallback:
            warnings.append(
                f"edge {i} ({e.source}->{e.target}) used fallback routing; inspect or adjust the IR"
            )

        route = simplify_polyline([p1, s] + core[1:-1] + [t, p2])
        routed[i] = route
        for a, b in zip(route, route[1:]):
            if a.x == b.x:
                y0, y1 = sorted((a.y, b.y))
                for y in range(_snap(y0), _snap(y1) + ROUTE_STEP, ROUTE_STEP):
                    used[(_snap(a.x), y)] += 1
            elif a.y == b.y:
                x0, x1 = sorted((a.x, b.x))
                for x in range(_snap(x0), _snap(x1) + ROUTE_STEP, ROUTE_STEP):
                    used[(x, _snap(a.y))] += 1

    for i in range(len(edges)):
        routes.append(routed[i])
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


Rect = Tuple[float, float, float, float]


def _box_rect(b: Box, pad: float = 0) -> Rect:
    return (b.left - pad, b.top - pad, b.right + pad, b.bottom + pad)


def _rects_overlap(a: Rect, b: Rect, pad: float = 0) -> bool:
    return not (
        a[2] + pad <= b[0]
        or b[2] + pad <= a[0]
        or a[3] + pad <= b[1]
        or b[3] + pad <= a[1]
    )


def _segment_intersects_rect(
    a: Point, b: Point, rect: Rect, pad: float = 0, interior: bool = False
) -> bool:
    left, top, right, bottom = (
        rect[0] - pad,
        rect[1] - pad,
        rect[2] + pad,
        rect[3] + pad,
    )
    if a.x == b.x:
        low, high = sorted((a.y, b.y))
        if interior:
            return left < a.x < right and max(low, top) < min(high, bottom)
        return left <= a.x <= right and max(low, top) <= min(high, bottom)
    if a.y == b.y:
        low, high = sorted((a.x, b.x))
        if interior:
            return top < a.y < bottom and max(low, left) < min(high, right)
        return top <= a.y <= bottom and max(low, left) <= min(high, right)
    # Routes should be orthogonal. Treat a diagonal's bounding box as a
    # conservative intersection so lint never silently accepts it.
    segment_rect = (min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))
    return _rects_overlap(segment_rect, (left, top, right, bottom))


def _point_on_boundary(p: Point, b: Box) -> bool:
    return (
        (p.x in {b.left, b.right} and b.top <= p.y <= b.bottom)
        or (p.y in {b.top, b.bottom} and b.left <= p.x <= b.right)
    )


def _title_rect(title: str) -> Rect:
    width = max(40.0, len(title) * TITLE_FONT * 0.58)
    return (MARGIN_X, TITLE_Y - TITLE_FONT, MARGIN_X + width, TITLE_Y + 5)


def _group_label_rects(grects: Sequence[Tuple[str, str, int, int, int, int]]) -> List[Rect]:
    result = []
    for _, label, x, y, _, _ in grects:
        result.append((x + 7, y + 3, x + 14 + len(label) * 6.4, y + 19))
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


def _placement_is_clear(
    placement: LabelPlacement,
    obstacles: Sequence[Rect],
    routes: Sequence[Sequence[Point]],
    edge_index: int,
    placed: Sequence[LabelPlacement],
    width: int,
    height: int,
) -> bool:
    rect = placement.rect
    if rect[0] < 4 or rect[1] < 4 or rect[2] > width - 4 or rect[3] > height - 4:
        return False
    if any(_rects_overlap(rect, obstacle, 2) for obstacle in obstacles):
        return False
    if any(_rects_overlap(rect, other.rect, 3) for other in placed):
        return False
    for route_index, route in enumerate(routes):
        if route_index == edge_index:
            continue
        if any(_segment_intersects_rect(a, b, rect, 1) for a, b in zip(route, route[1:])):
            return False
    if placement.leader_start and placement.leader_end:
        if any(
            _segment_intersects_rect(
                placement.leader_start, placement.leader_end, obstacle, interior=True
            )
            for obstacle in obstacles
        ):
            return False
    return True


def place_edge_labels(
    title: str,
    boxes: Sequence[Box],
    edges: Sequence[Edge],
    routes: Sequence[Sequence[Point]],
    grects: Sequence[Tuple[str, str, int, int, int, int]],
    width: int,
    height: int,
) -> Tuple[List[Optional[LabelPlacement]], List[str]]:
    obstacles = [_box_rect(b) for b in boxes]
    obstacles.append(_title_rect(title))
    obstacles.extend(_group_label_rects(grects))
    placed: List[LabelPlacement] = []
    result: List[Optional[LabelPlacement]] = []
    warnings: List[str] = []

    for edge_index, (edge, route) in enumerate(zip(edges, routes)):
        label = edge_label_text(edge)
        if not label:
            result.append(None)
            continue
        text_width = max(30.0, len(label) * 6.4 + 10)
        candidates = []
        for segment_index, (a, b) in enumerate(zip(route, route[1:])):
            horizontal = a.y == b.y
            length = abs(a.x - b.x) + abs(a.y - b.y)
            if not horizontal and a.x != b.x:
                continue
            low, high = sorted((a.x, b.x) if horizontal else (a.y, b.y))
            for position in _ordered_positions(low, high):
                midpoint_distance = abs(position - (low + high) / 2)
                for gap in (LABEL_GAP, 14, 24, 34, 44, 54, 64):
                    if horizontal:
                        above_bottom = a.y - gap
                        above_y = above_bottom - LABEL_HEIGHT + 12
                        below_top = a.y + gap
                        below_y = below_top + 12
                        candidates.append((
                            (0, gap, midpoint_distance, -length, segment_index, 0),
                            LabelPlacement(
                                position,
                                above_y,
                                text_width,
                                leader_start=Point(position, a.y),
                                leader_end=Point(position, int(above_bottom)),
                            ),
                        ))
                        candidates.append((
                            (0, gap, midpoint_distance, -length, segment_index, 1),
                            LabelPlacement(
                                position,
                                below_y,
                                text_width,
                                leader_start=Point(position, a.y),
                                leader_end=Point(position, int(below_top)),
                            ),
                        ))
                    else:
                        right_left = a.x + gap
                        right_x = right_left + text_width / 2
                        left_right = a.x - gap
                        left_x = left_right - text_width / 2
                        baseline = position + 4
                        candidates.append((
                            (1, gap, midpoint_distance, -length, segment_index, 0),
                            LabelPlacement(
                                right_x,
                                baseline,
                                text_width,
                                leader_start=Point(a.x, position),
                                leader_end=Point(int(right_left), position),
                            ),
                        ))
                        candidates.append((
                            (1, gap, midpoint_distance, -length, segment_index, 1),
                            LabelPlacement(
                                left_x,
                                baseline,
                                text_width,
                                leader_start=Point(a.x, position),
                                leader_end=Point(int(left_right), position),
                            ),
                        ))

        selected = None
        for _, candidate in sorted(candidates, key=lambda item: item[0]):
            if _placement_is_clear(
                candidate, obstacles, routes, edge_index, placed, width, height
            ):
                selected = candidate
                break

        if selected is None:
            mx, my, horizontal = longest_segment_mid(route)
            if horizontal:
                selected = LabelPlacement(mx, my - 7, text_width, fallback=True)
            else:
                selected = LabelPlacement(
                    mx + 7 + text_width / 2, my + 4, text_width, fallback=True
                )
            warnings.append(
                f"edge {edge_index} ({edge.source}->{edge.target}) has no collision-free label position"
            )

        placed.append(selected)
        result.append(selected)

    return result, warnings


def svg_block(b: Box) -> str:
    x, y, w, h = b.x, b.y, b.w, b.h
    common = 'class="block"'
    parts = []
    if b.kind == "mux":
        pts = f"{x+12},{y} {x+w-12},{y+8} {x+w-12},{y+h-8} {x+12},{y+h}"
        parts.append(f'<polygon points="{pts}" class="block mux"/>')
    elif b.kind == "io":
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h//2}" class="block io"/>')
    elif b.kind == "fsm":
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" class="block fsm"/>')
    elif b.kind == "memory":
        parts.append(f'<rect x="{x+7}" y="{y+7}" width="{w}" height="{h}" rx="4" class="memory-shadow"/>')
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" class="block memory"/>')
        parts.append(f'<line x1="{x+12}" y1="{y+14}" x2="{x+w-12}" y2="{y+14}" class="symbol-line"/>')
    elif b.kind == "fifo":
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" class="block fifo"/>')
        parts.append(f'<line x1="{x+13}" y1="{y+10}" x2="{x+13}" y2="{y+h-10}" class="symbol-line"/>')
        parts.append(f'<line x1="{x+w-13}" y1="{y+10}" x2="{x+w-13}" y2="{y+h-10}" class="symbol-line"/>')
    elif b.kind == "reg":
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="3" class="block reg"/>')
        parts.append(f'<line x1="{x+9}" y1="{y+8}" x2="{x+9}" y2="{y+h-8}" class="symbol-line"/>')
    elif b.kind == "arbiter":
        pts = f"{x+10},{y} {x+w-10},{y} {x+w},{y+h//2} {x+w-10},{y+h} {x+10},{y+h} {x},{y+h//2}"
        parts.append(f'<polygon points="{pts}" class="block arbiter"/>')
    else:
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" {common}/>')

    lines = b.label.split("\n")[:2]
    if b.subtitle:
        label_y = b.cy - 5
    else:
        label_y = b.cy + 5 - (len(lines)-1)*8
    for j, line in enumerate(lines):
        parts.append(f'<text x="{b.cx}" y="{label_y + j*17}" class="block-label">{escape(line)}</text>')
    if b.subtitle:
        parts.append(f'<text x="{b.cx}" y="{b.cy+17}" class="subtitle">{escape(b.subtitle)}</text>')
    return "\n".join(parts)


def edge_label_text(e: Edge) -> str:
    label = e.label
    if e.width:
        width_text = f"{e.width}b"
        if not label:
            label = width_text
        elif width_text not in label and str(e.width) not in label:
            label = f"{label} · {width_text}"
    return label


def svg_edge_path(e: Edge, route: Sequence[Point]) -> str:
    pts = " ".join(f"{p.x},{p.y}" for p in route)
    cls = f"edge {e.kind}"
    return f'<polyline points="{pts}" class="{cls}" marker-end="url(#arrow)"/>'


def svg_edge_label(e: Edge, placement: Optional[LabelPlacement]) -> str:
    label = edge_label_text(e)
    if not label or placement is None:
        return ""
    parts = []
    if placement.leader_start and placement.leader_end:
        parts.append(
            f'<line x1="{placement.leader_start.x}" y1="{placement.leader_start.y}" '
            f'x2="{placement.leader_end.x}" y2="{placement.leader_end.y}" class="label-leader"/>'
        )
    rect = placement.rect
    parts.append(
        f'<rect x="{rect[0]:.1f}" y="{rect[1]:.1f}" width="{placement.width:.1f}" '
        f'height="{placement.height:.1f}" rx="3" class="edge-label-bg"/>'
    )
    parts.append(
        f'<text x="{placement.x:.1f}" y="{placement.y:.1f}" class="edge-label">'
        f'{escape(label)}</text>'
    )
    return "\n".join(parts)


def render(
    title: str,
    boxes: List[Box],
    edges: List[Edge],
    groups: List[dict],
    diagnostics: Optional[List[str]] = None,
) -> str:
    width, height = layout_boxes(boxes)
    width = max(width, _ceil_snap(MARGIN_X * 2 + len(title) * TITLE_FONT * 0.58))
    grects = group_rects(boxes, groups)
    if grects:
        min_top = min(y for _, _, _, y, _, _ in grects)
        if min_top < 38:
            shift = _snap(38 - min_top)
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
        boxes, edges, routes, placements, title, grects
    )
    if diagnostics is not None:
        diagnostics.extend(route_warnings)
        diagnostics.extend(label_warnings)
        diagnostics.extend(geometry_warnings)

    css = """
    .bg{fill:#ffffff}.title{font:600 18px ui-sans-serif,system-ui,sans-serif;fill:#111827}
    .block{fill:#f8fafc;stroke:#1f2937;stroke-width:1.6}.memory-shadow{fill:#fff;stroke:#6b7280;stroke-width:1.2}
    .memory{fill:#f8fafc}.fifo{fill:#f8fafc}.fsm{fill:#fff;stroke-dasharray:5 3}.io{fill:#fff}
    .arbiter{fill:#f8fafc;stroke:#1f2937;stroke-width:1.6}.mux{fill:#f8fafc;stroke:#1f2937;stroke-width:1.6}
    .symbol-line{stroke:#6b7280;stroke-width:1.1}.block-label{font:600 14px ui-sans-serif,system-ui,sans-serif;fill:#111827;text-anchor:middle}
    .subtitle{font:11px ui-sans-serif,system-ui,sans-serif;fill:#4b5563;text-anchor:middle}
    .group{fill:#f9fafb;fill-opacity:.72;stroke:#9ca3af;stroke-width:1;stroke-dasharray:6 4}
    .group-label{font:600 11px ui-sans-serif,system-ui,sans-serif;fill:#4b5563}
    .edge{fill:none;stroke:#374151;stroke-width:1.5;stroke-linejoin:round;stroke-linecap:square}
    .edge.control{stroke:#6b7280;stroke-dasharray:5 4}.edge.clock{stroke:#6b7280;stroke-dasharray:2 3}
    .edge.response{stroke:#374151}.edge-label{font:11px ui-sans-serif,system-ui,sans-serif;fill:#374151;text-anchor:middle}
    .edge-label-bg{fill:#fff;fill-opacity:.94;stroke:none}
    .label-leader{stroke:#9ca3af;stroke-width:1;stroke-dasharray:2 2}
    """

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="diagram-title" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<title id="diagram-title">{escape(title)}</title>',
        "<defs>",
        '<marker id="arrow" viewBox="0 0 10 10" refX="8.3" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#374151"/></marker>',
        f"<style>{css}</style>",
        "</defs>",
        f'<rect x="0" y="0" width="{width}" height="{height}" class="bg"/>',
        f'<text x="{MARGIN_X}" y="{TITLE_Y}" class="title">{escape(title)}</text>',
    ]

    for _, label, x, y, w, h in grects:
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" class="group"/>')
        parts.append(f'<text x="{x+10}" y="{y+15}" class="group-label">{escape(label)}</text>')

    # Routes stay behind nodes. Labels are placed last so valid labels are never
    # occluded; collision-aware placement keeps them off the nodes themselves.
    for e, route in zip(edges, routes):
        parts.append(svg_edge_path(e, route))
    for b in boxes:
        parts.append(svg_block(b))
    for e, placement in zip(edges, placements):
        rendered_label = svg_edge_label(e, placement)
        if rendered_label:
            parts.append(rendered_label)
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def lint_geometry(
    boxes: List[Box],
    edges: List[Edge],
    routes: Optional[Sequence[Sequence[Point]]] = None,
    placements: Optional[Sequence[Optional[LabelPlacement]]] = None,
    title: str = "",
    grects: Sequence[Tuple[str, str, int, int, int, int]] = (),
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
        if len(b.subtitle) > 34:
            warnings.append(f"block {b.id} has a long subtitle that may overflow")

    if routes is None:
        return warnings
    if len(routes) != len(edges):
        warnings.append("route count does not match edge count")
        return warnings

    title_rect = _title_rect(title) if title else None
    group_labels = _group_label_rects(grects)
    for edge_index, (edge, route) in enumerate(zip(edges, routes)):
        if len(route) < 2:
            warnings.append(f"edge {edge_index} ({edge.source}->{edge.target}) has no usable route")
            continue
        if not _point_on_boundary(route[0], by_id[edge.source]):
            warnings.append(f"edge {edge_index} does not start on block {edge.source}")
        if not _point_on_boundary(route[-1], by_id[edge.target]):
            warnings.append(f"edge {edge_index} does not end on block {edge.target}")

        crossed = set()
        for a, b in zip(route, route[1:]):
            if a.x != b.x and a.y != b.y:
                warnings.append(f"edge {edge_index} contains a non-orthogonal segment")
            for block in boxes:
                if block.id in crossed:
                    continue
                if _segment_intersects_rect(a, b, _box_rect(block), interior=True):
                    warnings.append(f"edge {edge_index} crosses block {block.id}")
                    crossed.add(block.id)
            if title_rect and _segment_intersects_rect(a, b, title_rect, 2):
                warnings.append(f"edge {edge_index} crosses the diagram title")
                title_rect = None  # Emit at most one title warning per lint pass.

    if placements is None:
        return warnings

    label_rects: List[Tuple[int, Rect]] = []
    for edge_index, placement in enumerate(placements):
        if placement is None:
            continue
        rect = placement.rect
        label_rects.append((edge_index, rect))
        for block in boxes:
            if _rects_overlap(rect, _box_rect(block), 1):
                warnings.append(f"edge {edge_index} label overlaps block {block.id}")
        if title and _rects_overlap(rect, _title_rect(title), 1):
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
        if placement.leader_start and placement.leader_end:
            for block in boxes:
                if _segment_intersects_rect(
                    placement.leader_start,
                    placement.leader_end,
                    _box_rect(block),
                    interior=True,
                ):
                    warnings.append(f"edge {edge_index} label leader crosses block {block.id}")

    for i, (edge_a, rect_a) in enumerate(label_rects):
        for edge_b, rect_b in label_rects[i + 1:]:
            if _rects_overlap(rect_a, rect_b, 2):
                warnings.append(f"edge {edge_a} label overlaps edge {edge_b} label")
    return warnings


def example_json() -> str:
    return json.dumps({
        "title": "Datapath",
        "blocks": [
            {"id": "in", "label": "Input FIFO", "kind": "fifo", "at": [0, 1]},
            {"id": "alu", "label": "Execute", "kind": "logic", "at": [1, 1]},
            {"id": "ram", "label": "BRAM", "kind": "memory", "at": [1, 2]},
            {"id": "out", "label": "Output", "kind": "io", "at": [2, 1]},
            {"id": "ctrl", "label": "Control", "kind": "fsm", "at": [1, 0]},
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
        out.write_text(svg, encoding="utf-8")
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
