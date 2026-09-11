#!/usr/bin/env python3
"""Stable CLI and import facade for the RTL architecture renderer."""

from pathlib import Path
import sys

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from rtl_diagram.cli import *
from rtl_diagram.core import *
from rtl_diagram.ir import *
from rtl_diagram.labels import *
from rtl_diagram.layout import *
from rtl_diagram.routing import *
from rtl_diagram.svg import *


if __name__ == "__main__":
    raise SystemExit(main())
