import unittest

from support import NNUE_JSON, renderer


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

    def test_nnue_layout_and_routes_remain_compact_and_direct(self):
        title, boxes, edges, groups, warnings = renderer.load_diagram(NNUE_JSON)
        width, height = renderer.layout_boxes(boxes, edges)
        routes, route_warnings = renderer.route_edges(edges, boxes, width, height)
        by_id = {box.id: box for box in boxes}
        route_by_endpoints = {
            (edge.source, edge.target): route
            for edge, route in zip(edges, routes)
        }
        edge_index_by_endpoints = {
            (edge.source, edge.target): i for i, edge in enumerate(edges)
        }

        self.assertEqual([], warnings)
        self.assertEqual([], route_warnings)
        eval_gap = by_id["eval_memory"].left - by_id["eval_in"].right
        self.assertLessEqual(eval_gap, renderer.COL_GAP + 30)

        thread_read = route_by_endpoints[("eval_in", "eval_memory")]
        side_to_move = route_by_endpoints[("eval_in", "activation")]
        self.assertLess(thread_read[0].x, by_id["eval_in"].right)
        self.assertLess(side_to_move[0].x, by_id["eval_in"].right)
        self.assertTrue(renderer._point_on_boundary(thread_read[0], by_id["eval_in"]))
        self.assertTrue(renderer._point_on_boundary(side_to_move[0], by_id["eval_in"]))
        self.assertGreater(side_to_move[0].y, thread_read[0].y)
        self.assertEqual(2, len(thread_read), thread_read)
        self.assertEqual(thread_read[0].y, thread_read[1].y)

        def perpendicular_crossing(route_a, route_b):
            for a1, a2 in zip(route_a, route_a[1:]):
                for b1, b2 in zip(route_b, route_b[1:]):
                    if a1.y == a2.y and b1.x == b2.x:
                        if (
                            min(a1.x, a2.x) <= b1.x <= max(a1.x, a2.x)
                            and min(b1.y, b2.y) <= a1.y <= max(b1.y, b2.y)
                        ):
                            return True
                    if a1.x == a2.x and b1.y == b2.y:
                        if (
                            min(b1.x, b2.x) <= a1.x <= max(b1.x, b2.x)
                            and min(a1.y, a2.y) <= b1.y <= max(a1.y, a2.y)
                        ):
                            return True
            return False

        self.assertFalse(perpendicular_crossing(thread_read, side_to_move))

        mirrored_write = route_by_endpoints[("lane_update", "eval_memory")]
        self.assertLessEqual(len(mirrored_write), 5, mirrored_write)
        self.assertFalse(
            perpendicular_crossing(
                mirrored_write,
                route_by_endpoints[("eval_control", "activation")],
            )
        )
        self.assertFalse(
            perpendicular_crossing(
                mirrored_write,
                route_by_endpoints[("eval_control", "weight_rows")],
            )
        )

        partial_to_tree = route_by_endpoints[("partial_regs", "upper_tree")]
        self.assertEqual(2, len(partial_to_tree), partial_to_tree)
        self.assertTrue(
            partial_to_tree[0].x == partial_to_tree[1].x
            or partial_to_tree[0].y == partial_to_tree[1].y
        )

        bias_to_update = route_by_endpoints[("accumulator_bias", "lane_update")]
        self.assertEqual(2, len(bias_to_update), bias_to_update)
        self.assertEqual(bias_to_update[0].x, bias_to_update[1].x)

        update_to_mirror = route_by_endpoints[("lane_update", "update_memory")]
        mirror_to_update = route_by_endpoints[("update_memory", "lane_update")]
        self.assertEqual(2, len(update_to_mirror), update_to_mirror)
        self.assertEqual(2, len(mirror_to_update), mirror_to_update)
        self.assertNotEqual(update_to_mirror[0].y, mirror_to_update[0].y)
        self.assertFalse(perpendicular_crossing(update_to_mirror, mirror_to_update))

        update_done = route_by_endpoints[("lane_update", "update_done")]
        self.assertEqual(2, len(update_done), update_done)
        self.assertEqual(update_done[0].x, update_done[1].x)
        update_done_length = sum(
            abs(start.x - end.x) + abs(start.y - end.y)
            for start, end in zip(update_done, update_done[1:])
        )
        self.assertLessEqual(
            update_done_length, renderer.ROW_GAP + renderer.ROUTE_STEP
        )
        self.assertLessEqual(
            abs(by_id["lane_update"].cx - by_id["update_done"].cx),
            renderer.ROUTE_STEP,
        )

        sequencer_to_state = route_by_endpoints[("eval_control", "eval_memory")]
        local_top = min(by_id["eval_control"].top, by_id["eval_memory"].top)
        local_bottom = max(by_id["eval_control"].bottom, by_id["eval_memory"].bottom)
        self.assertGreaterEqual(min(point.y for point in sequencer_to_state), local_top)
        self.assertLessEqual(max(point.y for point in sequencer_to_state), local_bottom)
        self.assertLessEqual(len(sequencer_to_state), 4, sequencer_to_state)
        self.assertEqual(2, len(sequencer_to_state), sequencer_to_state)
        self.assertEqual(sequencer_to_state[0].x, sequencer_to_state[1].x)

        sequencer_to_activation = route_by_endpoints[("eval_control", "activation")]
        self.assertLessEqual(len(sequencer_to_activation), 4, sequencer_to_activation)
        self.assertEqual(2, len(sequencer_to_activation), sequencer_to_activation)
        self.assertFalse(perpendicular_crossing(side_to_move, sequencer_to_activation))

        sequencer_to_weights = route_by_endpoints[("eval_control", "weight_rows")]
        weights_to_products = route_by_endpoints[("weight_rows", "products")]
        self.assertEqual(2, len(sequencer_to_weights), sequencer_to_weights)
        self.assertEqual(sequencer_to_weights[0].y, sequencer_to_weights[1].y)
        self.assertEqual(2, len(weights_to_products), weights_to_products)
        self.assertEqual(weights_to_products[0].x, weights_to_products[1].x)

        self.assertLessEqual(width, 1700)
        self.assertGreater(height, 950)
        self.assertLessEqual(height, 1150)
        self.assertGreater(by_id["upper_tree"].y, by_id["partial_regs"].y)
        self.assertLess(by_id["running_sum"].x, by_id["upper_tree"].x)
        self.assertLess(by_id["score_clip"].x, by_id["running_sum"].x)
        self.assertLess(by_id["weight_rows"].bottom, by_id["products"].top)
        self.assertLessEqual(
            abs(by_id["weight_rows"].cx - by_id["products"].cx),
            renderer.ROUTE_STEP,
        )
        self.assertLess(
            by_id["output_bias"].top - by_id["running_sum"].bottom,
            renderer.ROW_GAP + 20,
        )
        group_lefts = {
            group_id: left
            for group_id, _, left, _, _, _ in renderer.group_rects(boxes, groups)
        }
        self.assertEqual(group_lefts["update"], group_lefts["evaluation"])

        placements, label_warnings = renderer.place_edge_labels(
            title,
            boxes,
            edges,
            routes,
            renderer.group_rects(boxes, groups),
            width,
            height,
        )
        self.assertEqual([], label_warnings)
        group_bounds = {
            group_id: (left, top, left + group_width, top + group_height)
            for group_id, _, left, top, group_width, group_height
            in renderer.group_rects(boxes, groups)
        }
        for edge, placement in zip(edges, placements):
            if placement is None:
                continue
            source_group = by_id[edge.source].group
            if source_group and source_group == by_id[edge.target].group:
                self.assertTrue(
                    renderer._rect_contains(
                        group_bounds[source_group],
                        placement.rect,
                        renderer.LABEL_GROUP_INSET,
                    ),
                    (edge.source, edge.target, placement.rect),
                )

        shifted_leaders = [
            placement
            for placement in placements
            if placement is not None
            and placement.leader_start is not None
            and abs(placement.leader_start.x - placement.x) >= renderer.ROUTE_STEP
        ]
        self.assertTrue(shifted_leaders)
        for endpoints in (("products", "partial_regs"), ("partial_regs", "upper_tree")):
            placement = placements[edge_index_by_endpoints[endpoints]]
            self.assertIsNotNone(placement)
            self.assertFalse(
                renderer._rects_overlap(
                    placement.rect, renderer._box_rect(by_id["partial_regs"], 6)
                )
            )

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
