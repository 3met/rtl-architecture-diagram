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


def _choose_main_row(
    members_by_row: Dict[int, List[Box]], edges: Sequence[Edge]
) -> int:
    """Choose the architectural datapath row, not merely the first tied row."""
    ids_by_row = {
        row: {box.id for box in members}
        for row, members in members_by_row.items()
    }

    def key(row: int) -> Tuple[int, int, int, int, int]:
        row_ids = ids_by_row[row]
        internal_data = sum(
            edge.kind == "data"
            and edge.source in row_ids
            and edge.target in row_ids
            for edge in edges
        )
        incident_data = sum(
            edge.kind == "data"
            and (edge.source in row_ids or edge.target in row_ids)
            for edge in edges
        )
        controller_count = sum(
            box.kind in {"fsm", "arbiter"}
            for box in members_by_row[row]
        )
        return (
            -len(members_by_row[row]),
            -internal_data,
            -incident_data,
            controller_count,
            row,
        )

    return min(members_by_row, key=key)


def _choose_fold_split(
    main: Sequence[Box],
    members: Sequence[Box],
    edges: Sequence[Edge],
    main_row: int,
) -> int:
    """Choose a compact fold without separating shared support hardware.

    A midpoint-only fold is attractive geometrically but can strand a
    controller or memory on the opposite return lane from half of its fanout.
    Evaluate the few balanced cuts around the midpoint and strongly prefer a
    cut that keeps each support block's main-row consumers together.  The
    result remains deterministic and keeps an ordinary pipeline at the exact
    midpoint.
    """
    midpoint = (len(main) + 1) // 2
    if len(main) < 4:
        return midpoint

    main_index = {box.id: index for index, box in enumerate(main)}
    support = [box for box in members if box.id not in main_index]
    connected_indices: Dict[str, set[int]] = defaultdict(set)
    for edge in edges:
        if edge.source in main_index:
            connected_indices[edge.target].add(main_index[edge.source])
        if edge.target in main_index:
            connected_indices[edge.source].add(main_index[edge.target])

    low = max(2, midpoint - 1)
    high = min(len(main) - 2, midpoint + 2)
    best = (float("inf"), midpoint)
    for split in range(low, high + 1):
        score = abs(split - (len(main) - split)) * 4
        for box in support:
            indices = connected_indices.get(box.id, set())
            if not indices:
                continue
            has_head = any(index < split for index in indices)
            has_tail = any(index >= split for index in indices)
            if has_head and has_tail:
                score += 80
            elif box.row < main_row and has_tail:
                # A support block declared above the datapath should not be
                # pulled below the fold merely because every consumer landed
                # one position past a naive midpoint.
                score += 26
        candidate = (score, abs(split - midpoint), split)
        if candidate < (best[0], abs(best[1] - midpoint), best[1]):
            best = (score, split)
    return best[1]


