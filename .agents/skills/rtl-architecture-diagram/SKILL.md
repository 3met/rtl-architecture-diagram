---
name: rtl-architecture-diagram
description: Create compact SVG architecture and microarchitecture diagrams from Verilog, SystemVerilog, VHDL, Chisel-generated RTL, or hardware-design code. Use for datapaths, pipelines, memories, FIFOs, arbiters, FSM/control, module hierarchy, and RTL dataflow. Do not use for gate-level schematics, PCB schematics, or timing waveforms.
---

# RTL architecture diagrams

Produce a compact hardware-architecture SVG with the bundled deterministic renderer. **Never hand-author Mermaid, PlantUML, Graphviz, or SVG unless the user explicitly asks for that format.**

## Workflow

1. Establish the requested view boundary and top module. If the scope is ambiguous, choose the most likely boundary and state the assumption.
2. Inspect only the RTL needed for that scope. Trace instantiations and architecturally relevant data/control paths, keeping a compact source-evidence map for major blocks and connections.
3. Choose the abstraction before drawing. Show semantic hardware units, not literal RTL statements.
4. Write `<name>.diagram.json` using the compact IR below. Normally omit `at` and let the renderer infer semantic ranks and lanes. Add `at:[column,row]` only as an architectural anchor; never use pixel coordinates.
5. Run:
   `python <skill-dir>/scripts/render.py <name>.diagram.json -o <name>.svg --lint --strict`
6. Resolve every warning by correcting the JSON IR or shortening labels. Do not hand-edit generated SVG geometry.
7. Inspect the rendered SVG visually when an image/browser viewer is available and re-render until clear.
8. Re-check every major block and connection against the source-evidence map. Identify any inference or uncertainty explicitly.
9. Return both JSON and SVG unless the user asks for only one, plus a short list of the main RTL source files used.
10. For README or other Markdown previews, embed the generated SVG path directly, for example `![Datapath](docs/datapath.svg)`. Do not rasterize it merely to obtain a preview. Chat/agent surfaces that accept local image references can likewise display the SVG file directly; rasterize only when the destination cannot render SVG.

## Abstraction rules

- Prefer <=25 blocks and <=45 edges per diagram. Split large designs hierarchically.
- Show memories, FIFOs/queues, arbiters, major muxes, functional units, important controllers/FSMs, and meaningful pipeline boundaries.
- Collapse trivial combinational logic, assigns, constants, reset plumbing, ordinary registers, and implementation-only decode.
- Use primitive gate kinds only for architecturally meaningful gates. This skill is not intended to expand a design into a gate-level netlist.
- Aggregate related signals into one named interface/bus. Do not draw every `valid`, `ready`, ID, or field separately unless important to the architecture.
- Show bit widths only when useful. Use RTL signal/module names only when they aid traceability; prefer short architectural labels.
- Do not invent a block or connection to make the diagram look complete. Omit uncertain detail or label it as an inference.
- Main datapath flows left-to-right. Very long rows may fold into a compact two-line serpentine while preserving pipeline adjacency. Put control above it and memories/support structures below when practical; a memory controlled from above and consumed by the datapath may move directly above its consumer, and the renderer may use a nearby fold-side shelf when a nominal lower-row position would waste space. Let it wrap longer block titles and subtitles into at most two balanced lines and size from those rendered lines instead of forcing every block to be wide.
- Use `"bigger":true` or `"smaller":true` sparingly when architectural importance should change a node's visual prominence. Never set both. Omit both for normal nodes; do not use prominence merely to repair layout.
- Keep completion tags, status outputs, and other leaf terminals near their producer instead of placing an unrelated state or memory block between them. Prefer the semantic layout with the shortest clear arrows and least total route length.
- Use `kind:"control"` for control edges and `kind:"response"` for backwards/return paths so the renderer keeps them away from the main datapath.
- Normally omit `from_side` and `to_side`, and never encode cosmetic port order in the JSON. The renderer orders ports from destination geometry, aligns connected support blocks, and keeps clear aligned links straight. Use an explicit side only when the attachment side is architecturally meaningful.
- Cross-component state handoffs should use the clear corridor between vertically separated group enclosures and enter a lower state memory from the north. Keep this automatic; do not add cosmetic route coordinates to the JSON.
- Ports attach to the visible node outline, including the curved boundary of pill-shaped I/O blocks. Near-aligned group enclosure starts share an exact edge; intentionally different indentation remains distinct.
- Arrowheads are broad schematic triangles rendered separately from their shafts. Shafts terminate at the triangle base so the line cannot project through and visually blunt the point.
- Keep labels for internal connections inside their shared group enclosure. The renderer may attach a collision-aware orthogonal leader off-center when that produces a quieter, more compact placement; arrows and other leaders must not cross it.
- Avoid crossing hierarchy boundaries unless the connection matters to the requested view.

## Compact IR

```json
{
  "title": "TT subsystem",
  "blocks": [
    {"id":"q","label":"Request FIFO","kind":"fifo"},
    {"id":"a","label":"Arbiter","kind":"arbiter"},
    {"id":"m","label":"TT RAM","kind":"memory"}
  ],
  "edges": [
    {"from":"q","to":"a","label":"request","width":96},
    {"from":"a","to":"m","label":"lookup"}
  ]
}
```

Allowed block kinds: `module`, `logic`, `memory`, `fifo`, `mux`, `demux`, `reg`, `counter`, `fsm`, `arbiter`, `io`, `alu`, `adder`, `subtractor`, `addsub`, `multiplier`, `comparator`, `and`, `or`, `xor`, `not`.

Multi-bit edges (`"width":N`, where `N > 1`) render as thicker bus routes and receive an `Nb` width label automatically. Data, control, response, and clock routes use distinct line colors/styles.

`at:[column,row]` is optional. Omitted positions are inferred deterministically from dataflow, block kind, groups, and explicit anchors elsewhere in the same diagram.

Use `references/IR.md` only when advanced fields (groups, explicit port sides, routing hints, subtitles) are needed.
