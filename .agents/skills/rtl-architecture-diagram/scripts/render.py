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


BLOCK_KINDS = {
    "module", "logic", "memory", "fifo", "mux", "demux", "reg", "counter",
    "fsm", "arbiter", "io", "alu", "adder", "subtractor", "addsub",
    "multiplier", "comparator",
    "and", "or", "xor", "not",
}
EDGE_KINDS = {"data", "control", "response", "clock"}
CONTROL_EDGE_KINDS = {"control", "clock"}
ARITHMETIC_KINDS = {
    "alu", "adder", "subtractor", "addsub", "multiplier", "comparator",
}
SIDES = {"n", "s", "e", "w"}
ROUTE_HINTS = {"auto", "top", "bottom"}

RECOMMENDED_MAX_BLOCKS = 25
RECOMMENDED_MAX_EDGES = 45

# Geometry. Kept compact on purpose.
MARGIN_X = 62
MARGIN_TOP = 72
MARGIN_BOTTOM = 62
COL_GAP = 88
DENSE_COL_GAP = 72
DENSE_ROW_THRESHOLD = 8
FOLD_ROW_THRESHOLD = 9
ROW_GAP = 64
GROUP_PAD = 22
GROUP_EDGE_ALIGN = 20
ROUTE_CLEAR = 14
ROUTE_STEP = 10
PORT_STUB = 40
VERTICAL_PORT_STUB = 20
FONT = 14
SMALL_FONT = 11
TITLE_FONT = 20
TITLE_Y = 32
TOP_LANE_Y = 50
LABEL_HEIGHT = 18
LABEL_GAP = 8
LABEL_BLOCK_CLEAR = 6
LABEL_GROUP_INSET = 6
TITLE_CONTENT_GAP = 14
NEAR_WIDTH_TOLERANCE = 8
NEAR_HEIGHT_TOLERANCE = 6
LABEL_SEARCH_GAPS = (LABEL_GAP, 14, *range(20, 111, 6))
PROMINENCE_SCALE = {"normal": 1.0, "bigger": 1.14, "smaller": 0.90}
BLOCK_FONT_SIZE = {"normal": float(FONT), "bigger": 15.0, "smaller": 13.0}
SUBTITLE_FONT_SIZE = {
    "normal": float(SMALL_FONT), "bigger": 11.5, "smaller": 10.5,
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


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
    prominence: str = "normal"
    size_explicit: bool = False

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
    leader_bend: Optional[Point] = None

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


# ---------------------------------------------------------------------------
# Text measurement and automatic sizing
# ---------------------------------------------------------------------------


def _split_endpoint(value: str) -> Tuple[str, str]:
    if "." in value:
        node, port = value.split(".", 1)
        return node.strip(), port.strip()
    return value.strip(), ""


def estimate_ui_text_width(text: str, font_size: float) -> float:
    width = 0.0
    for char in text:
        if char in " ilI1|.,:;!'`":
            width += 3.0
        elif char in "MW@%#":
            width += 8.2
        elif char.isupper():
            width += 6.4
        elif char.isdigit():
            width += 5.8
        else:
            width += 5.4
    return width * (font_size / 10.5)


def _balanced_text_lines(text: str, font_size: float) -> List[str]:
    explicit = text.split("\n")[:2]
    if len(explicit) > 1:
        return explicit
    unbroken = explicit[0]
    words = unbroken.split()
    if len(words) < 2:
        return [unbroken]
    choices = []
    for split in range(1, len(words)):
        left = " ".join(words[:split])
        right = " ".join(words[split:])
        left_width = estimate_ui_text_width(left, font_size)
        right_width = estimate_ui_text_width(right, font_size)
        choices.append((max(left_width, right_width), abs(left_width - right_width), split, left, right))
    _, _, _, left, right = min(choices)
    return [left, right]


def _text_lines(text: str, available_width: float, font_size: float) -> List[str]:
    if not text:
        return []
    explicit = text.split("\n")[:2]
    if len(explicit) > 1:
        return explicit
    if estimate_ui_text_width(explicit[0], font_size) <= available_width:
        return explicit
    return _balanced_text_lines(text, font_size)


def _block_font_size(prominence: str) -> float:
    return BLOCK_FONT_SIZE.get(prominence, BLOCK_FONT_SIZE["normal"])


def _subtitle_font_size(prominence: str) -> float:
    return SUBTITLE_FONT_SIZE.get(prominence, SUBTITLE_FONT_SIZE["normal"])


def block_label_lines(label: str, available_width: float, prominence: str = "normal") -> List[str]:
    return _text_lines(label, available_width, _block_font_size(prominence))


def block_subtitle_lines(subtitle: str, available_width: float, prominence: str = "normal") -> List[str]:
    return _text_lines(subtitle, available_width, _subtitle_font_size(prominence))


def _sized_text_width(
    text: str, max_width: int, padding: int, font_size: float
) -> float:
    if not text:
        return 0.0
    explicit = text.split("\n")[:2]
    full_width = max(estimate_ui_text_width(line, font_size) for line in explicit)
    if len(explicit) > 1 or full_width + padding > max_width:
        return max(
            estimate_ui_text_width(line, font_size)
            for line in _balanced_text_lines(text, font_size)
        )
    return full_width


def _auto_size(
    label: str, subtitle: str, kind: str, prominence: str = "normal"
) -> Tuple[int, int]:
    label_font = _block_font_size(prominence)
    subtitle_font = _subtitle_font_size(prominence)
    if kind in {"mux", "demux"}:
        min_width, max_width, label_padding, subtitle_padding = 86, 150, 30, 24
        label_inset = 28
        base_height, wrapped_height = 60, 66
    elif kind in {"reg", "counter"}:
        min_width, max_width, label_padding, subtitle_padding = 104, 160, 40, 22
        label_inset = 40
        base_height, wrapped_height = 54, 60
    elif kind in {"and", "or", "xor", "not"}:
        min_width, max_width, label_padding, subtitle_padding = 86, 150, 30, 24
        label_inset = 28
        base_height, wrapped_height = 60, 66
    elif kind == "io":
        min_width, max_width, label_padding, subtitle_padding = 104, 180, 30, 24
        label_inset = 28
        base_height, wrapped_height = 54, 62
    else:
        min_width, max_width = 126, 190
        label_padding = 50 if kind in ARITHMETIC_KINDS else 34
        label_inset = 50 if kind in ARITHMETIC_KINDS else 26
        subtitle_padding = 26
        base_height, wrapped_height = 62, 70

    label_width = _sized_text_width(label, max_width, label_padding, label_font)
    subtitle_width = _sized_text_width(
        subtitle, max_width, subtitle_padding, subtitle_font
    )
    w = int(max(min_width, min(max_width, max(
        label_width + label_padding,
        subtitle_width + subtitle_padding,
    ))))
    label_room = w - label_inset
    subtitle_room = w - subtitle_padding
    label_count = len(block_label_lines(label, label_room, prominence))
    subtitle_count = len(block_subtitle_lines(subtitle, subtitle_room, prominence))
    h = wrapped_height if label_count > 1 else base_height
    if subtitle_count:
        content_height = 68 + 14 * (label_count - 1) + 14 * (subtitle_count - 1)
        h = max(h, content_height)

    scale = PROMINENCE_SCALE.get(prominence, PROMINENCE_SCALE["normal"])
    w = int(math.ceil(w * scale))
    h = int(math.ceil(h * scale))
    return w, h


# ---------------------------------------------------------------------------
# Semantic placement and IR parsing
# ---------------------------------------------------------------------------


def _harmonize_near_adjacent_sizes(
    boxes: Sequence[Box], edges: Sequence[Edge]
) -> None:
    """Round only very similar neighboring auto sizes up to a shared value."""
    by_id = {box.id: box for box in boxes}
    pairs: set[Tuple[str, str]] = set()

    clusters: Dict[str, List[Box]] = defaultdict(list)
    for box in boxes:
        clusters[box.group or "__ungrouped__"].append(box)
    for members in clusters.values():
        by_row: Dict[int, List[Box]] = defaultdict(list)
        by_col: Dict[int, List[Box]] = defaultdict(list)
        for box in members:
            by_row[box.row].append(box)
            by_col[box.col].append(box)
        for row_members in by_row.values():
            ordered = sorted(row_members, key=lambda box: (box.col, box.id))
            pairs.update(
                tuple(sorted((left.id, right.id)))
                for left, right in zip(ordered, ordered[1:])
            )
        for col_members in by_col.values():
            ordered = sorted(col_members, key=lambda box: (box.row, box.id))
            pairs.update(
                tuple(sorted((upper.id, lower.id)))
                for upper, lower in zip(ordered, ordered[1:])
            )

    # A direct short semantic connection also counts when parallel placement
    # means the pair is not consecutive in a row or column.
    for edge in edges:
        source, target = by_id[edge.source], by_id[edge.target]
        if (
            source.group == target.group
            and abs(source.col - target.col) <= 1
            and abs(source.row - target.row) <= 1
        ):
            pairs.add(tuple(sorted((source.id, target.id))))

    def harmonize(attribute: str, tolerance: int) -> None:
        eligible = {
            box.id
            for box in boxes
            if not box.size_explicit
        }
        parent = {box_id: box_id for box_id in eligible}
        low = {box_id: getattr(by_id[box_id], attribute) for box_id in eligible}
        high = dict(low)

        def find(box_id: str) -> str:
            while parent[box_id] != box_id:
                parent[box_id] = parent[parent[box_id]]
                box_id = parent[box_id]
            return box_id

        candidates = []
        for left_id, right_id in pairs:
            if left_id not in eligible or right_id not in eligible:
                continue
            left, right = by_id[left_id], by_id[right_id]
            if left.prominence != right.prominence:
                continue
            difference = abs(getattr(left, attribute) - getattr(right, attribute))
            if difference <= tolerance:
                candidates.append((difference, left_id, right_id))

        for _, left_id, right_id in sorted(candidates):
            left_root, right_root = find(left_id), find(right_id)
            if left_root == right_root:
                continue
            combined_low = min(low[left_root], low[right_root])
            combined_high = max(high[left_root], high[right_root])
            # Prevent transitive creep: 130~137 and 137~144 must not make
            # 130 and 144 equal when the endpoints are no longer near-sized.
            if combined_high - combined_low > tolerance:
                continue
            parent[right_root] = left_root
            low[left_root] = combined_low
            high[left_root] = combined_high

        group_max: Dict[str, int] = defaultdict(int)
        for box_id in eligible:
            root = find(box_id)
            group_max[root] = max(group_max[root], getattr(by_id[box_id], attribute))
        for box_id in eligible:
            setattr(by_id[box_id], attribute, group_max[find(box_id)])

    harmonize("w", NEAR_WIDTH_TOLERANCE)
    harmonize("h", NEAR_HEIGHT_TOLERANCE)


def _median_int(values: Sequence[int]) -> int:
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def _infer_missing_positions(
    boxes: List[Box],
    edges: Sequence[Edge],
    groups: Sequence[dict],
    explicit_positions: Dict[str, Tuple[int, int]],
) -> None:
    """Infer deterministic semantic columns/rows while preserving anchors."""
    if len(explicit_positions) == len(boxes):
        return

    by_id = {box.id: box for box in boxes}
    cluster_of = {
        box.id: box.group or "__ungrouped__"
        for box in boxes
    }
    members_by_cluster: Dict[str, List[Box]] = defaultdict(list)
    for box in boxes:
        members_by_cluster[cluster_of[box.id]].append(box)

    # Dataflow determines horizontal rank. Response/control/clock links are
    # excluded because they point backward or vertically by convention.
    ranks: Dict[str, int] = {}
    for cluster, members in members_by_cluster.items():
        member_ids = {box.id for box in members}
        successors: Dict[str, set[str]] = defaultdict(set)
        predecessors: Dict[str, set[str]] = defaultdict(set)
        for edge in edges:
            if (
                edge.kind == "data"
                and edge.source in member_ids
                and edge.target in member_ids
                and edge.source != edge.target
            ):
                successors[edge.source].add(edge.target)
                predecessors[edge.target].add(edge.source)

        indegree = {
            box.id: len(predecessors.get(box.id, set()))
            for box in members
        }
        ready = sorted(node for node, degree in indegree.items() if degree == 0)
        cluster_ranks = {box.id: 0 for box in members}
        visited = set()
        while ready:
            node = ready.pop(0)
            visited.add(node)
            for target in sorted(successors.get(node, set())):
                cluster_ranks[target] = max(
                    cluster_ranks[target], cluster_ranks[node] + 1
                )
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
                    ready.sort()

        # Cyclic data edges are uncommon at this abstraction level. Keep them
        # deterministic and readable instead of rejecting the IR.
        next_rank = max(cluster_ranks.values(), default=-1) + 1
        for node in sorted(member_ids - visited):
            known_predecessor_rank = max(
                (
                    cluster_ranks[pred] + 1
                    for pred in predecessors.get(node, set())
                    if pred in visited
                ),
                default=0,
            )
            cluster_ranks[node] = max(next_rank, known_predecessor_rank)
            next_rank = cluster_ranks[node] + 1
            visited.add(node)

        explicit_offsets = [
            explicit_positions[box.id][0] - cluster_ranks[box.id]
            for box in members
            if box.id in explicit_positions
        ]
        column_offset = _median_int(explicit_offsets) if explicit_offsets else 0
        for box in members:
            ranks[box.id] = cluster_ranks[box.id] + column_offset

    data_in: Dict[str, int] = defaultdict(int)
    data_out: Dict[str, int] = defaultdict(int)
    response_out: Dict[str, int] = defaultdict(int)
    for edge in edges:
        if edge.kind == "data":
            data_out[edge.source] += 1
            data_in[edge.target] += 1
        elif edge.kind == "response":
            response_out[edge.source] += 1

    def row_offset(box: Box) -> int:
        if box.kind == "fsm":
            return -1
        if box.kind == "memory":
            if response_out[box.id] or not (
                data_in[box.id] and data_out[box.id]
            ):
                return 1
        return 0

    declared_groups = [str(group["id"]) for group in groups]
    group_slot = {group_id: index for index, group_id in enumerate(declared_groups)}
    extra_groups = sorted(
        cluster
        for cluster in members_by_cluster
        if cluster != "__ungrouped__" and cluster not in group_slot
    )
    for group_id in extra_groups:
        group_slot[group_id] = len(group_slot)

    base_rows: Dict[str, int] = {}
    for cluster, members in members_by_cluster.items():
        explicit_bases = [
            explicit_positions[box.id][1] - row_offset(box)
            for box in members
            if box.id in explicit_positions
        ]
        if explicit_bases:
            base_rows[cluster] = _median_int(explicit_bases)
        elif cluster == "__ungrouped__":
            base_rows[cluster] = 1
        else:
            base_rows[cluster] = 1 + 3 * group_slot.get(cluster, 0)

    occupied = set(explicit_positions.values())
    missing = sorted(
        (box for box in boxes if box.id not in explicit_positions),
        key=lambda box: (
            group_slot.get(cluster_of[box.id], -1),
            ranks[box.id],
            box.id,
        ),
    )
    for box in missing:
        box.col = ranks[box.id]
        preferred_row = base_rows[cluster_of[box.id]] + row_offset(box)
        role_offset = row_offset(box)
        if role_offset < 0:
            row_candidates = [
                preferred_row - delta
                for delta in range(0, len(boxes) + 1)
            ]
        elif role_offset > 0:
            row_candidates = [
                preferred_row + delta
                for delta in range(0, len(boxes) + 1)
            ]
        else:
            row_candidates = [preferred_row]
            for delta in range(1, len(boxes) + 1):
                row_candidates.extend(
                    [preferred_row + delta, preferred_row - delta]
                )
        box.row = next(
            row
            for row in row_candidates
            if (box.col, row) not in occupied
        )
        occupied.add((box.col, box.row))


def _parse_prominence(raw: dict, block_id: str) -> str:
    bigger = raw.get("bigger", False)
    smaller = raw.get("smaller", False)
    if not isinstance(bigger, bool):
        raise DiagramError(f"block {block_id}: 'bigger' must be true or false")
    if not isinstance(smaller, bool):
        raise DiagramError(f"block {block_id}: 'smaller' must be true or false")
    if bigger and smaller:
        raise DiagramError(
            f"block {block_id}: 'bigger' and 'smaller' cannot both be true"
        )
    return "bigger" if bigger else ("smaller" if smaller else "normal")


def _parse_position(
    raw: dict,
    block_id: str,
    occupied: Dict[Tuple[int, int], str],
    explicit: Dict[str, Tuple[int, int]],
) -> Tuple[int, int]:
    position = raw.get("at")
    if position is None:
        return 0, 0
    if not (
        isinstance(position, list)
        and len(position) == 2
        and all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in position
        )
    ):
        raise DiagramError(
            f"block {block_id}: 'at' must be [column,row] integers when provided"
        )
    col, row = position
    if (col, row) in occupied:
        raise DiagramError(
            f"blocks {occupied[(col, row)]} and {block_id} "
            f"share grid position {position}"
        )
    occupied[(col, row)] = block_id
    explicit[block_id] = (col, row)
    return col, row


