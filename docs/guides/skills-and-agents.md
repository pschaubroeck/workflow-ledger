---
type: guide
title: Use shared markers from skills and agents
description: Initialize harness-neutral local capture, optionally install the operations skill, declare execution boundaries, and inspect local records.
---

# Use shared markers from skills and agents

Use `workflow-ledger init` to create a local policy and store configuration,
and optionally install the bundled operations skill. Shared markers then let an
agent or skill declare when selected work starts and finishes. They use the
same record model and local store as Python instrumentation, without depending
on a particular agent runtime. A marker is a caller-declared boundary: it
doesn't discover execution from loaded instructions, transcript text, or
configuration.

Use this path when the caller can place a start and finish around the actual
execution. If the host supplies its own lifecycle integration, treat that as an
optional runtime adapter and consult its separate integration guide.

## Contents

- [Initialize the local setup](#initialize-the-local-setup)
- [Choose a scope](#choose-a-scope)
- [Mark an invocation](#mark-an-invocation)
- [Inspect the result](#inspect-the-result)
- [Handle capture failures](#handle-capture-failures)
- [Next steps](#next-steps)

## Initialize the local setup

For a first-time install, start with [Getting started](getting-started.md),
which shows how an AI assistant can check or install the release in your chosen
Python environment. These instructions don't require a repository checkout.

Use that same Python interpreter for the CLI. The `init` command previews
changes by default. With no selection flags, it enables the baseline
`skill=personal:workflow-ledger-operations` and
`workflow=caller:observability-context` selectors. It accepts explicit paths
for the local line-delimited JSON (JSONL) store and policy. The default paths
are under `~/.workflow-ledger/`.

```text
python -m workflow_ledger init --store ~/.workflow-ledger/observations.jsonl --policy ~/.workflow-ledger/policy.json
```

The selector is an exact `SCOPE=IDENTITY` pair. Supplying one or more
`--select` options replaces the baseline with your exact selection. Choose
`--all` only when you explicitly want broad capture, or `--disabled` to
initialize with capture off. Those modes are mutually exclusive with each
other and with `--select`.

Use only exact identities established by supported runtime documentation or
user-visible configuration. The baseline identities above are defined by this
package. For additional assets, an assistant should show where each candidate
identity came from. It should not guess identities or scan transcripts, rollout
files, or unrelated configuration. Use `--disabled` to initialize without
starting capture.

To install the bundled `workflow-ledger-operations` skill, provide a
runtime-appropriate skill directory with `--skill-root PATH`. This option is
optional and does not enable runtime hooks. Existing non-managed skill
directories and modified skill directories are preserved and the installer
refuses to overwrite them. The host may need a reload or new session before it
can use the installed skill; check its integration guide for runtime-specific
behavior.

Review the preview. To write the changes, repeat the command with `--apply`:

```text
python -m workflow_ledger init --store ~/.workflow-ledger/observations.jsonl --policy ~/.workflow-ledger/policy.json --apply
```

By default, `init` uses preview mode (`--dry-run`). Use `--replace-policy` only
when you intentionally want to replace a different existing policy. Without
that flag, `init` preserves the existing policy and refuses a conflicting
replacement. Review any existing policy before choosing replacement.

## Choose a scope

The policy records only selected assets unless you explicitly choose broad
capture. Each `--select SCOPE=IDENTITY` entry must use the exact identity
reported or documented by the runtime integration. Exclusions take precedence
over inclusions. An empty include list can mean broad selection in a policy;
use `--all` explicitly when that is what you intend. See the
[attachment contract](../reference/attachment-contract.md) for selector and
attribution behavior.

The assistant setup example in [Getting started](getting-started.md) shows how
to discuss the selection, preview the setup, and get approval before applying
it. The shared setup configures local package state; it does
not configure host-specific hook environments.

## Mark an invocation

Start immediately before the selected skill or agent begins. Use a fresh,
caller-chosen session key for a task and a distinct invocation key for each
execution. Don't derive identifiers by searching transcripts or runtime files.

```text
workflow-ledger-marker --store ~/.workflow-ledger/observations.jsonl --policy ~/.workflow-ledger/policy.json start --session task-2026-01 --kind skill --identity personal:my-skill --invocation lookup-1
```

Keep the returned opaque `trace_id` and `span_id`. Finish after execution has
ended, using the returned handle and a known outcome:

```text
workflow-ledger-marker --store ~/.workflow-ledger/observations.jsonl --policy ~/.workflow-ledger/policy.json finish --handle TRACE_ID:SPAN_ID --outcome ok
```

Use `error` or `cancelled` only when you know the outcome; otherwise use
`unknown`. If an interruption leaves the end unobserved, leave the invocation
open rather than claiming it finished.
Retries of the same start use the same session and invocation key. A distinct
execution needs a new invocation key.

For nested execution, pass `--parent TRACE_ID:SPAN_ID` to the child's start only
when you know its enclosing invocation. Concurrent agents keep separate handles;
there is no implicit current-skill or current-agent context. A child whose
parent record is missing or filtered remains a capture gap; don't remove the
parent option to bypass selection.

An optional `workflow` marker can represent a caller-declared context parent.
Start it immediately before the first selected child, pass its handle as the
child's `--parent`, and finish it when the enclosing task ends. Its interval
starts at that selected invocation, so it doesn't represent time spent earlier
in the task.

## Inspect the result

Query using the same policy and store:

```text
workflow-ledger --policy ~/.workflow-ledger/policy.json report --store ~/.workflow-ledger/observations.jsonl --format text
```

The report summarizes selected records and store health. It may show unknown
token usage and incomplete boundaries. A recorded duration covers an operation
window and can include idle time. It can't show hidden model calls, active
execution time, or unmarked work. Selected records don't prove complete capture
or assess work quality.

## Handle capture failures

Marker output includes the attempted timestamp and opaque handle even when a
write fails. If a start fails, preserve those values and pause before running
the selected work. For `permission_denied`, request access only to the chosen
store directory. If approved, retry the original start with the same identity,
parent, policy, store, handle, and timestamp, using `--retry-handle` and
`--timestamp-ns`; omit `--session` and `--invocation` on that retry. If the host
denies access, or storage is full, unavailable, or locked out, explain the
capture gap. Then decide whether to stop or proceed without observation. Don't
continue silently.

If finishing fails, preserve the original handle, outcome, and timestamp. Retry
the finish after you resolve the storage issue; don't rerun the observed work
just to repair a marker. Repeating a finish records the same boundary.

The markers add tool calls and instruction context to agent-driven work. They
don't call a model or send telemetry to a backend. Keep prompts, responses,
tool arguments, and tool results out of names and explicitly supplied metadata.

For optional native Codex hook capture, see the
[Codex integration guide](../integrations/codex.md). Check the
[compatibility reference](../reference/compatibility.md) for verified scope.
See the separate integration guides for runtime-specific automatic capture and
artifact import.

## Next steps

- Use the [Python getting-started guide](getting-started.md) for explicit spans
  inside an application call stack.
- Use the [Codex integration guide](../integrations/codex.md) if you need native
  Codex artifact import or hooks.
