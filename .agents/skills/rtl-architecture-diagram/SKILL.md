---
name: rtl-architecture-diagram
description: Create compact SVG architecture and microarchitecture diagrams from a hardware or HDL design. Use for datapaths, pipelines, memories, FIFOs, arbiters, FSM/control, module hierarchy, and RTL dataflow. Do not use for gate-level schematics, PCB schematics, or timing waveforms.
---

# RTL architecture diagrams

Produce a compact hardware-architecture SVG with the bundled deterministic renderer. Do not substitute Mermaid, PlantUML, Graphviz, or hand-authored SVG unless the user explicitly requests that format.

## Workflow

1. Establish the requested view boundary and top module. If ambiguous, choose the most likely scope and state the assumption.
2. Inspect the relevant design sources. Trace instantiations and the data/control paths needed to support each major block and connection.
3. Choose the abstraction before drawing. Show semantic hardware units, not literal RTL statements.
4. Write `<name>.diagram.json` using the compact IR below. Normally omit `at` and let the renderer infer semantic ranks and lanes. Add `at:[column,row]` only as an architectural anchor; never use pixel coordinates.
5. Render and validate it:
   `python <skill-dir>/scripts/render.py <name>.diagram.json -o <name>.svg --lint --strict`
6. Resolve every warning in the JSON and re-render. Do not edit generated SVG geometry.
7. Inspect the SVG visually when possible and correct unclear abstraction, labels, or flow.
8. Return the JSON and SVG unless the user requests otherwise. Name the main source files used and identify any inference or uncertainty.

## Abstraction rules

- Keep each diagram focused; split large designs into hierarchical views.
- Show memories, FIFOs/queues, arbiters, major muxes, functional units, important controllers/FSMs, and meaningful pipeline boundaries.
- Collapse trivial combinational logic, assigns, constants, reset plumbing, ordinary registers, and implementation-only decode.
- Use primitive gate kinds only for architecturally meaningful gates. This skill is not intended to expand a design into a gate-level netlist.
- Aggregate related signals into named interfaces or buses. Draw individual handshake or field signals only when architecturally important.
- Show bit widths only when useful. Use RTL signal/module names only when they aid traceability; prefer short architectural labels.
- Do not invent a block or connection to make the diagram look complete. Omit uncertain detail or label it as an inference.
- Use `"bigger":true` or `"smaller":true` sparingly when architectural importance should change a node's visual prominence. Never set both. Omit both for normal nodes; do not use prominence merely to repair layout.
- Use `kind:"control"` for control signals and `kind:"response"` for return paths.
- Let the renderer choose placement, port sides, and routes. Add optional layout or routing hints only when they communicate architecture or fix an unclear render.
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

Use `width` for multi-bit buses. Omit `at` to use automatic placement.

Read `references/IR.md` when using groups, subtitles, prominence, explicit sizing or placement, port sides, or routing hints.