def _parse_size(
    raw: dict, block_id: str, label: str, subtitle: str, kind: str, prominence: str
) -> Tuple[int, int]:
    if "size" not in raw:
        return _auto_size(label, subtitle, kind, prominence)
    size = raw["size"]
    if not (
        isinstance(size, list)
        and len(size) == 2
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 20
            for value in size
        )
    ):
        raise DiagramError(f"block {block_id}: size must be [width,height] > 20")
    return int(size[0]), int(size[1])


def _parse_blocks(
    raw_blocks: Sequence[object],
) -> Tuple[List[Box], Dict[str, Tuple[int, int]]]:
    boxes: List[Box] = []
    seen = set()
    occupied: Dict[Tuple[int, int], str] = {}
    explicit: Dict[str, Tuple[int, int]] = {}

    for index, raw in enumerate(raw_blocks):
        if not isinstance(raw, dict):
            raise DiagramError(f"block {index} must be an object")
        block_id = str(raw.get("id", "")).strip()
        if not block_id:
            raise DiagramError(f"block {index} has no id")
        if "." in block_id:
            raise DiagramError(
                f"block {block_id!r}: ids cannot contain '.' because dots "
                "delimit port names"
            )
        if block_id in seen:
            raise DiagramError(f"duplicate block id: {block_id}")
        seen.add(block_id)

        label = str(raw.get("label", block_id)).strip()
        kind = str(raw.get("kind", "module"))
        if kind not in BLOCK_KINDS:
            raise DiagramError(f"block {block_id}: unknown kind {kind!r}")
        subtitle = str(raw.get("subtitle", "")).strip()
        group = (
            str(raw["group"]).strip() if raw.get("group") is not None else None
        )
        prominence = _parse_prominence(raw, block_id)
        col, row = _parse_position(raw, block_id, occupied, explicit)
        width, height = _parse_size(
            raw, block_id, label, subtitle, kind, prominence
        )
        boxes.append(
            Box(
                id=block_id,
                label=label,
                kind=kind,
                col=col,
                row=row,
                subtitle=subtitle,
                group=group,
                w=width,
                h=height,
                prominence=prominence,
                size_explicit="size" in raw,
            )
        )
    return boxes, explicit


