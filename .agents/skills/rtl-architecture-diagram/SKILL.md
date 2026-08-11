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
4. Write `<name>.diagram.json` using the compact IR below. Use semantic grid positions; never pixel coordinates.
5. Run:
   `python <skill-dir>/scripts/render.py <name>.diagram.json -o <name>.svg --lint --strict`
6. Resolve every warning by correcting the JSON IR or shortening labels. Do not hand-edit generated SVG geometry.
7. Inspect the rendered SVG visually when an image/browser viewer is available and re-render until clear.
8. Re-check every major block and connection against the source-evidence map. Identify any inference or uncertainty explicitly.
9. Return both JSON and SVG unless the user asks for only one, plus a short list of the main RTL source files used.

## Abstraction rules

- Prefer <=25 blocks and <=45 edges per diagram. Split large designs hierarchically.
- Show memories, FIFOs/queues, arbiters, major muxes, functional units, important controllers/FSMs, and meaningful pipeline boundaries.
- Collapse trivial combinational logic, assigns, constants, reset plumbing, ordinary registers, and implementation-only decode.
- Aggregate related signals into one named interface/bus. Do not draw every `valid`, `ready`, ID, or field separately unless important to the architecture.
- Show bit widths only when useful. Use RTL signal/module names only when they aid traceability; prefer short architectural labels.
- Do not invent a block or connection to make the diagram look complete. Omit uncertain detail or label it as an inference.
- Main datapath flows left-to-right. Put control above it and memories/support structures below when practical.
- Use `kind:"control"` for control edges and `kind:"response"` for backwards/return paths so the renderer keeps them away from the main datapath.
- Avoid crossing hierarchy boundaries unless the connection matters to the requested view.

## Compact IR

```json
{
  "title": "TT subsystem",
  "blocks": [
    {"id":"q","label":"Request FIFO","kind":"fifo","at":[0,1]},
    {"id":"a","label":"Arbiter","kind":"arbiter","at":[1,1]},
    {"id":"m","label":"TT RAM","kind":"memory","at":[2,2]}
  ],
  "edges": [
    {"from":"q","to":"a","label":"request","width":96},
    {"from":"a","to":"m","label":"lookup"}
  ]
}
```

Allowed block kinds: `module`, `logic`, `memory`, `fifo`, `mux`, `reg`, `fsm`, `arbiter`, `io`.

Use `references/IR.md` only when advanced fields (groups, explicit port sides, routing hints, subtitles) are needed.
