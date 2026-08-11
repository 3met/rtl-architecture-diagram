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
  "at": [2, 1],
  "subtitle": "optional short detail",
  "group": "optional_group_id",
  "size": [160, 70]
}
```

- `at:[column,row]` is required. It is semantic placement, not pixels.
- `id` must be unique and cannot contain `.` because dots delimit endpoint port names.
- `kind`: `module|logic|memory|fifo|mux|reg|fsm|arbiter|io`.
- `subtitle` should normally be one short line.
- `size` is optional `[width,height]` in SVG units; omit unless auto-size is inadequate.

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

Only `from` and `to` are required. Endpoint suffixes such as `.out` are optional semantic port names; node IDs are the portion before the first dot. Port names are not drawn, but they provide a stable tie-break when otherwise-equivalent connections share a block side.

- `kind`: `data` (default), `control`, `response`, `clock`.
- `width`: positive integer. Renderer appends `Nb` to the label when useful.
- `from_side` / `to_side`: `n|s|e|w`; normally omit and let geometry infer them.
- `via`: `auto` (default), `top`, or `bottom`. Use only to correct a difficult route. Exterior routes remain obstacle-checked.

The renderer places edge labels in clear space and adds a short leader when needed. Shorten labels or change the semantic layout if strict lint cannot find a collision-free position.

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

- Main datapath: row 1 or 2, increasing columns.
- Control/FSM: rows above the datapath.
- Memories/queues/support: rows below when that makes flow clearer.
- Parallel lanes: separate rows.
- Feedback/return paths: mark `kind:"response"`; control arcs: `kind:"control"`.
- Do not use empty spacer blocks. Leave gaps by skipping row/column indices.