def _parse_edges(raw_edges: Sequence[object], boxes: Sequence[Box]) -> List[Edge]:
    by_id = {box.id: box for box in boxes}
    edges: List[Edge] = []
    for index, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            raise DiagramError(f"edge {index} must be an object")
        if "from" not in raw or "to" not in raw:
            raise DiagramError(f"edge {index} requires 'from' and 'to'")
        source, source_port = _split_endpoint(str(raw["from"]))
        target, target_port = _split_endpoint(str(raw["to"]))
        if source not in by_id:
            raise DiagramError(f"edge {index}: unknown source block {source!r}")
        if target not in by_id:
            raise DiagramError(f"edge {index}: unknown target block {target!r}")

        kind = str(raw.get("kind", "data"))
        if kind not in EDGE_KINDS:
            raise DiagramError(f"edge {index}: unknown kind {kind!r}")
        from_side = raw.get("from_side")
        to_side = raw.get("to_side")
        if from_side is not None and from_side not in SIDES:
            raise DiagramError(f"edge {index}: from_side must be n/s/e/w")
        if to_side is not None and to_side not in SIDES:
            raise DiagramError(f"edge {index}: to_side must be n/s/e/w")
        via = str(raw.get("via", "auto"))
        if via not in ROUTE_HINTS:
            raise DiagramError(f"edge {index}: via must be auto/top/bottom")
        width = raw.get("width")
        if width is not None and (
            not isinstance(width, int) or isinstance(width, bool) or width <= 0
        ):
            raise DiagramError(f"edge {index}: width must be a positive integer")
        edges.append(
            Edge(
                source=source,
                target=target,
                source_port=source_port,
                target_port=target_port,
                label=str(raw.get("label", "")).strip(),
                width=width,
                kind=kind,
                from_side=from_side,
                to_side=to_side,
                via=via,
            )
        )
    return edges


