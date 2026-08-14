# Diagram IR reference

The renderer accepts a compact JSON object. Prefer defaults and add optional fields only when they improve the architectural meaning or fix an unclear render.

## Top level

```json
{
  "title": "optional title",
  "blocks": [],
  "edges": [],
  "groups": []
}
```

- `blocks` is required and must be non-empty.
- `edges` and `groups` are optional arrays.
- `title` defaults to `Architecture`.

## Block

```json
{
  "id": "unique_id",
  "label": "Visible label",
  "kind": "module",
  "subtitle": "optional short detail",
  "bigger": true,
  "group": "optional_group_id",
  "at": [1, 0],
  "size": [160, 70]
}
```

- `id` is required and unique. Do not use `.` because dots delimit endpoint port names.
- `label` defaults to `id`.
- `kind` defaults to `module`. Choose from `module`, `logic`, `memory`, `fifo`, `mux`, `demux`, `reg`, `counter`, `fsm`, `arbiter`, `io`, `alu`, `adder`, `subtractor`, `addsub`, `multiplier`, `comparator`, `and`, `or`, `xor`, and `not`.
- `subtitle` adds a short second-level description.
- `group` places the block in a visual enclosure declared under `groups`.
- `at:[column,row]` anchors semantic order and lane, not pixels. Omit it for automatic placement. Anchored and automatic blocks may be mixed, but two anchors cannot share a position.
- `bigger` and `smaller` are optional booleans for architectural prominence. Never set both.
- `size:[width,height]` fixes the SVG-unit dimensions. Omit it unless automatic sizing is inadequate; both values must exceed 20.

Use `logic` for generic combinational behavior. Use a specific arithmetic or gate kind only when its symbol helps explain the architecture.

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

- `from` and `to` are required and must name existing block IDs.
- Optional endpoint suffixes such as `.out` are semantic port names. They are not displayed and do not control port order.
- `label` defaults to empty.
- `width` is an optional positive integer. Values greater than one render as a bus and add an `Nb` width annotation when useful.
- `kind` is `data` (default), `control`, `response`, or `clock`.
- `from_side` and `to_side` may be `n`, `s`, `e`, or `w`. Normally omit them; use them only when the attachment side matters.
- `via` is `auto` (default), `top`, or `bottom`. Use an exterior route only after automatic routing produces an unclear result.

Keep endpoint port names consistent when several edges share a logical interface. Let the renderer choose their visual ordering.

## Validation

Run `render.py ... --lint --strict` before delivery. Strict mode writes the SVG but returns exit status 1 when any warning remains. Revise the JSON and render again until it passes.

## Groups

A block can carry `"group":"frontend"`. Optionally define its visible label:

```json
"groups": [
  {"id":"frontend","label":"TT Frontend"},
  {"id":"memory","label":"Memory path"}
]
```

Group IDs must be unique. Groups are visual enclosures and do not change connectivity. Declare every used group to avoid lint warnings.

## Layout conventions

- Prefer automatic placement.
- Use columns for semantic progression and rows for parallel lanes when anchors are necessary.
- Place controllers above and support memories below the datapath when that makes the flow clearer.
- Mark feedback and return paths as `response`; mark control arcs as `control`.
- Do not create spacer blocks or use explicit sizes and positions for cosmetic alignment.
