---
type: guide
title: Optional Codex integrations
description: Import a selected Codex rollout file or collect supported Codex lifecycle events with optional native hooks.
---

# Optional Codex integrations

Workflow Ledger supports two opt-in Codex paths: rollout import and local
hooks. Import a named rollout file to read its newline-delimited JSON. Enable the
local plugin to capture supported lifecycle events. The harness-agnostic Python
core works without either path. They provide different evidence and
aren't interchangeable.

Install the Python package using the release instructions in
[Getting started](../guides/getting-started.md). The Python package is
available from GitHub Releases. Installing the package and running
`workflow-ledger init` do not install or configure Codex hooks. The local
plugin distribution path is separate; the source-checkout packaging steps below
are for development and evaluation; a released Codex plugin archive is not
available yet.

## Contents

- [Import one selected rollout file](#import-one-selected-rollout-file)
- [Capture native lifecycle events with hooks](#capture-native-lifecycle-events-with-hooks)
- [Build and install the local plugin from a checkout](#build-and-install-the-local-plugin-from-a-checkout)
- [Configure and trust hooks](#configure-and-trust-hooks)
- [Verify capture](#verify-capture)
- [Disable and remove the plugin](#disable-and-remove-the-plugin)
- [What Codex evidence establishes](#what-codex-evidence-establishes)
- [Next steps](#next-steps)

## Import one selected rollout file

The importer reads only the file passed on the command line. It doesn't search
Codex session directories or inspect transcripts for skill or agent names.

```text
.venv/bin/workflow-ledger import-codex /path/to/selected-rollout.jsonl --store telemetry.jsonl
.venv/bin/workflow-ledger report --store telemetry.jsonl --format text
```

The current best-effort mapping recognizes session metadata and cumulative
`total_token_usage` snapshots. It keeps the latest cumulative snapshot rather
than adding snapshots together. Re-importing the same observation is
idempotent; a changed observation appends a revision, and reports use the latest
revision. A decreasing counter is ambiguous, so the importer withholds the
affected token count. Missing fields remain unknown.

The importer requires a session identifier. It excludes source paths, prompts,
tool bodies, and native session IDs from output; retained identifiers are stable
opaque values. The rollout record format isn't a stable public Codex API, so
diagnostics report unsupported or malformed records.

An imported duration is an inferred observation window between source
timestamps. It may include idle time. The import doesn't establish active
execution time, per-call model usage, tool timing, skill or agent invocation,
session outcome, or complete capture. Token totals stay at the run level unless
the source provides actual ownership evidence; this adapter doesn't provide
that evidence.

## Capture native lifecycle events with hooks

The optional local plugin maps supported Codex events to the shared record
contract: session start/end, tool before/after, and subagent start/stop. Hook
collection is separate from artifact import. Installing the plugin doesn't
enable or trust its hooks. Review the hook definitions in Codex and explicitly
enable capture through the host configuration.

### Build and install the local plugin from a checkout

This path requires a source checkout. A released distribution path for the
Codex plugin is not available yet. From the repository root, build the
Codex-format package and a local marketplace catalog. The builder requires a
new marketplace destination and doesn't overwrite an existing catalog:

```text
.venv/bin/python tools/package_plugin.py --format codex --output work/workflow-ledger-plugin.zip --marketplace-dir work/workflow-ledger-marketplace
```

The `codex` format includes the native `.codex-plugin/plugin.json` manifest and
omits the portable root `plugin.json`. Packaging and hook discovery checks passed
with Codex CLI 0.160.0; the root manifest prevented hook discovery in that test.
Use the default `portable` format for other consumers.

Register the generated local marketplace and install the plugin:

```text
codex plugin marketplace add ./work/workflow-ledger-marketplace
codex plugin add workflow-ledger@workflow-ledger-local
```

The generated archive includes the companion skill as well as the hook plugin.
Tests found that Codex CLI 0.160.0 sometimes omits a plugin's bundled skills
from the session skill catalog ([upstream issue #37143](https://github.com/openai/codex/issues/37143)).
If you also use the skill, install its canonical folder separately and copy it
again after repository updates:

```text
cp -R .agents/skills/workflow-ledger-operations ~/.agents/skills/
```

This skill copy doesn't enable the plugin or hook capture. Check plugin
availability and hook discovery on the intended host; packaging and fixture
checks alone don't prove desktop installation.

### Configure and trust hooks

Installation alone doesn't enable hook capture. Before starting Codex,
configure its environment and enable the plugin through the host. Review exact
hook definitions in the review flow (`/hooks` in the CLI). Review changed
definitions again. See the
[Codex hooks documentation](https://learn.chatgpt.com/docs/hooks).

| Variable | Value |
| --- | --- |
| `WORKFLOW_LEDGER_ENABLED` | Set to `1` to enable hooks; unset or another value disables them |
| `WORKFLOW_LEDGER_PYTHON` | Absolute path to the installed package's Python interpreter |
| `WORKFLOW_LEDGER_STORE` | Absolute path to the cooperative observations store |
| `WORKFLOW_LEDGER_POLICY` | Optional policy file; omit it to select all supported native hook records |
| `WORKFLOW_LEDGER_RETENTION_POLICY` | Optional JSON rotation/cap file; omitted uses 5 MiB rotation and 250 MiB archive cap |

The hook launcher requires `python3` on `PATH`, then invokes `WORKFLOW_LEDGER_PYTHON`. For a
CLI launch, pass the values directly to the Codex process (replace the example
paths):

```text
WORKFLOW_LEDGER_ENABLED=1 WORKFLOW_LEDGER_PYTHON=/absolute/repo/.venv/bin/python WORKFLOW_LEDGER_STORE=/chosen/telemetry.jsonl WORKFLOW_LEDGER_POLICY=/chosen/policy.json WORKFLOW_LEDGER_RETENTION_POLICY=/chosen/retention.json codex
```

For desktop use, supply the same variables to the application's launch
environment and restart the host. Variables in an unrelated terminal may not
reach an already running app. The retention file can set `enabled`,
`rotation_bytes`, and `archive_cap_bytes`. Omit it to use the defaults.

If Codex supplies the session identifier, caller-declared markers may use it to
share a trace with hook events. Don't guess a native identifier or search
rollout files to find one. Without the host identifier, keep caller-declared
markers in a separate caller-chosen trace.

### Verify capture

Inspect the declared adapter coverage without enabling capture:

```text
.venv/bin/workflow-ledger capabilities codex-hooks
```

Then enable the plugin and run one small session with the intended selection
policy. Confirm that the configured store receives records and inspect the
human-readable report:

```text
.venv/bin/workflow-ledger report --store /chosen/telemetry.jsonl --format text
```

Check for missing or filtered records, storage diagnostics, and the expected
session/tool/subagent boundaries before relying on capture. Hook/helper errors
are fail-open and reported with sanitized diagnostics; storage categories can
include `permission_denied`, `storage_full`, `storage_unavailable`, and
`lock_timeouts`. A missing event or a helper write failure is a capture gap.

### Disable and remove the plugin

- Set `WORKFLOW_LEDGER_ENABLED=0` or unset it, then restart Codex to stop hook capture.
- Disable or remove the plugin through Codex. With the local marketplace install
  above, removal uses:

  ```text
  codex plugin remove workflow-ledger@workflow-ledger-local
  codex plugin marketplace remove workflow-ledger-local
  ```

- If the marketplace contains other plugins, remove only this plugin and keep
  the marketplace.
- Removing or disabling the plugin doesn't delete existing observations.

Hook events retain opaque runtime identities and boundary timestamps. Outcomes
remain unknown when the event source lacks a reliable result flag. The mapping
doesn't expose native skill lifecycle, model-call, or token-usage events. Tool
and subagent outcomes also remain unknown because the portable event envelope
lacks a reliable result flag. Hooks don't read transcripts or retain prompts,
tool arguments/results, or raw runtime IDs.

Capture is incomplete if the host omits an event or the helper can't write a
record. Inspect diagnostics and run a selected test before relying on capture.

Hooks don't associate tools with active skills. Caller-declared skill markers
can share a trace with native events when the caller has the session identifier.
That link doesn't establish tool or token ownership by the skill. Session and
subagent boundaries don't prove success or active execution throughout their
elapsed windows.

## What Codex evidence establishes

| Path | Can establish | Can't establish |
| --- | --- | --- |
| Selected artifact import | An observed session window and the latest supported cumulative usage snapshot | Active time, per-call usage, tool timing, skill/agent invocation ownership, outcome, complete capture |
| Native hooks | Boundaries for supported session, tool, and subagent events delivered to the hook | Skill lifecycle, model calls, token use, reliable tool/subagent outcomes, skill-to-tool ownership, complete capture |

Use [the attachment contract](../reference/attachment-contract.md) for shared
identity, selection, and ownership semantics, and
[compatibility](../reference/compatibility.md) for
the verified CLI versions and remaining live-host limits.

## Next steps

- Use the harness-neutral [Python quickstart](../guides/getting-started.md) for
  caller-declared instrumentation.
- Use the [skills and agents guide](../guides/skills-and-agents.md) for shared
  markers and reusable adoption instructions.
