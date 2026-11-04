---
type: Concept
title: Measurement Model
description: Explain what Workflow Ledger records, what evidence supports an observation, and where the core leaves values unknown.
---

# Measurement Model

Workflow Ledger stores operational metadata about linked work. Each record
describes one observed operation and its timing, outcome, identity, or usage
evidence. Records contain no transcripts and don't imply complete capture.

Structured events are separate records for queryable operational facts such as
retries or storage failures. Their severity describes the event; a span's
outcome describes the operation lifecycle. See [structured events](../guides/structured-events.md)
for the OpenTelemetry Logs data model record format and recording API.

## Contents

- [Operation kinds](#operation-kinds)
- [Evidence and identity](#evidence-and-identity)
- [Timing and relationships](#timing-and-relationships)
- [Usage and unknown values](#usage-and-unknown-values)
- [Next steps](#next-steps)

## Operation kinds

The core supports six operation kinds: `workflow`, `agent`, `skill`, `model`,
`tool`, and `wait`. Applications can create explicit spans with the Python API.
Adapters can attach events only where their source exposes a supported
lifecycle. A caller may declare a wait and link it to a known invocation.

Callers can record configured or loaded assets as context, but that doesn't
prove the asset executed. A skill runs only when the caller declares its boundary
or a source provides lifecycle evidence. The core doesn't infer execution from a
transcript, a configuration, or an asset mention.

## Evidence and identity

Records distinguish identity from display names and retain the evidence that
established each identity. Caller-declared identities and supported native identities can
support measured ownership. Context-only or legacy labels don't prove that an
asset owns a child operation. If evidence is missing or ambiguous, reports leave
ownership unattributed.

Runtime adapters retain safe operational metadata and omit source content.
The library includes provider usage only when the provider or runtime supplies
it; it doesn't make model calls to collect observations.

## Timing and relationships

An operation can have direct, inferred, estimated, or unavailable timing. These
qualities describe the source of the interval; they don't express execution
quality. Operation intervals may overlap, so summed operation duration isn't
workflow elapsed time. Overlap between agent windows doesn't establish active
execution or a dependency.

A `wait` span represents a declared or source-provided waiting interval. Its
optional `waits_for` link points to an invocation identified by the caller or
adapter. The link records that relationship as evidence; it doesn't prove that
the target caused the wait or completed successfully.

## Usage and unknown values

Input and output tokens are primary categories. Cached input is a subset of
input, and reasoning tokens are a subset of output; don't add the subsets to
their parent categories. Missing usage is unknown, not zero. Run-level totals
remain unattributed unless the source establishes ownership.

Reports describe observed fields and coverage. Coverage can't prove that the
recorder captured every operation, and outcomes don't score workflow quality.

## Next steps

For exact identity, selection, lifecycle, migration, and failure rules, see the
[attachment and attribution contract](../reference/attachment-contract.md).
For runtime-specific evidence, see the [integration guides](../README.md#integrations).
