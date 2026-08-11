import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "rtl-architecture-diagram"
RENDERER_PATH = SKILL / "scripts" / "render.py"
EXAMPLE_JSON = SKILL / "examples" / "tt.diagram.json"
EXAMPLE_SVG = SKILL / "examples" / "tt.svg"


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


class RendererTests(unittest.TestCase):
    def test_checked_in_example_is_current_and_lints_cleanly(self):
        actual, warnings = render_example()

        self.assertEqual([], warnings)
        self.assertEqual(EXAMPLE_SVG.read_text(encoding="utf-8"), actual)
        ET.fromstring(actual)

    def test_cli_renders_from_an_arbitrary_working_directory(self):
        expected, _ = render_example()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "diagram.svg"
            result = subprocess.run(
                [
                    sys.executable,
                    str(RENDERER_PATH),
                    str(EXAMPLE_JSON),
                    "-o",
                    str(output),
                    "--lint",
                    "--strict",
                ],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("lint: ok", result.stderr)
            self.assertEqual(expected, output.read_text(encoding="utf-8"))

    def test_ports_remain_exactly_on_block_boundaries(self):
        boxes = [
            renderer.Box("source", "Source block", "module", 0, 0),
            renderer.Box("target", "Target", "module", 1, 0),
        ]
        renderer.layout_boxes(boxes)
        edges = [renderer.Edge("source", "target")]
        by_id = {box.id: box for box in boxes}
        sides = renderer.edge_sides(edges, by_id)
        ports = renderer.assign_ports(edges, sides, by_id)

        source_port, target_port, _, _ = ports[0]
        self.assertEqual(boxes[0].right, source_port.x)
        self.assertEqual(boxes[1].left, target_port.x)

    def test_long_edge_label_is_placed_clear_of_blocks(self):
        boxes = [
            renderer.Box("source", "Source", "module", 0, 0),
            renderer.Box("target", "Target", "module", 1, 0),
        ]
        width, height = renderer.layout_boxes(boxes)
        edge = renderer.Edge(
            "source",
            "target",
            label="architectural request",
            width=128,
        )
        routes, route_warnings = renderer.route_edges([edge], boxes, width, height)
        placements, label_warnings = renderer.place_edge_labels(
            "Label test", boxes, [edge], routes, [], width, height
        )

        self.assertEqual([], route_warnings)
        self.assertEqual([], label_warnings)
        self.assertIsNotNone(placements[0])
        for box in boxes:
            self.assertFalse(
                renderer._rects_overlap(placements[0].rect, renderer._box_rect(box), 1)
            )
        geometry_warnings = renderer.lint_geometry(
            boxes, [edge], routes, placements, "Label test"
        )
        self.assertFalse(any("label overlaps" in warning for warning in geometry_warnings))

    def test_geometry_lint_detects_a_route_through_a_block(self):
        boxes = [
            renderer.Box("source", "Source", "module", 0, 0, x=0, y=0, w=60, h=40),
            renderer.Box("obstacle", "Obstacle", "module", 1, 0, x=100, y=0, w=60, h=40),
            renderer.Box("target", "Target", "module", 2, 0, x=200, y=0, w=60, h=40),
        ]
        edges = [renderer.Edge("source", "target")]
        routes = [[renderer.Point(60, 20), renderer.Point(200, 20)]]

        warnings = renderer.lint_geometry(boxes, edges, routes)

        self.assertIn("edge 0 crosses block obstacle", warnings)

    def test_forced_exterior_route_avoids_an_intervening_block(self):
        boxes = [
            renderer.Box("source", "Source", "module", 0, 1),
            renderer.Box("obstacle", "Obstacle", "module", 1, 1),
            renderer.Box("target", "Target", "module", 2, 1),
        ]
        width, height = renderer.layout_boxes(boxes)
        edge = renderer.Edge("source", "target", via="top")

        routes, route_warnings = renderer.route_edges([edge], boxes, width, height)
        warnings = renderer.lint_geometry(boxes, [edge], routes)

        self.assertEqual([], route_warnings)
        self.assertNotIn("edge 0 crosses block obstacle", warnings)

    def test_automatic_exterior_lanes_clear_title_and_blocks(self):
        boxes = [
            renderer.Box("target_a", "Target A", "module", 0, 1),
            renderer.Box("target_b", "Target B", "module", 1, 1),
            renderer.Box("control_a", "Control A", "fsm", 2, 0),
            renderer.Box("control_b", "Control B", "fsm", 3, 0),
        ]
        edges = [
            renderer.Edge("control_a", "target_a", kind="control"),
            renderer.Edge("control_b", "target_b", kind="clock"),
        ]
        warnings = []

        renderer.render("Exterior lane test", boxes, edges, [], warnings)

        self.assertFalse(any("crosses" in warning for warning in warnings), warnings)

    def test_port_names_stabilize_equivalent_connection_order(self):
        boxes = [
            renderer.Box("source", "Source", "module", 0, 0),
            renderer.Box("far", "Far", "module", 2, 0),
            renderer.Box("near", "Near", "module", 1, 0),
        ]
        renderer.layout_boxes(boxes)
        edges = [
            renderer.Edge("source", "far", source_port="z_port"),
            renderer.Edge("source", "near", source_port="a_port"),
        ]
        by_id = {box.id: box for box in boxes}
        sides = renderer.edge_sides(edges, by_id)
        ports = renderer.assign_ports(edges, sides, by_id)

        self.assertLess(ports[1][0].y, ports[0][0].y)

    def test_svg_text_is_escaped(self):
        boxes = [
            renderer.Box(
                id="unit",
                label='Decode <&> "Issue"',
                kind="logic",
                col=0,
                row=0,
            )
        ]

        svg = renderer.render("A < B & C", boxes, [], [])

        self.assertIn("A &lt; B &amp; C", svg)
        self.assertIn("Decode &lt;&amp;&gt; &quot;Issue&quot;", svg)
        self.assertNotIn("Decode <&>", svg)

    def test_invalid_ir_is_rejected(self):
        invalid = {
            "blocks": [
                {"id": "dup", "at": [0, 0]},
                {"id": "dup", "at": [1, 0]},
            ],
            "edges": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid.diagram.json"
            path.write_text(json.dumps(invalid), encoding="utf-8")

            with self.assertRaisesRegex(renderer.DiagramError, "duplicate block id"):
                renderer.load_diagram(path)

    def test_dotted_block_id_is_rejected(self):
        invalid = {"blocks": [{"id": "bad.id", "at": [0, 0]}], "edges": []}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid.diagram.json"
            path.write_text(json.dumps(invalid), encoding="utf-8")

            with self.assertRaisesRegex(renderer.DiagramError, "cannot contain"):
                renderer.load_diagram(path)

    def test_strict_mode_fails_when_warnings_remain(self):
        diagram = {"blocks": [{"id": "alone", "at": [0, 0]}], "edges": []}
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "isolated.diagram.json"
            output_path = Path(temp_dir) / "isolated.svg"
            input_path.write_text(json.dumps(diagram), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(RENDERER_PATH),
                    str(input_path),
                    "-o",
                    str(output_path),
                    "--strict",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("block alone is isolated", result.stderr)
            self.assertTrue(output_path.exists())


if __name__ == "__main__":
    unittest.main()
