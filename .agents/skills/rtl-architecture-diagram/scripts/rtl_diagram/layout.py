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
                estimate_edge_label_width(label),
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
        main_row = _choose_main_row(members_by_row, edges or ())
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
                    pair_label_width = labeled_pair_gap.get(
                        tuple(sorted((previous_main.id, b.id))), 0
                    )
                    label_padding = (
                        24 if len(main) >= DENSE_ROW_THRESHOLD else 80
                    )
                    pair_label_gap = (
                        min(
                            COL_GAP + label_padding,
                            _ceil_snap(pair_label_width + label_padding),
                        )
                        if pair_label_width
                        else 0
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
            split = _choose_fold_split(main, members, edges or (), main_row)
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
            desired_by_id: Dict[str, float] = {}
            row_members = members_by_row[row]
            directly_anchored: set[str] = set()
            for support_box in row_members:
                main_anchors = [
                    other
                    for other in neighbors.get(support_box.id, [])
                    if other.id in placed_ids and other.row == main_row
                ]
                if main_anchors:
                    directly_anchored.add(support_box.id)
                    desired_by_id[support_box.id] = sum(
                        other.cx for other in main_anchors
                    ) / len(main_anchors)

            # A leaf such as a completion flag may connect only to another
            # support-row block. Propagate that neighbor's datapath anchor so
            # the leaf remains beside its producer instead of drifting to a
            # nominal semantic column on the far side of the group.
            unresolved = {box.id for box in row_members} - desired_by_id.keys()
            for _ in range(len(row_members)):
                resolved_now: Dict[str, float] = {}
                for support_id in unresolved:
                    connected = [
                        other
                        for other in neighbors.get(support_id, [])
                        if other.id in desired_by_id
                    ]
                    if connected:
                        propagated = sum(
                            desired_by_id[other.id] for other in connected
                        ) / len(connected)
                        support_box = by_id[support_id]
                        neighbor_col = sum(other.col for other in connected) / len(
                            connected
                        )
                        semantic_anchor = column_anchor(support_box.col)
                        if support_box.col > neighbor_col:
                            propagated = max(propagated, semantic_anchor)
                        elif support_box.col < neighbor_col:
                            propagated = min(propagated, semantic_anchor)
                        resolved_now[support_id] = propagated
                if not resolved_now:
                    break
                desired_by_id.update(resolved_now)
                unresolved -= resolved_now.keys()
            for support_box in row_members:
                desired_by_id.setdefault(
                    support_box.id,
                    column_anchor(support_box.col),
                )
            ordered = sorted(
                row_members,
                key=lambda b: (
                    desired_by_id[b.id],
                    0 if b.id in directly_anchored else 1,
                    b.col,
                    b.id,
                ),
            )
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
                    desired_by_id[b.id]
                    if connected_main
                    else (
                        sum(other.cx for other in anchors) / len(anchors)
                        if anchors
                        else desired_by_id[b.id]
                    )
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
                    and anchors
                    and all(other.id in folded_ids for other in anchors)
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
                    support_leaf_gap = (
                        PORT_STUB + ROUTE_CLEAR
                        if b.id not in directly_anchored
                        else 2 * ROUTE_CLEAR + ROUTE_STEP
                    )
                    pair_label_width = labeled_pair_gap.get(
                        tuple(sorted((previous.id, b.id))),
                        0,
                    )
                    labeled_gap = (
                        _ceil_snap(pair_label_width + 16)
                        if pair_label_width
                        else 0
                    )
                    x = max(
                        x,
                        previous.right + max(support_leaf_gap, labeled_gap),
                    )
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

    if edges:
        # Dense datapaths and bridge-state groups influence one another. A few
        # deterministic relaxation passes converge quickly and avoid a hard
        # dependency on whichever group happened to be packed first.
        _place_interstitial_groups(boxes, edges)
        for _ in range(3):
            _reflow_dense_groups(boxes, edges)
            _place_interstitial_groups(boxes, edges)

    width = max(b.right for b in boxes) + MARGIN_X
    height = max(b.bottom for b in boxes) + MARGIN_BOTTOM
    return _snap(width), _snap(height)


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

__all__ = [name for name in globals() if not name.startswith("__")]