def _normalize_groups(
    raw_groups: Sequence[object], boxes: Sequence[Box], warnings: List[str]
) -> List[dict]:
    groups: List[dict] = []
    group_ids = set()
    for index, raw in enumerate(raw_groups):
        if not isinstance(raw, dict) or not str(raw.get("id", "")).strip():
            raise DiagramError(f"group {index} requires an id")
        group = dict(raw)
        group["id"] = str(raw["id"]).strip()
        group_id = group["id"]
        if group_id in group_ids:
            raise DiagramError(f"duplicate group id: {group_id}")
        group_ids.add(group_id)
        groups.append(group)

    for box in boxes:
        if box.group and box.group not in group_ids:
            warnings.append(
                f"block {box.id} references undeclared group {box.group!r}; "
                "group will still be drawn"
            )
            group_ids.add(box.group)
            groups.append({"id": box.group, "label": box.group})
    return groups


def load_diagram(
    path: Path,
) -> Tuple[str, List[Box], List[Edge], List[dict], List[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise DiagramError(f"invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise DiagramError("top-level JSON must be an object")

    raw_blocks = data.get("blocks", [])
    raw_edges = data.get("edges", [])
    raw_groups = data.get("groups", [])
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise DiagramError("'blocks' must be a non-empty array")
    if not isinstance(raw_edges, list):
        raise DiagramError("'edges' must be an array")
    if not isinstance(raw_groups, list):
        raise DiagramError("'groups' must be an array")

    warnings: List[str] = []
    boxes, explicit_positions = _parse_blocks(raw_blocks)
    edges = _parse_edges(raw_edges, boxes)
    groups = _normalize_groups(raw_groups, boxes, warnings)
    _infer_missing_positions(boxes, edges, groups, explicit_positions)
    _harmonize_near_adjacent_sizes(boxes, edges)

    if len(boxes) > RECOMMENDED_MAX_BLOCKS:
        warnings.append(
            f"{len(boxes)} blocks: consider splitting the diagram "
            f"(recommended <={RECOMMENDED_MAX_BLOCKS})"
        )
    if len(edges) > RECOMMENDED_MAX_EDGES:
        warnings.append(
            f"{len(edges)} edges: consider aggregating buses or splitting the "
            f"diagram (recommended <={RECOMMENDED_MAX_EDGES})"
        )
    return str(data.get("title", "Architecture")), boxes, edges, groups, warnings


# ---------------------------------------------------------------------------
# Physical layout
# ---------------------------------------------------------------------------


def layout_boxes(boxes: List[Box], edges: Optional[Sequence[Edge]] = None) -> Tuple[int, int]:
    rows = sorted({b.row for b in boxes})
    row_h = {r: max(b.h for b in boxes if b.row == r) for r in rows}

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
        row_center = _snap(row_y[b.row] + row_h[b.row] / 2)
        b.y = int(row_center - b.h / 2)

    by_id = {b.id: b for b in boxes}
    neighbors: Dict[str, List[Box]] = defaultdict(list)
    incoming_controls: Dict[str, List[Box]] = defaultdict(list)
    control_predecessors: Dict[str, set[str]] = defaultdict(set)
    labeled_pair_gap: Dict[Tuple[str, str], int] = {}
    for edge in edges or ():
        neighbors[edge.source].append(by_id[edge.target])
        neighbors[edge.target].append(by_id[edge.source])
        if edge.kind in CONTROL_EDGE_KINDS:
            incoming_controls[edge.target].append(by_id[edge.source])
            control_predecessors[edge.target].add(edge.source)
        label = edge_label_text(edge)
        if label:
            pair = tuple(sorted((edge.source, edge.target)))
            labeled_pair_gap[pair] = max(
                labeled_pair_gap.get(pair, 0),
                min(COL_GAP + 30, _ceil_snap(estimate_edge_label_width(label) + 24)),
            )

    clusters: Dict[str, List[Box]] = defaultdict(list)
    for b in boxes:
        clusters[b.group or "__ungrouped__"].append(b)

    # Groups normally represent independent datapaths that may share semantic
    # column numbers but occupy different row bands. Pack each group around its
    # busiest row, then align support rows to the blocks they connect to.
    cluster_order = sorted(
        clusters,
        key=lambda key: (
            min(b.col for b in clusters[key]),
            min(b.row for b in clusters[key]),
            key,
        ),
    )
    placed_cluster_rows: List[Dict[int, Tuple[int, int]]] = []

    for cluster_key in cluster_order:
        members = clusters[cluster_key]
        members_by_row: Dict[int, List[Box]] = defaultdict(list)
        for b in members:
            members_by_row[b.row].append(b)
        main_row = min(
            members_by_row,
            key=lambda row: (-len(members_by_row[row]), row),
        )
        main = sorted(members_by_row[main_row], key=lambda b: (b.col, b.id))
        cluster_col_gap = DENSE_COL_GAP if len(main) >= DENSE_ROW_THRESHOLD else COL_GAP

        cursor = MARGIN_X
        last_col: Optional[int] = None
        previous_main: Optional[Box] = None
        for b in main:
            if last_col is not None:
                semantic_gap = min(2, max(0, b.col - last_col - 1)) * 24
                adjacent_gap = cluster_col_gap + semantic_gap
                if previous_main is not None:
                    pair_label_gap = labeled_pair_gap.get(
                        tuple(sorted((previous_main.id, b.id))), 0
                    )
                    if (
                        control_predecessors.get(previous_main.id, set())
                        & control_predecessors.get(b.id, set())
                    ):
                        # Keep sibling destinations under a shared controller
                        # compact so its fanout can remain straight. Fine-grain
                        # label offsets handle the local wire annotation.
                        pair_label_gap = 0
                    adjacent_gap = max(
                        adjacent_gap,
                        pair_label_gap,
                    )
                cursor += adjacent_gap
            b.x = _snap(cursor)
            cursor = b.right
            last_col = b.col
            previous_main = b

        # Very long datapaths become hard to read and produce panoramic SVGs.
        # Fold only genuinely long rows into a two-line serpentine: the first
        # tail block sits directly below the fold point, then the remaining
        # tail proceeds right-to-left. This preserves local pipeline adjacency
        # and leaves shorter rows in the familiar left-to-right layout.
        anchor_main = main
        folded_ids: set[str] = set()
        folded_tail: List[Box] = []
        fold_center_y: Optional[int] = None
        fold_depth = 0
        if len(main) >= FOLD_ROW_THRESHOLD:
            split = (len(main) + 1) // 2
            head = main[:split]
            tail = main[split:]
            tail_height = max(b.h for b in tail)
            fold_top = _snap(max(b.bottom for b in head) + ROW_GAP)
            fold_center = _snap(fold_top + tail_height / 2)

            first_tail = tail[0]
            first_tail.x = int(round(head[-1].cx - first_tail.w / 2))
            first_tail.y = int(fold_center - first_tail.h / 2)
            previous_tail = first_tail
            for b in tail[1:]:
                b.x = _snap(previous_tail.left - cluster_col_gap - b.w)
                b.y = int(fold_center - b.h / 2)
                previous_tail = b

            left_shift = max(0, MARGIN_X - min(b.left for b in main))
            if left_shift:
                left_shift = _ceil_snap(left_shift)
                for b in main:
                    b.x += left_shift

            folded_ids = {b.id for b in tail}
            folded_tail = tail
            fold_center_y = fold_center
            fold_depth = tail_height + ROW_GAP
            for b in members:
                if b.row > main_row:
                    b.y += fold_depth
            anchor_main = head

        placed_ids = {b.id for b in main}
        anchor_by_col = {b.col: b.cx for b in anchor_main}
        main_cols = sorted(anchor_by_col)
        typical_step = max(
            120,
            int(
                sum(
                    anchor_by_col[right] - anchor_by_col[left]
                    for left, right in zip(main_cols, main_cols[1:])
                )
                / max(1, len(main_cols) - 1)
            ),
        )

        def column_anchor(col: int) -> float:
            if col in anchor_by_col:
                return anchor_by_col[col]
            lower = [c for c in main_cols if c < col]
            upper = [c for c in main_cols if c > col]
            if lower and upper:
                lo, hi = max(lower), min(upper)
                ratio = (col - lo) / (hi - lo)
                return anchor_by_col[lo] + ratio * (anchor_by_col[hi] - anchor_by_col[lo])
            if lower:
                lo = max(lower)
                return anchor_by_col[lo] + (col - lo) * typical_step
            hi = min(upper)
            return anchor_by_col[hi] - (hi - col) * typical_step

        auxiliary_rows = sorted(
            (row for row in members_by_row if row != main_row),
            key=lambda row: (abs(row - main_row), row),
        )
        for row in auxiliary_rows:
            ordered = sorted(members_by_row[row], key=lambda b: (b.col, b.id))
            previous: Optional[Box] = None
            previous_col: Optional[int] = None
            for b in ordered:
                connected_main = [
                    other for other in neighbors.get(b.id, []) if other.id in placed_ids and other.row == main_row
                ]
                connected_placed = [
                    other for other in neighbors.get(b.id, []) if other.id in placed_ids
                ]
                anchors = connected_main or connected_placed
                desired_cx = (
                    sum(other.cx for other in anchors) / len(anchors)
                    if anchors
                    else column_anchor(b.col)
                )
                # Keep a connected support block's center exact even when its
                # width is not a route-grid multiple. Its north/south port then
                # snaps to the same track as the datapath port, avoiding a
                # pointless one-step hook at the endpoint.
                x = max(MARGIN_X, int(round(desired_cx - b.w / 2)))
                promoted_to_upper_support = False
                upper_controllers = [
                    other
                    for other in incoming_controls.get(b.id, [])
                    if other.id in placed_ids and other.row < main_row
                ]
                if (
                    b.row > main_row
                    and b.kind in {"memory", "fifo"}
                    and connected_main
                    and upper_controllers
                    and not any(other.id in folded_ids for other in connected_main)
                ):
                    candidate_y = int(
                        round(
                            sum(other.cy for other in upper_controllers)
                            / len(upper_controllers)
                            - b.h / 2
                        )
                    )
                    candidate_rect = (
                        x - ROUTE_CLEAR,
                        candidate_y - ROUTE_CLEAR,
                        x + b.w + ROUTE_CLEAR,
                        candidate_y + b.h + ROUTE_CLEAR,
                    )
                    if not any(
                        other.id in placed_ids
                        and _rects_overlap(candidate_rect, _box_rect(other))
                        for other in members
                    ):
                        # A memory controlled from the upper support row and
                        # consumed by the datapath is clearer directly above
                        # its consumer: both control and data connections become
                        # short, orthogonal links. The semantic JSON row remains
                        # a hint rather than a cosmetic placement command.
                        b.y = candidate_y
                        promoted_to_upper_support = True
                if (
                    not promoted_to_upper_support
                    and folded_ids
                    and any(other.id in folded_ids for other in anchors)
                ):
                    # A support block for the folded tail gets its own lower
                    # lane immediately beneath the return row. Place it from
                    # the actual folded geometry instead of adding another
                    # nominal row offset, which would leave an excessive gap.
                    b.y = _snap(max(tail_block.bottom for tail_block in folded_tail) + ROW_GAP)
                uses_fold_shelf = False
                if (
                    not promoted_to_upper_support
                    and folded_tail
                    and fold_center_y is not None
                    and b.row > main_row
                    and anchors
                    and not any(other.id in folded_ids for other in anchors)
                    and any(
                        x < tail_block.right + ROUTE_CLEAR
                        and x + b.w + ROUTE_CLEAR > tail_block.left
                        for tail_block in folded_tail
                    )
                ):
                    # A lower support block for the unfolded head would land
                    # on top of the return row. Give it a compact side shelf
                    # beside the fold instead of pushing it into a remote
                    # third lane. This keeps wrap-around layouts balanced while
                    # retaining its architectural relationship to the head.
                    x = _ceil_snap(
                        max(tail_block.right for tail_block in folded_tail)
                        + cluster_col_gap
                    )
                    b.y = int(fold_center_y - b.h / 2)
                    uses_fold_shelf = True
                vertically_overlaps_previous = previous is not None and not (
                    b.bottom + ROUTE_CLEAR <= previous.top
                    or previous.bottom + ROUTE_CLEAR <= b.top
                )
                if previous is not None and vertically_overlaps_previous and not uses_fold_shelf:
                    prior_col = previous_col if previous_col is not None else b.col
                    semantic_gap = min(2, max(0, b.col - prior_col - 1)) * 24
                    x = max(x, previous.right + cluster_col_gap + semantic_gap)
                b.x = x
                previous = b
                previous_col = b.col
                placed_ids.add(b.id)

        current_rows = {
            row: (
                min(b.left for b in row_members),
                max(b.right for b in row_members),
            )
            for row, row_members in members_by_row.items()
        }
        shift = 0
        for previous_rows in placed_cluster_rows:
            for row in set(current_rows) & set(previous_rows):
                current_left, _ = current_rows[row]
                _, previous_right = previous_rows[row]
                shift = max(shift, previous_right + COL_GAP - current_left)
        shift = max(0, _ceil_snap(shift))
        if shift:
            for b in members:
                b.x += shift
            current_rows = {
                row: (left + shift, right + shift)
                for row, (left, right) in current_rows.items()
            }
        placed_cluster_rows.append(current_rows)

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
    # A modest vertical preference lets controllers/support blocks above or
    # below a datapath connect through facing north/south ports even when they
    # are slightly offset horizontally. Clearly horizontal flows remain e/w.
    if abs(dx) >= abs(dy) * 1.25:
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
    return _rendered_boundary_point(b, side, int(x), int(y))


def _rendered_boundary_point(b: Box, side: str, x: int, y: int) -> Point:
    if b.kind != "io":
        return Point(int(x), int(y))
    radius = min(b.w / 2, b.h // 2)
    if side in {"e", "w"}:
        offset = min(radius, abs(y - b.cy))
        reach = math.sqrt(max(0.0, radius * radius - offset * offset))
        arc_center_x = b.right - radius if side == "e" else b.left + radius
        boundary_x = arc_center_x + reach if side == "e" else arc_center_x - reach
        return Point(int(round(boundary_x)), int(y))
    offset = min(radius, abs(x - b.cx))
    if b.left + radius <= x <= b.right - radius:
        return Point(int(x), b.top if side == "n" else b.bottom)
    arc_center_x = b.left + radius if x < b.cx else b.right - radius
    horizontal_offset = min(radius, abs(x - arc_center_x))
    reach = math.sqrt(max(0.0, radius * radius - horizontal_offset * horizontal_offset))
    arc_center_y = b.top + radius if side == "n" else b.bottom - radius
    boundary_y = arc_center_y - reach if side == "n" else arc_center_y + reach
    return Point(int(x), int(round(boundary_y)))


def outward(p: Point, side: str, dist: Optional[int] = None) -> Point:
    if dist is None:
        # Horizontal approaches benefit from a visibly longer final run before
        # the arrowhead. Inter-row gaps are tighter, so north/south ports keep a
        # compact stub and do not let opposing controller stubs cross.
        dist = PORT_STUB if side in {"e", "w"} else VERTICAL_PORT_STUB
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
    # When enclosure starts are already visually close, align them exactly by
    # extending the slightly inset group. Larger differences remain untouched
    # because they usually communicate intentional hierarchy or indentation.
    for base_index in range(len(result)):
        base_left = result[base_index][2]
        close_indices = [
            index
            for index in range(base_index, len(result))
            if abs(result[index][2] - base_left) <= GROUP_EDGE_ALIGN
        ]
        if len(close_indices) < 2:
            continue
        aligned_left = min(result[index][2] for index in close_indices)
        for index in close_indices:
            gid, label, left, top, width, height = result[index]
            result[index] = (
                gid,
                label,
                aligned_left,
                top,
                width + left - aligned_left,
                height,
            )
    return result


def _inside_rect(x: int, y: int, rect: Tuple[int, int, int, int]) -> bool:
    l, t, r, b = rect
    return l <= x <= r and t <= y <= b


# ---------------------------------------------------------------------------
# Port assignment and orthogonal routing
# ---------------------------------------------------------------------------


def blocked_cells(boxes: Sequence[Box], ignore: set[str], width: int, height: int) -> set[Tuple[int, int]]:
    blocked = set()
    for b in boxes:
        # Endpoint blocks waive only the external clearance halo. Their actual
        # interiors remain blocked so a feedback route cannot cut back through
        # its own source or target after leaving the port.
        clearance = 0 if b.id in ignore else ROUTE_CLEAR
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
            bend = 0.0 if (pdx, pdy) in {(0, 0), (dx, dy)} else 10.0
            axis = "h" if dx else "v"
            occupied_axes = used_axes.get((nx, ny), set()) if used_axes is not None else set()
            # Crossing an existing route is visually more ambiguous than
            # briefly sharing its direction, so keep a much stronger penalty
            # for perpendicular occupancy. Both remain soft constraints: A*
            # may still use a congested channel when geometry leaves no choice.
            crossing = 44.0 if occupied_axes and axis not in occupied_axes else 0.0
            sharing = 5.0 if axis in occupied_axes else 0.0
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
        rect = (
            box.left - ROUTE_CLEAR,
            box.top - ROUTE_CLEAR,
            box.right + ROUTE_CLEAR,
            box.bottom + ROUTE_CLEAR,
        )
        for a, b in zip(points, points[1:]):
            if _segment_intersects_rect(a, b, rect, interior=True):
                return False
    return True


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
            if abs(dy) > ROW_GAP * 3 and abs(dx) >= abs(dy) * 0.6:
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
) -> Tuple[List[List[Point]], List[str]]:
    boxes = {b.id: b for b in boxes_list}
    group_members: Dict[str, List[Box]] = defaultdict(list)
    for box in boxes_list:
        if box.group:
            group_members[box.group].append(box)
    sides = edge_sides(edges, boxes)
    ports = assign_ports(edges, sides, boxes)
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

    # Route main data first, then control/response. A* sees earlier routes and
    # mildly avoids them without making diagrams balloon outward.
    # Long datapaths claim clear channels before local wiring. Control and
    # response links follow, using the occupancy/crossing penalties above.
    order = sorted(
        range(len(edges)),
        key=lambda i: (
            {"data": 0, "clock": 1, "control": 2, "response": 3}[edges[i].kind],
            -(
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
        s = outward(p1, fs)
        t = outward(p2, ts)

        via = resolved_via(e, boxes)
        used_fallback = False
        endpoint_ids = {e.source, e.target}

        if via == "top":
            lane_y = top_lane_base + top_count * ROUTE_STEP
            top_count += 1
            first, fallback_a = astar_route(
                s, Point(s.x, lane_y), boxes_list, width, height, endpoint_ids, used, lane_y, used_axes
            )
            last, fallback_b = astar_route(
                Point(t.x, lane_y), t, boxes_list, width, height, endpoint_ids, used, lane_y, used_axes
            )
            core = simplify_polyline(first + [Point(t.x, lane_y)] + last[1:])
            used_fallback = fallback_a or fallback_b
        elif via == "bottom":
            lane_y = bottom_lane_base + bottom_count * ROUTE_STEP
            bottom_count += 1
            first, fallback_a = astar_route(
                s, Point(s.x, lane_y), boxes_list, width, height, endpoint_ids, used, lane_y, used_axes
            )
            last, fallback_b = astar_route(
                Point(t.x, lane_y), t, boxes_list, width, height, endpoint_ids, used, lane_y, used_axes
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
                            candidate, boxes_list, endpoint_ids
                        ):
                            cross_group_handoff = candidate
                            break

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
                candidate = simplify_polyline(
                    [s, Point(s.x, mid_y), Point(t.x, mid_y), t]
                )
                if route_clear_of_boxes(candidate, boxes_list, endpoint_ids):
                    local_control = candidate

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
                candidate_xs = list(range(_ceil_snap(low_x), _floor_snap(high_x) + 1, ROUTE_STEP))
                candidate_xs.sort(key=lambda x: (abs(x - desired_x), abs(x - t.x)))
                for corridor_x in candidate_xs:
                    candidate = simplify_polyline(
                        [s, Point(corridor_x, s.y), Point(corridor_x, t.y), t]
                    )
                    if route_clear_of_boxes(candidate, boxes_list, endpoint_ids):
                        long_control = candidate
                        break

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
                core, used_fallback = astar_route(
                    s, t, boxes_list, width, height, endpoint_ids, used, preferred_y, used_axes
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
                    used_axes[(_snap(a.x), y)].add("v")
            elif a.y == b.y:
                x0, x1 = sorted((a.x, b.x))
                for x in range(_snap(x0), _snap(x1) + ROUTE_STEP, ROUTE_STEP):
                    used[(x, _snap(a.y))] += 1
                    used_axes[(x, _snap(a.y))].add("h")

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


# ---------------------------------------------------------------------------
# Edge-label geometry and placement
# ---------------------------------------------------------------------------


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


def _rect_contains(outer: Rect, inner: Rect, inset: float = 0) -> bool:
    return (
        inner[0] >= outer[0] + inset
        and inner[1] >= outer[1] + inset
        and inner[2] <= outer[2] - inset
        and inner[3] <= outer[3] - inset
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


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Return true for crossings, touches, and collinear overlap."""
    def orientation(p: Point, q: Point, r: Point) -> float:
        return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)

    def on_segment(p: Point, q: Point, r: Point) -> bool:
        return (
            min(p.x, r.x) <= q.x <= max(p.x, r.x)
            and min(p.y, r.y) <= q.y <= max(p.y, r.y)
        )

    o1 = orientation(a, b, c)
    o2 = orientation(a, b, d)
    o3 = orientation(c, d, a)
    o4 = orientation(c, d, b)
    if (o1 > 0 > o2 or o2 > 0 > o1) and (o3 > 0 > o4 or o4 > 0 > o3):
        return True
    return (
        (o1 == 0 and on_segment(a, c, b))
        or (o2 == 0 and on_segment(a, d, b))
        or (o3 == 0 and on_segment(c, a, d))
        or (o4 == 0 and on_segment(c, b, d))
    )


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


def estimate_edge_label_width(label: str) -> float:
    """Estimate rendered 10.5px UI-font width with compact pill padding."""
    width = 0.0
    for char in label:
        if char in " ilI1|.,:;!'`":
            width += 3.0
        elif char in "MW@%#":
            width += 8.2
        elif char.isupper():
            width += 6.4
        elif char.isdigit():
            width += 5.8
        else:
            width += 5.4
    return max(28.0, width + 10.0)


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
        for _, candidate in sorted(
            candidates,
            key=lambda item: (
                item[0][0],
                _label_route_congestion(item[1], routes, edge_index),
                *item[0][1:],
            ),
        ):
            if _placement_is_clear(
                candidate,
                obstacles,
                routes,
                edge_index,
                placed,
                width,
                height,
                containment,
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
            if containment is not None:
                selected = _fit_label_inside(selected, containment)
            warnings.append(
                f"edge {edge_index} ({edge.source}->{edge.target}) has no collision-free label position"
            )

        placed.append(selected)
        result[edge_index] = selected

    return result, warnings


# ---------------------------------------------------------------------------
# SVG generation
# ---------------------------------------------------------------------------


SVG_CSS = """
    .bg{fill:#ffffff}
    .title{font:650 20px Inter,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;fill:#0f172a;text-anchor:middle}
    .block{fill:#f8fafc;stroke:#334155;stroke-width:1.7}
    .node.bigger .block{stroke-width:2}
    .node.smaller .block{stroke-width:1.4}
    .module{fill:#eef2ff;stroke:#4f46e5}
    .logic,.alu,.adder,.subtractor,.addsub,.multiplier,.comparator,.and,.or,.xor,.not{fill:#fff7ed;stroke:#c2410c}
    .memory-shadow{fill:#ccfbf1;stroke:#0f766e;stroke-width:1.2}
    .memory,.fifo{fill:#ecfeff;stroke:#0f766e}
    .reg,.counter{fill:#eff6ff;stroke:#2563eb}
    .fsm,.arbiter{fill:#fdf2f8;stroke:#be185d}
    .fsm{stroke-dasharray:5 3}
    .mux,.demux{fill:#fefce8;stroke:#a16207}
    .io{fill:#f0fdf4;stroke:#15803d}
    .symbol-line{fill:none;stroke:#64748b;stroke-width:1.2}
    .clock-glyph{stroke:#2563eb;stroke-width:1.7}
    .operator-badge{fill:#fff;stroke:#c2410c;stroke-width:1.2}
    .operator{font:700 12px Inter,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;fill:#9a3412;text-anchor:middle}
    .operator.alu-operator{font-size:9px}
    .block-label{font:600 14px Inter,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;fill:#0f172a;text-anchor:middle}
    .block-label.bigger{font-size:15px;font-weight:650}
    .block-label.smaller{font-size:13px;font-weight:550}
    .subtitle{font:11px Inter,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;fill:#475569;text-anchor:middle}
    .subtitle.bigger{font-size:11.5px}
    .subtitle.smaller{font-size:10.5px}
    .group{fill-opacity:.58;stroke-width:1.1;stroke-dasharray:6 4}
    .group.group-0{fill:#eff6ff;stroke:#93c5fd}
    .group.group-1{fill:#f0fdf4;stroke:#86efac}
    .group.group-2{fill:#fff7ed;stroke:#fdba74}
    .group.group-3{fill:#faf5ff;stroke:#d8b4fe}
    .group-label{font:650 13px Inter,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;fill:#334155}
    .edge{fill:none;stroke:#475569;stroke-width:1.6;stroke-linejoin:round;stroke-linecap:butt}
    .edge.data{stroke:#2563eb}
    .edge.bus{stroke-width:3}
    .edge.control{stroke:#a16207;stroke-dasharray:6 4}
    .edge.clock{stroke:#7c3aed;stroke-dasharray:2 3}
    .edge.response{stroke:#0f766e}
    .arrowhead{stroke:none}
    .arrowhead.data{fill:#2563eb}
    .arrowhead.control{fill:#a16207}
    .arrowhead.clock{fill:#7c3aed}
    .arrowhead.response{fill:#0f766e}
    .edge-label{font:500 10.5px Inter,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;fill:#334155;text-anchor:middle}
    .edge-label.data{fill:#1d4ed8}
    .edge-label.control{fill:#854d0e}
    .edge-label.clock{fill:#6d28d9}
    .edge-label.response{fill:#0f766e}
    .edge-label-bg{fill:#fff;fill-opacity:.98;stroke:#cbd5e1;stroke-width:.7}
    .label-leader{fill:none;stroke:#cbd5e1;stroke-width:.9}
    """


def svg_block_shadow(b: Box) -> str:
    if b.kind != "memory":
        return ""
    return (
        f'<rect x="{b.x+7}" y="{b.y+7}" width="{b.w}" height="{b.h}" '
        'rx="4" class="memory-shadow"/>'
    )


def svg_block(b: Box) -> str:
    x, y, w, h = b.x, b.y, b.w, b.h
    parts = [f'<g class="node {b.prominence}">']
    if b.kind == "mux":
        pts = f"{x},{y} {x+w},{y+8} {x+w},{y+h-8} {x},{y+h}"
        parts.append(f'<polygon points="{pts}" class="block mux"/>')
    elif b.kind == "demux":
        pts = f"{x},{y+8} {x+w},{y} {x+w},{y+h} {x},{y+h-8}"
        parts.append(f'<polygon points="{pts}" class="block demux"/>')
    elif b.kind == "io":
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h//2}" class="block io"/>')
    elif b.kind == "fsm":
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" class="block fsm"/>')
    elif b.kind == "memory":
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" class="block memory"/>')
    elif b.kind == "fifo":
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" class="block fifo"/>')
        parts.append(f'<line x1="{x+13}" y1="{y+10}" x2="{x+13}" y2="{y+h-10}" class="symbol-line"/>')
        parts.append(f'<line x1="{x+w-13}" y1="{y+10}" x2="{x+w-13}" y2="{y+h-10}" class="symbol-line"/>')
    elif b.kind == "reg":
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2" class="block reg"/>')
        parts.append(
            f'<polyline points="{x},{y+h-20} {x+8},{y+h-14} {x},{y+h-8}" '
            'class="symbol-line clock-glyph"/>'
        )
    elif b.kind == "counter":
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2" class="block counter"/>')
        parts.append(
            f'<polyline points="{x},{y+h-20} {x+8},{y+h-14} {x},{y+h-8}" '
            'class="symbol-line clock-glyph"/>'
        )
    elif b.kind == "arbiter":
        pts = f"{x+10},{y} {x+w-10},{y} {x+w},{y+h//2} {x+w-10},{y+h} {x+10},{y+h} {x},{y+h//2}"
        parts.append(f'<polygon points="{pts}" class="block arbiter"/>')
    elif b.kind in {"alu", "adder", "subtractor", "addsub", "multiplier", "comparator"}:
        operator = {
            "alu": "ALU",
            "adder": "+",
            "subtractor": "−",
            "addsub": "±",
            "multiplier": "×",
            "comparator": "=",
        }[b.kind]
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" class="block {b.kind}"/>')
        if b.kind == "alu":
            parts.append(f'<rect x="{x+8}" y="{y+8}" width="30" height="18" rx="6" class="operator-badge"/>')
            parts.append(f'<text x="{x+23}" y="{y+21}" class="operator alu-operator">{operator}</text>')
        else:
            # Keep the operation unmistakable without stealing horizontal room
            # from long datapath names. The compact badge sits fully inside the
            # corner and leaves a clear gap before the centered block label.
            parts.append(f'<circle cx="{x+14}" cy="{y+14}" r="8" class="operator-badge"/>')
            parts.append(f'<text x="{x+14}" y="{y+18}" class="operator">{operator}</text>')
    elif b.kind == "and":
        d = (
            f"M {x},{y} L {x+w//2},{y} "
            f"C {x+w-5},{y} {x+w-5},{y+h} {x+w//2},{y+h} L {x},{y+h} Z"
        )
        parts.append(f'<path d="{d}" class="block and"/>')
    elif b.kind in {"or", "xor"}:
        d = (
            f"M {x},{y} Q {x+20},{y+h//2} {x},{y+h} "
            f"Q {x+w*3//5},{y+h} {x+w},{y+h//2} Q {x+w*3//5},{y} {x},{y} Z"
        )
        parts.append(f'<path d="{d}" class="block {b.kind}"/>')
        if b.kind == "xor":
            parts.append(
                f'<path d="M {x-6},{y} Q {x+14},{y+h//2} {x-6},{y+h}" '
                'class="symbol-line gate-extra"/>'
            )
    elif b.kind == "not":
        pts = f"{x},{y} {x+w-14},{y+h//2} {x},{y+h}"
        parts.append(f'<polygon points="{pts}" class="block not"/>')
        parts.append(f'<circle cx="{x+w-7}" cy="{y+h//2}" r="7" class="block not not-bubble"/>')
    else:
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" class="block {b.kind}"/>')

    if b.kind in {"reg", "counter"}:
        label_room = w - 40
        subtitle_room = w - 22
    else:
        label_room = w - (50 if b.kind in ARITHMETIC_KINDS else 26)
        subtitle_room = w - 26
    lines = block_label_lines(b.label, label_room, b.prominence)
    subtitles = block_subtitle_lines(b.subtitle, subtitle_room, b.prominence)
    total_step = max(0, len(lines) - 1) * 17
    if subtitles:
        total_step += 20 + max(0, len(subtitles) - 1) * 14
    label_y = b.cy + 4 - total_step / 2
    prominence_class = f" {b.prominence}" if b.prominence != "normal" else ""
    for j, line in enumerate(lines):
        parts.append(
            f'<text x="{b.cx}" y="{label_y + j*17:.1f}" '
            f'class="block-label{prominence_class}">{escape(line)}</text>'
        )
    if subtitles:
        subtitle_y = label_y + max(0, len(lines) - 1) * 17 + 20
        for j, line in enumerate(subtitles):
            parts.append(
                f'<text x="{b.cx}" y="{subtitle_y + j*14:.1f}" '
                f'class="subtitle{prominence_class}">{escape(line)}</text>'
            )
    parts.append("</g>")
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


def _svg_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _arrow_geometry(e: Edge, route: Sequence[Point]) -> Tuple[List[Point], List[Point]]:
    if len(route) < 2:
        return list(route), []
    tip = route[-1]
    previous = route[-2]
    dx, dy = tip.x - previous.x, tip.y - previous.y
    segment_length = math.hypot(dx, dy)
    if segment_length == 0:
        return list(route), []
    is_bus = bool(e.width and e.width > 1)
    desired_length = 11.0 if is_bus else 10.0
    desired_half_width = 5.5 if is_bus else 5.0
    head_length = min(desired_length, segment_length * 0.8)
    half_width = desired_half_width * head_length / desired_length
    ux, uy = dx / segment_length, dy / segment_length
    base = Point(tip.x - ux * head_length, tip.y - uy * head_length)
    perpendicular_x, perpendicular_y = -uy, ux
    head = [
        Point(base.x + perpendicular_x * half_width, base.y + perpendicular_y * half_width),
        tip,
        Point(base.x - perpendicular_x * half_width, base.y - perpendicular_y * half_width),
    ]
    return [*route[:-1], base], head


def svg_edge_shaft(e: Edge, route: Sequence[Point]) -> str:
    shaft, _ = _arrow_geometry(e, route)
    pts = " ".join(f"{_svg_number(p.x)},{_svg_number(p.y)}" for p in shaft)
    is_bus = bool(e.width and e.width > 1)
    cls = f"edge {e.kind}" + (" bus" if is_bus else "")
    return f'<polyline points="{pts}" class="{cls}"/>'


def svg_edge_arrowhead(e: Edge, route: Sequence[Point]) -> str:
    _, head = _arrow_geometry(e, route)
    if not head:
        return ""
    pts = " ".join(f"{_svg_number(p.x)},{_svg_number(p.y)}" for p in head)
    is_bus = bool(e.width and e.width > 1)
    cls = f"arrowhead {e.kind}" + (" bus" if is_bus else "")
    return f'<polygon points="{pts}" class="{cls}"/>'


def svg_edge_path(e: Edge, route: Sequence[Point]) -> str:
    return "\n".join(part for part in (svg_edge_shaft(e, route), svg_edge_arrowhead(e, route)) if part)


def svg_edge_label(e: Edge, placement: Optional[LabelPlacement]) -> str:
    label = edge_label_text(e)
    if not label or placement is None:
        return ""
    parts = []
    leader_segments = _leader_segments(placement)
    if leader_segments:
        leader_points = [leader_segments[0][0], *(end for _, end in leader_segments)]
        points = " ".join(f"{point.x},{point.y}" for point in leader_points)
        parts.append(f'<polyline points="{points}" class="label-leader"/>')
    rect = placement.rect
    parts.append(
        f'<rect x="{rect[0]:.1f}" y="{rect[1]:.1f}" width="{placement.width:.1f}" '
        f'height="{placement.height:.1f}" rx="5" class="edge-label-bg"/>'
    )
    parts.append(
        f'<text x="{placement.x:.1f}" y="{placement.y:.1f}" class="edge-label {e.kind}">'
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
