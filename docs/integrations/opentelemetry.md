---
type: guide
title: Optional OpenTelemetry export
description: Explicitly export a selected local observation snapshot through OpenTelemetry Protocol (OTLP) over HTTP.
---

# Optional OpenTelemetry export

OpenTelemetry is an optional export integration. Workflow Ledger records
locally first; it sends no data to a collector or backend until you explicitly
request an export with an endpoint. Collector deployment, backend setup, and
dashboards are outside this repository.

## Contents

- [Install the optional extra](#install-the-optional-extra)
- [Export a local snapshot](#export-a-local-snapshot)
- [Understand the exported data](#understand-the-exported-data)
- [Next steps](#next-steps)

## Install the optional extra

```text
.venv/bin/python -m pip install -e '.[otel]'
```

See [compatibility](../reference/compatibility.md) for supported optional
dependency versions.

## Export a local snapshot

Export is a separate, explicit action. Supply the OpenTelemetry Protocol (OTLP)
endpoint over HTTP. The integration doesn't use an ambient default endpoint:

```text
.venv/bin/workflow-ledger export-otel --store telemetry.jsonl --endpoint https://collector.example
```

To send authentication headers, set a named environment variable containing a
JSON object and pass its name with `--headers-env`. Header values aren't stored
in observations or diagnostics. The integration ignores ambient exporter header
variables.

The command applies the selected-record policy before export. By default, it
reads the active line-delimited JSON store. Include retained archives with
`--include-archives`. Export diagnostics report skipped incomplete spans and
trace/metric delivery failures. Incomplete records can't become trace spans,
though their independently known usage can still contribute to the metric
snapshot.

**Expected result:** on successful delivery, the command prints JSON export
counts and exits successfully. If the exporter reports delivery errors, it
prints diagnostics and exits nonzero. The exporter doesn't create a local
report file; it sends the selected records to the endpoint you supplied.

Python applications can call `export_records(records, endpoint=...)` from
`workflow_ledger.otel`; callers must supply the endpoint. The integration constructs
private SDK/exporter objects and doesn't register global OpenTelemetry providers
or broaden capture implicitly.

## Understand the exported data

Completed records with start and end timestamps export as OpenTelemetry spans.
They preserve known timing and parent identity. The exporter encodes supported
operation identity, source, harness, measurement quality, ownership associations,
and token source/quality as `workflow_ledger.*` attributes. Missing timing means no
span; a trace isn't a guarantee of complete capture.

Metrics are snapshot gauges describing the selected store at export time. They
include known token totals, known and unknown token observations, operation
counts, and duration sums/counts. Metric labels omit run and session IDs.
labels. Re-exporting the same store replaces the snapshot meaning; it doesn't
represent new token consumption. These metrics aren't a streaming rate,
additive counters, a billing ledger, or duration histograms. Use exported spans
for individual operation timing.

The exporter doesn't call a model provider. It sends the records already
collected by the local package to the endpoint you supplied. Unknown usage stays
unknown, and the export doesn't allocate unattributed run totals to skills or
agents. See [measurement interpretation](../concepts/measurement-model.md) and the
[attachment contract](../reference/attachment-contract.md) for the semantics that carry
through export.

## Next steps

- Review [compatibility](../reference/compatibility.md) for verified receiver coverage and
  live-backend validation limits.
- Use the [CLI and storage reference](../reference/cli-and-storage.md) for
  selection, retention, report, and archive behavior.