def _place_interstitial_groups(
    boxes: Sequence[Box], edges: Sequence[Edge]
) -> None:
    """Place small state/bridge groups in a free band between their clients.

    A group that connects an upper datapath to a lower datapath is often live
    state rather than another pipeline stage.  Leaving it on the upper row
    forces both lower read paths to traverse most of the canvas.  When a real
    vertical band exists, arrange that small group horizontally inside it and
    align each member with the external blocks it actually serves.
    """
    by_id = {box.id: box for box in boxes}
    members_by_group: Dict[str, List[Box]] = defaultdict(list)
    for box in boxes:
        if box.group:
            members_by_group[box.group].append(box)

    neighbors: Dict[str, List[Box]] = defaultdict(list)
    for edge in edges:
        neighbors[edge.source].append(by_id[edge.target])
        neighbors[edge.target].append(by_id[edge.source])

    for group_id, members in sorted(members_by_group.items()):
        if not 2 <= len(members) <= 4:
            continue
        if not all(box.kind in {"memory", "fifo", "reg"} for box in members):
            continue
        external = [
            other
            for box in members
            for other in neighbors.get(box.id, [])
            if other.group != group_id
        ]
        if not external:
            continue
        group_center = sum(box.cy for box in members) / len(members)
        upper = [box for box in external if box.cy < group_center]
        lower = [box for box in external if box.cy > group_center]
        if not upper or not lower:
            continue
        upper_group_ids = {box.group for box in upper if box.group}
        lower_group_ids = {box.group for box in lower if box.group}
        upper_scope = [
            box for box in boxes if box.group in upper_group_ids
        ] or upper
        lower_scope = [
            box for box in boxes if box.group in lower_group_ids
        ] or lower
        upper_bottom = max(box.bottom for box in upper_scope)
        lower_top = min(box.top for box in lower_scope)
        tallest = max(box.h for box in members)
        if lower_top - upper_bottom < tallest + 2 * GROUP_PAD + ROUTE_CLEAR:
            continue

        center_y = _snap((upper_bottom + lower_top) / 2)
        desired: Dict[str, float] = {}
        for box in members:
            connected = [
                other
                for other in neighbors.get(box.id, [])
                if other.group != group_id
            ]
            if connected:
                # Repeated architectural connections intentionally carry
                # weight here (for example read + response around one state
                # memory), so use the list rather than a de-duplicated set.
                desired[box.id] = sum(other.cx for other in connected) / len(connected)
            else:
                desired[box.id] = box.cx

        ordered = sorted(members, key=lambda box: (desired[box.id], box.id))
        compact_gap = 2 * ROUTE_CLEAR
        packed_width = (
            sum(box.w for box in ordered)
            + compact_gap * max(0, len(ordered) - 1)
        )
        target_center = sum(desired.values()) / len(desired)
        cursor = max(MARGIN_X, _snap(target_center - packed_width / 2))
        for box in ordered:
            box.y = int(center_y - box.h / 2)
            box.x = _snap(cursor)
            cursor = box.right + compact_gap


