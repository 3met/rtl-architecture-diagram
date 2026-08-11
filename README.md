# RTL Architecture Diagram Codex skill

A repo-local Codex skill for turning RTL into compact, deterministic SVG architecture/microarchitecture diagrams.

## Install

Copy the included `.agents/` directory into the root of your repository. Codex discovers repo skills under `.agents/skills`.

Then invoke it explicitly when desired:

```text
$rtl-architecture-diagram draw the transposition-table microarchitecture
```

Or ask naturally for an RTL architecture/datapath diagram; the skill description is written so Codex can select it implicitly.

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

The suite checks the CLI, deterministic output, endpoint and label geometry, routing diagnostics, SVG escaping, and invalid-IR handling without installing dependencies.

## Design

Codex is responsible for understanding RTL, retaining source evidence, and selecting architectural blocks/connections. It emits a small semantic JSON IR. The renderer owns all SVG geometry, sizing, exact port placement, obstacle-aware orthogonal routing, collision-aware labels, geometric linting, and visual conventions. Codex should revise the JSON rather than hand-editing SVG coordinates.
