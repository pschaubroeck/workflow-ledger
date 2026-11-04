---
type: reference
title: CLI and Local Storage
description: Reference local command-line workflows, line-delimited JSON storage, and retention settings.
---

# CLI and Local Storage

The command-line interface reads and writes local records. Source imports,
collection, and exports are explicit; there is no background collector.

## Commands

Install the core package to use `workflow-ledger`. Optional integrations
require their documented extras.

```text
workflow-ledger init --dry-run
workflow-ledger init --apply
workflow-ledger demo --store observations.jsonl --scenario parallel
workflow-ledger report --store observations.jsonl --format text
workflow-ledger report --store observations.jsonl --group-by skill
workflow-ledger workflow TRACE_ID --store observations.jsonl --format text
workflow-ledger capabilities explicit
```

`init` previews local setup by default. With no selection flags, it enables the
baseline operations skill and caller workflow context. Supply one or more exact
selectors to replace that baseline, explicitly select `--all` for broad
capture, or use `--disabled` to initialize with collection off. Review the
reported policy and store paths before repeating the command with `--apply`.
It can install the packaged operations skill under `--skill-root`; user-owned
or modified skill directories are preserved. A differing existing policy
requires the explicit `--replace-policy` option after review. `init` configures
local collection only; it doesn't enable runtime hooks or export data. See the
[AI-assisted setup guide](../guides/getting-started.md#ask-an-ai-assistant-to-set-it-up).

The `demo` command writes synthetic activity and marks its token observations as
synthetic. `report` summarizes the selected records and distinguishes
unknown usage from zero. `workflow` inspects one trace. `capabilities` reports
the declared coverage for `explicit`, `codex`, `agents`, or `codex-hooks`.

Other explicit commands are:

- `import-codex SOURCE --store PATH` — read the selected Codex artifact file;
  this doesn't turn artifact import into live hook capture.
- `cost-view --store PATH` — produce the sanitized usage and coverage view for
  an external analyzer.
- `export-otel --store PATH --endpoint URL` — export a local snapshot through
  the OpenTelemetry Protocol over HTTP. See the [OpenTelemetry integration](../integrations/opentelemetry.md).

Use `--policy PATH` or `--retention-policy PATH` before the command. Put
`--disabled` before the command to return before reading source data, writing,
loading optional adapters, or exporting.

## Local line-delimited JSON store

The default store contains append-only JSON records, one per line. Reports use
the latest revision with the same trace and span identity. The store supports
one writer process and serializes calls from threads in that process. Shared
marker and hook writers coordinate through `SessionStore`'s lock. Don't mix an
uncoordinated `JsonlStore` writer into that active file.

Structured events use a separate `EventStore` file because each event is an
immutable record, not a span revision. See [structured events](../guides/structured-events.md)
for the OpenTelemetry Logs data model JSONL fields, severity mapping, and Python
recording example.

The default retention policy rotates the active file before an append would
exceed 5 MiB. It retains archives without an age limit until their combined size
exceeds 250 MiB, then removes the oldest matching archives first. The active
file isn't purged. Rotation and cap enforcement run synchronously during
writes.

Reports, workflow inspection, and cost views include retained archives by
default and resolve revisions across files. OpenTelemetry export reads only the
active file unless you pass `--include-archives`. An archive-aware marker retry
index is private and can be rebuilt; its disk usage is additional to the
archive cap.

To customize rotation and the archive cap, save a JSON policy:

```json
{
  "enabled": true,
  "rotation_bytes": 5242880,
  "archive_cap_bytes": 262144000
}
```

Pass it with `--retention-policy PATH` before a CLI command. Marker and Codex
hook processes can read `WORKFLOW_LEDGER_RETENTION_POLICY`. Python callers can pass a
`RetentionPolicy` to `JsonlStore` or `SessionStore`. Set `enabled` to `false` to
disable rotation and purging. The archive cap must be at least the rotation
threshold.

The local store isn't a multi-writer transactional database. See the
[compatibility summary](compatibility.md) for its supported scope.
