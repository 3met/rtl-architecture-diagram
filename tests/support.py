import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "rtl-architecture-diagram"
RENDERER_PATH = SKILL / "scripts" / "render.py"
EXAMPLE_JSON = SKILL / "examples" / "tt.diagram.json"
EXAMPLE_SVG = SKILL / "examples" / "tt.svg"
NNUE_JSON = ROOT / "examples" / "nnue-evaluator.diagram.json"
NNUE_SVG = ROOT / "examples" / "nnue-evaluator.svg"


def load_renderer():
    spec = importlib.util.spec_from_file_location("rtl_architecture_renderer", RENDERER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load renderer from {RENDERER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


renderer = load_renderer()


def render_example():
    title, boxes, edges, groups, warnings = renderer.load_diagram(EXAMPLE_JSON)
    return renderer.render(title, boxes, edges, groups, warnings), warnings


def render_nnue_example():
    title, boxes, edges, groups, warnings = renderer.load_diagram(NNUE_JSON)
    return renderer.render(title, boxes, edges, groups, warnings), warnings
