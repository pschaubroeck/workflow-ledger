---
type: Directory Index
title: Workflow Ledger Documentation
description: Navigate user guides, measurement concepts, runtime integrations, references, and maintainer guidance.
---

# Workflow Ledger Documentation

Use this index to choose a path through the repository documentation. The core
library and its setup workflow are harness-agnostic; runtime-specific pages
describe optional adapters and their verified limits. The released package is
not published yet. The getting-started guide starts with an AI-assisted package
check and setup, then presents manual installation and contributor checkout as
secondary paths.

## Contents

- [Start here](#start-here)
- [Next steps](#next-steps)
- [Integrations](#integrations)
- [Reference](#reference)
- [Maintainers](#maintainers)

## Start here

- [Getting started](guides/getting-started.md) — ask an AI assistant to check
  or install the release from your configured package index and initialize a
  harness-neutral local setup.
- [Skills and agents](guides/skills-and-agents.md) — initialize selected
  capture, optionally install the operations skill, and declare execution and
  wait boundaries with the shared marker command.
- [Measurement model](concepts/measurement-model.md) — understand evidence,
  identity, timing, attribution, and unknown values.
- [Structured events](guides/structured-events.md) — record queryable operational
  events with consistent severity and span correlation.

## Next steps

After recording observations, use the [CLI and storage reference](reference/cli-and-storage.md)
to inspect local records, manage retention, or explicitly export a snapshot.

## Integrations

- [Codex](integrations/codex.md) — selected artifact import and optional native
  lifecycle hooks. Codex-specific setup remains in this integration guide.
- [OpenAI Agents SDK integration](integrations/openai-agents-sdk.md) — use the
  optional adapter for supported SDK lifecycle events and explicitly declared
  skill work.
- [OpenTelemetry](integrations/opentelemetry.md) — explicitly export a local
  snapshot using the OpenTelemetry Protocol (OTLP).

## Reference

- [Attachment and attribution contract](reference/attachment-contract.md) —
  schema, identity, selection, lifecycle, ownership, and failure semantics.
- [Structured event schema](reference/event-schema.json) — machine-readable
  OpenTelemetry Logs data model contract represented as JSON Lines (JSONL), with
  Workflow Ledger extensions.
- [Shared event vocabulary](reference/event-vocabulary.json) — recommended
  names, severity levels, and attributes for caller-recorded retry and terminal
  failure events.
- [CLI and storage](reference/cli-and-storage.md) — local commands, reports,
  line-delimited JSON storage, and retention behavior.
- [Compatibility](reference/compatibility.md) — implemented surfaces and the
  environments and event paths verified to date.

## Maintainers

- [Maintainer documentation](maintainers/README.md) — release, contribution,
  and naming guidance.
- [Repository instructions](../AGENTS.md) — contribution boundaries and
  verification commands.

See the [repository README](../README.md) for a short product overview and
quickstart.
