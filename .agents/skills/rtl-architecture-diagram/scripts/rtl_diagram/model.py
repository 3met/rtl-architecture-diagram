"""Stable data model shared by layout, routing, labels, and SVG output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


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
    height: float = 18
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
    """Raised when the compact architecture JSON IR is invalid."""

