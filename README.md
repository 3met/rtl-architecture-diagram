# RTL Architecture Diagram Codex skill

A repo-local Codex skill for turning hardware or HDL designs into compact, deterministic SVG architecture diagrams.

## Example

The NNUE evaluator datapath is defined as semantic JSON and rendered with the bundled deterministic renderer.

[![NNUE Evaluator Datapath](examples/nnue-evaluator.svg)](examples/nnue-evaluator.svg)

[Diagram IR](examples/nnue-evaluator.diagram.json) · [Rendered SVG](examples/nnue-evaluator.svg)

Markdown renderers that support SVG can preview the generated file directly:

```markdown
![NNUE Evaluator Datapath](examples/nnue-evaluator.svg)
```

## Install

Copy `.agents/` into the root of your repository. Codex discovers repository skills under `.agents/skills`.

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

The renderer is dependency-free and self-contained under `scripts/`, so copying `.agents/` requires no packaging or installation step. Use `render.py` as the CLI and import entry point.

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

The suite covers the CLI, IR validation, deterministic layout, routing, labels, and SVG output.

## How it works

Codex extracts the diagram boundary, architectural blocks, and connections from the design source, then writes a compact JSON IR. The renderer turns that IR into deterministic layout, routing, labels, hardware symbols, and SVG.

Block `at:[column,row]` values are optional semantic anchors. Prefer automatic placement, and revise the JSON rather than editing generated SVG geometry.

## Built-in hardware notation

Supported blocks include modules, logic, memories, FIFOs, muxes/demuxes, registers, counters, FSMs, arbiters, I/O, arithmetic units, comparators, and meaningful logic gates. Edge kinds distinguish data, control, response, and clock paths; multi-bit widths render as buses. See the bundled [IR reference](.agents/skills/rtl-architecture-diagram/references/IR.md) for all fields.
