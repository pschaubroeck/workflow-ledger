---
type: reference
title: Attachment and attribution contract
description: Define how runtime events and explicit helpers attach asset identity, lifecycle, and attribution to schema 3 records.
---

# Attachment and attribution contract (schema 3)

## Contents

- [Identity](#identity)
- [Selection](#selection)
- [Lifecycle](#lifecycle)
- [Ownership and relationships](#ownership-and-relationships)
- [Compatibility and failures](#compatibility-and-failures)
- [Next steps](#next-steps)

The library can attach through supported runtime lifecycle events or explicit
Python helpers. Asset definitions don't need a central registry. A runtime
without a native skill event needs an explicit skill wrapper to measure invocation.

## Identity

`AssetIdentity(namespace, logical_id, version=None, provenance="caller_declared")`
identifies an asset separately from its display name. Its selection key is
`namespace:logical_id`. Versions remain unknown unless supplied by the source or
caller; paths and SDK package versions don't establish an asset revision.
Trace/span IDs identify one invocation. Source adapters hash native names/IDs
before retention. Caller names and logical IDs must already be safe to retain.

## Selection

Policies use exact keys at runtime, workflow, agent, skill, model, or tool scope.
Global disable wins, then exclusions, then inclusions. An empty inclusion list
selects all observations. Include entries use OR matching. Selectors ignore asset
version, so they can match one asset across revisions; reports distinguish them.

An asset matches its own observations and descendants carrying a measured
invocation association. Merely configured/loaded contextual associations can't
select unrelated descendants. Excluding an active asset also excludes descendants
associated with that invocation, even if another selector includes them.
Agent selectors follow the nearest active agent: a nested agent replaces its
outer agent association and can match an independent selector. Workflow associations
and active skill associations continue through nested agents.

Runtime disable/exclusion stops before source reads or optional setup. Selective
asset filtering needs source events to discover identity; it drops matching output
records rather than disabling the application's execution. Filtering preserves
original parent IDs and marks a known removed parent as `filtered_parent`.
The system creates no replacement execution span. Query/export selection applies
the same policy to previously stored records; it doesn't remove the stored history.

## Lifecycle

Configured and loaded observations carry a timestamp but no execution duration,
outcome, or usage. Explicit invocations emit an `invoked` start revision and a
terminal `completed`, `failed`, or `cancelled` revision. Storage and reports select
the latest revision by trace/span ID. An interrupted invocation can remain
`invoked`; an absent completion never implies success. Artifact-only observations
use `observed` when native lifecycle is unavailable. A `wait` span records the
caller-declared or native interval spent waiting. Its optional `waits_for` link
identifies a target invocation; it doesn't establish that the target caused the
wait or completed successfully.

## Ownership and relationships

Each association includes asset identity, kind, lifecycle evidence, and optional
invocation trace/span IDs. Only an invoked association backed by native or
caller-declared evidence establishes measured ownership. Contextual and legacy
labels don't. A nested agent replaces the inherited measured agent association
with the current agent; nested skills remain visible together.

A model observation belongs to a skill only when its measured skill associations
resolve to exactly one logical identity and version. Multiple distinct active
skills or revisions leave usage unattributed. Association links preserve all
supported invocation relationships when a single parent is insufficient.
Known root usage still overrides child usage for each token category, and stays
unattributed unless the source actually establishes ownership.

Token usage source is one of `explicit`, `provider_response`,
`native_cumulative_snapshot`, `openai-agents`, `synthetic`, or `other`; it's not
a free-form description. Use `other` when no built-in category applies. Legacy
records with an unrecognized source remain readable, but reports and exports use
`other` so historical arbitrary labels don't continue across those boundaries.

Wait links are separate from ownership associations. Record a wait link only when
the caller or source identifies a target invocation; leave the target unknown
otherwise. Reports resolve links only when selected records include the target
and compare timestamp windows when both intervals have bounded timestamps. Interval
overlap doesn't prove causation or active execution.

## Compatibility and failures

Schema 1 and 2 records migrate to schema 3, preserving historical labels and
ownership evidence without inventing wait links. The parser rejects schema 1
records that contain schema 2 or 3 fields and schema 2 records that contain
schema 3 fields.
The core rejects unknown schema versions and arbitrary content fields.

Recording failures increment diagnostics without changing application control
flow. Filter diagnostics count dropped observations, so an excluded explicit
invocation can account for both its start and terminal revision. Store/report
counts use latest records. Disabled helpers return before inspecting event data.
Explicit context setup validates caller-provided IDs before binding them.

## Next steps

- Follow [Skills and agents](../guides/skills-and-agents.md) to select assets and
  record explicit skill or agent boundaries.
- Check [compatibility](compatibility.md) before relying on a runtime adapter.
- Keep skill ownership and token usage unknown unless the source provides
  measured identity and usage evidence.
