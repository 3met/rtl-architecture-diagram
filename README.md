# RTL Architecture Diagram Codex skill

A repo-local Codex skill for turning RTL into compact, deterministic SVG architecture/microarchitecture diagrams.

## Example

The NNUE evaluator datapath is defined as semantic JSON and rendered with the bundled deterministic renderer.

[![NNUE Evaluator Datapath](examples/nnue-evaluator.svg)](examples/nnue-evaluator.svg)

[Diagram IR](examples/nnue-evaluator.diagram.json) · [Rendered SVG](examples/nnue-evaluator.svg)

Agents and Markdown renderers that support SVG can preview the generated file directly; no PNG conversion or separate rendering step is needed:

```markdown
![NNUE Evaluator Datapath](examples/nnue-evaluator.svg)
```

## Install

Copy the included `.agents/` directory into the root of your repository. Codex discovers repo skills under `.agents/skills`.

Then invoke it explicitly when desired:

```text
$rtl-architecture-diagram draw the transposition-table microarchitecture
```

Or ask naturally for an RTL architecture/datapath diagram; the skill description is written so Codex can select it implicitly.

## Repository layout

```text
.agents/skills/rtl-architecture-diagram/
├── SKILL.md                 Agent workflow and abstraction rules
├── agents/openai.yaml       Skill discovery metadata
├── assets/icon.svg          Skill icon
├── examples/                Small bundled smoke-test diagram
├── references/IR.md         Complete JSON IR reference
└── scripts/
    ├── render.py            Stable renderer CLI/import façade
    └── rtl_diagram/         Model, geometry, and route-quality modules
examples/                    Repository-level showcase diagrams
tests/                       Focused CLI, IR, layout, routing, and SVG suites
```

The renderer remains dependency-free and self-contained under `scripts/`, so the `.agents/` directory can be copied into another repository without packaging or installation work. `render.py` remains the stable CLI/import surface; shared data types and orthogonal geometry primitives live in `scripts/rtl_diagram/`, while renderer-specific sizing, IR, layout, routing, labels, SVG, and linting remain deliberately colocated.

## Test the renderer

From the skill directory:

```bash
python scripts/render.py examples/tt.diagram.json -o /tmp/tt.svg --lint --strict
```

The renderer uses only the Python standard library.

From the repository root, run the regression suite:

```bash
python -m unittest discover -s tests -v
```

The suite checks the CLI, deterministic output, automatic and anchored semantic placement, endpoint and label geometry, routing diagnostics, SVG escaping, and invalid-IR handling without installing dependencies.

## Architecture

Codex owns semantic interpretation: the diagram boundary, architectural blocks, connections, and source evidence. It emits a compact JSON IR. The renderer owns deterministic geometry, including:

- automatic or anchored placement;
- dataflow-scored primary rows, connectivity-driven support placement, and interstitial state-group placement;
- node sizing and hardware symbols;
- exact port attachment, adaptive stubs, and orthogonal routing;
- simple elbow/channel selection before obstacle-search fallback;
- nearest-route/nearest-boundary label leaders and post-route wire de-overlap;
- whole-diagram crossover/bend scoring with bounded automatic port reordering;
- collision-aware labels and geometric linting;
- SVG styling and output.

Block `at:[column,row]` values are optional. Missing ranks and lanes are inferred from connectivity, block kinds, groups, and any explicit anchors. Revise semantic JSON rather than hand-editing generated SVG coordinates.

## Built-in hardware notation

Supported blocks include modules, generic logic, memories, FIFOs, muxes/demuxes, registers, counters, FSMs, arbiters, I/O, ALUs, adders, subtractors, selectable add/sub units, multipliers, comparators, and meaningful AND/OR/XOR/NOT gates.

The visual conventions include:

- explicit arithmetic-operation badges and register clock notches;
- thicker bus routes with shaft-safe broad arrowheads;
- signal-semantic colors for data, control, clock, and response paths;
- two-line title/subtitle wrapping and optional `bigger`/`smaller` prominence;
- compact group-aware layout with long-row folding and support shelves;
- bridge-state bands between vertically separated datapaths;
- straight singleton links, clear cross-group corridors, and aligned group frames;
- local exterior lanes for nearby feedback paths;
- dedicated parallel tracks where routes would otherwise share a segment;
- quiet label placement with collision-aware orthogonal leaders;
- a centered diagram title and dependency-free UI font stack.
