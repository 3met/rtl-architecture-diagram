import unittest

from support import NNUE_JSON, renderer


def route_length(route):
    return sum(
        abs(start.x - end.x) + abs(start.y - end.y)
        for start, end in zip(route, route[1:])
    )


class LayoutTests(unittest.TestCase):
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

    def test_multiple_io_ports_touch_the_visible_rounded_boundary(self):
        terminal = renderer.Box(
            "terminal", "Terminal", "io", 0, 0,
            x=20, y=20, w=140, h=60,
        )
        upper = renderer.port_point(terminal, "e", 0, 2)
        lower = renderer.port_point(terminal, "e", 1, 2)

        self.assertLess(upper.x, terminal.right)
        self.assertLess(lower.x, terminal.right)
        self.assertTrue(renderer._point_on_boundary(upper, terminal))
        self.assertTrue(renderer._point_on_boundary(lower, terminal))

        target = renderer.Box(
            "target", "Target", "module", 1, 0,
            x=240, y=20, w=100, h=60,
        )
        edge = renderer.Edge("terminal", "target")
        route = [upper, renderer.Point(target.left, upper.y)]
        warnings = renderer.lint_geometry([terminal, target], [edge], [route])
        self.assertFalse(any("crosses block terminal" in warning for warning in warnings))

    def test_nearly_aligned_group_starts_share_an_edge(self):
        boxes = [
            renderer.Box("a", "A", "module", 0, 0, group="a", x=60, y=80, w=80, h=60),
            renderer.Box("b", "B", "module", 0, 1, group="b", x=72, y=180, w=80, h=60),
            renderer.Box("c", "C", "module", 0, 2, group="c", x=120, y=280, w=80, h=60),
        ]
        rects = renderer.group_rects(
            boxes,
            [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        )
        lefts = {group_id: left for group_id, _, left, _, _, _ in rects}

        self.assertEqual(lefts["a"], lefts["b"])
        self.assertNotEqual(lefts["a"], lefts["c"])

    def test_dense_rows_use_balanced_compact_spacing(self):
        boxes = [
            renderer.Box(f"block_{i}", f"Block {i}", "module", i, 0)
            for i in range(renderer.DENSE_ROW_THRESHOLD)
        ]
        renderer.layout_boxes(boxes)
        gaps = [right.left - left.right for left, right in zip(boxes, boxes[1:])]

        self.assertTrue(
            all(abs(gap - renderer.DENSE_COL_GAP) <= renderer.ROUTE_STEP / 2 for gap in gaps),
            gaps,
        )
        self.assertLess(renderer.DENSE_COL_GAP, renderer.COL_GAP)

    def test_very_long_rows_fold_into_a_serpentine(self):
        boxes = [
            renderer.Box(f"stage_{i}", f"Stage {i}", "module", i, 0)
            for i in range(renderer.FOLD_ROW_THRESHOLD)
        ]
        width, height = renderer.layout_boxes(boxes)
        split = (len(boxes) + 1) // 2
        head = boxes[:split]
        tail = boxes[split:]

        self.assertTrue(all(box.y == head[0].y for box in head))
        self.assertTrue(all(box.y > head[0].y for box in tail))
        self.assertTrue(all(right.x < left.x for left, right in zip(tail, tail[1:])))
        self.assertLess(width, 1800)
        self.assertGreater(height, 250)

    def test_dense_group_reflow_aligns_shared_support_with_consumers(self):
        boxes = [
            renderer.Box(f"stage_{i}", f"Stage {i}", "module", i, 1, group="g")
            for i in range(renderer.FOLD_ROW_THRESHOLD)
        ]
        support = renderer.Box("control", "Control", "fsm", 5, 0, group="g")
        boxes.append(support)
        edges = [
            renderer.Edge(f"stage_{i}", f"stage_{i + 1}")
            for i in range(renderer.FOLD_ROW_THRESHOLD - 1)
        ]
        edges.extend(
            [
                renderer.Edge("control", "stage_4", kind="control"),
                renderer.Edge("control", "stage_5", kind="control"),
            ]
        )

        renderer.layout_boxes(boxes, edges)

        self.assertEqual(boxes[4].cx, boxes[5].cx)
        self.assertLessEqual(abs(support.cx - boxes[4].cx), renderer.ROUTE_STEP)
        self.assertLess(support.y, boxes[4].y)

    def test_nnue_bridge_placement_and_routes_are_compact(self):
        title, boxes, edges, groups, warnings = renderer.load_diagram(NNUE_JSON)
        width, height = renderer.layout_boxes(boxes, edges)
        routes, route_warnings = renderer.route_edges(edges, boxes, width, height)
        by_id = {box.id: box for box in boxes}
        route_by_endpoints = {
            (edge.source, edge.target): route
            for edge, route in zip(edges, routes)
        }

        self.assertEqual([], warnings)
        self.assertEqual([], route_warnings)
        self.assertLessEqual(width, 1650)
        self.assertGreaterEqual(height, 900)
        self.assertLessEqual(height, 1050)

        update_bottom = max(
            box.bottom for box in boxes if box.group == "update"
        )
        evaluation_top = min(
            box.top for box in boxes if box.group == "eval"
        )
        state_boxes = [box for box in boxes if box.group == "state"]
        self.assertTrue(all(box.top > update_bottom for box in state_boxes))
        self.assertTrue(all(box.bottom < evaluation_top for box in state_boxes))
        self.assertLessEqual(
            max(box.cy for box in state_boxes) - min(box.cy for box in state_boxes),
            renderer.ROUTE_STEP,
        )

        lengths = [route_length(route) for route in routes]
        crossing_count = sum(
            len(renderer.perpendicular_route_crossings(left, right))
            for left_index, left in enumerate(routes)
            for right in routes[left_index + 1:]
        )
        bend_count = sum(max(0, len(route) - 2) for route in routes)
        self.assertLessEqual(sum(lengths), 7450)
        self.assertLessEqual(max(lengths), 1400)
        self.assertLessEqual(sum(length > 600 for length in lengths), 4)
        self.assertLessEqual(crossing_count, 8)
        self.assertLessEqual(bend_count, 33)
        self.assertLessEqual(
            abs(by_id["snapshot"].cx - by_id["lane_update"].cx),
            250,
        )
        self.assertLess(by_id["acc_bias"].bottom, by_id["lane_update"].top)
        self.assertLessEqual(
            abs(by_id["acc_bias"].cx - by_id["lane_update"].cx),
            renderer.ROUTE_STEP,
        )
        self.assertLess(
            route_by_endpoints[("bucket", "out_bias")][0].y,
            route_by_endpoints[("bucket", "weight_rows")][0].y,
        )
        for left_index, left_route in enumerate(routes):
            for right_route in routes[left_index + 1:]:
                self.assertEqual(
                    0,
                    renderer.collinear_route_overlap_length(
                        left_route, right_route
                    ),
                )

        for endpoints in (
            ("update_state", "lane_update"),
            ("lane_update", "update_state"),
            ("snapshot", "reorder_clip"),
            ("weight_rows", "mac"),
            ("mac", "partials"),
            ("out_bias", "sum_tree"),
            ("sum_tree", "result_reg"),
        ):
            self.assertEqual(2, len(route_by_endpoints[endpoints]), endpoints)

        self.assertLess(by_id["sum_tree"].y, by_id["result_reg"].y)
        self.assertGreater(by_id["result_reg"].x, by_id["score_clip"].x)
        self.assertGreater(by_id["score_clip"].x, by_id["result"].x)

        grects = renderer.group_rects(boxes, groups)
        group_bounds = {
            group_id: (left, top, left + group_width, top + group_height)
            for group_id, _, left, top, group_width, group_height in grects
        }
        self.assertFalse(
            renderer._rects_overlap(group_bounds["update"], group_bounds["state"])
        )
        self.assertFalse(
            renderer._rects_overlap(group_bounds["state"], group_bounds["eval"])
        )

        placements, label_warnings = renderer.place_edge_labels(
            title, boxes, edges, routes, grects, width, height
        )
        self.assertEqual([], label_warnings)
        geometry_warnings = renderer.lint_geometry(
            boxes, edges, routes, placements, title, grects, width
        )
        self.assertEqual([], geometry_warnings)

    def test_render_leaves_whitespace_below_title(self):
        title, boxes, edges, groups, warnings = renderer.load_diagram(NNUE_JSON)

        renderer.render(title, boxes, edges, groups, warnings)

        first_group_top = min(
            top for _, _, _, top, _, _ in renderer.group_rects(boxes, groups)
        )
        self.assertGreaterEqual(
            first_group_top - (renderer.TITLE_Y + 5),
            renderer.TITLE_CONTENT_GAP,
        )

    def test_near_adjacent_auto_sizes_round_up_without_transitive_creep(self):
        boxes = [
            renderer.Box("a", "A", "logic", 0, 1, w=130, h=62),
            renderer.Box("b", "B", "logic", 1, 1, w=137, h=66),
            renderer.Box("c", "C", "logic", 2, 1, w=144, h=74),
            renderer.Box(
                "fixed", "Fixed", "logic", 3, 1,
                w=140, h=70, size_explicit=True,
            ),
        ]
        edges = [
            renderer.Edge("a", "b"),
            renderer.Edge("b", "c"),
            renderer.Edge("c", "fixed"),
        ]
        renderer._harmonize_near_adjacent_sizes(boxes, edges)

        self.assertEqual((137, 66), (boxes[0].w, boxes[0].h))
        self.assertEqual((137, 66), (boxes[1].w, boxes[1].h))
        self.assertEqual((144, 74), (boxes[2].w, boxes[2].h))
        self.assertEqual((140, 70), (boxes[3].w, boxes[3].h))
