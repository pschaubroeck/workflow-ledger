---
type: guide
title: Record structured events
description: Record consistent operational events with stable names, severity, attributes, and trace correlation.
---

# Record structured events

Use structured events for operational facts that need a stable severity and
queryable fields. They complement operation spans: a span describes an
operation's lifecycle, while an event records something that happened during
that operation.

## Contents

- [Record an event](#record-an-event)
- [Event fields and severity](#event-fields-and-severity)
- [Shared event vocabulary](#shared-event-vocabulary)
- [Ask an AI assistant to add events](#ask-an-ai-assistant-to-add-events)
- [Scope and export](#scope-and-export)
- [Next steps](#next-steps)

## Record an event

Create an event store separately from the span store and pass it to the
recorder:

```python
from workflow_ledger import EventStore, JsonlStore, Recorder

events = EventStore("events.jsonl")
recorder = Recorder(JsonlStore("spans.jsonl"), event_sink=events)

with recorder.span("workflow", "review"):
    recorder.emit_event(
        "agent.operation.retrying",
        "WARN",
        body="Retrying a transient operation",
        attributes={
            "agent.operation.name": "review",
            "agent.retry.attempt": 1,
            "agent.retry.reason": "unavailable",
        },
    )
```

The event goes into `events.jsonl`, a JSON Lines file with one object per line.
When you emit it inside a selected span, it carries that span's trace and span
IDs and measured asset associations. An event outside a span has no trace correlation. Both
stores use the configured retention policy; event archives use the same archive
naming and size-cap behavior as span archives.

**Expected result:** the event store contains one record with the event name,
severity, timestamps, attributes, and available correlation IDs. The body is
for human context; queries should use `EventName`, `SeverityText`, and
`Attributes`.

## Event fields and severity

Each JSON Lines (JSONL) record is a JSON representation of the OpenTelemetry
Logs data model, using its canonical field names. It is a supported subset of the model,
with Workflow Ledger metadata placed in standard `Resource` and `Attributes`
fields:

| Field | Meaning |
| --- | --- |
| `Timestamp` | Event time in Unix nanoseconds |
| `ObservedTimestamp` | Time Workflow Ledger recorded the event, in Unix nanoseconds |
| `SeverityText`, `SeverityNumber` | Source level and normalized severity number |
| `EventName` | Stable event type |
| `Body` | Optional human-readable context |
| `TraceId`, `SpanId` | Optional trace correlation, represented as lowercase hexadecimal strings |
| `Resource.Attributes.workflow_ledger.harness` | Runtime or integration that recorded the event |
| `InstrumentationScope.Name` | `workflow-ledger` |
| `Attributes` | Event-specific and Workflow Ledger fields |

`Attributes` contains user-provided strings, signed 64-bit integers, or Boolean values,
plus these product fields:
`workflow_ledger.event_id`, `workflow_ledger.schema_version`, and
`workflow_ledger.associations`. Association objects preserve measured asset
context. Reserve the `workflow_ledger.*` prefix for the product; give your
own stable attributes a prefix such as `app.retry.count`. Keep queryable
facts out of `Body`.

This JSONL representation follows the Logs data model, but it is not
OpenTelemetry Protocol (OTLP) JSON or an OTLP wire payload. A downstream
consumer can read the standard fields directly and map the product-prefixed
attributes it needs. The [machine-readable
schema](../reference/event-schema.json) defines this product's supported subset
and extensions.

For example, a retry event is stored like this:

```json
{"Timestamp":1760000000000000000,"ObservedTimestamp":1760000000000000000,"SeverityText":"WARN","SeverityNumber":13,"EventName":"agent.operation.retrying","Body":"Retrying a transient operation","Resource":{"Attributes":{"workflow_ledger.harness":"explicit"}},"InstrumentationScope":{"Name":"workflow-ledger"},"Attributes":{"agent.operation.name":"review","agent.retry.attempt":1,"agent.retry.reason":"unavailable","workflow_ledger.event_id":"0123456789abcdef0123456789abcdef","workflow_ledger.schema_version":2,"workflow_ledger.associations":[]}}
```

Severity uses the lowest number in each [OpenTelemetry severity range](https://opentelemetry.io/docs/specs/otel/logs/data-model/):

| Level | Number | Use |
| --- | ---: | --- |
| `TRACE` | 1 | Fine-grained execution detail |
| `DEBUG` | 5 | Diagnostic detail useful during investigation |
| `INFO` | 9 | Expected operational milestones |
| `WARN` | 13 | Unexpected condition that did not stop the operation |
| `ERROR` | 17 | Operation failed or could not complete |
| `FATAL` | 21 | Critical condition requiring termination or immediate intervention |

Severity is not a substitute for the span's `outcome`. For example, an event
can be `WARN` while its operation later completes successfully. See the
[machine-readable event schema](../reference/event-schema.json) for exact
limits and types.

## Shared event vocabulary

Use the [shared event vocabulary](../reference/event-vocabulary.json) when a
retry or terminal operation failure needs a queryable event. It defines
`agent.operation.retrying` and `agent.operation.failed`, their default levels,
and the required attributes. The names are a common recommendation
for caller instrumentation; this library does not currently emit them.

The catalog is an initial, deliberately small vocabulary. The `agent.*`
namespace is for these shared caller events. Reserve `workflow_ledger.*`
event names for events emitted by Workflow Ledger itself, and use a stable
namespace you own for events outside the catalog. Custom names remain supported.
Do not add an event for information already represented by a span unless the
event adds a queryable point-in-time fact, such as a retry decision or failure
type.

## Ask an AI assistant to add events

This prompt works with any coding assistant that can inspect your project:

> Inspect how this project uses Workflow Ledger. Propose structured events
> for important operational conditions in this code path. Check the shared event
> vocabulary in `docs/reference/event-vocabulary.json` first and use its event
> names, severity levels, and required attributes where they fit. For other
> events, use a stable namespace owned by this project. Do not use the reserved
> `workflow_ledger.*` event namespace. Keep queryable values in attributes
> rather than message text, use low-cardinality values, preserve unknown values,
> and do not record prompts, responses, transcripts, tool arguments or results,
> credentials, or arbitrary exception messages. Avoid duplicating span duration
> or outcome. Show the proposed events and files before editing. After approval,
> implement them and explain how to inspect the resulting JSONL records.

Review the proposed event names and attributes for stability before adopting
them. Event names and fields become the interface downstream consumers query.

## Scope and export

Workflow Ledger defines the record, local append-only storage, severity
mapping, and span correlation. It does not provide a collector, database,
dashboard, alerting rules, or hosted service. The existing `export-otel` command
exports spans and metrics; it does not export event records. Consumers can read
the JSON Lines file directly or map its documented fields into the log format
they use. The core package does not depend on the OpenTelemetry SDK.

## Next steps

- Review the [event schema](../reference/event-schema.json) before building a
  downstream query or adapter.
- Review the [shared event vocabulary](../reference/event-vocabulary.json) for
  recommended event names and required attributes.
- Use the [measurement model](../concepts/measurement-model.md) to interpret
  span identity, evidence, lifecycle, and unknown values.
