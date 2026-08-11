import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from support import EXAMPLE_JSON, EXAMPLE_SVG, RENDERER_PATH, render_example, renderer


class CliAndIrTests(unittest.TestCase):
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

    def test_prominence_flags_scale_auto_sized_nodes_and_validate(self):
        diagram = {
            "blocks": [
                {"id": "small", "label": "Execute", "smaller": True},
                {"id": "normal", "label": "Execute"},
                {"id": "big", "label": "Execute", "bigger": True},
            ],
            "edges": [
                {"from": "small", "to": "normal"},
                {"from": "normal", "to": "big"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "prominence.json"
            path.write_text(json.dumps(diagram), encoding="utf-8")
            _, boxes, _, _, warnings = renderer.load_diagram(path)

        self.assertEqual([], warnings)
        by_id = {box.id: box for box in boxes}
        self.assertLess(by_id["small"].w, by_id["normal"].w)
        self.assertLess(by_id["normal"].w, by_id["big"].w)
        self.assertEqual("smaller", by_id["small"].prominence)
        self.assertEqual("bigger", by_id["big"].prominence)

        diagram["blocks"][0]["bigger"] = True
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid-prominence.json"
            path.write_text(json.dumps(diagram), encoding="utf-8")
            with self.assertRaisesRegex(renderer.DiagramError, "cannot both be true"):
                renderer.load_diagram(path)

    def test_at_is_optional_and_inferred_from_graph_semantics(self):
        diagram = {
            "title": "Automatic placement",
            "blocks": [
                {"id": "input", "label": "Input", "kind": "io"},
                {"id": "execute", "label": "Execute", "kind": "logic"},
                {"id": "output", "label": "Output", "kind": "io"},
                {"id": "control", "label": "Control", "kind": "fsm"},
                {"id": "bias", "label": "Bias ROM", "kind": "memory"},
            ],
            "edges": [
                {"from": "input", "to": "execute"},
                {"from": "execute", "to": "output"},
                {"from": "control", "to": "execute", "kind": "control"},
                {"from": "bias", "to": "execute"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "automatic.diagram.json"
            path.write_text(json.dumps(diagram), encoding="utf-8")
            title, boxes, edges, groups, warnings = renderer.load_diagram(path)
            reversed_path = Path(temp_dir) / "automatic-reversed.diagram.json"
            reversed_diagram = dict(diagram)
            reversed_diagram["blocks"] = list(reversed(diagram["blocks"]))
            reversed_path.write_text(json.dumps(reversed_diagram), encoding="utf-8")
            _, reversed_boxes, _, _, reversed_warnings = renderer.load_diagram(
                reversed_path
            )

        by_id = {box.id: box for box in boxes}
        self.assertEqual([], warnings)
        self.assertEqual([], reversed_warnings)
        self.assertLess(by_id["input"].col, by_id["execute"].col)
        self.assertLess(by_id["execute"].col, by_id["output"].col)
        self.assertLess(by_id["control"].row, by_id["execute"].row)
        self.assertGreater(by_id["bias"].row, by_id["execute"].row)
        self.assertEqual(
            len(boxes), len({(box.col, box.row) for box in boxes})
        )
        self.assertEqual(
            {box.id: (box.col, box.row) for box in boxes},
            {box.id: (box.col, box.row) for box in reversed_boxes},
        )
        diagnostics = []
        renderer.render(title, boxes, edges, groups, diagnostics)
        self.assertEqual([], diagnostics)

    def test_partial_at_values_are_preserved_as_semantic_anchors(self):
        diagram = {
            "blocks": [
                {"id": "a", "kind": "logic"},
                {"id": "b", "kind": "logic", "at": [5, 4]},
                {"id": "c", "kind": "logic"},
            ],
            "edges": [
                {"from": "a", "to": "b"},
                {"from": "b", "to": "c"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "anchored.diagram.json"
            path.write_text(json.dumps(diagram), encoding="utf-8")
            _, boxes, _, _, warnings = renderer.load_diagram(path)

        by_id = {box.id: box for box in boxes}
        self.assertEqual([], warnings)
        self.assertEqual((5, 4), (by_id["b"].col, by_id["b"].row))
        self.assertEqual((4, 4), (by_id["a"].col, by_id["a"].row))
        self.assertEqual((6, 4), (by_id["c"].col, by_id["c"].row))

    def test_malformed_optional_at_is_rejected(self):
        invalid = {"blocks": [{"id": "bad", "at": ["left", 1]}], "edges": []}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid-at.diagram.json"
            path.write_text(json.dumps(invalid), encoding="utf-8")

            with self.assertRaisesRegex(renderer.DiagramError, "when provided"):
                renderer.load_diagram(path)

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
