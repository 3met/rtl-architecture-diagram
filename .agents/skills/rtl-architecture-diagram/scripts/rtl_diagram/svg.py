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
from .labels import *
from .routing import *

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
    .group-label-bg{fill:#fff;fill-opacity:.96}
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
    .label-leader-halo{fill:none;stroke:#ffffff;stroke-width:4}
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
    is_bus = bool(e.width and e.width * (e.count or 1) > 1)
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
    is_bus = bool(e.width and e.width * (e.count or 1) > 1)
    cls = f"edge {e.kind}" + (" bus" if is_bus else "")
    return f'<polyline points="{pts}" class="{cls}"/>'


def svg_edge_arrowhead(e: Edge, route: Sequence[Point]) -> str:
    _, head = _arrow_geometry(e, route)
    if not head:
        return ""
    pts = " ".join(f"{_svg_number(p.x)},{_svg_number(p.y)}" for p in head)
    is_bus = bool(e.width and e.width * (e.count or 1) > 1)
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
        if placement.fallback:
            parts.append(
                f'<polyline points="{points}" class="label-leader-halo"/>'
            )
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

__all__ = [name for name in globals() if not name.startswith("__")]
