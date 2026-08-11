import unittest
import xml.etree.ElementTree as ET

from support import renderer


class SvgTests(unittest.TestCase):
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
        self.assertIn("Decode &lt;&amp;&gt;</text>", svg)
        self.assertIn("&quot;Issue&quot;</text>", svg)
        self.assertNotIn("Decode <&>", svg)

    def test_common_hardware_symbols_and_bus_notation(self):
        symbol_kinds = {
            "mux", "demux", "reg", "counter", "alu", "adder", "subtractor",
            "addsub", "multiplier", "comparator", "and", "or", "xor", "not",
        }
        for kind in sorted(symbol_kinds):
            with self.subTest(kind=kind):
                block = renderer.Box(kind, kind.upper(), kind, 0, 0, x=20, y=20, w=140, h=60)
                rendered = renderer.svg_block(block)
                self.assertIn(f'class="block {kind}"', rendered)
                ET.fromstring(f"<svg>{rendered}</svg>")

        register_svg = renderer.svg_block(
            renderer.Box("r", "Register", "reg", 0, 0, x=20, y=20, w=140, h=60)
        )
        self.assertIn('class="symbol-line clock-glyph"', register_svg)
        self.assertIn("20,60 28,66 20,72", register_svg)
        self.assertNotIn('<line x1="29"', register_svg)
        register_width, register_height = renderer._auto_size(
            "Two-Row Running Sum", "bias seed + both MAC rows", "reg"
        )
        self.assertLessEqual(register_width, 160)
        self.assertGreaterEqual(register_height, 70)
        wrapped_register_svg = renderer.svg_block(
            renderer.Box(
                "sum", "Two-Row Running Sum", "reg", 0, 0,
                subtitle="bias seed + both MAC rows", x=20, y=20,
                w=register_width, h=register_height,
            )
        )
        self.assertIn(">Two-Row</text>", wrapped_register_svg)
        self.assertIn(">Running Sum</text>", wrapped_register_svg)

        result_width, result_height = renderer._auto_size(
            "Result Register + Score Clip", "saturate to +/-30999", "logic"
        )
        self.assertLess(result_width, 160)
        self.assertGreaterEqual(result_height, 80)
        wrapped_result_svg = renderer.svg_block(
            renderer.Box(
                "result", "Result Register + Score Clip", "logic", 0, 0,
                subtitle="saturate to +/-30999", x=20, y=20,
                w=result_width, h=result_height,
            )
        )
        self.assertIn(">Result Register</text>", wrapped_result_svg)
        self.assertIn(">+ Score Clip</text>", wrapped_result_svg)

        memory_svg = renderer.svg_block(
            renderer.Box("m", "Memory", "memory", 0, 0, x=20, y=20, w=140, h=60)
        )
        self.assertNotIn("<line", memory_svg)

        io_svg = renderer.svg_block(
            renderer.Box("io", "Terminal", "io", 0, 0, x=20, y=20, w=140, h=60)
        )
        self.assertIn('rx="30" class="block io"', io_svg)

        self.assertIn(">+</text>", renderer.svg_block(
            renderer.Box("add", "Add", "adder", 0, 0, x=20, y=20, w=140, h=60)
        ))
        self.assertIn(">−</text>", renderer.svg_block(
            renderer.Box("sub", "Subtract", "subtractor", 0, 0, x=20, y=20, w=140, h=60)
        ))
        self.assertIn(">±</text>", renderer.svg_block(
            renderer.Box("addsub", "Add/sub", "addsub", 0, 0, x=20, y=20, w=140, h=60)
        ))
        arithmetic_svg = renderer.svg_block(
            renderer.Box("mul", "Multiply", "multiplier", 0, 0, x=20, y=20, w=140, h=60)
        )
        self.assertIn('<circle cx="34" cy="34" r="8"', arithmetic_svg)

        edge = renderer.Edge("source", "target", width=64)
        rendered_edge = renderer.svg_edge_path(
            edge, [renderer.Point(0, 20), renderer.Point(100, 20)]
        )
        self.assertIn('class="edge data bus"', rendered_edge)
        self.assertIn('<polyline points="0,20 89,20" class="edge data bus"/>', rendered_edge)
        self.assertIn(
            '<polygon points="89,25.5 100,20 89,14.5" class="arrowhead data bus"/>',
            rendered_edge,
        )
        self.assertNotIn("marker-end", rendered_edge)
        self.assertEqual("64b", renderer.edge_label_text(edge))

        label = "signed products · 768b"
        self.assertLess(
            renderer.estimate_edge_label_width(label), len(label) * 6.2 + 16
        )

    def test_subtitles_wrap_to_two_lines_and_increase_height(self):
        subtitle = "one packed state snapshot accepted every cycle"
        width, height = renderer._auto_size("State Mirror", subtitle, "module")
        lines = renderer.block_subtitle_lines(subtitle, width - 26)

        self.assertEqual(2, len(lines))
        self.assertGreaterEqual(height, 82)
        rendered = renderer.svg_block(
            renderer.Box(
                "state", "State Mirror", "module", 0, 0,
                subtitle=subtitle, w=width, h=height, x=20, y=20,
            )
        )
        for line in lines:
            self.assertIn(f">{line}</text>", rendered)
        self.assertNotIn(f">{subtitle}</text>", rendered)

    def test_memory_shadow_stays_behind_routes_and_arrow_tips_stop_at_nodes(self):
        boxes = [
            renderer.Box("source", "Source", "module", 0, 0),
            renderer.Box("memory", "Memory", "memory", 1, 0),
        ]
        svg = renderer.render(
            "Layering test", boxes, [renderer.Edge("source", "memory")], []
        )

        shadow_index = svg.index('class="memory-shadow"')
        edge_index = svg.index('class="edge data"')
        memory_face_index = svg.index('class="block memory"')
        arrowhead_index = svg.index('class="arrowhead data"')
        self.assertLess(shadow_index, edge_index)
        self.assertLess(edge_index, memory_face_index)
        self.assertLess(memory_face_index, arrowhead_index)
        self.assertNotIn("<marker", svg)
        self.assertNotIn("marker-end", svg)
        self.assertIn('points="200,100 280,100" class="edge data"', svg)
        self.assertIn('points="280,105 290,100 280,95" class="arrowhead data"', svg)
        self.assertIn('.edge.data{stroke:#2563eb}', svg)
        self.assertIn('stroke-linecap:butt', svg)
        self.assertNotIn('class="title-rule"', svg)
        self.assertIn('.clock-glyph{stroke:#2563eb;stroke-width:1.7}', svg)
        self.assertIn('.group-label{font:650 13px Inter,"Segoe UI"', svg)
        self.assertIn('.title{font:650 20px Inter,"Segoe UI"', svg)

        root = ET.fromstring(svg)
        title_text = next(
            element
            for element in root.findall("{http://www.w3.org/2000/svg}text")
            if element.attrib.get("class") == "title"
        )
        self.assertAlmostEqual(
            float(title_text.attrib["x"]), float(root.attrib["width"]) / 2
        )
