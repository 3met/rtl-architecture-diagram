import unittest

from support import renderer


class RoutingTests(unittest.TestCase):
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

    def test_edge_label_prefers_the_quieter_half_of_a_wire(self):
        edges = [
            renderer.Edge("a", "b", label="signal"),
            renderer.Edge("c", "d"),
            renderer.Edge("e", "f"),
        ]
        routes = [
            [renderer.Point(20, 100), renderer.Point(300, 100)],
            [renderer.Point(190, 72), renderer.Point(300, 72)],
            [renderer.Point(190, 128), renderer.Point(300, 128)],
        ]
        placements, warnings = renderer.place_edge_labels(
            "", [], edges, routes, [], 340, 180
        )

        self.assertEqual([], warnings)
        self.assertIsNotNone(placements[0])
        self.assertLess(placements[0].x, 160)

    def test_indexed_label_congestion_matches_generic_geometry(self):
        routes = [
            [renderer.Point(20, 40), renderer.Point(180, 40)],
            [renderer.Point(80, 10), renderer.Point(80, 150)],
            [
                renderer.Point(30, 120),
                renderer.Point(140, 120),
                renderer.Point(140, 160),
            ],
        ]
        compiled = renderer._compile_foreign_route_segments(routes, 0)
        placements = [
            renderer.LabelPlacement(x, y, width)
            for x in range(20, 181, 20)
            for y in range(20, 161, 20)
            for width in (28, 64, 110)
        ]

        self.assertTrue(all(
            renderer._label_route_congestion(placement, routes, 0)
            == renderer._compiled_label_route_congestion(placement, compiled)
            for placement in placements
        ))

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

    def test_perpendicular_wire_crossing_detection(self):
        routes = [
            [renderer.Point(40, 20), renderer.Point(160, 20)],
            [renderer.Point(100, -40), renderer.Point(100, 80)],
        ]

        crossings = renderer.perpendicular_route_crossings(*routes)

        self.assertEqual([renderer.Point(100, 20)], crossings)

    def test_route_quality_prefers_a_detour_over_a_wire_crossover(self):
        crossed = [
            [renderer.Point(80, 80), renderer.Point(120, 80)],
            [renderer.Point(100, 20), renderer.Point(100, 140)],
        ]
        detoured = [
            [renderer.Point(80, 80), renderer.Point(120, 80)],
            [
                renderer.Point(100, 20),
                renderer.Point(130, 20),
                renderer.Point(130, 140),
                renderer.Point(100, 140),
            ],
        ]

        self.assertLess(
            renderer.route_quality_score(detoured),
            renderer.route_quality_score(crossed),
        )

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

    def test_far_same_row_output_uses_the_outer_port(self):
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

        self.assertLess(ports[0][0].y, ports[1][0].y)

    def test_label_leader_cannot_cross_a_foreign_arrow(self):
        placement = renderer.LabelPlacement(
            100, 64, 48,
            leader_start=renderer.Point(100, 100),
            leader_end=renderer.Point(100, 76),
        )
        routes = [
            [renderer.Point(60, 100), renderer.Point(140, 100)],
            [renderer.Point(80, 88), renderer.Point(120, 88)],
        ]
        self.assertFalse(
            renderer._placement_is_clear(
                placement, [], routes, 0, [], 240, 180
            )
        )

    def test_astar_prefers_a_detour_to_crossing_an_existing_route(self):
        used = {(x, 100): 1 for x in range(50, 151, renderer.ROUTE_STEP)}
        used_axes = {
            (x, 100): {"h"} for x in range(50, 151, renderer.ROUTE_STEP)
        }
        route, used_fallback = renderer.astar_route(
            renderer.Point(100, 40),
            renderer.Point(100, 160),
            [],
            220,
            220,
            set(),
            used,
            None,
            used_axes,
        )

        self.assertFalse(used_fallback)
        for a, b in zip(route, route[1:]):
            crosses_used_horizontal = (
                a.x == b.x
                and 50 <= a.x <= 150
                and min(a.y, b.y) <= 100 <= max(a.y, b.y)
            )
            self.assertFalse(crosses_used_horizontal, route)

    def test_direct_router_prefers_a_simple_elbow_before_astar(self):
        route = renderer.direct_orthogonal_route(
            renderer.Point(40, 40),
            renderer.Point(180, 140),
            [],
            set(),
            240,
            200,
            {},
            {},
        )

        self.assertIsNotNone(route)
        self.assertLessEqual(len(route), 3)
        self.assertEqual(renderer.Point(40, 40), route[0])
        self.assertEqual(renderer.Point(180, 140), route[-1])

    def test_nearby_exterior_hint_uses_a_local_lane(self):
        boxes = [
            renderer.Box("left", "Left", "module", 0, 1),
            renderer.Box("right", "Right", "memory", 1, 1),
        ]
        width, height = renderer.layout_boxes(boxes)
        edge = renderer.Edge("right", "left", kind="response", via="bottom")
        routes, warnings = renderer.route_edges([edge], boxes, width, height)

        self.assertEqual([], warnings)
        self.assertLessEqual(
            max(point.y for point in routes[0]),
            max(box.bottom for box in boxes) + 2 * renderer.ROUTE_CLEAR + renderer.ROUTE_STEP,
        )
        self.assertEqual([], renderer.lint_geometry(boxes, [edge], routes))

    def test_label_leader_uses_nearest_label_boundary(self):
        placement = renderer.LabelPlacement(
            100,
            100,
            80,
            leader_start=renderer.Point(200, 97),
            leader_end=renderer.Point(100, 88),
        )

        attached = renderer._attach_leader_to_label(placement)

        self.assertEqual(renderer.Point(140, 97), attached.leader_end)
        self.assertIsNone(attached.leader_bend)

    def test_label_leader_uses_nearest_point_anywhere_on_owning_route(self):
        placement = renderer.LabelPlacement(
            80,
            60,
            80,
            leader_start=renderer.Point(0, 0),
            leader_end=renderer.Point(40, 60),
        )
        route = [
            renderer.Point(0, 0),
            renderer.Point(0, 100),
            renderer.Point(100, 100),
        ]

        attached = renderer._attach_leader_to_route(placement, route)

        self.assertEqual(renderer.Point(80, 100), attached.leader_start)
        self.assertEqual(renderer.Point(80, 66), attached.leader_end)
        self.assertIsNone(attached.leader_bend)

    def test_route_cleanup_removes_collinear_wire_sharing(self):
        previous = [renderer.Point(100, 40), renderer.Point(100, 160)]
        route = [
            renderer.Point(20, 40),
            renderer.Point(100, 40),
            renderer.Point(100, 160),
            renderer.Point(180, 160),
        ]

        cleaned = renderer._deoverlap_route(route, [previous], [], set())

        self.assertEqual(
            0,
            renderer.collinear_route_overlap_length(cleaned, previous),
        )
