# Workflow Ledger

[![CI](https://github.com/pschaubroeck/workflow-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/pschaubroeck/workflow-ledger/actions/workflows/ci.yml)
![Python 3.10–3.14 tested](https://img.shields.io/badge/python-3.10%E2%80%933.14%20tested-3776AB?logo=python&logoColor=white)
![Core runtime dependencies: 0](https://img.shields.io/badge/core_runtime_dependencies-0-2ea44f)

Measure agent workflows as linked operations: workflows, agents, skills, model
calls, tools, and explicit waits. The core is a zero-dependency Python library
that records operational metadata locally. Optional adapters attach supported
runtime events, and OpenTelemetry export is always explicit.

The core is harness-agnostic. It does not inspect transcripts, infer that a
skill ran because it was configured or mentioned, or invent token usage. Missing
measurements stay unknown. This repository provides local collection and
reporting; collector deployment, dashboards, and backend operation are outside
its scope.

## Set it up with an AI assistant

Ask an AI assistant to check the Python environment you want to use, install the
released package from its configured package index if needed, and help initialize
local capture. The release is not published yet; until it is available, see the
contributor section below for source-checkout steps.

Start with the [getting-started guide](docs/guides/getting-started.md) for a
harness-neutral prompt and example conversation. The assistant helps confirm
exact assets to record, suggests default local paths, and can install the bundled
operations skill when you choose a runtime-appropriate location. Initialization
previews changes by default; the assistant should show the preview and ask
before applying it.

## Choose a path

- [Getting started](docs/guides/getting-started.md) — released-package install
  and an AI-assisted setup walkthrough.
- [Skills and agents](docs/guides/skills-and-agents.md) — initialize selection,
  install the operations skill, and declare execution and wait boundaries.
- [Runtime integrations](docs/README.md#integrations) — optional Codex hooks,
  an adapter for the OpenAI Agents SDK, and OpenTelemetry export.
- [Measurement model](docs/concepts/measurement-model.md) — identity, evidence,
  token usage, timing, overlap, and unknown values.
- [Reference](docs/README.md#reference) — attachment contract, CLI/storage, and
  compatibility.
- [Maintainers](docs/maintainers/README.md) — release, contribution, and project
  naming guidance.

## Contributor source-checkout workflow

These commands are for developing this repository before or alongside a
published release. They are not the normal consumer installation path.

```text
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python examples/quickstart.py --store /tmp/observations.jsonl
.venv/bin/workflow-ledger report --store /tmp/observations.jsonl --format text
```

The example records a workflow, agent, and tool using the public API. It makes
no model or external tool calls. The report reads the local JSONL file and shows
the recorded operation counts and measurement limits.

## Development checks

```text
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/python -m ruff check src tests benchmarks examples plugins tools
.venv/bin/python -m build
```

See the [documentation index](docs/README.md) for the complete guide and
reference map. The [compatibility page](docs/reference/compatibility.md)
distinguishes implemented behavior from environments and event paths that have
been verified.
