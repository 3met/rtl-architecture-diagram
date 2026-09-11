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
from .geometry import (
    box_rect as _box_rect,
    rect_contains as _rect_contains,
    rects_overlap as _rects_overlap,
    segment_intersects_rect as _segment_intersects_rect,
    segments_intersect as _segments_intersect,
)
from .metrics import *
from .model import *

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
# Text measurement and automatic sizing
# ---------------------------------------------------------------------------


def _split_endpoint(value: str) -> Tuple[str, str]:
    if "." in value:
        node, port = value.split(".", 1)
        return node.strip(), port.strip()
    return value.strip(), ""


def edge_label_text(edge: Edge) -> str:
    """Return the visible edge label, including an implicit bus width."""
    label = edge.label
    if edge.width:
        width_text = (
            f"{edge.count} × {edge.width}b"
            if edge.count
            else f"{edge.width}b"
        )
        if not label:
            label = width_text
        elif (
            width_text not in label
            and (edge.count is not None or str(edge.width) not in label)
        ):
            label = f"{label} · {width_text}"
    return label


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


def _snap(value: float, step: int = ROUTE_STEP) -> int:
    return int(round(value / step) * step)


def _floor_snap(value: float, step: int = ROUTE_STEP) -> int:
    return int(math.floor(value / step) * step)


def _ceil_snap(value: float, step: int = ROUTE_STEP) -> int:
    return int(math.ceil(value / step) * step)


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

__all__ = [name for name in globals() if not name.startswith("__")]