def _reflow_dense_groups(
    boxes: Sequence[Box], edges: Sequence[Edge]
) -> None:
    """Lay out dense datapaths as compact proximity graphs.

    Semantic columns still define pipeline order, but a dense group is not
    forced to consume one panoramic row.  Cross-group consumers can pull an
    early stage toward the hardware feeding it, the forward path occupies a
    compact upper lane, and the result path returns beneath it.  Support
    hardware is then aligned from actual connectivity rather than nominal
    column number.
    """
    by_id = {box.id: box for box in boxes}
    members_by_group: Dict[str, List[Box]] = defaultdict(list)
    for box in boxes:
        if box.group:
            members_by_group[box.group].append(box)

    links: Dict[str, List[Tuple[Box, Edge]]] = defaultdict(list)
    for edge in edges:
        links[edge.source].append((by_id[edge.target], edge))
        links[edge.target].append((by_id[edge.source], edge))

    for group_id, members in sorted(members_by_group.items()):
        members_by_row: Dict[int, List[Box]] = defaultdict(list)
        for box in members:
            members_by_row[box.row].append(box)
        main_row = _choose_main_row(members_by_row, edges)
        main = sorted(members_by_row[main_row], key=lambda box: (box.col, box.id))
        if len(main) < FOLD_ROW_THRESHOLD:
            continue

        split = (len(main) + 1) // 2
        head = main[:split]
        tail = main[split:]
        head_center_y = _snap(min(box.cy for box in head))
        head_top = min(box.top for box in head)
        gap = DENSE_COL_GAP

        cursor = MARGIN_X
        previous: Optional[Box] = None
        for box in head:
            if previous is not None:
                cursor = previous.right + gap
                external_centers = [
                    other.cx
                    for other, _ in links.get(box.id, [])
                    if other.group != group_id
                ]
                if external_centers:
                    target_center = _median_int(
                        [previous.cx, *external_centers]
                    )
                    target_left = int(round(target_center - box.w / 2))
                    cursor = max(
                        cursor,
                        min(target_left, cursor + 4 * COL_GAP),
                    )
            box.x = _snap(cursor)
            box.y = int(head_center_y - box.h / 2)
            previous = box

        tail_height = max(box.h for box in tail)
        tail_center_y = _snap(
            max(box.bottom for box in head) + ROW_GAP + tail_height / 2
        )
        first_tail = tail[0]
        first_tail.x = int(round(head[-1].cx - first_tail.w / 2))
        first_tail.y = int(tail_center_y - first_tail.h / 2)
        previous = first_tail
        for box in tail[1:]:
            connecting_labels = [
                edge_label_text(edge)
                for edge in edges
                if {edge.source, edge.target} == {previous.id, box.id}
                and edge_label_text(edge)
            ]
            label_gap = max(
                (
                    min(
                        COL_GAP + 40,
                        _ceil_snap(estimate_edge_label_width(label) + 30),
                    )
                    for label in connecting_labels
                ),
                default=0,
            )
            tail_gap = max(gap, label_gap)
            box.x = _snap(previous.left - tail_gap - box.w)
            box.y = int(tail_center_y - box.h / 2)
            previous = box

        main_ids = {box.id for box in main}
        upper_support: List[Tuple[float, Box]] = []
        lower_support: List[Tuple[float, Box]] = []
        for box in members:
            if box.id in main_ids:
                continue
            main_links = [
                (other, edge)
                for other, edge in links.get(box.id, [])
                if other.id in main_ids
            ]
            if not main_links:
                desired_center = box.cx
            elif box.kind in {"memory", "fifo"}:
                data_links = [
                    (other, edge)
                    for other, edge in main_links
                    if edge.kind == "data"
                ]
                anchors = data_links or main_links
                desired_center = sum(other.cx for other, _ in anchors) / len(anchors)
            else:
                main_center = sum(other.cx for other, _ in main_links) / len(main_links)
                same_group_links = [
                    other
                    for other, _ in links.get(box.id, [])
                    if other.group == group_id
                ]
                all_center = (
                    sum(other.cx for other in same_group_links) / len(same_group_links)
                    if same_group_links
                    else main_center
                )
                desired_center = (
                    main_center
                    if box.kind == "fsm"
                    else 0.65 * main_center + 0.35 * all_center
                )
            target = upper_support if box.row < main_row else lower_support
            target.append((desired_center, box))

        def place_support_lane(
            support: List[Tuple[float, Box]], upper: bool
        ) -> None:
            if not support:
                return
            ordered = sorted(support, key=lambda item: (item[0], item[1].col, item[1].id))
            lane_height = max(box.h for _, box in ordered)
            lane_center = _snap(
                head_top - ROW_GAP - lane_height / 2
                if upper
                else max(box.bottom for box in tail) + ROW_GAP + lane_height / 2
            )
            previous_box: Optional[Box] = None
            for desired_center, box in ordered:
                box.y = int(lane_center - box.h / 2)
                candidate_x = max(
                    MARGIN_X,
                    int(round(desired_center - box.w / 2)),
                )
                if previous_box is not None:
                    connecting_labels = [
                        edge_label_text(edge)
                        for edge in edges
                        if {edge.source, edge.target}
                        == {previous_box.id, box.id}
                        and edge_label_text(edge)
                    ]
                    label_gap = max(
                        (
                            _ceil_snap(
                                estimate_edge_label_width(label)
                                + 2 * PORT_STUB
                                + 2 * LABEL_BLOCK_CLEAR
                            )
                            for label in connecting_labels
                        ),
                        default=0,
                    )
                    candidate_x = max(
                        candidate_x,
                        previous_box.right + max(2 * ROUTE_CLEAR, label_gap),
                    )
                box.x = _snap(candidate_x)
                previous_box = box

        place_support_lane(upper_support, True)
        place_support_lane(lower_support, False)


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
            if response_out[box.id]:
                return 1
            if box.group and data_out[box.id] and not data_in[box.id]:
                return -1
            if data_in[box.id] and not data_out[box.id]:
                return 1
            if data_out[box.id] and not data_in[box.id]:
                return 1
        return 0

    # Derive group bands from the inter-group dataflow graph. Declaration order
    # carries no architectural meaning. Storage-only groups with both upstream
    # and downstream clients may occupy the boundary between those clients.
    cluster_successors: Dict[str, set[str]] = defaultdict(set)
    cluster_predecessors: Dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        source_cluster = cluster_of[edge.source]
        target_cluster = cluster_of[edge.target]
        if (
            edge.kind == "data"
            and source_cluster != target_cluster
        ):
            cluster_successors[source_cluster].add(target_cluster)
            cluster_predecessors[target_cluster].add(source_cluster)

    cluster_indegree = {
        cluster: len(cluster_predecessors.get(cluster, set()))
        for cluster in members_by_cluster
    }
    ready_clusters = sorted(
        cluster for cluster, degree in cluster_indegree.items() if degree == 0
    )
    cluster_rank = {cluster: 0 for cluster in members_by_cluster}
    visited_clusters = set()
    while ready_clusters:
        cluster = ready_clusters.pop(0)
        visited_clusters.add(cluster)
        for target in sorted(cluster_successors.get(cluster, set())):
            cluster_rank[target] = max(
                cluster_rank[target], cluster_rank[cluster] + 1
            )
            cluster_indegree[target] -= 1
            if cluster_indegree[target] == 0:
                ready_clusters.append(target)
                ready_clusters.sort()
    for cluster in sorted(set(members_by_cluster) - visited_clusters):
        cluster_rank[cluster] = max(
            (cluster_rank[pred] + 1
             for pred in cluster_predecessors.get(cluster, set())),
            default=0,
        )

    cluster_order = sorted(
        members_by_cluster,
        key=lambda cluster: (cluster_rank[cluster], cluster),
    )
    cluster_order_index = {
        cluster: index for index, cluster in enumerate(cluster_order)
    }
    local_row_bounds = {
        cluster: (
            min(row_offset(box) for box in members),
            max(row_offset(box) for box in members),
        )
        for cluster, members in members_by_cluster.items()
    }

    base_rows: Dict[str, int] = {}
    for cluster in cluster_order:
        members = members_by_cluster[cluster]
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
            predecessors = [
                predecessor
                for predecessor in cluster_predecessors.get(cluster, set())
                if predecessor in base_rows
            ]
            if not predecessors:
                base_rows[cluster] = 1
                continue
            predecessor_bottom = max(
                base_rows[predecessor] + local_row_bounds[predecessor][1]
                for predecessor in predecessors
            )
            is_storage_bridge = (
                bool(cluster_successors.get(cluster))
                and all(
                    box.kind in {"memory", "fifo", "reg"}
                    for box in members
                )
            )
            target_top = predecessor_bottom + (0 if is_storage_bridge else 1)
            base_rows[cluster] = target_top - local_row_bounds[cluster][0]

    occupied_by_cluster: Dict[str, set[Tuple[int, int]]] = defaultdict(set)
    for box_id, position in explicit_positions.items():
        occupied_by_cluster[cluster_of[box_id]].add(position)
    missing = sorted(
        (box for box in boxes if box.id not in explicit_positions),
        key=lambda box: (
            cluster_order_index.get(cluster_of[box.id], -1),
            ranks[box.id],
            box.id,
        ),
    )
    for box in missing:
        box.col = ranks[box.id]
        occupied = occupied_by_cluster[cluster_of[box.id]]
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
        # Automatic peers may share a semantic rank/lane. Physical layout
        # separates and orders them; only hard overrides reserve coordinates.


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


def _parse_importance(raw: dict, owner: str) -> float:
    value = raw.get("importance", 1.0)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise DiagramError(f"{owner}: importance must be a positive number")
    return float(value)


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
        importance = _parse_importance(raw, f"block {block_id}")
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
                importance=importance,
                position_fixed="at" in raw,
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
        count = raw.get("count")
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool) or count <= 0
        ):
            raise DiagramError(f"edge {index}: count must be a positive integer")
        if count is not None and width is None:
            raise DiagramError(f"edge {index}: count requires width")
        importance = _parse_importance(raw, f"edge {index}")
        edges.append(
            Edge(
                source=source,
                target=target,
                source_port=source_port,
                target_port=target_port,
                label=str(raw.get("label", "")).strip(),
                width=width,
                count=count,
                kind=kind,
                from_side=from_side,
                to_side=to_side,
                via=via,
                importance=importance,
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

__all__ = [name for name in globals() if not name.startswith("__")]
