# Diagram IR reference

The renderer accepts JSON. Minimal fields are intentionally small.

## Top level

```json
{
  "title": "optional title",
  "blocks": [],
  "edges": [],
  "groups": []
}
```

## Block

```json
{
  "id": "unique_id",
  "label": "Visible label",
  "kind": "module",
  "subtitle": "optional short detail",
  "bigger": true,
  "group": "optional_group_id",
  "size": [160, 70]
}
```

- `at:[column,row]` is optional. Omit it for automatic placement. When present, it is a semantic anchor rather than a pixel position.
- Anchored and unanchored blocks may be mixed. Explicit anchors are preserved; missing columns and rows are inferred deterministically from dataflow, block kind, group order, and nearby anchors.
- Automatic columns follow forward `data` edges. FSMs occupy an upper control lane; one-sided or response-returning memories occupy a lower support lane, while FIFOs remain in the datapath. Parallel or cyclic nodes receive stable collision-free lanes.
- `id` must be unique and cannot contain `.` because dots delimit endpoint port names.
- `kind`: `module|logic|memory|fifo|mux|demux|reg|counter|fsm|arbiter|io|alu|adder|subtractor|addsub|multiplier|comparator|and|or|xor|not`.
- `logic` is the general-purpose combinational block. Use the more specific arithmetic or gate kinds when the symbol materially improves architectural readability; do not model every RTL operator as a separate gate.
- `adder`, `subtractor`, and `addsub` render explicit `+`, `−`, and `±` operation badges. Use `addsub` when one datapath performs a selected add/subtract operation.
- `reg` and `counter` use a clock-input notch on the block boundary; no separate clock port hint is needed for the glyph.
- `subtitle` may contain an explicit newline and otherwise wraps automatically into at most two balanced lines when needed. Keep it concise.
- `bigger` / `smaller`: optional booleans that increase or decrease visual prominence through automatic size, type, and outline weight. They are mutually exclusive. Omit both for normal importance.
- `size` is optional `[width,height]` in SVG units; omit unless auto-size is inadequate. An explicit size fixes geometry, while `bigger` or `smaller` can still affect type and outline emphasis.

## Edge

```json
{
  "from": "producer.out",
  "to": "consumer.in",
  "label": "request",
  "width": 64,
  "kind": "data",
  "from_side": "e",
  "to_side": "w",
  "via": "auto"
}
```

Only `from` and `to` are required. Endpoint suffixes such as `.out` are optional semantic port names; node IDs are the portion before the first dot. Port names are not drawn and do not prescribe visual order. The renderer orders shared-side ports from destination geometry, using port names only as a deterministic final tie-break.

- `kind`: `data` (default), `control`, `response`, `clock`.
- Wire styling follows `kind`: blue data, dashed amber control, teal response/return, and dotted purple clock. Bus width changes stroke weight, not color meaning.
- `width`: positive integer. Widths greater than one use thicker bus notation, and the renderer appends `Nb` to the label when useful.
- `from_side` / `to_side`: `n|s|e|w`; normally omit and let geometry infer them.
- `via`: `auto` (default), `top`, or `bottom`. Use only to correct a difficult route. Exterior routes remain obstacle-checked.

The renderer places edge labels in clear space, favors the quieter portion of a route, and adds a short orthogonal leader when needed. Leader attachment prefers the label center but may shift or use a compact elbow when that avoids wasted space or congestion. Arrows and other label leaders are obstacles, so they cannot cross a leader. A label whose endpoints share a group remains inset within that group enclosure. Shorten labels or change the semantic layout if strict lint cannot find a collision-free position.

Automatic routing is orthogonal and obstacle-aware. It routes long datapaths first, penalizes wire crossings, aligns singleton ports when that preserves a straight connection, gives bent arrows a clear final approach, and keeps short backward control/response links local. A state handoff from an upper group to a lower state memory uses the open inter-group corridor and enters the memory from the north, avoiding lower-group controller fanout. Port endpoints lie on the visible node geometry rather than only its rectangular bounds, including curved I/O terminals. Arrow shafts end at the base of separately drawn broad arrowheads, preventing the shaft from showing through the point. Use `via` only when the semantic layout still needs an explicit exterior lane.

## Validation

Run `render.py ... --lint --strict` before delivery. Strict mode writes the SVG but returns exit status 1 when any architectural, routing, endpoint, or label warning remains; revise the JSON IR and render again.

## Groups

A block can carry `"group":"frontend"`. Optionally define its visible label:

```json
"groups": [
  {"id":"frontend","label":"TT Frontend"},
  {"id":"memory","label":"Memory path"}
]
```

Groups are visual enclosures only; they do not change connectivity.

## Layout conventions

- Prefer automatic placement. Add `at` only to express an intentional order, parallel lane, or component relationship that connectivity and block kinds cannot establish.
- Main datapath: row 1 or 2, increasing columns.
- Columns express semantic order, not a fixed global x-coordinate. Each group is compacted around its busiest row; rows with eight blocks use a tighter balanced gap, and rows with nine or more blocks fold into a two-line serpentine while preserving adjacent pipeline stages. A memory with an upper controller and a main-row consumer may move directly above that consumer when the position is clear, making both links short. A lower support block that would collide with the return row can move to a side shelf beside the fold, while return-row support uses the nearest clear lower lane. Connected controllers, memories, and support blocks align to that local datapath, so unrelated blocks in other rows do not create large horizontal gaps. Longer titles and subtitles may wrap automatically into two balanced lines, and automatic dimensions are calculated from those visible lines. Automatically sized adjacent nodes whose width or height differs only slightly are rounded up to the same dimension; the tolerance is deliberately small and never overrides explicit `size` or a different prominence level.
- Group enclosures whose left edges are already within one small routing interval align to the same start line by extending the inset enclosure. Larger offsets remain untouched because they may communicate intentional hierarchy.
- Place completion/status leaf blocks near the stage that produces them. Strict lint warns when a same-group leaf connection takes a large avoidable detour around intervening hardware.
- Control/FSM: rows above the datapath.
- Memories/queues/support: rows below when that makes flow clearer.
- Parallel lanes: separate rows.
- Feedback/return paths: mark `kind:"response"`; control arcs: `kind:"control"`.
- Do not use empty spacer blocks. Leave gaps by skipping row/column indices.
